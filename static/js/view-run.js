/* Live-Ansicht eines Laufs: wer ist wo, was ist passiert, was sagt das Protokoll. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, API, fail, statusBadge } = AW;

  const MAX_LINES = 20000;
  const MAX_RENDER = 8000;

  const HOST_STATE_LABELS = {
    wartet: "wartet", laeuft: "läuft", bereit: "bereit", fertig: "fertig",
    failed: "fehlgeschlagen", unreachable: "nicht erreichbar", wiederholt: "wiederholt",
  };
  const RESULT_LABELS = {
    ok: "ok", changed: "geändert", failed: "fehlgeschlagen",
    skipped: "übersprungen", unreachable: "nicht erreichbar",
  };

  /* -- Ereignisse auf den Zustand anwenden (wie im Dienst) ------------- */

  function emptyHost(name) {
    return { name, state: "wartet", current_task: null, current_task_name: "",
             counts: { ok: 0, changed: 0, failed: 0, skipped: 0, unreachable: 0 },
             last_duration: null, last_error: null, updated_at: null };
  }

  function hostOf(state, name) {
    if (!state.hosts[name]) state.hosts[name] = emptyHost(name);
    return state.hosts[name];
  }

  function applyEvent(state, event) {
    switch (event.e) {
      case "play_start": {
        const play = { uuid: event.play_uuid, name: event.name || "(ohne Namen)",
                       pattern: event.pattern || [], hosts: event.hosts || [],
                       check_mode: !!event.check_mode, tasks: [], started_at: event.t };
        state.plays.push(play);
        state.current_play = play.uuid;
        for (const name of play.hosts) hostOf(state, name);
        break;
      }
      case "task_start": {
        if (state.tasks[event.task_uuid]) break;
        const task = { uuid: event.task_uuid, play_uuid: event.play_uuid || state.current_play,
                       name: event.name || event.action || "(Task)", action: event.action || "",
                       path: event.path || "", is_handler: !!event.is_handler,
                       no_log: !!event.no_log, order: state.task_order.length,
                       started_at: event.t, running: [], results: {},
                       counts: { ok: 0, changed: 0, failed: 0, skipped: 0, unreachable: 0 } };
        state.tasks[task.uuid] = task;
        state.task_order.push(task.uuid);
        state.current_task = task.uuid;
        const play = state.plays.find((item) => item.uuid === task.play_uuid);
        if (play) play.tasks.push(task.uuid);
        break;
      }
      case "host_task_start": {
        const host = hostOf(state, event.host);
        host.state = "laeuft";
        host.current_task = event.task_uuid;
        host.current_task_name = event.task_name || "";
        host.updated_at = event.t;
        const task = state.tasks[event.task_uuid];
        if (task && !task.running.includes(event.host)) task.running.push(event.host);
        break;
      }
      case "host_result": {
        const host = hostOf(state, event.host);
        const data = event.data || {};
        const ignored = !!event.ignore_errors;
        let key = host.counts[event.status] !== undefined ? event.status : "ok";
        if (event.status === "failed" && ignored) key = "ok";
        host.counts[key] = (host.counts[key] || 0) + 1;
        host.last_duration = event.duration;
        host.updated_at = event.t;
        host.current_task = null;
        host.current_task_name = "";
        if ((event.status === "failed" || event.status === "unreachable") && !ignored) {
          host.state = event.status;
          host.last_error = String(data.msg || data.stderr || data.reason || "");
        } else if (host.state !== "failed" && host.state !== "unreachable") {
          host.state = "bereit";
        }
        const task = state.tasks[event.task_uuid];
        if (task) {
          task.running = task.running.filter((name) => name !== event.host);
          task.counts[key] = (task.counts[key] || 0) + 1;
          task.results[event.host] = {
            status: event.status, ignored, duration: event.duration,
            msg: data.msg || null, rc: data.rc, stderr: data.stderr || null,
            item_count: data.item_count, censored: !!data.censored,
            delegated_to: data.delegated_to, ts: event.t,
          };
        }
        break;
      }
      case "item_result": {
        const task = state.tasks[event.task_uuid];
        if (!task) break;
        task.items = task.items || [];
        if (task.items.length < 500) {
          const data = event.data || {};
          task.items.push({ host: event.host, status: event.status, item: data.item,
                            msg: data.msg || null });
        }
        break;
      }
      case "diff": {
        const task = state.tasks[event.task_uuid];
        if (!task) break;
        task.diffs = task.diffs || [];
        if (task.diffs.length < 50) task.diffs.push({ host: event.host, diff: event.diff });
        break;
      }
      case "retry": {
        const host = hostOf(state, event.host);
        host.state = "wiederholt";
        break;
      }
      case "stats": {
        state.stats = event.hosts || {};
        for (const name of Object.keys(state.stats)) {
          const host = hostOf(state, name);
          const summary = state.stats[name];
          host.current_task = null;
          host.current_task_name = "";
          if (summary.unreachable) host.state = "unreachable";
          else if (summary.failures) host.state = "failed";
          else host.state = "fertig";
        }
        break;
      }
      case "no_hosts_matched":
        state.notices.push("Kein Host passte auf das Muster dieses Plays.");
        break;
      case "no_hosts_remaining":
        state.notices.push("Keine Hosts mehr übrig – der Lauf wurde abgebrochen.");
        break;
      default: break;
    }
  }

  /* -- Ansicht ---------------------------------------------------------- */

  AW.views.run = {
    label: "Lauf", title: "Lauf", cap: "runs.view",

    async mount(container, route) {
      const runId = route.segments[0];
      if (!runId) { AW.go("runs"); return; }

      let meta = null;
      let state = { plays: [], tasks: {}, task_order: [], hosts: {}, stats: {},
                    notices: [], current_task: null, progress: {} };
      let lines = [];
      let selectedTask = null;
      let socket = null;
      let dirty = false;
      let renderTimer = null;
      let finishedToastShown = false;

      /* Kopfbereich */
      const titleNode = h("h1", { text: "Lauf " + runId });
      const subNode = h("div", { class: "sub" });
      const badgeRow = h("div", { class: "btn-row" });
      const actions = h("div", { class: "btn-row" });
      const statsHost = h("div", { class: "grid stats", style: { marginBottom: "16px" } });
      const noticeHost = h("div", {});
      const hostsHost = h("div", { class: "hosts" });
      const hostsCount = h("span", { class: "faint small" });
      const tasksHost = h("div", { class: "tasks" });
      const tasksWrap = h("div", { class: "list-scroll" }, tasksHost);

      /* Protokoll */
      const consoleNode = h("div", { class: "console" });
      const autoScroll = h("input", { type: "checkbox", checked: true });
      const wrapToggle = h("input", { type: "checkbox", checked: true });
      const onlyProblems = h("input", { type: "checkbox" });
      const logSearch = h("input", { type: "search", placeholder: "im Protokoll suchen …" });
      let searchTerm = "";

      function lineMatchesFilter(text) {
        if (onlyProblems.checked) {
          const plain = AW.stripAnsi(text);
          if (!/fatal:|FAILED|failed=|unreachable|ERROR|WARNING|\*\*\*/i.test(plain)) return false;
        }
        if (searchTerm && !AW.stripAnsi(text).toLowerCase().includes(searchTerm)) return false;
        return true;
      }

      function lineNode(text) {
        const node = h("span", { class: "ln" });
        if (searchTerm) {
          const plain = AW.stripAnsi(text);
          const index = plain.toLowerCase().indexOf(searchTerm);
          if (index >= 0) {
            node.appendChild(document.createTextNode(plain.slice(0, index)));
            node.appendChild(h("mark", { text: plain.slice(index, index + searchTerm.length) }));
            node.appendChild(document.createTextNode(plain.slice(index + searchTerm.length) + "\n"));
            return node;
          }
        }
        node.appendChild(AW.ansiToFragment(text + "\n"));
        return node;
      }

      function renderConsole() {
        const visible = lines.filter(lineMatchesFilter).slice(-MAX_RENDER);
        AW.clear(consoleNode);
        const frag = document.createDocumentFragment();
        for (const text of visible) frag.appendChild(lineNode(text));
        consoleNode.appendChild(frag);
        if (autoScroll.checked) consoleNode.scrollTop = consoleNode.scrollHeight;
      }

      function appendLines(incoming) {
        for (const text of incoming) {
          lines.push(text);
          if (lines.length > MAX_LINES) lines.shift();
        }
        const atBottom = consoleNode.scrollHeight - consoleNode.scrollTop -
                         consoleNode.clientHeight < 60;
        if (searchTerm || onlyProblems.checked) { renderConsole(); return; }
        const frag = document.createDocumentFragment();
        for (const text of incoming) frag.appendChild(lineNode(text));
        consoleNode.appendChild(frag);
        while (consoleNode.childElementCount > MAX_RENDER) consoleNode.firstChild.remove();
        if (autoScroll.checked && atBottom) consoleNode.scrollTop = consoleNode.scrollHeight;
      }

      consoleNode.addEventListener("scroll", () => {
        const atBottom = consoleNode.scrollHeight - consoleNode.scrollTop -
                         consoleNode.clientHeight < 60;
        if (!atBottom && autoScroll.checked) autoScroll.checked = false;
      });
      logSearch.addEventListener("input", AW.debounce(() => {
        searchTerm = logSearch.value.toLowerCase().trim();
        renderConsole();
      }, 220));
      onlyProblems.addEventListener("change", renderConsole);
      wrapToggle.addEventListener("change", () => {
        consoleNode.style.whiteSpace = wrapToggle.checked ? "pre-wrap" : "pre";
      });
      autoScroll.addEventListener("change", () => {
        if (autoScroll.checked) consoleNode.scrollTop = consoleNode.scrollHeight;
      });

      /* -- Kopf und Kennzahlen ------------------------------------------ */

      function renderHead() {
        if (!meta) return;
        const request = meta.request || {};
        titleNode.textContent = request.playbook || runId;
        const bits = [
          request.limit ? "--limit " + request.limit : "ganzes Inventory",
          (request.tags || []).length ? "--tags " + request.tags.join(",") : null,
          (request.skip_tags || []).length ? "--skip-tags " + request.skip_tags.join(",") : null,
          request.check ? "Probelauf (--check)" : null,
          request.diff ? "--diff" : null,
          request.forks ? "--forks " + request.forks : null,
          request.verbosity ? "-" + "v".repeat(request.verbosity) : null,
        ].filter(Boolean);
        AW.mount(subNode,
          h("span", { class: "mono", text: bits.join("  ·  ") }),
          h("div", { class: "tiny faint mt",
            text: "Lauf " + runId + " · ausgelöst von " + (request.user || "–") +
                  " (" + (AW.runsHelpers.SOURCE_LABELS[request.source] || request.source) + ")" +
                  " · Start " + AW.fmtTime(meta.created_at) +
                  (request.label ? " · " + request.label : "") }));

        AW.mount(badgeRow, statusBadge(meta.status),
          meta.rc !== null && meta.rc !== undefined
            ? h("span", { class: "badge plain", title: meta.rc_meaning || "",
                          text: "rc " + meta.rc }) : null,
          meta.rc_meaning ? h("span", { class: "small faint", text: meta.rc_meaning }) : null);

        const active = meta.status === "running" || meta.status === "queued";
        AW.clear(actions);
        if (active && AW.can("runs.cancel")) {
          actions.appendChild(h("button", { class: "danger", onclick: cancelRun },
            icon("stop"), "Abbrechen"));
        }
        if (!active && AW.can("runs.start")) {
          actions.appendChild(h("button", { onclick: repeatRun }, icon("refresh"), "Erneut starten"));
        }
        actions.appendChild(h("a", { class: "btn", href: "/api/runs/" + encodeURIComponent(runId) + "/log?download=1",
          download: "" }, icon("download"), h("span", { class: "hide-xs", text: "Protokoll" })));
        if (!active && AW.can("runs.cancel")) {
          actions.appendChild(h("button", { class: "ghost icon", title: "Lauf löschen",
            onclick: deleteRun }, icon("trash")));
        }

        const progress = meta.progress || {};
        const counts = meta.counts || {};
        AW.mount(statsHost,
          h("div", { class: "stat" }, h("div", { class: "k", text: "Dauer" }),
            h("div", { class: "v", text: AW.fmtDuration(meta.duration) })),
          h("div", { class: "stat" }, h("div", { class: "k", text: "Schritte" }),
            h("div", { class: "v" }, String(progress.tasks_done || 0),
              h("small", { text: " / " + (progress.tasks_total || "?") })),
            h("div", { style: { marginTop: "7px" } }, AW.runsHelpers.progressBar(meta))),
          h("div", { class: "stat" }, h("div", { class: "k", text: "Hosts" }),
            h("div", { class: "v", text: String(Object.keys(state.hosts).length || meta.host_count || 0) })),
          h("div", { class: "stat" }, h("div", { class: "k", text: "ok / geändert" }),
            h("div", { class: "v" }, h("span", { class: "c-ok", text: String(counts.ok || 0) }),
              h("small", { text: " / " }), h("span", { class: "c-changed", text: String(counts.changed || 0) }))),
          h("div", { class: "stat" }, h("div", { class: "k", text: "Fehler" }),
            h("div", { class: "v" },
              h("span", { class: (counts.failed || counts.unreachable) ? "c-failed" : "",
                          text: String((counts.failed || 0) + (counts.unreachable || 0)) }))));

        const notices = (state.notices || []).concat(meta.error ? [meta.error] : []);
        AW.clear(noticeHost);
        if (meta.status === "interrupted") {
          noticeHost.appendChild(h("div", { class: "notice warn" },
            h("strong", { text: "Dieser Lauf wurde nicht zu Ende geführt" }),
            h("div", { class: "small mt", text:
              "Der Dienst wurde beendet, während das Playbook lief. Was Ansible bis dahin " +
              "gemacht hat, steht im Protokoll – der tatsächliche Zustand der Hosts ist offen." })));
        }
        for (const text of notices) {
          noticeHost.appendChild(h("div", { class: "notice warn small", text }));
        }
      }

      /* -- Hostmatrix --------------------------------------------------- */

      function renderHosts() {
        const names = Object.keys(state.hosts).sort();
        hostsCount.textContent = names.length ? names.length + " Hosts" : "";
        AW.clear(hostsHost);
        if (!names.length) {
          hostsHost.appendChild(h("div", { class: "faint small",
            text: meta && meta.status === "queued"
              ? "Der Lauf wartet noch auf einen freien Platz."
              : "Noch keine Hostmeldungen – Ansible startet gerade." }));
          return;
        }
        for (const name of names) {
          const host = state.hosts[name];
          const counts = host.counts || {};
          const dotClass = host.state === "laeuft" ? "s-running"
            : host.state === "failed" ? "s-failed"
            : host.state === "unreachable" ? "s-unreachable"
            : (host.state === "fertig" || host.state === "bereit") ? "s-ok" : "s-waiting";
          hostsHost.appendChild(h("div", { class: "host " + host.state },
            h("div", { class: "host-top" },
              h("span", { class: "state-dot " + dotClass +
                                 (host.state === "laeuft" ? " pulse" : "") }),
              h("span", { class: "host-name", title: name, text: name }),
              h("span", { class: "tiny faint", text: HOST_STATE_LABELS[host.state] || host.state })),
            h("div", { class: "host-task", title: host.current_task_name || "",
              text: host.current_task_name
                ? "▶ " + host.current_task_name
                : (host.last_duration !== null && host.last_duration !== undefined
                    ? "letzter Schritt " + AW.fmtDuration(host.last_duration) : "") }),
            h("div", { class: "host-counts" },
              h("span", { class: "c-ok" }, "ok ", h("b", { text: String(counts.ok || 0) })),
              h("span", { class: "c-changed" }, "geänd. ", h("b", { text: String(counts.changed || 0) })),
              counts.failed ? h("span", { class: "c-failed" }, "fehl. ", h("b", { text: String(counts.failed) })) : null,
              counts.unreachable ? h("span", { class: "c-unreachable" }, "unerr. ", h("b", { text: String(counts.unreachable) })) : null,
              counts.skipped ? h("span", { class: "c-skipped" }, "übersp. ", h("b", { text: String(counts.skipped) })) : null),
            host.last_error ? h("div", { class: "host-err", title: host.last_error,
                                         text: host.last_error }) : null));
        }
      }

      /* -- Schritte ----------------------------------------------------- */

      function renderTasks() {
        const scroll = tasksWrap.scrollTop;
        AW.clear(tasksHost);
        if (!state.task_order.length) {
          tasksHost.appendChild(AW.emptyState("Noch keine Schritte."));
          return;
        }
        const seen = new Set();
        for (const play of state.plays) {
          tasksHost.appendChild(h("div", { class: "play-head" },
            icon("server", 13),
            h("span", { text: play.name }),
            h("span", { class: "pat", text: (play.pattern || []).join(",") }),
            play.check_mode ? h("span", { class: "badge plain tiny", text: "Probelauf" }) : null,
            h("div", { class: "grow" }),
            h("span", { class: "tiny faint", text: (play.hosts || []).length + " Hosts" })));
          for (const uuid of play.tasks) {
            seen.add(uuid);
            tasksHost.appendChild(taskRow(state.tasks[uuid]));
          }
        }
        for (const uuid of state.task_order) {
          if (!seen.has(uuid)) tasksHost.appendChild(taskRow(state.tasks[uuid]));
        }
        tasksWrap.scrollTop = scroll;
      }

      function taskRow(task) {
        if (!task) return null;
        const counts = task.counts || {};
        const dots = [];
        const names = Object.keys(task.results || {}).sort();
        for (const name of names) {
          const result = task.results[name];
          const cls = result.status === "failed" && !result.ignored ? "s-failed"
            : result.status === "unreachable" ? "s-unreachable"
            : result.status === "changed" ? "s-changed"
            : result.status === "skipped" ? "s-skipped" : "s-ok";
          dots.push(h("span", { class: "state-dot " + cls,
                                title: name + ": " + (RESULT_LABELS[result.status] || result.status) }));
        }
        for (const name of task.running || []) {
          dots.push(h("span", { class: "state-dot s-running pulse", title: name + ": läuft" }));
        }
        const durations = names.map((n) => task.results[n].duration).filter((d) => d !== null && d !== undefined);
        const maxDuration = durations.length ? Math.max.apply(null, durations) : null;
        const isRunning = (task.running || []).length > 0;
        return h("div", {
          class: "task" + (counts.failed ? " has-failed" : "") +
                 (selectedTask === task.uuid ? " on" : ""),
          onclick: () => showTask(task.uuid),
        },
          h("div", { class: "task-num", text: String(task.order + 1) }),
          h("div", { class: "task-main" },
            h("div", { class: "task-name" },
              task.is_handler ? h("span", { class: "badge plain tiny", text: "Handler" }) : null,
              " " + task.name,
              task.no_log ? h("span", { class: "badge plain tiny", text: "no_log" }) : null),
            h("div", { class: "task-meta", title: task.path,
                       text: [task.action, task.path].filter(Boolean).join("  ·  ") }),
            dots.length ? h("div", { class: "task-dots" }, dots) : null),
          h("div", { class: "task-right" },
            isRunning ? h("span", { class: "spinner small" }) : null,
            maxDuration !== null ? h("span", { class: "task-dur", text: AW.fmtDuration(maxDuration) }) : null,
            icon("chevronR", 14)));
      }

      /* -- Schritt-Details ---------------------------------------------- */

      async function showTask(uuid) {
        selectedTask = uuid;
        renderTasks();
        const task = state.tasks[uuid];
        if (!task) return;
        const names = Object.keys(task.results || {}).sort();
        const body = h("div", {});

        body.appendChild(h("dl", { class: "kv mb" },
          h("dt", { text: "Modul" }), h("dd", { text: task.action || "–" }),
          h("dt", { text: "Quelle" }), h("dd", { text: task.path || "–" }),
          h("dt", { text: "Art" }), h("dd", { text: task.is_handler ? "Handler" : "Task" })));

        if (task.no_log) {
          body.appendChild(h("div", { class: "notice warn small" },
            "Dieser Schritt ist mit no_log markiert. Ergebnisse werden absichtlich nicht angezeigt."));
        }

        if (names.length) {
          const rows = names.map((name) => {
            const result = task.results[name];
            return h("tr", {},
              h("td", { class: "nowrap" },
                h("span", { class: "badge " + (result.status === "changed" ? "changed" :
                  result.status === "failed" ? (result.ignored ? "changed" : "failed") :
                  result.status === "unreachable" ? "unreachable" :
                  result.status === "skipped" ? "skipped" : "ok") },
                  RESULT_LABELS[result.status] || result.status),
                result.ignored ? h("span", { class: "tiny faint", text: " ignoriert" }) : null),
              h("td", { class: "mono", text: name }),
              h("td", { class: "num nowrap", text: AW.fmtDuration(result.duration) }),
              h("td", { class: "num", text: result.rc === null || result.rc === undefined ? "–" : String(result.rc) }),
              h("td", { class: "wrap-any small",
                text: result.censored ? "(unterdrückt)" :
                      (result.msg || result.stderr || (result.item_count ? result.item_count + " Elemente" : "–")) }));
          });
          body.appendChild(h("div", { class: "table-wrap card pad-0" },
            h("table", {}, h("thead", {}, h("tr", {},
              h("th", { text: "Ergebnis" }), h("th", { text: "Host" }),
              h("th", { class: "num", text: "Dauer" }), h("th", { class: "num", text: "rc" }),
              h("th", { text: "Meldung" }))), h("tbody", {}, rows))));
        } else {
          body.appendChild(h("div", { class: "notice small", text: "Für diesen Schritt liegen noch keine Ergebnisse vor." }));
        }

        if ((task.items || []).length) {
          body.appendChild(h("details", { class: "box mt" },
            h("summary", { text: "Schleifen-Elemente (" + task.items.length + ")" }),
            h("div", {}, h("div", { class: "table-wrap" }, h("table", {},
              h("thead", {}, h("tr", {}, h("th", { text: "Host" }), h("th", { text: "Ergebnis" }),
                h("th", { text: "Element" }), h("th", { text: "Meldung" }))),
              h("tbody", {}, ...task.items.map((item) => h("tr", {},
                h("td", { class: "mono small", text: item.host }),
                h("td", {}, h("span", { class: "badge " + (item.status === "failed" ? "failed" :
                  item.status === "changed" ? "changed" : item.status === "skipped" ? "skipped" : "ok") },
                  RESULT_LABELS[item.status] || item.status)),
                h("td", { class: "mono small wrap-any", text: String(item.item === null || item.item === undefined ? "–" : item.item) }),
                h("td", { class: "small wrap-any", text: item.msg || "" })))))))));
        }

        for (const entry of task.diffs || []) {
          for (const part of entry.diff || []) {
            const text = part.prepared || buildDiffText(part);
            if (!text) continue;
            body.appendChild(h("details", { class: "box mt", open: (task.diffs || []).length === 1 },
              h("summary", { text: "Änderung auf " + entry.host + (part.path ? " – " + part.path : "") }),
              h("div", {}, h("pre", { class: "diff" }, AW.diffToFragment(text)))));
          }
        }

        const rawBox = h("div", { class: "mt" },
          h("button", { class: "small", onclick: async (event) => {
            event.target.disabled = true;
            try {
              const data = await API.get("/api/runs/" + encodeURIComponent(runId) +
                "/events?limit=400&task=" + encodeURIComponent(uuid));
              AW.mount(rawBox, h("pre", { class: "diff",
                text: data.events.map((e) => JSON.stringify(e, null, 1)).join("\n") }));
            } catch (err) { fail(err, "Ereignisse nicht abrufbar"); }
          } }, icon("eye"), "Rohereignisse dieses Schritts anzeigen"));
        body.appendChild(rawBox);

        modal({ title: "Schritt " + (task.order + 1) + ": " + task.name,
                width: "900px", body });
      }

      function buildDiffText(part) {
        if (part.before === undefined && part.after === undefined) return "";
        const before = String(part.before === undefined ? "" : part.before).split("\n");
        const after = String(part.after === undefined ? "" : part.after).split("\n");
        const out = ["--- vorher", "+++ nachher"];
        for (const line of before) out.push("-" + line);
        for (const line of after) out.push("+" + line);
        return out.join("\n");
      }

      /* -- Aktionen ----------------------------------------------------- */

      async function cancelRun() {
        const ok = await confirmDialog({
          title: "Lauf abbrechen?",
          body: "Ansible wird unterbrochen. Schritte, die auf den Hosts schon ausgeführt wurden, " +
                "bleiben ausgeführt – es wird nichts zurückgerollt.",
          confirmLabel: "Ja, abbrechen", danger: true,
        });
        if (!ok) return;
        try {
          await API.post("/api/runs/" + encodeURIComponent(runId) + "/cancel");
          toast("Abbruch angefordert", "ok");
        } catch (err) { fail(err, "Abbruch nicht möglich"); }
      }

      async function deleteRun() {
        const ok = await confirmDialog({
          title: "Lauf löschen?",
          body: "Protokoll und Ereignisse dieses Laufs werden entfernt.",
          confirmLabel: "Löschen", danger: true,
        });
        if (!ok) return;
        try {
          await API.del("/api/runs/" + encodeURIComponent(runId));
          toast("Lauf gelöscht", "ok");
          AW.go("runs");
        } catch (err) { fail(err, "Löschen nicht möglich"); }
      }

      function repeatRun() {
        const request = meta.request || {};
        AW.go("start", {
          playbook: request.playbook || "", limit: request.limit || "",
          tags: (request.tags || []).join(","), skip_tags: (request.skip_tags || []).join(","),
          check: request.check ? "1" : "", diff: request.diff ? "1" : "",
          forks: request.forks || "", verbosity: request.verbosity || "",
        });
      }

      /* -- Aufbau ------------------------------------------------------- */

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("div", { class: "btn-row", style: { marginBottom: "4px" } },
              h("a", { href: AW.linkTo("runs"), class: "small" }, "‹ alle Läufe"),
              badgeRow),
            titleNode, subNode),
          actions),
        noticeHost, statsHost,
        h("div", { class: "card pad-0", style: { marginBottom: "16px" } },
          h("div", { class: "card-head" }, icon("server", 14), h("h2", { text: "Hosts" }),
            h("div", { class: "grow" }), hostsCount),
          h("div", { class: "card-body" }, hostsHost)),
        h("div", { class: "split wide-left" },
          h("div", { class: "card pad-0" },
            h("div", { class: "card-head" }, icon("list", 14), h("h2", { text: "Schritte" }),
              h("div", { class: "grow" }),
              h("span", { class: "tiny faint", text: "Schritt anklicken für Details" })),
            tasksWrap),
          h("div", { class: "card pad-0" },
            h("div", { class: "card-head" }, icon("terminal", 14), h("h2", { text: "Protokoll" }),
              h("div", { class: "grow" })),
            h("div", { class: "card-body" },
              h("div", { class: "log-toolbar" },
                logSearch,
                h("label", { class: "check" }, onlyProblems, h("span", { text: "nur Auffälligkeiten" })),
                h("label", { class: "check" }, autoScroll, h("span", { text: "mitlaufen" })),
                h("label", { class: "check" }, wrapToggle, h("span", { text: "umbrechen" })),
                h("div", { class: "grow" }),
                h("button", { class: "small ghost", title: "Protokolltext kopieren",
                  onclick: () => AW.copyText(lines.map(AW.stripAnsi).join("\n")) }, "kopieren")),
              consoleNode))));

      /* -- Daten laden und live halten ---------------------------------- */

      function scheduleRender() {
        dirty = true;
        if (renderTimer) return;
        renderTimer = setTimeout(() => {
          renderTimer = null;
          if (!dirty) return;
          dirty = false;
          renderHead();
          renderHosts();
          renderTasks();
        }, 250);
      }

      function applySnapshot(snapshot) {
        if (snapshot.meta) meta = snapshot.meta;
        if (snapshot.state) {
          state = Object.assign({ plays: [], tasks: {}, task_order: [], hosts: {},
                                  stats: {}, notices: [] }, snapshot.state);
        }
        if (snapshot.log_tail) {
          lines = snapshot.log_tail.slice();
          renderConsole();
        }
        renderHead();
        renderHosts();
        renderTasks();
      }

      try {
        applySnapshot(await API.get("/api/runs/" + encodeURIComponent(runId)));
      } catch (err) {
        AW.mount(container, h("div", { class: "notice error" },
          h("strong", { text: "Lauf nicht gefunden" }),
          h("div", { class: "small mt", text: err.detail || String(err) }),
          h("div", { class: "mt" }, h("a", { class: "btn", href: AW.linkTo("runs") }, "Zur Übersicht"))));
        return;
      }

      const connNote = h("div", {});
      container.insertBefore(connNote, noticeHost);

      socket = API.socket("/ws/runs/" + encodeURIComponent(runId), {
        onOpen: () => AW.clear(connNote),
        onReconnect: (attempt) => {
          AW.mount(connNote, h("div", { class: "notice warn small" },
            "Live-Verbindung unterbrochen – neuer Versuch (" + attempt + ") …"));
        },
        onMessage: (message) => {
          if (message.type === "init") { applySnapshot(message); return; }
          if (message.type === "log") { appendLines(message.lines || []); return; }
          if (message.type === "events") {
            for (const event of message.events || []) applyEvent(state, event);
            if (message.progress && meta) meta.progress = message.progress;
            scheduleRender();
            return;
          }
          if (message.type === "status") {
            meta = message.meta;
            if (message.state) state = Object.assign(state, message.state);
            scheduleRender();
            if (!finishedToastShown && meta.status !== "running" && meta.status !== "queued") {
              finishedToastShown = true;
              const kind = meta.status === "success" ? "ok" :
                           (meta.status === "cancelled" ? "warn" : "error");
              toast("Lauf " + (AW.STATUS_LABELS[meta.status] || meta.status) +
                    " nach " + AW.fmtDuration(meta.duration), kind, 8000);
            }
            return;
          }
          if (message.type === "overflow") {
            AW.mount(connNote, h("div", { class: "notice warn small" },
              "Es kamen mehr Meldungen als darstellbar – ein Teil wurde übersprungen. " +
              "Das vollständige Protokoll steht im Download."));
          }
        },
      });

      const ticker = setInterval(() => {
        if (meta && (meta.status === "running" || meta.status === "queued")) {
          meta.duration = Math.round(((Date.now() / 1000) - (meta.started_at || meta.created_at)) * 10) / 10;
          renderHead();
        }
      }, 1000);

      return () => { if (socket) socket.close(); clearInterval(ticker); clearTimeout(renderTimer); };
    },
  };
})(window.AW);

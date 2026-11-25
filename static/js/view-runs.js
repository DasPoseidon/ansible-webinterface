/* Uebersicht aller Laeufe, mit laufender Aktualisierung. */
"use strict";

(function (AW) {
  const { h, icon, toast, API, fail, statusBadge } = AW;

  function progressBar(meta) {
    const progress = meta.progress || {};
    const total = progress.tasks_total || 0;
    const done = progress.tasks_done || 0;
    const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const cls = meta.status === "failed" ? " failed" : (meta.status === "success" ? " ok" : "");
    return h("div", { class: "progress" + cls, title: done + " von " + (total || "?") + " Schritten" },
      h("div", { style: { width: (meta.status === "queued" ? 0 : percent) + "%" } }));
  }

  function countsLine(meta) {
    const counts = meta.counts || {};
    const parts = [];
    const add = (key, cls, label) => {
      if (counts[key]) parts.push(h("span", { class: cls, text: label + " " + counts[key] }));
    };
    add("ok", "c-ok", "ok");
    add("changed", "c-changed", "geändert");
    add("failed", "c-failed", "fehlgeschlagen");
    add("unreachable", "c-unreachable", "nicht erreichbar");
    add("skipped", "c-skipped", "übersprungen");
    if (!parts.length) return h("span", { class: "faint", text: "–" });
    return h("span", { class: "host-counts", style: { display: "inline-flex" } }, parts);
  }

  const SOURCE_LABELS = { web: "Oberfläche", cron: "Zeitplan", "cron-offline": "Zeitplan (Notbetrieb)", api: "API" };

  function runRow(meta, onClick) {
    const request = meta.request || {};
    return h("tr", { class: "clickable", dataset: { run: meta.id }, onclick: onClick },
      h("td", { class: "nowrap" }, statusBadge(meta.status)),
      h("td", {},
        h("div", { text: request.playbook || "–" }),
        h("div", { class: "s faint tiny mono ellipsis", style: { maxWidth: "320px" },
          text: [request.limit ? "--limit " + request.limit : "ganzes Inventory",
                 request.check ? "Probelauf" : null,
                 (request.tags || []).length ? "Tags: " + request.tags.join(",") : null,
                 request.label || null].filter(Boolean).join("  ·  ") })),
      h("td", { class: "nowrap hide-xs", title: AW.fmtTime(meta.created_at) },
        h("div", { text: AW.fmtTimeShort(meta.created_at) }),
        h("div", { class: "tiny faint", text: AW.fmtAgo(meta.created_at) })),
      h("td", { class: "nowrap num hide-xs", text: AW.fmtDuration(meta.duration) }),
      h("td", { class: "hide-xs" }, countsLine(meta)),
      h("td", { class: "nowrap hide-xs" },
        h("div", { class: "small", text: request.user || "–" }),
        h("div", { class: "tiny faint", text: SOURCE_LABELS[request.source] || request.source || "" })),
      h("td", { style: { width: "110px" } },
        meta.status === "running" || meta.status === "queued" ? progressBar(meta) :
          (meta.failed_hosts || []).length
            ? h("span", { class: "tiny c-failed ellipsis", style: { maxWidth: "110px", display: "block" },
                          title: meta.failed_hosts.join(", "),
                          text: meta.failed_hosts.length + " Host(s) betroffen" })
            : h("span", { class: "tiny faint", text: meta.host_count ? meta.host_count + " Hosts" : "" })));
  }

  AW.views.runs = {
    label: "Läufe", title: "Läufe", cap: "runs.view",

    async mount(container) {
      const rows = new Map();
      let all = [];
      const filterState = { status: "", playbook: "", source: "", text: "" };

      const activeHost = h("div", { class: "grid", style: { marginBottom: "16px" } });
      const tbody = h("tbody");
      const countNode = h("span", { class: "faint small" });

      const statusSelect = h("select", { onchange: () => { filterState.status = statusSelect.value; render(); } },
        h("option", { value: "", text: "Alle Zustände" }),
        ...["running", "queued", "success", "failed", "cancelled", "interrupted", "error"]
          .map((s) => h("option", { value: s, text: AW.STATUS_LABELS[s] || s })));
      const sourceSelect = h("select", { onchange: () => { filterState.source = sourceSelect.value; render(); } },
        h("option", { value: "", text: "Alle Auslöser" }),
        h("option", { value: "web", text: "Oberfläche" }),
        h("option", { value: "cron", text: "Zeitplan" }),
        h("option", { value: "cron-offline", text: "Zeitplan (Notbetrieb)" }),
        h("option", { value: "api", text: "API" }));
      const search = h("input", { type: "search", placeholder: "Playbook, Host, Benutzer …",
        oninput: AW.debounce(() => { filterState.text = search.value.toLowerCase().trim(); render(); }, 200) });

      function matches(meta) {
        const request = meta.request || {};
        if (filterState.status && meta.status !== filterState.status) return false;
        if (filterState.source && request.source !== filterState.source) return false;
        if (filterState.text) {
          const haystack = [request.playbook, request.limit, request.user, request.label,
                            (meta.failed_hosts || []).join(" "), (request.tags || []).join(" ")]
                            .filter(Boolean).join(" ").toLowerCase();
          if (!haystack.includes(filterState.text)) return false;
        }
        return true;
      }

      function open(id) { AW.go("run", { segments: [id] }); }

      function render() {
        const visible = all.filter(matches);
        AW.clear(tbody);
        rows.clear();
        if (!visible.length) {
          tbody.appendChild(h("tr", {}, h("td", { colspan: "7" },
            AW.emptyState(all.length ? "Kein Lauf passt zu den Filtern."
                                     : "Noch kein Lauf vorhanden.",
              AW.can("runs.start") ? h("div", { class: "mt" },
                h("button", { class: "primary", onclick: () => AW.go("start") },
                  icon("play"), "Playbook starten")) : null))));
        } else {
          for (const meta of visible) {
            const row = runRow(meta, () => open(meta.id));
            rows.set(meta.id, row);
            tbody.appendChild(row);
          }
        }
        countNode.textContent = visible.length === all.length
          ? all.length + " Läufe"
          : visible.length + " von " + all.length + " Läufen";
        renderActive();
      }

      function renderActive() {
        const active = all.filter((meta) => meta.status === "running" || meta.status === "queued");
        AW.clear(activeHost);
        if (!active.length) return;
        activeHost.appendChild(h("h2", { class: "mb", text: "Gerade aktiv" }));
        const cards = h("div", { class: "grid two" });
        for (const meta of active) {
          const request = meta.request || {};
          const progress = meta.progress || {};
          cards.appendChild(h("div", { class: "card", style: { cursor: "pointer" },
                                       onclick: () => open(meta.id) },
            h("div", { class: "btn-row", style: { marginBottom: "8px" } },
              statusBadge(meta.status), h("div", { class: "grow" }),
              h("span", { class: "small faint", text: AW.fmtDuration(meta.duration) })),
            h("div", { style: { fontWeight: "500" }, text: request.playbook }),
            h("div", { class: "tiny faint mono ellipsis",
              text: request.limit ? "--limit " + request.limit : "ganzes Inventory" }),
            h("div", { class: "mt" }, progressBar(meta)),
            h("div", { class: "btn-row", style: { marginTop: "8px" } },
              h("span", { class: "tiny faint", text:
                (progress.tasks_done || 0) + " / " + (progress.tasks_total || "?") + " Schritte" }),
              h("div", { class: "grow" }), countsLine(meta))));
        }
        activeHost.appendChild(cards);
      }

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Läufe" }),
            h("div", { class: "sub", text: "Verlauf aller Playbook-Ausführungen, auch der geplanten." })),
          h("div", { class: "btn-row" },
            h("button", { onclick: () => load() }, icon("refresh"), "Aktualisieren"),
            AW.can("runs.start") ? h("button", { class: "primary", onclick: () => AW.go("start") },
              icon("play"), "Playbook starten") : null)),
        activeHost,
        h("div", { class: "card pad-0" },
          h("div", { class: "card-head" },
            h("div", { style: { minWidth: "190px", flex: "1" } }, search),
            statusSelect, sourceSelect,
            h("div", { class: "grow" }), countNode),
          h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {},
                h("th", { text: "Zustand" }),
                h("th", { text: "Playbook / Ziel" }),
                h("th", { class: "hide-xs", text: "Start" }),
                h("th", { class: "num hide-xs", text: "Dauer" }),
                h("th", { class: "hide-xs", text: "Ergebnis" }),
                h("th", { class: "hide-xs", text: "Ausgelöst von" }),
                h("th", { text: "" }))),
              tbody))));

      async function load() {
        try {
          const data = await API.get("/api/runs?limit=300");
          all = data.runs;
          render();
        } catch (err) { fail(err, "Läufe nicht abrufbar"); }
      }

      AW.onRunsMessage = (message) => {
        if (message.type === "init") { all = message.runs || []; render(); return; }
        if (message.type === "run_deleted") {
          all = all.filter((meta) => meta.id !== message.id);
          render();
          return;
        }
        if (message.type !== "run") return;
        const meta = message.meta;
        const index = all.findIndex((item) => item.id === meta.id);
        if (index >= 0) all[index] = meta;
        else all.unshift(meta);
        const row = rows.get(meta.id);
        if (row && matches(meta)) {
          const replacement = runRow(meta, () => open(meta.id));
          row.replaceWith(replacement);
          rows.set(meta.id, replacement);
          renderActive();
        } else {
          render();
        }
      };

      await load();
      const timer = setInterval(() => {
        // Dauer und Fortschritt der aktiven Laeufe mitlaufen lassen
        if (all.some((meta) => meta.status === "running")) renderActive();
      }, 1000);

      return () => { AW.onRunsMessage = null; clearInterval(timer); };
    },
  };

  AW.runsHelpers = { progressBar, countsLine, SOURCE_LABELS };
})(window.AW);

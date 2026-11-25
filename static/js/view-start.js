/* Playbook starten: Auswahl von Playbook, Zielen und Optionen. */
"use strict";

(function (AW) {
  const { h, icon, toast, API, fail } = AW;

  AW.views.start = {
    label: "Playbook starten", title: "Playbook starten", cap: "runs.start",

    async mount(container, route) {
      const params = route.params || {};
      let playbooks = [];
      let discovery = {};
      let inventory = null;
      let selectedPlaybook = params.playbook || "";
      let availableTags = [];
      let mode = params.limit ? "custom" : "all";
      const selected = new Set();

      /* -- Bausteine ---------------------------------------------------- */

      const playbookList = h("div", { class: "list list-scroll" });
      const playbookSearch = h("input", { type: "search", placeholder: "Playbook suchen …",
        oninput: AW.debounce(() => renderPlaybooks(), 150) });
      const targetHost = h("div", {});
      const resolvedNode = h("div", { class: "small mt" });
      const optionsHost = h("div", {});
      const previewHost = h("div", {});
      const startButton = h("button", { class: "primary", onclick: () => start() },
        icon("play"), "Starten");
      const pillHost = h("div", { class: "host-pills" });

      const customLimit = h("input", { type: "text", value: params.limit || "",
        placeholder: "z. B. webservers:!web03.example.com",
        oninput: AW.debounce(() => { updateLimit(); }, 300) });

      const tagsSelect = h("select", { multiple: true, size: 5 });
      const skipTagsSelect = h("select", { multiple: true, size: 5 });
      const checkBox = h("input", { type: "checkbox", checked: params.check === "1" });
      const diffBox = h("input", { type: "checkbox", checked: params.diff === "1" });
      const forksInput = h("input", { type: "number", min: "1", max: "500",
                                      value: params.forks || "", placeholder: "Vorgabe" });
      const verbositySelect = h("select", {},
        ...[["0", "normal"], ["1", "-v"], ["2", "-vv"], ["3", "-vvv"], ["4", "-vvvv"]]
          .map(([value, label]) => h("option", { value, text: label,
            selected: String(params.verbosity || "0") === value })));
      const extraVars = AW.miniEditor({ value: "", mode: "yaml" });

      /* -- Playbooks ---------------------------------------------------- */

      function renderPlaybooks() {
        const needle = playbookSearch.value.toLowerCase().trim();
        AW.clear(playbookList);
        const visible = playbooks.filter((item) =>
          !needle || (item.path + " " + item.name).toLowerCase().includes(needle));
        if (!visible.length) {
          playbookList.appendChild(playbooks.length
            ? AW.emptyState("Kein Playbook passt zur Suche.")
            : AW.emptyState("Es wurde kein Playbook gefunden.", noPlaybooksHint()));
          return;
        }
        for (const item of visible) {
          playbookList.appendChild(h("button", {
            class: "list-item" + (item.path === selectedPlaybook ? " on" : ""),
            onclick: () => selectPlaybook(item.path),
          },
            h("div", { class: "grow" },
              h("div", { class: "t", text: item.path }),
              h("div", { class: "s", text: item.valid
                ? (item.play_count + " Play(s) · " + item.task_count + " Schritte · " + item.name)
                : "fehlerhaft: " + (item.error || "") })),
            item.valid ? null : h("span", { class: "badge failed tiny", text: "YAML-Fehler" })));
        }
      }

      function noPlaybooksHint() {
        return h("div", { class: "small faint mt", style: { textAlign: "left" } },
          h("div", { class: "mb", text: "Gesucht wurde in " + (discovery.ansible_dir || "–") +
                                        " nach diesen Mustern:" }),
          h("div", { class: "mono tiny mb", text: (discovery.globs || []).join("   ") }),
          h("div", { text:
            "Als Playbook gilt eine YAML-Datei, die eine Liste von Plays enthält " +
            "(also Einträge mit hosts: oder import_playbook:). Dateien mit " +
            "YAML-Fehlern werden trotzdem aufgeführt." }),
          h("div", { class: "mt", text:
            "Liegen die Playbooks tiefer verschachtelt, in der config.yaml unter " +
            "discovery.playbook_globs ein passendes Muster ergänzen, etwa " +
            "\"*/*/*.yml\". Übersprungen werden die Ordner: " +
            (discovery.excluded || []).join(", ") + "." }));
      }

      async function selectPlaybook(path) {
        selectedPlaybook = path;
        renderPlaybooks();
        renderOptions();
        loadTags();
        refreshPreview();
      }

      async function loadTags() {
        availableTags = [];
        if (!selectedPlaybook) { fillTagSelects(); return; }
        const local = playbooks.find((item) => item.path === selectedPlaybook);
        if (local && local.tags) availableTags = local.tags.slice();
        fillTagSelects();
        try {
          const data = await API.get("/api/playbooks/tags?playbook=" + encodeURIComponent(selectedPlaybook));
          if (data.ok) {
            const had = availableTags.length > 0;
            availableTags = data.tags;
            fillTagSelects();
            if (had !== (availableTags.length > 0)) renderOptions();
          }
        } catch (err) { /* Tags sind optional */ }
      }

      function fillTagSelects() {
        const wanted = (params.tags || "").split(",").filter(Boolean);
        const wantedSkip = (params.skip_tags || "").split(",").filter(Boolean);
        for (const [select, preset] of [[tagsSelect, wanted], [skipTagsSelect, wantedSkip]]) {
          const previous = Array.from(select.selectedOptions || []).map((o) => o.value);
          AW.clear(select);
          const keep = previous.length ? previous : preset;
          for (const tag of availableTags) {
            select.appendChild(h("option", { value: tag, text: tag, selected: keep.includes(tag) }));
          }
          if (!availableTags.length) {
            select.appendChild(h("option", { value: "", text: "(keine Tags im Playbook)", disabled: true }));
          }
        }
      }

      /* -- Ziele -------------------------------------------------------- */

      function currentLimit() {
        if (mode === "all") return "";
        if (mode === "custom") return customLimit.value.trim();
        return Array.from(selected).map((entry) => entry.split(":").slice(1).join(":")).join(",");
      }

      function renderPills() {
        AW.clear(pillHost);
        if (mode !== "pick" || !selected.size) return;
        for (const entry of Array.from(selected).sort()) {
          const [kind, ...rest] = entry.split(":");
          const name = rest.join(":");
          pillHost.appendChild(h("span", { class: "pill" },
            icon(kind === "group" ? "server" : "terminal", 11),
            h("span", { text: name }),
            h("button", { title: "Entfernen", onclick: () => {
              selected.delete(entry); renderTargets(); updateLimit();
            } }, "×")));
        }
        pillHost.appendChild(h("button", { class: "small ghost", onclick: () => {
          selected.clear(); renderTargets(); updateLimit();
        } }, "alle abwählen"));
      }

      const expanded = new Set(["all"]);

      function treeNode(node, depth) {
        const key = "group:" + node.name;
        const isSelected = selected.has(key);
        const open = expanded.has(node.name);
        const hasChildren = (node.children || []).length || (node.hosts || []).length;
        const row = h("div", { class: "tree-row" },
          hasChildren ? h("button", { class: "caret", onclick: () => {
            if (open) expanded.delete(node.name); else expanded.add(node.name);
            renderTargets();
          } }, icon(open ? "chevronD" : "chevronR", 13)) : h("span", { style: { width: "16px" } }),
          node.name === "all" ? null : h("input", { type: "checkbox", checked: isSelected,
            style: { width: "auto" },
            onchange: (event) => {
              if (event.target.checked) selected.add(key); else selected.delete(key);
              updateLimit(); renderPills();
            } }),
          h("label", { class: "name" }, icon("server", 13),
            h("span", { text: node.name === "all" ? "all (ganzes Inventory)" : node.name }),
            h("span", { class: "count", text: "(" + node.host_count + ")" })));
        const children = [];
        if (open) {
          for (const child of node.children || []) children.push(treeNode(child, depth + 1));
          for (const host of node.hosts || []) {
            const hostKey = "host:" + host;
            children.push(h("div", { class: "tree-row" },
              h("span", { style: { width: "16px" } }),
              h("input", { type: "checkbox", checked: selected.has(hostKey), style: { width: "auto" },
                onchange: (event) => {
                  if (event.target.checked) selected.add(hostKey); else selected.delete(hostKey);
                  updateLimit(); renderPills();
                } }),
              h("label", { class: "name" }, icon("terminal", 12),
                h("span", { class: "mono", text: host }))));
          }
        }
        return h("div", {}, row,
          children.length ? h("div", { class: "tree-node" }, children) : null);
      }

      function renderTargets() {
        const modeButtons = h("div", { class: "seg" },
          ...[["all", "Ganzes Inventory"], ["pick", "Gruppen & Hosts"], ["custom", "Eigener Ausdruck"]]
            .map(([value, label]) => h("button", {
              class: mode === value ? "on" : "",
              onclick: () => { mode = value; renderTargets(); updateLimit(); },
            }, label)));

        let detail = null;
        if (mode === "all") {
          detail = h("div", { class: "notice small mt" },
            inventory && inventory.ok
              ? "Das Playbook läuft gegen alle Hosts, auf die seine eigenen hosts-Angaben passen – " +
                "insgesamt kennt das Inventory " + inventory.counts.hosts + " Hosts."
              : "Das Inventory konnte nicht gelesen werden.");
        } else if (mode === "custom") {
          detail = h("div", { class: "mt" },
            h("div", { class: "field" },
              h("label", { text: "--limit Ausdruck" }), customLimit,
              h("div", { class: "hint", text:
                "Wie auf der Kommandozeile: Komma für „oder“, : für Verknüpfung, " +
                "! für Ausschluss, & für Schnittmenge. Beispiel: webservers:!web03.example.com" })));
        } else {
          const tree = h("div", { class: "tree list-scroll mt" });
          if (inventory && inventory.ok) {
            for (const root of inventory.tree) tree.appendChild(treeNode(root, 0));
            const ungrouped = (inventory.hosts || []).filter((host) => !host.groups.length);
            if (ungrouped.length) {
              tree.appendChild(h("div", { class: "tree-row mt" },
                h("span", { style: { width: "16px" } }),
                h("span", { class: "faint small", text: "ohne Gruppe:" })));
              for (const host of ungrouped) {
                const hostKey = "host:" + host.name;
                tree.appendChild(h("div", { class: "tree-row" },
                  h("span", { style: { width: "16px" } }),
                  h("input", { type: "checkbox", checked: selected.has(hostKey), style: { width: "auto" },
                    onchange: (event) => {
                      if (event.target.checked) selected.add(hostKey); else selected.delete(hostKey);
                      updateLimit(); renderPills();
                    } }),
                  h("label", { class: "name" }, icon("terminal", 12),
                    h("span", { class: "mono", text: host.name }))));
              }
            }
          } else {
            tree.appendChild(h("div", { class: "notice error small",
              text: (inventory && inventory.error) || "Inventory nicht lesbar" }));
          }
          detail = h("div", {}, tree, pillHost);
        }

        AW.mount(targetHost, modeButtons, detail, resolvedNode);
        renderPills();
      }

      const updateLimit = AW.debounce(async () => {
        const limit = currentLimit();
        refreshPreview();
        if (mode === "pick" && !selected.size) {
          AW.mount(resolvedNode, h("span", { class: "faint",
            text: "Noch nichts ausgewählt – bitte Gruppen oder Hosts anhaken." }));
          startButton.disabled = true;
          return;
        }
        startButton.disabled = !selectedPlaybook;
        if (!limit) {
          AW.mount(resolvedNode, h("span", { class: "faint", text: "Kein --limit: alle passenden Hosts." }));
          return;
        }
        AW.mount(resolvedNode, AW.spinner(true), " prüft Ausdruck …");
        try {
          const data = await API.get("/api/inventory/resolve?limit=" + encodeURIComponent(limit));
          if (!data.ok) {
            AW.mount(resolvedNode, h("span", { class: "c-failed", text: "Ausdruck ungültig: " + data.error }));
            return;
          }
          if (!data.hosts.length) {
            AW.mount(resolvedNode, h("span", { class: "c-failed" },
              icon("alert", 13), " Dieser Ausdruck trifft keinen einzigen Host."));
            return;
          }
          AW.mount(resolvedNode,
            h("span", { class: "c-ok" }, icon("check", 13), " " + data.hosts.length + " Host(s): "),
            h("span", { class: "mono faint", text: data.hosts.slice(0, 12).join(", ") +
              (data.hosts.length > 12 ? " …" : "") }));
        } catch (err) {
          AW.mount(resolvedNode, h("span", { class: "faint", text: "Prüfung nicht möglich." }));
        }
      }, 250);

      /* -- Optionen ----------------------------------------------------- */

      function renderOptions() {
        const hasTags = availableTags.length > 0;
        AW.mount(optionsHost,
          hasTags
            ? h("div", { class: "fields-row" },
                h("div", { class: "field" }, h("label", { text: "Nur diese Tags (--tags)" }), tagsSelect,
                  h("div", { class: "hint", text: "Mehrfachauswahl mit Strg bzw. Cmd." })),
                h("div", { class: "field" }, h("label", { text: "Diese Tags überspringen" }), skipTagsSelect))
            : h("div", { class: "field" },
                h("label", { text: "Tags" }),
                h("div", { class: "hint", text: "Dieses Playbook vergibt keine Tags – es laufen immer alle Schritte." })),
          h("div", { class: "fields-row" },
            h("div", { class: "field" },
              h("label", { text: "Parallele Prozesse (--forks)" }), forksInput,
              h("div", { class: "hint", text: "Leer lassen für die Vorgabe aus der ansible.cfg." })),
            h("div", { class: "field" },
              h("label", { text: "Ausführlichkeit" }), verbositySelect,
              h("div", { class: "hint", text: "Mehr -v erzeugt deutlich mehr Protokoll." }))),
          h("div", { class: "field" },
            h("label", { class: "check" }, checkBox,
              h("span", {}, h("span", { text: "Probelauf (--check)" }),
                h("span", { class: "lbl-sub", text:
                  "Ansible ändert nichts, sondern meldet nur, was es ändern würde. " +
                  "Nicht alle Module unterstützen das zuverlässig." }))),
            h("label", { class: "check" }, diffBox,
              h("span", {}, h("span", { text: "Änderungen anzeigen (--diff)" }),
                h("span", { class: "lbl-sub", text: "Zeigt die Unterschiede an geänderten Dateien." })))),
          h("div", { class: "field" },
            h("label", { text: "Zusatzvariablen (--extra-vars, YAML)" }), extraVars.node,
            h("div", { class: "hint", text: "Zum Beispiel: app_version: \"1.5.0\"" })));
        extraVars.attach();
        extraVars.refresh();
      }

      /* -- Vorschau ----------------------------------------------------- */

      const refreshPreview = AW.debounce(async () => {
        if (!selectedPlaybook) {
          AW.mount(previewHost, h("div", { class: "faint small",
            text: "Links ein Playbook auswählen." }));
          return;
        }
        AW.mount(previewHost, AW.spinner(true), " ermittelt Schritte …");
        const limit = currentLimit();
        const tags = Array.from(tagsSelect.selectedOptions || []).map((o) => o.value).filter(Boolean);
        const skip = Array.from(skipTagsSelect.selectedOptions || []).map((o) => o.value).filter(Boolean);
        try {
          const query = "/api/playbooks/preview?playbook=" + encodeURIComponent(selectedPlaybook) +
            "&limit=" + encodeURIComponent(limit) + "&tags=" + encodeURIComponent(tags.join(",")) +
            "&skip_tags=" + encodeURIComponent(skip.join(","));
          const data = await API.get(query);
          const tasks = data.tasks || {};
          if (!tasks.ok) {
            AW.mount(previewHost, h("div", { class: "notice error small" },
              h("strong", { text: "Ansible kann dieses Playbook nicht einlesen" }),
              h("pre", { class: "diff", style: { marginTop: "8px", maxHeight: "200px" },
                         text: tasks.error || "" })));
            return;
          }
          const hostList = (data.hosts && data.hosts.hosts) || [];
          AW.mount(previewHost,
            h("div", { class: "small mb" },
              h("strong", { text: tasks.task_count + " Schritte" }),
              h("span", { class: "faint", text: " in " + (tasks.plays || []).length + " Play(s)" }),
              hostList.length ? h("span", { class: "faint",
                text: " · " + hostList.length + " betroffene Hosts" }) : null),
            ...(tasks.plays || []).map((play) => h("details", { class: "box", open: (tasks.plays || []).length <= 2 },
              h("summary", {}, h("span", { text: play.name || "(ohne Namen)" }),
                h("span", { class: "faint mono tiny", text: "  " + play.pattern }),
                h("span", { class: "faint tiny", text: "  " + play.tasks.length + " Schritte" })),
              h("div", {}, h("ol", { class: "small", style: { margin: "0", paddingLeft: "20px" } },
                ...play.tasks.map((task) => h("li", {},
                  h("span", { text: task.name }),
                  (task.tags || []).length ? h("span", { class: "faint tiny mono",
                    text: "  [" + task.tags.join(",") + "]" }) : null)))))));
        } catch (err) {
          AW.mount(previewHost, h("div", { class: "faint small",
            text: "Vorschau nicht verfügbar: " + (err.detail || err) }));
        }
      }, 400);

      /* -- Starten ------------------------------------------------------ */

      async function start() {
        if (!selectedPlaybook) { toast("Bitte zuerst ein Playbook auswählen", "warn"); return; }
        const body = {
          playbook: selectedPlaybook,
          limit: currentLimit(),
          tags: Array.from(tagsSelect.selectedOptions || []).map((o) => o.value).filter(Boolean),
          skip_tags: Array.from(skipTagsSelect.selectedOptions || []).map((o) => o.value).filter(Boolean),
          check: checkBox.checked,
          diff: diffBox.checked,
          extra_vars: extraVars.value.trim(),
          verbosity: parseInt(verbositySelect.value, 10) || 0,
          forks: forksInput.value ? parseInt(forksInput.value, 10) : null,
        };
        if (!body.check) {
          const limitText = body.limit || "alle passenden Hosts";
          const ok = await AW.confirmDialog({
            title: "Playbook wirklich ausführen?",
            body: selectedPlaybook + "\nZiel: " + limitText +
                  "\n\nDie Änderungen werden auf den Hosts tatsächlich durchgeführt.",
            confirmLabel: "Jetzt ausführen",
          });
          if (!ok) return;
        }
        startButton.disabled = true;
        try {
          const result = await API.post("/api/runs", body);
          AW.go("run", { segments: [result.id] });
        } catch (err) {
          startButton.disabled = false;
          fail(err, "Start nicht möglich");
        }
      }

      /* -- Aufbau ------------------------------------------------------- */

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Playbook starten" }),
            h("div", { class: "sub", text: "Playbook wählen, Ziele festlegen, Optionen prüfen." })),
          h("div", { class: "btn-row" },
            h("button", { onclick: () => load(true) }, icon("refresh"), "Neu einlesen"),
            startButton)),
        h("div", { class: "split wide-left" },
          h("div", {},
            h("div", { class: "card pad-0" },
              h("div", { class: "card-head" }, icon("file", 14), h("h2", { text: "Playbook" }),
                h("div", { class: "grow" })),
              h("div", { class: "card-body" }, playbookSearch),
              playbookList),
            h("div", { class: "card", style: { marginTop: "16px" } },
              h("h2", { class: "mb" }, icon("server", 14), " Ziele"),
              targetHost)),
          h("div", {},
            h("div", { class: "card" },
              h("h2", { class: "mb" }, icon("settings", 14), " Optionen"),
              optionsHost),
            h("div", { class: "card", style: { marginTop: "16px" } },
              h("h2", { class: "mb" }, icon("list", 14), " Was passiert (Vorschau)"),
              previewHost))));

      async function load(force) {
        try {
          const [pbData, invData] = await Promise.all([
            API.get("/api/playbooks" + (force ? "?refresh=true" : "")),
            API.get("/api/inventory" + (force ? "?refresh=true" : "")),
          ]);
          playbooks = pbData.playbooks || [];
          discovery = pbData;
          inventory = invData;
          if (!selectedPlaybook && playbooks.length) {
            const preferred = playbooks.find((item) => /^site\.ya?ml$/i.test(item.path));
            selectedPlaybook = (preferred || playbooks[0]).path;
          }
          renderPlaybooks();
          renderTargets();
          renderOptions();
          loadTags();
          updateLimit();
          refreshPreview();
          if (inventory && !inventory.ok) {
            toast("Inventory konnte nicht gelesen werden: " + inventory.error, "error");
          }
        } catch (err) { fail(err, "Daten nicht abrufbar"); }
      }

      tagsSelect.addEventListener("change", () => refreshPreview());
      skipTagsSelect.addEventListener("change", () => refreshPreview());
      await load();
      return () => {};
    },
  };
})(window.AW);

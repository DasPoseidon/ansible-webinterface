/* Playbooks, Inventory und Variablendateien bearbeiten. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, API, fail } = AW;

  async function safeList(path) {
    try { return await API.get("/api/files?path=" + encodeURIComponent(path)); }
    catch (err) { return null; }
  }

  AW.views.editor = {
    label: "Playbooks & Inventory", title: "Playbooks & Inventory", cap: "files.read",

    async mount(container, route) {
      const params = route.params || {};
      let groups = [];
      let currentPath = params.path || "";
      let editor = null;

      const listHost = h("div", { class: "list-scroll" });
      const editorHost = h("div", {});
      const gitHost = h("div", {});
      const search = h("input", { type: "search", placeholder: "Datei suchen …",
        oninput: AW.debounce(() => renderList(), 150) });

      function renderList() {
        const needle = search.value.toLowerCase().trim();
        AW.clear(listHost);
        let shown = 0;
        for (const group of groups) {
          const entries = group.entries.filter((entry) =>
            !needle || entry.path.toLowerCase().includes(needle));
          if (!entries.length) continue;
          listHost.appendChild(h("div", { class: "play-head" },
            icon(group.icon, 13), h("span", { text: group.label }),
            h("div", { class: "grow" }),
            h("span", { class: "tiny faint", text: String(entries.length) })));
          for (const entry of entries) {
            shown += 1;
            listHost.appendChild(h("button", {
              class: "list-item" + (entry.path === currentPath ? " on" : ""),
              onclick: () => open(entry.path),
            },
              h("div", { class: "grow" },
                h("div", { class: "t", text: entry.label || entry.path }),
                h("div", { class: "s", text: entry.hint || entry.path })),
              entry.badge ? h("span", { class: "badge tiny " + (entry.badgeKind || "plain"),
                                        text: entry.badge }) : null));
          }
        }
        if (!shown) listHost.appendChild(AW.emptyState("Keine Datei gefunden."));
      }

      async function open(path) {
        if (editor && editor.isDirty()) {
          const ok = await confirmDialog({
            title: "Ungespeicherte Änderungen",
            body: "An " + editor.path + " gibt es Änderungen, die noch nicht gespeichert sind.",
            confirmLabel: "Verwerfen und wechseln", danger: true,
          });
          if (!ok) return;
        }
        currentPath = path;
        history.replaceState(null, "", AW.linkTo("editor", { path }));
        renderList();
        if (!editor) {
          editor = new AW.FileEditor({
            path,
            onSaved: () => { loadGit(); loadFiles(); },
            onDirty: () => renderList(),
          });
          AW.mount(editorHost, editor.node);
        }
        editor.path = path;
        await editor.load(path);
      }

      async function loadFiles() {
        const config = AW.state.config;
        const inventoryRel = config.inventory.replace(config.ansible_dir + "/", "");
        const [pbData, root] = await Promise.all([
          API.get("/api/playbooks").catch(() => ({ playbooks: [] })),
          safeList(""),
        ]);
        // Nur Ordner abfragen, die es wirklich gibt - sonst quittiert der
        // Dienst mit 400 und der Browser protokolliert einen Fehler.
        const rootDirs = new Set(((root && root.entries) || [])
          .filter((entry) => entry.type === "dir").map((entry) => entry.name));
        const varListings = await Promise.all(["group_vars", "host_vars"]
          .filter((name) => rootDirs.has(name)).map((name) => safeList(name)));

        const playbooks = (pbData.playbooks || []).map((item) => ({
          path: item.path, label: item.path,
          hint: item.valid ? (item.play_count + " Play(s) · " + item.task_count + " Schritte · " + item.name)
                           : (item.error || "fehlerhaft"),
          badge: item.valid ? null : "YAML-Fehler", badgeKind: "failed",
        }));

        const inventoryEntries = [{ path: inventoryRel, label: inventoryRel, hint: "Inventory" }];
        const configEntries = [];
        for (const entry of (root && root.entries) || []) {
          if (entry.type !== "file") continue;
          if (entry.path === inventoryRel) continue;
          if (/^ansible\.cfg$/i.test(entry.name) || /\.(cfg|ini|conf)$/i.test(entry.name)) {
            configEntries.push({ path: entry.path, label: entry.name, hint: "Konfiguration" });
          }
        }
        const varEntries = [];
        for (const listing of varListings) {
          for (const entry of (listing && listing.entries) || []) {
            if (entry.type === "file" && entry.editable) {
              varEntries.push({ path: entry.path, label: entry.path, hint: "Variablen" });
            }
          }
        }

        groups = [
          { label: "Inventory", icon: "server", entries: inventoryEntries },
          { label: "Playbooks", icon: "file", entries: playbooks },
          { label: "Variablen", icon: "list", entries: varEntries },
          { label: "Konfiguration", icon: "settings", entries: configEntries },
        ].filter((group) => group.entries.length);
        renderList();
      }

      async function loadGit() {
        if (!AW.state.config.git_enabled) {
          AW.mount(gitHost, h("div", { class: "notice small" },
            "Git ist in der Konfiguration abgeschaltet – Änderungen werden nur als " +
            "Sicherungskopie festgehalten."));
          return;
        }
        try {
          const info = await API.get("/api/git");
          if (!info.repo) {
            AW.mount(gitHost, h("div", { class: "notice warn small" },
              "Das Ansible-Verzeichnis ist kein Git-Repository – es werden nur " +
              "Sicherungskopien angelegt."));
            return;
          }
          const canWrite = AW.can("git.write");
          AW.mount(gitHost, h("div", { class: "card" },
            h("div", { class: "btn-row" },
              icon("branch", 14),
              h("span", { class: "mono small", text: info.branch }),
              info.dirty
                ? h("span", { class: "badge changed", text: info.dirty + " offene Änderung(en)" })
                : h("span", { class: "badge ok", text: "alles eingecheckt" }),
              info.ahead ? h("span", { class: "badge plain", text: info.ahead + " nicht gepusht" }) : null,
              h("div", { class: "grow" }),
              info.dirty && canWrite ? h("button", { class: "small", onclick: commitAll },
                icon("save"), "Alles committen") : null,
              (info.remotes || []).length && canWrite
                ? h("button", { class: "small", onclick: push }, icon("upload"), "Push") : null,
              h("button", { class: "small ghost", onclick: showStatus }, icon("eye"), "Details")),
            info.head ? h("div", { class: "tiny faint mt mono", text: "HEAD: " + info.head }) : null));
        } catch (err) {
          AW.mount(gitHost, h("div", { class: "notice error small",
            text: "Git-Status nicht abrufbar: " + (err.detail || err) }));
        }
      }

      async function commitAll() {
        const message = await AW.promptDialog({
          title: "Alle offenen Änderungen committen",
          label: "Commit-Nachricht",
          value: "Änderungen über das Webinterface",
        });
        if (message === null) return;
        try {
          const result = await API.post("/api/git/commit", { message });
          toast(result.committed ? "Commit " + result.commit + " erstellt"
                                 : (result.reason || "Nichts zu committen"),
                result.committed ? "ok" : "warn");
          loadGit();
        } catch (err) { fail(err, "Commit fehlgeschlagen"); }
      }

      async function push() {
        try {
          const result = await API.post("/api/git/push");
          toast(result.ok ? "Push erfolgreich" : "Push fehlgeschlagen: " + result.error,
                result.ok ? "ok" : "error");
          loadGit();
        } catch (err) { fail(err, "Push fehlgeschlagen"); }
      }

      async function showStatus() {
        const body = h("div", {}, AW.spinner());
        modal({ title: "Git-Status", width: "760px", body });
        try {
          const [info, diff] = await Promise.all([
            API.get("/api/git"), API.text("/api/git/diff"),
          ]);
          AW.mount(body,
            (info.changes || []).length
              ? h("div", { class: "table-wrap card pad-0 mb" }, h("table", {},
                  h("thead", {}, h("tr", {}, h("th", { text: "Zustand" }), h("th", { text: "Datei" }))),
                  h("tbody", {}, ...info.changes.map((change) => h("tr", {},
                    h("td", {}, h("span", { class: "badge tiny plain", text: change.label })),
                    h("td", { class: "mono small", text: change.path }))))))
              : h("div", { class: "notice ok small", text: "Keine offenen Änderungen." }),
            diff && diff.trim()
              ? h("details", { class: "box", open: true },
                  h("summary", { text: "Unterschiede der versionierten Dateien" }),
                  h("div", {}, h("pre", { class: "diff" }, AW.diffToFragment(diff))))
              : null);
        } catch (err) {
          AW.mount(body, h("div", { class: "notice error", text: err.detail || String(err) }));
        }
      }

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Playbooks & Inventory" }),
            h("div", { class: "sub", text:
              "Beim Speichern wird geprüft (YAML und Ansible-Syntax), eine Sicherungskopie " +
              "angelegt und ein Git-Commit erstellt." })),
          h("div", { class: "btn-row" },
            AW.can("runs.start") ? h("button", { onclick: () => {
              if (currentPath) AW.go("start", { playbook: currentPath });
              else AW.go("start");
            } }, icon("play"), "Dieses Playbook starten") : null)),
        gitHost,
        h("div", { class: "split", style: { marginTop: "16px" } },
          h("div", { class: "card pad-0" },
            h("div", { class: "card-head" }, h("div", { style: { flex: "1" } }, search)),
            listHost),
          editorHost));

      await loadFiles();
      loadGit();
      if (!currentPath && groups.length) {
        currentPath = groups[0].entries[0].path;
      }
      if (currentPath) await open(currentPath);

      return () => {};
    },
  };
})(window.AW);

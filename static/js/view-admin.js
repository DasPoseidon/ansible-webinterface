/* Verwaltung: Systemzustand, Benutzer, Tokens, Audit, eigenes Passwort. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, API, fail } = AW;

  AW.views.admin = {
    label: "Verwaltung", title: "Verwaltung", cap: "system.view",

    async mount(container, route) {
      const params = route.params || {};
      const tabs = [
        { key: "status", label: "Zustand", cap: "system.view" },
        { key: "users", label: "Benutzer", cap: "users.manage" },
        { key: "tokens", label: "API-Tokens", cap: "users.manage" },
        { key: "audit", label: "Protokoll", cap: "audit.view" },
        { key: "account", label: "Mein Zugang", cap: "system.view" },
      ].filter((tab) => AW.can(tab.cap));
      let active = tabs.some((tab) => tab.key === params.tab) ? params.tab : tabs[0].key;

      const tabBar = h("div", { class: "tabs" });
      const panel = h("div", {});

      function renderTabs() {
        AW.clear(tabBar);
        for (const tab of tabs) {
          tabBar.appendChild(h("button", { class: active === tab.key ? "on" : "",
            onclick: () => { active = tab.key;
              history.replaceState(null, "", AW.linkTo("admin", { tab: active }));
              renderTabs(); renderPanel(); } }, tab.label));
        }
      }

      async function renderPanel() {
        AW.mount(panel, AW.spinner());
        try {
          if (active === "status") await renderStatus();
          else if (active === "users") await renderUsers();
          else if (active === "tokens") await renderTokens();
          else if (active === "audit") await renderAudit();
          else await renderAccount();
        } catch (err) {
          AW.mount(panel, h("div", { class: "notice error" },
            h("div", { text: err.detail || String(err) })));
        }
      }

      /* -- Zustand ------------------------------------------------------ */

      async function renderStatus() {
        const status = await API.get("/api/status");
        const config = status.config || {};
        const git = status.git || {};
        const runs = status.runs || {};
        AW.mount(panel,
          status.problems.length
            ? h("div", { class: "notice error" },
                h("strong", { text: "Die Konfiguration passt nicht zur Umgebung" }),
                h("ul", {}, ...status.problems.map((problem) => h("li", { text: problem }))))
            : h("div", { class: "notice ok small" },
                icon("check", 13), " Pfade und Ansible sind erreichbar."),
          !status.crontab_available
            ? h("div", { class: "notice warn small" },
                h("strong", { text: "cron ist nicht verfügbar. " }),
                "Der Befehl crontab wurde nicht gefunden – geplante Jobs lassen sich " +
                "nicht verwalten, bis cron im Container installiert ist.")
            : null,
          h("div", { class: "grid two mt" },
            h("div", { class: "card" },
              h("h2", { class: "mb" }, icon("folder", 14), " Pfade"),
              h("dl", { class: "kv" },
                h("dt", { text: "Ansible-Verzeichnis" }), h("dd", { text: config.ansible_dir }),
                h("dt", { text: "Inventory" }), h("dd", { text: config.inventory }),
                h("dt", { text: "Daten-/Konfigordner" }), h("dd", { text: config.data_dir }),
                h("dt", { text: "Zustandsverzeichnis" }), h("dd", { text: status.state_dir }),
                h("dt", { text: "Konfigurationsdatei" }), h("dd", { text: status.config_file || "(Vorgaben)" }),
                h("dt", { text: "ansible-playbook" }), h("dd", { text: status.ansible_playbook }),
                h("dt", { text: "Python" }), h("dd", { text: status.python }))),
            h("div", { class: "card" },
              h("h2", { class: "mb" }, icon("branch", 14), " Git"),
              git.repo
                ? h("dl", { class: "kv" },
                    h("dt", { text: "Branch" }), h("dd", { text: git.branch || "–" }),
                    h("dt", { text: "Offene Änderungen" }), h("dd", { text: String(git.dirty || 0) }),
                    h("dt", { text: "Remotes" }), h("dd", { text: (git.remotes || []).join(", ") || "keine" }),
                    h("dt", { text: "HEAD" }), h("dd", { text: git.head || "–" }))
                : h("div", { class: "small faint", text:
                    "Kein Git-Repository – Änderungen werden nur als Sicherungskopie abgelegt." }),
              h("h2", { class: "mb mt-lg" }, icon("play", 14), " Läufe"),
              h("dl", { class: "kv" },
                h("dt", { text: "Gerade aktiv" }), h("dd", { text: String((runs.active || []).length) }),
                h("dt", { text: "Gespeichert" }), h("dd", { text: String(runs.total || 0) }),
                h("dt", { text: "Gleichzeitig erlaubt" }), h("dd", { text: String(runs.max_concurrent) }),
                h("dt", { text: "Aufbewahrt" }), h("dd", { text: runs.keep + " Läufe" })))));
      }

      /* -- Benutzer ----------------------------------------------------- */

      async function renderUsers() {
        const data = await API.get("/api/users");
        const roleLabels = {};
        for (const role of AW.state.roles || []) roleLabels[role.name] = role.label;

        const rows = data.users.map((user) => h("tr", {},
          h("td", {}, h("div", { text: user.name }),
            h("div", { class: "tiny faint", text: user.sessions
              ? user.sessions + " offene Sitzung(en)" : "nicht angemeldet" })),
          h("td", {}, h("select", { onchange: async (event) => {
            try {
              await API.put("/api/users/" + encodeURIComponent(user.name), { role: event.target.value });
              toast("Rolle geändert: " + user.name + " → " + event.target.value, "ok");
              renderPanel();
            } catch (err) { fail(err, "Rolle nicht änderbar"); event.target.value = user.role; }
          } }, ...(AW.state.roles || []).map((role) =>
            h("option", { value: role.name, text: role.name, selected: role.name === user.role })))),
          h("td", { class: "small faint hide-xs", text: roleLabels[user.role] || "" }),
          h("td", { class: "nowrap hide-xs", text: user.last_login ? AW.fmtTime(user.last_login) : "noch nie" }),
          h("td", {}, h("label", { class: "switch", title: user.disabled ? "gesperrt" : "aktiv" },
            h("input", { type: "checkbox", checked: !user.disabled, onchange: async (event) => {
              try {
                await API.put("/api/users/" + encodeURIComponent(user.name),
                              { disabled: !event.target.checked });
                toast(event.target.checked ? "Zugang freigegeben" : "Zugang gesperrt", "ok");
                renderPanel();
              } catch (err) { fail(err, "Nicht änderbar"); event.target.checked = !user.disabled; }
            } }), h("span", {}))),
          h("td", {}, h("div", { class: "btn-row end" },
            h("button", { class: "small ghost", title: "Passwort setzen",
              onclick: () => setPassword(user) }, "Passwort"),
            user.sessions ? h("button", { class: "small ghost", title: "Alle Sitzungen beenden",
              onclick: async () => {
                try {
                  const result = await API.post("/api/users/" + encodeURIComponent(user.name) + "/sessions/clear");
                  toast(result.beendet + " Sitzung(en) beendet", "ok");
                  renderPanel();
                } catch (err) { fail(err, "Fehlgeschlagen"); }
              } }, "abmelden") : null,
            h("button", { class: "small ghost", title: "Benutzer löschen",
              onclick: async () => {
                const ok = await confirmDialog({ title: "Benutzer löschen?",
                  body: user.name + " verliert den Zugang sofort.",
                  confirmLabel: "Löschen", danger: true });
                if (!ok) return;
                try {
                  await API.del("/api/users/" + encodeURIComponent(user.name));
                  toast("Benutzer gelöscht", "ok");
                  renderPanel();
                } catch (err) { fail(err, "Löschen fehlgeschlagen"); }
              } }, icon("trash", 13))))));

        AW.mount(panel,
          h("div", { class: "card" },
            h("h2", { class: "mb", text: "Was die Rollen dürfen" }),
            h("dl", { class: "kv" },
              ...(AW.state.roles || []).flatMap((role) => [
                h("dt", { text: role.name }), h("dd", { text: role.label })]))),
          h("div", { class: "card pad-0 mt" },
            h("div", { class: "card-head" }, icon("users", 14), h("h2", { text: "Benutzer" }),
              h("div", { class: "grow" }),
              h("button", { class: "small primary", onclick: createUser }, icon("plus"), "Anlegen")),
            h("div", { class: "table-wrap" }, h("table", {},
              h("thead", {}, h("tr", {},
                h("th", { text: "Name" }), h("th", { text: "Rolle" }),
                h("th", { class: "hide-xs", text: "Bedeutung" }),
                h("th", { class: "hide-xs", text: "Letzte Anmeldung" }),
                h("th", { text: "Aktiv" }), h("th", { text: "" }))),
              h("tbody", {}, rows)))));
      }

      function createUser() {
        const name = h("input", { type: "text", placeholder: "Benutzername" });
        const password = h("input", { type: "password", placeholder: "mindestens 10 Zeichen",
                                      autocomplete: "new-password" });
        const role = h("select", {}, ...(AW.state.roles || []).map((item) =>
          h("option", { value: item.name, text: item.name + " – " + item.label,
                        selected: item.name === "operator" })));
        modal({
          title: "Benutzer anlegen", width: "560px",
          body: h("div", {},
            h("div", { class: "field" }, h("label", { text: "Benutzername" }), name,
              h("div", { class: "hint", text: "Buchstaben, Zahlen und . _ - @" })),
            h("div", { class: "field" }, h("label", { text: "Passwort" }), password),
            h("div", { class: "field" }, h("label", { text: "Rolle" }), role)),
          buttons: [
            { label: "Abbrechen", onClick: (close) => close() },
            { label: "Anlegen", class: "primary", onClick: async (close) => {
              try {
                await API.post("/api/users", { name: name.value.trim(),
                                               password: password.value, role: role.value });
                toast("Benutzer angelegt", "ok");
                close();
                renderPanel();
              } catch (err) { fail(err, "Anlegen fehlgeschlagen"); }
            } },
          ],
        });
      }

      function setPassword(user) {
        const password = h("input", { type: "password", autocomplete: "new-password",
                                      placeholder: "mindestens 10 Zeichen" });
        modal({
          title: "Passwort setzen für " + user.name, width: "480px",
          body: h("div", {},
            h("div", { class: "field" }, h("label", { text: "Neues Passwort" }), password),
            h("div", { class: "notice warn small", text:
              "Alle offenen Sitzungen dieses Benutzers werden dabei beendet." })),
          buttons: [
            { label: "Abbrechen", onClick: (close) => close() },
            { label: "Setzen", class: "primary", onClick: async (close) => {
              try {
                await API.put("/api/users/" + encodeURIComponent(user.name),
                              { password: password.value });
                toast("Passwort gesetzt", "ok");
                close();
                renderPanel();
              } catch (err) { fail(err, "Fehlgeschlagen"); }
            } },
          ],
        });
      }

      /* -- Tokens ------------------------------------------------------- */

      async function renderTokens() {
        const data = await API.get("/api/tokens");
        AW.mount(panel,
          h("div", { class: "notice small" },
            h("strong", { text: "Wofür API-Tokens? " }),
            "Der Cron-Client nutzt ein eigenes Token mit der Rolle operator, das beim Start " +
            "automatisch angelegt wird (Name cron-client). Weitere Tokens braucht man nur, " +
            "wenn andere Programme Läufe anstoßen sollen. Ein Token wird nur einmal " +
            "angezeigt – danach ist nur noch sein Hash gespeichert."),
          h("div", { class: "card pad-0 mt" },
            h("div", { class: "card-head" }, h("h2", { text: "Tokens" }),
              h("div", { class: "grow" }),
              h("button", { class: "small primary", onclick: createToken }, icon("plus"), "Token anlegen")),
            h("div", { class: "table-wrap" }, h("table", {},
              h("thead", {}, h("tr", {},
                h("th", { text: "Name" }), h("th", { text: "Rolle" }),
                h("th", { class: "hide-xs", text: "Angelegt" }),
                h("th", { class: "hide-xs", text: "Zuletzt benutzt" }), h("th", { text: "" }))),
              h("tbody", {}, ...data.tokens.map((token) => h("tr", {},
                h("td", {}, h("div", { text: token.name }),
                  h("div", { class: "tiny faint mono", text: token.id })),
                h("td", {}, h("span", { class: "badge tiny plain", text: token.role })),
                h("td", { class: "nowrap hide-xs", text: AW.fmtTime(token.created_at) }),
                h("td", { class: "nowrap hide-xs", text: token.last_used ? AW.fmtTime(token.last_used) : "noch nie" }),
                h("td", {}, h("div", { class: "btn-row end" },
                  h("button", { class: "small ghost", onclick: async () => {
                    const ok = await confirmDialog({ title: "Token löschen?",
                      body: token.name === "cron-client"
                        ? "Das ist das Token des Cron-Clients. Ohne gültiges Token melden " +
                          "geplante Läufe den Dienst nicht mehr an und laufen im Notbetrieb. " +
                          "Beim nächsten Start des Dienstes wird ein neues Token angelegt."
                        : token.name + " verliert sofort den Zugang.",
                      confirmLabel: "Löschen", danger: true });
                    if (!ok) return;
                    try {
                      await API.del("/api/tokens/" + encodeURIComponent(token.id));
                      toast("Token gelöscht", "ok");
                      renderPanel();
                    } catch (err) { fail(err, "Löschen fehlgeschlagen"); }
                  } }, icon("trash", 13)))))))))));
      }

      function createToken() {
        const name = h("input", { type: "text", placeholder: "z. B. Monitoring" });
        const role = h("select", {}, ...(AW.state.roles || []).map((item) =>
          h("option", { value: item.name, text: item.name, selected: item.name === "operator" })));
        modal({
          title: "API-Token anlegen", width: "520px",
          body: h("div", {},
            h("div", { class: "field" }, h("label", { text: "Name" }), name),
            h("div", { class: "field" }, h("label", { text: "Rolle" }), role,
              h("div", { class: "hint", text: "So wenig wie möglich – operator genügt zum Starten." }))),
          buttons: [
            { label: "Abbrechen", onClick: (close) => close() },
            { label: "Anlegen", class: "primary", onClick: async (close) => {
              try {
                const result = await API.post("/api/tokens", { name: name.value.trim(), role: role.value });
                close();
                modal({ title: "Token angelegt", width: "620px",
                  body: h("div", {},
                    h("div", { class: "notice warn", text: result.note }),
                    h("pre", { class: "diff", text: result.token }),
                    h("div", { class: "btn-row mt" },
                      h("button", { onclick: () => AW.copyText(result.token) }, "Kopieren"))),
                  buttons: [{ label: "Fertig", class: "primary", onClick: (c) => { c(); renderPanel(); } }] });
              } catch (err) { fail(err, "Anlegen fehlgeschlagen"); }
            } },
          ],
        });
      }

      /* -- Protokoll ---------------------------------------------------- */

      async function renderAudit() {
        const search = h("input", { type: "search", placeholder: "Aktion filtern, z. B. lauf. oder datei." });
        const userFilter = h("input", { type: "text", placeholder: "Benutzer" });
        const tbody = h("tbody");

        async function load() {
          AW.mount(tbody, h("tr", {}, h("td", { colspan: "5" }, AW.spinner())));
          try {
            const query = "/api/audit?limit=400" +
              (search.value.trim() ? "&action=" + encodeURIComponent(search.value.trim()) : "") +
              (userFilter.value.trim() ? "&user=" + encodeURIComponent(userFilter.value.trim()) : "");
            const data = await API.get(query);
            AW.clear(tbody);
            if (!data.entries.length) {
              tbody.appendChild(h("tr", {}, h("td", { colspan: "5" },
                AW.emptyState("Keine Einträge."))));
              return;
            }
            for (const entry of data.entries) {
              tbody.appendChild(h("tr", {},
                h("td", { class: "nowrap small", text: AW.fmtTime(entry.ts) }),
                h("td", { text: entry.user }),
                h("td", {}, h("span", { class: "mono small", text: entry.action }),
                  entry.ok ? null : h("span", { class: "badge failed tiny", text: "abgewiesen" })),
                h("td", { class: "mono small wrap-any", text: entry.target || "" }),
                h("td", { class: "tiny faint wrap-any",
                  text: entry.detail ? JSON.stringify(entry.detail) : "" })));
            }
          } catch (err) {
            AW.mount(tbody, h("tr", {}, h("td", { colspan: "5" },
              h("div", { class: "notice error", text: err.detail || String(err) }))));
          }
        }
        search.addEventListener("input", AW.debounce(load, 350));
        userFilter.addEventListener("input", AW.debounce(load, 350));

        AW.mount(panel, h("div", { class: "card pad-0" },
          h("div", { class: "card-head" },
            h("div", { style: { flex: "1", minWidth: "180px" } }, search),
            h("div", { style: { width: "150px" } }, userFilter),
            h("button", { class: "small", onclick: load }, icon("refresh", 13))),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {},
              h("th", { text: "Zeit" }), h("th", { text: "Benutzer" }), h("th", { text: "Aktion" }),
              h("th", { text: "Betroffen" }), h("th", { text: "Details" }))),
            tbody))));
        await load();
      }

      /* -- Eigener Zugang ----------------------------------------------- */

      async function renderAccount() {
        const current = h("input", { type: "password", autocomplete: "current-password" });
        const next = h("input", { type: "password", autocomplete: "new-password" });
        const repeat = h("input", { type: "password", autocomplete: "new-password" });
        const user = AW.state.user;
        AW.mount(panel,
          h("div", { class: "grid two" },
            h("div", { class: "card" },
              h("h2", { class: "mb", text: "Angemeldet als" }),
              h("dl", { class: "kv" },
                h("dt", { text: "Benutzer" }), h("dd", { text: user.name }),
                h("dt", { text: "Rolle" }), h("dd", { text: user.role })),
              h("h3", { class: "mt-lg mb", text: "Berechtigungen" }),
              h("div", { class: "host-pills" },
                ...Object.keys(user.capabilities).map((key) =>
                  h("span", { class: "pill", style: { opacity: user.capabilities[key] ? "1" : ".4" } },
                    icon(user.capabilities[key] ? "check" : "x", 11), key)))),
            h("div", { class: "card" },
              h("h2", { class: "mb", text: "Passwort ändern" }),
              h("div", { class: "field" }, h("label", { text: "Aktuelles Passwort" }), current),
              h("div", { class: "field" }, h("label", { text: "Neues Passwort" }), next,
                h("div", { class: "hint", text: "Mindestens 10 Zeichen." })),
              h("div", { class: "field" }, h("label", { text: "Neues Passwort wiederholen" }), repeat),
              h("button", { class: "primary", onclick: async (event) => {
                if (next.value !== repeat.value) { toast("Die Passwörter stimmen nicht überein", "warn"); return; }
                if (next.value.length < 10) { toast("Mindestens 10 Zeichen", "warn"); return; }
                event.target.disabled = true;
                try {
                  const result = await API.post("/api/password",
                    { current_password: current.value, new_password: next.value });
                  toast(result.note || "Passwort geändert", "ok", 8000);
                  setTimeout(() => AW.boot(), 1200);
                } catch (err) { event.target.disabled = false; fail(err, "Änderung fehlgeschlagen"); }
              } }, icon("save"), "Passwort ändern"))));
      }

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Verwaltung" }),
            h("div", { class: "sub", text: "Zustand, Zugänge und Protokoll." }))),
        tabBar, panel);

      renderTabs();
      await renderPanel();
      return () => {};
    },
  };
})(window.AW);

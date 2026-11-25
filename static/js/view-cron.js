/* Zeitplan: Ansible-Jobs in der crontab von root verwalten. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, API, fail } = AW;

  AW.views.cron = {
    label: "Zeitplan (Cron)", title: "Zeitplan", cap: "cron.view",

    async mount(container) {
      let data = null;
      let playbooks = [];
      const canEdit = AW.can("cron.write");

      const jobsHost = h("div", {});
      const unmanagedHost = h("div", {});
      const infoHost = h("div", {});

      /* -- Formular ----------------------------------------------------- */

      function jobForm(job) {
        const isNew = !job;
        job = job || { name: "", schedule: "0 3 * * *", enabled: true, playbook: "",
                       limit: "", tags: [], skip_tags: [], check: false, diff: false,
                       extra_vars: "", forks: null, verbosity: 0 };

        const name = h("input", { type: "text", value: job.name, placeholder: "z. B. Nächtlicher Deploy" });
        const schedule = h("input", { type: "text", value: job.schedule, class: "mono" });
        const scheduleInfo = h("div", { class: "hint" });
        const playbook = h("select", {},
          h("option", { value: "", text: "– bitte wählen –" }),
          ...playbooks.map((item) => h("option", { value: item.path, text: item.path,
                                                   selected: item.path === job.playbook })));
        const limit = h("input", { type: "text", value: job.limit, class: "mono",
          placeholder: "leer = alle passenden Hosts" });
        const limitInfo = h("div", { class: "hint" });
        const tags = h("input", { type: "text", value: (job.tags || []).join(","),
                                  placeholder: "mit Komma trennen" });
        const skipTags = h("input", { type: "text", value: (job.skip_tags || []).join(",") });
        const check = h("input", { type: "checkbox", checked: !!job.check });
        const diff = h("input", { type: "checkbox", checked: !!job.diff });
        const enabled = h("input", { type: "checkbox", checked: job.enabled !== false });
        const forks = h("input", { type: "number", min: "1", max: "500", value: job.forks || "" });
        const verbosity = h("select", {}, ...[0, 1, 2, 3, 4].map((level) =>
          h("option", { value: String(level), text: level ? "-" + "v".repeat(level) : "normal",
                        selected: Number(job.verbosity || 0) === level })));
        const extraVars = AW.miniEditor({ value: job.extra_vars || "", mode: "yaml" });

        const checkSchedule = AW.debounce(async () => {
          try {
            const result = await API.post("/api/cron/validate", { schedule: schedule.value });
            if (!result.ok) {
              AW.mount(scheduleInfo, h("span", { class: "c-failed", text: result.error }));
              return;
            }
            AW.mount(scheduleInfo,
              h("span", { class: "c-ok", text: result.description }),
              result.next_runs.length
                ? h("div", { class: "faint tiny mono",
                    text: "nächste Läufe: " + result.next_runs.slice(0, 3).join("  ·  ") })
                : h("div", { class: "faint tiny", text: "läuft nur beim Systemstart" }));
          } catch (err) {
            AW.mount(scheduleInfo, h("span", { class: "faint", text: "Prüfung nicht möglich" }));
          }
        }, 300);
        schedule.addEventListener("input", checkSchedule);
        checkSchedule();

        const checkLimit = AW.debounce(async () => {
          if (!limit.value.trim()) {
            AW.mount(limitInfo, h("span", { class: "faint", text: "Kein --limit: alle passenden Hosts." }));
            return;
          }
          try {
            const result = await API.get("/api/inventory/resolve?limit=" + encodeURIComponent(limit.value.trim()));
            if (!result.ok) {
              AW.mount(limitInfo, h("span", { class: "c-failed", text: result.error }));
            } else if (!result.hosts.length) {
              AW.mount(limitInfo, h("span", { class: "c-failed", text: "Trifft keinen Host." }));
            } else {
              AW.mount(limitInfo, h("span", { class: "c-ok",
                text: result.hosts.length + " Host(s): " + result.hosts.slice(0, 8).join(", ") +
                      (result.hosts.length > 8 ? " …" : "") }));
            }
          } catch (err) { AW.clear(limitInfo); }
        }, 350);
        limit.addEventListener("input", checkLimit);
        checkLimit();

        const presets = h("div", { class: "btn-row", style: { marginBottom: "8px" } },
          ...[["0 3 * * *", "täglich 3:00"], ["30 2 * * 1-5", "Mo–Fr 2:30"],
              ["0 4 * * 0", "sonntags 4:00"], ["*/30 * * * *", "alle 30 min"],
              ["@reboot", "beim Start"]]
            .map(([value, label]) => h("button", { class: "small ghost", onclick: () => {
              schedule.value = value; checkSchedule();
            } }, label)));

        const body = h("div", {},
          h("div", { class: "fields-row" },
            h("div", { class: "field" }, h("label", { text: "Bezeichnung" }), name,
              h("div", { class: "hint", text: "Erscheint in der Liste und in der Lauf-Historie." })),
            h("div", { class: "field" }, h("label", { text: "Playbook" }), playbook)),
          h("div", { class: "field" },
            h("label", { text: "Zeitplan" }), presets, schedule, scheduleInfo),
          h("div", { class: "field" },
            h("label", { text: "Ziel (--limit)" }), limit, limitInfo),
          h("div", { class: "fields-row" },
            h("div", { class: "field" }, h("label", { text: "Nur diese Tags" }), tags),
            h("div", { class: "field" }, h("label", { text: "Tags überspringen" }), skipTags)),
          h("div", { class: "fields-row" },
            h("div", { class: "field" }, h("label", { text: "Parallele Prozesse" }), forks),
            h("div", { class: "field" }, h("label", { text: "Ausführlichkeit" }), verbosity)),
          h("div", { class: "field" },
            h("label", { class: "check" }, enabled, h("span", { text: "Job ist aktiv" })),
            h("label", { class: "check" }, check,
              h("span", {}, h("span", { text: "Probelauf (--check)" }),
                h("span", { class: "lbl-sub", text: "Ändert nichts, meldet nur." }))),
            h("label", { class: "check" }, diff, h("span", { text: "Änderungen anzeigen (--diff)" }))),
          h("div", { class: "field" },
            h("label", { text: "Zusatzvariablen (YAML)" }), extraVars.node,
            h("div", { class: "hint", text:
              "Werden in eine Datei neben der Konfiguration geschrieben und mit " +
              "--extra-vars übergeben." })));

        const dialog = modal({
          title: isNew ? "Neuen Cron-Job anlegen" : "Cron-Job bearbeiten",
          width: "720px", body,
          buttons: [
            { label: "Abbrechen", onClick: (close) => close() },
            { label: isNew ? "Anlegen" : "Speichern", class: "primary", onClick: async (close) => {
              const payload = {
                name: name.value.trim(), schedule: schedule.value.trim(),
                enabled: enabled.checked, playbook: playbook.value,
                limit: limit.value.trim(),
                tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean),
                skip_tags: skipTags.value.split(",").map((t) => t.trim()).filter(Boolean),
                check: check.checked, diff: diff.checked,
                extra_vars: extraVars.value.trim(),
                forks: forks.value ? parseInt(forks.value, 10) : null,
                verbosity: parseInt(verbosity.value, 10) || 0,
              };
              if (!payload.playbook) { toast("Bitte ein Playbook wählen", "warn"); return; }
              try {
                if (isNew) await API.post("/api/cron", payload);
                else await API.put("/api/cron/" + encodeURIComponent(job.id), payload);
                toast(isNew ? "Cron-Job angelegt" : "Cron-Job gespeichert", "ok");
                close();
                load();
              } catch (err) { fail(err, "Speichern fehlgeschlagen"); }
            } },
          ],
        });
        extraVars.attach();
        setTimeout(() => extraVars.refresh(), 60);
        return dialog;
      }

      /* -- Liste -------------------------------------------------------- */

      function renderJobs() {
        AW.clear(jobsHost);
        if (data.error) {
          jobsHost.appendChild(h("div", { class: "notice error" },
            h("strong", { text: "Die crontab ist nicht lesbar" }),
            h("div", { class: "small mt", text: data.error }),
            h("div", { class: "small mt faint", text:
              "Wenn im Container kein cron installiert ist, können hier keine Jobs " +
              "verwaltet werden." })));
          return;
        }
        if (!data.jobs.length) {
          jobsHost.appendChild(h("div", { class: "card" },
            AW.emptyState("Es sind noch keine Jobs über diese Oberfläche eingerichtet.",
              canEdit ? h("div", { class: "mt" },
                h("button", { class: "primary", onclick: () => jobForm(null) },
                  icon("plus"), "Ersten Job anlegen")) : null)));
          return;
        }
        const tbody = h("tbody");
        for (const job of data.jobs) {
          const request = {
            playbook: job.playbook, limit: job.limit, tags: job.tags,
            skip_tags: job.skip_tags, check: job.check, diff: job.diff,
            extra_vars: job.extra_vars, forks: job.forks, verbosity: job.verbosity,
            label: "Cron: " + job.name,
          };
          tbody.appendChild(h("tr", {},
            h("td", {}, canEdit
              ? h("label", { class: "switch", title: job.enabled ? "aktiv" : "ausgesetzt" },
                  h("input", { type: "checkbox", checked: job.enabled, onchange: async (event) => {
                    try {
                      await API.post("/api/cron/" + encodeURIComponent(job.id) + "/enabled",
                                     { enabled: event.target.checked });
                      toast(event.target.checked ? "Job aktiviert" : "Job ausgesetzt", "ok", 2500);
                      load();
                    } catch (err) { fail(err, "Umschalten fehlgeschlagen"); event.target.checked = job.enabled; }
                  } }), h("span", {}))
              : h("span", { class: "badge tiny " + (job.enabled ? "ok" : "queued"),
                            text: job.enabled ? "aktiv" : "aus" })),
            h("td", {},
              h("div", { text: job.name }),
              h("div", { class: "tiny faint mono ellipsis", style: { maxWidth: "260px" },
                text: [job.playbook, job.limit ? "--limit " + job.limit : null,
                       (job.tags || []).length ? "--tags " + job.tags.join(",") : null,
                       job.check ? "--check" : null, job.diff ? "--diff" : null]
                       .filter(Boolean).join("  ") })),
            h("td", { class: "nowrap" },
              h("div", { class: "mono small" }, job.schedule,
                job.valid ? null : h("span", { class: "badge failed tiny", text: "ungültig" })),
              h("div", { class: "tiny faint", text: job.valid ? job.description : job.schedule_error })),
            h("td", { class: "hide-xs" },
              job.enabled && (job.next_runs || []).length
                ? h("div", { class: "tiny mono" },
                    ...job.next_runs.slice(0, 2).map((value) => h("div", { text: value })))
                : h("span", { class: "tiny faint", text: job.enabled ? "–" : "ausgesetzt" })),
            h("td", {}, h("div", { class: "btn-row end" },
              AW.can("runs.start") ? h("button", { class: "small", title: "Jetzt sofort ausführen",
                onclick: async () => {
                  const ok = await confirmDialog({
                    title: "Jetzt ausführen?",
                    body: job.name + "\n" + job.playbook +
                          (job.limit ? "\nZiel: " + job.limit : "\nZiel: alle passenden Hosts"),
                    confirmLabel: "Jetzt starten",
                  });
                  if (!ok) return;
                  try {
                    const result = await API.post("/api/runs", request);
                    AW.go("run", { segments: [result.id] });
                  } catch (err) { fail(err, "Start fehlgeschlagen"); }
                } }, icon("play", 13), h("span", { class: "hide-xs", text: "starten" })) : null,
              canEdit ? h("button", { class: "small ghost", title: "Bearbeiten",
                onclick: () => jobForm(job) }, icon("edit", 13)) : null,
              canEdit ? h("button", { class: "small ghost", title: "Löschen",
                onclick: async () => {
                  const ok = await confirmDialog({
                    title: "Cron-Job löschen?", body: job.name + "\n" + job.schedule + "  " + job.playbook,
                    confirmLabel: "Löschen", danger: true });
                  if (!ok) return;
                  try {
                    await API.del("/api/cron/" + encodeURIComponent(job.id));
                    toast("Cron-Job gelöscht", "ok");
                    load();
                  } catch (err) { fail(err, "Löschen fehlgeschlagen"); }
                } }, icon("trash", 13)) : null))));
        }
        jobsHost.appendChild(h("div", { class: "card pad-0" },
          h("div", { class: "card-head" }, icon("clock", 14),
            h("h2", { text: "Geplante Ansible-Jobs" }),
            h("div", { class: "grow" }),
            h("span", { class: "faint small", text: data.jobs.length + " Jobs" })),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {},
              h("th", { text: "Aktiv" }), h("th", { text: "Job" }), h("th", { text: "Zeitplan" }),
              h("th", { class: "hide-xs", text: "Nächste Läufe" }), h("th", { text: "" }))),
            tbody))));
      }

      function renderUnmanaged() {
        AW.clear(unmanagedHost);
        const entries = (data.unmanaged || []).filter((item) => item.kind === "job");
        const settings = (data.unmanaged || []).filter((item) => item.kind === "setting");
        if (!entries.length && !settings.length) return;
        const ansibleOnes = entries.filter((item) => item.ansible);
        unmanagedHost.appendChild(h("div", { class: "card", style: { marginTop: "16px" } },
          h("h2", { class: "mb" }, icon("list", 14), " Übrige crontab-Einträge"),
          h("div", { class: "small faint mb", text:
            "Diese Zeilen stehen außerhalb des verwalteten Blocks und werden nicht angefasst." +
            (ansibleOnes.length ? " Ansible-Zeilen lassen sich übernehmen – danach laufen sie " +
             "über das Webinterface und erscheinen mit Protokoll in der Historie." : "") }),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {},
              h("th", { text: "Zeitplan" }), h("th", { text: "Befehl" }), h("th", { text: "" }))),
            h("tbody", {},
              ...entries.map((item) => h("tr", {},
                h("td", { class: "mono small nowrap" }, item.schedule,
                  h("div", { class: "tiny faint", text: item.description })),
                h("td", { class: "mono tiny wrap-any", text: item.command }),
                h("td", {}, h("div", { class: "btn-row end" },
                  item.ansible ? h("span", { class: "badge tiny plain", text: "Ansible" }) : null,
                  item.importable && canEdit
                    ? h("button", { class: "small", onclick: async () => {
                        const ok = await confirmDialog({
                          title: "Zeile übernehmen?",
                          body: "Erkanntes Playbook: " + item.detected_playbook +
                                "\n\nDie Zeile wird aus ihrer jetzigen Position entfernt und als " +
                                "verwalteter Job neu geschrieben. Zeitplan und Optionen bleiben gleich, " +
                                "der Lauf wird künftig protokolliert.",
                          confirmLabel: "Übernehmen" });
                        if (!ok) return;
                        try {
                          await API.post("/api/cron/import", { raw: item.raw });
                          toast("Zeile übernommen", "ok");
                          load();
                        } catch (err) { fail(err, "Übernehmen fehlgeschlagen"); }
                      } }, icon("download", 13), "übernehmen")
                    : (item.ansible ? h("span", { class: "tiny faint",
                        text: "kein Playbook erkennbar" }) : null)))))),
            )),
          settings.length ? h("div", { class: "mt small faint" },
            "Außerdem gesetzt: " + settings.map((item) => item.raw).join(", ")) : null));
      }

      function renderInfo() {
        AW.mount(infoHost, h("div", { class: "card" },
          h("div", { class: "small" },
            h("strong", { text: "So laufen geplante Jobs: " }),
            "Die crontab von root ruft den Client ",
            h("code", { text: data.cli || "" }),
            " auf. Der meldet den Lauf beim Dienst an – dadurch erscheint jeder geplante Lauf " +
            "mit Host-Status, Schritten und Protokoll in der Historie. Ist der Dienst " +
            "gerade nicht erreichbar, ruft der Client ansible-playbook direkt auf, " +
            "damit kein Termin ausfällt."),
          h("div", { class: "tiny faint mt mono",
            text: "Ausgabe der Cron-Mails zusätzlich in: " + (data.log_file || "") }),
          canEdit ? h("div", { class: "btn-row mt" },
            h("button", { class: "small ghost", onclick: showRaw }, icon("file", 13),
              "crontab im Rohtext"),
            h("button", { class: "small ghost", onclick: showBackups }, icon("history", 13),
              "Sicherungen")) : null));
      }

      async function showRaw() {
        const body = h("div", {}, AW.spinner());
        const dialog = modal({ title: "crontab von root (Rohtext)", width: "900px", body,
          buttons: [{ label: "Schließen", onClick: (close) => close() }] });
        try {
          const result = await API.get("/api/cron/raw");
          const host = h("div", {});
          const cm = AW.makeCodeMirror(host, { value: result.content, mode: "shell" });
          AW.mount(body,
            h("div", { class: "notice warn small" },
              "Hier wird die komplette crontab ersetzt. Vor dem Schreiben wird sie geprüft " +
              "und eine Sicherungskopie abgelegt."),
            host,
            h("div", { class: "btn-row mt end" },
              h("button", { class: "primary", onclick: async (event) => {
                event.target.disabled = true;
                try {
                  const saved = await API.put("/api/cron/raw", { content: cm.getValue() });
                  toast("crontab gespeichert (Sicherung: " + (saved.backup || "–") + ")", "ok");
                  dialog.close();
                  load();
                } catch (err) { event.target.disabled = false; fail(err, "Speichern fehlgeschlagen"); }
              } }, icon("save"), "crontab speichern")));
          setTimeout(() => cm.refresh(), 60);
        } catch (err) {
          AW.mount(body, h("div", { class: "notice error", text: err.detail || String(err) }));
        }
      }

      async function showBackups() {
        const body = h("div", {}, AW.spinner());
        modal({ title: "Sicherungen der crontab", width: "680px", body });
        try {
          const result = await API.get("/api/cron/backups");
          if (!result.backups.length) {
            AW.mount(body, AW.emptyState("Noch keine Sicherungen vorhanden."));
            return;
          }
          const view = h("pre", { class: "diff", text: "Eintrag links wählen …" });
          const list = h("div", { class: "list list-scroll" });
          for (const backup of result.backups) {
            list.appendChild(h("button", { class: "list-item", onclick: async () => {
              AW.mount(view, "lädt …");
              try {
                const content = await API.get("/api/cron/backups/" + encodeURIComponent(backup.name));
                AW.mount(view, h("span", { text: content.content }));
              } catch (err) { AW.mount(view, h("span", { text: String(err.detail || err) })); }
            } },
              h("div", { class: "grow" },
                h("div", { class: "t", text: AW.fmtTime(backup.mtime) }),
                h("div", { class: "s", text: backup.name + " · " + AW.fmtBytes(backup.size) }))));
          }
          AW.mount(body, h("div", { class: "split" }, h("div", { class: "card pad-0" }, list), view));
        } catch (err) {
          AW.mount(body, h("div", { class: "notice error", text: err.detail || String(err) }));
        }
      }

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Zeitplan" }),
            h("div", { class: "sub", text:
              "Geplante Playbook-Läufe – verwaltet in der crontab von root." })),
          h("div", { class: "btn-row" },
            h("button", { onclick: () => load() }, icon("refresh"), "Aktualisieren"),
            canEdit ? h("button", { class: "primary", onclick: () => jobForm(null) },
              icon("plus"), "Job anlegen") : null)),
        infoHost, h("div", { style: { height: "16px" } }), jobsHost, unmanagedHost);

      async function load() {
        try {
          const [cronData, pbData] = await Promise.all([
            API.get("/api/cron"),
            API.get("/api/playbooks").catch(() => ({ playbooks: [] })),
          ]);
          data = cronData;
          playbooks = pbData.playbooks || [];
          renderInfo();
          renderJobs();
          renderUnmanaged();
        } catch (err) { fail(err, "Zeitplan nicht abrufbar"); }
      }

      await load();
      return () => {};
    },
  };
})(window.AW);

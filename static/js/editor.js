/* Editor-Komponente: laden, pruefen, speichern, Historie, Sicherungen. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, API, fail } = AW;

  const CM_MODES = {
    yaml: "yaml", jinja2: "jinja2", shell: "shell", python: "python",
    properties: "properties", toml: "toml", dockerfile: "dockerfile",
    javascript: { name: "javascript", json: true },
  };

  function cmMode(name, path) {
    if (name && CM_MODES[name]) return CM_MODES[name];
    if (/\.(ya?ml)$/i.test(path || "")) return "yaml";
    return null;
  }

  function makeCodeMirror(host, options) {
    return CodeMirror(host, {
      value: options.value || "",
      mode: options.mode || null,
      theme: "material-darker",
      lineNumbers: true,
      lineWrapping: options.lineWrapping !== false,
      indentUnit: 2,
      tabSize: 2,
      indentWithTabs: false,
      smartIndent: true,
      matchBrackets: true,
      styleActiveLine: true,
      showTrailingSpace: true,
      readOnly: !!options.readOnly,
      foldGutter: true,
      gutters: ["CodeMirror-linenumbers", "CodeMirror-foldgutter"],
      extraKeys: options.extraKeys || {},
    });
  }

  /* -- Kleiner Editor fuer Formulare (z. B. Zusatzvariablen) ----------- */

  function miniEditor(options) {
    const host = h("div", { class: "cm-small" });
    let cm = null;
    const api = {
      node: host,
      attach() {
        if (cm) return cm;
        cm = makeCodeMirror(host, { value: options.value || "", mode: options.mode || "yaml" });
        if (options.onChange) cm.on("change", () => options.onChange(cm.getValue()));
        return cm;
      },
      get value() { return cm ? cm.getValue() : (options.value || ""); },
      set value(text) { if (cm) cm.setValue(text || ""); else options.value = text; },
      refresh() { if (cm) cm.refresh(); },
    };
    return api;
  }

  /* -- Vollwertiger Dateieditor ---------------------------------------- */

  class FileEditor {
    constructor(options) {
      this.options = options || {};
      this.path = this.options.path || "";
      this.data = null;
      this.cm = null;
      this.clean = "";
      this.node = h("div", { class: "editor-wrap" });
      this.cmHost = h("div");
      this.statusNode = h("span", { class: "small faint" });
      this.pathNode = h("div", { class: "path" });
      this.panel = h("div");
      this.buttons = {};
      this.build();
    }

    build() {
      const self = this;
      const mk = (key, label, iconName, cls, handler, title) => {
        const button = h("button", { class: cls || "", title: title || label,
                                     onclick: handler }, iconName ? icon(iconName) : null,
                         h("span", { class: "hide-xs", text: label }));
        self.buttons[key] = button;
        return button;
      };
      this.bar = h("div", { class: "editor-bar" },
        this.pathNode,
        this.statusNode,
        mk("save", "Speichern", "save", "primary", () => this.save(), "Speichern (Strg+S)"),
        mk("check", "Prüfen", "check", "", () => this.validate(), "Nur prüfen, nicht speichern"),
        mk("reload", "Neu laden", "refresh", "", () => this.reload()),
        mk("history", "Historie", "history", "", () => this.showHistory()),
        mk("backups", "Sicherungen", "clock", "", () => this.showBackups()),
        this.options.onClose ? h("button", { class: "ghost icon", title: "Schließen",
          onclick: () => this.options.onClose() }, icon("x")) : null);
      AW.mount(this.node, this.bar, this.cmHost, this.panel);
    }

    async load(path) {
      if (path) this.path = path;
      this.setStatus("lädt …");
      try {
        this.data = await API.get("/api/files/content?path=" + encodeURIComponent(this.path));
      } catch (err) {
        this.setStatus("");
        AW.mount(this.panel, h("div", { class: "notice error mt" },
          h("strong", { text: "Datei kann nicht geöffnet werden" }),
          h("div", { class: "small mt", text: err.detail || String(err) })));
        AW.mount(this.pathNode, h("span", { text: this.path }));
        if (this.cm) { this.cm.setValue(""); this.cm.setOption("readOnly", true); }
        return false;
      }
      this.clean = this.data.content;
      const mode = cmMode(this.data.editor_mode, this.path);
      if (!this.cm) {
        this.cm = makeCodeMirror(this.cmHost, {
          value: this.data.content, mode,
          readOnly: !this.canWrite(),
          extraKeys: {
            "Ctrl-S": () => this.save(), "Cmd-S": () => this.save(),
            "Ctrl-/": "toggleComment", "Cmd-/": "toggleComment",
          },
        });
        this.cm.on("change", () => this.onChange());
      } else {
        this.cm.setValue(this.data.content);
        this.cm.setOption("mode", mode);
        this.cm.setOption("readOnly", !this.canWrite());
        this.cm.clearHistory();
      }
      AW.clear(this.panel);
      this.renderPath();
      this.setStatus("");
      this.cm.refresh();
      return true;
    }

    canWrite() { return !!(AW.state.user && AW.state.user.capabilities["files.write"]); }
    isDirty() { return !!this.cm && this.cm.getValue() !== this.clean; }

    onChange() {
      this.renderPath();
      this.node.classList.toggle("cm-dirty", this.isDirty());
      if (this.options.onDirty) this.options.onDirty(this.isDirty());
    }

    renderPath() {
      const kindLabels = { playbook: "Playbook", inventory: "Inventory", yaml: "YAML",
                           json: "JSON", ini: "INI/Konfiguration", none: "Textdatei" };
      AW.mount(this.pathNode,
        h("span", { text: this.path }),
        this.isDirty() ? h("span", { class: "dirty", text: " ● ungespeichert" }) : null,
        this.data ? h("span", { class: "faint",
          text: "  " + (kindLabels[this.data.kind] || this.data.kind) +
                " · " + AW.fmtBytes(this.data.size) } ) : null);
    }

    setStatus(text) { this.statusNode.textContent = text || ""; }

    setBusy(busy) {
      for (const key of Object.keys(this.buttons)) {
        if (this.buttons[key]) this.buttons[key].disabled = busy;
      }
      if (!busy && !this.canWrite() && this.buttons.save) this.buttons.save.disabled = true;
    }

    renderValidation(result, extra) {
      if (!result) { AW.clear(this.panel); return; }
      const kind = result.ok ? (result.warnings && result.warnings.length ? "warn" : "ok") : "error";
      AW.mount(this.panel, h("div", { class: "notice " + kind + " mt" },
        h("strong", { text: result.ok ? "Prüfung bestanden" : "Prüfung fehlgeschlagen – nicht gespeichert" }),
        result.messages && result.messages.length
          ? h("ul", {}, ...result.messages.map((m) => h("li", { class: "mono wrap-any", text: m })))
          : null,
        result.warnings && result.warnings.length
          ? h("div", { class: "mt" }, h("div", { class: "small", text: "Hinweise:" }),
              h("ul", {}, ...result.warnings.map((m) => h("li", { class: "mono wrap-any small", text: m }))))
          : null,
        extra || null));
    }

    async validate() {
      if (!this.cm) return null;
      this.setBusy(true);
      this.setStatus("prüft …");
      try {
        const result = await API.post("/api/files/validate",
          { path: this.path, content: this.cm.getValue() });
        this.renderValidation(result);
        if (result.ok && !(result.warnings || []).length) toast("Inhalt ist in Ordnung", "ok", 2500);
        return result;
      } catch (err) {
        fail(err, "Prüfung nicht möglich");
        return null;
      } finally {
        this.setBusy(false);
        this.setStatus("");
      }
    }

    async save(options) {
      options = options || {};
      if (!this.cm || !this.canWrite()) return false;
      const content = this.cm.getValue();
      this.setBusy(true);
      this.setStatus("speichert …");
      try {
        const message = this.options.commitMessage ? this.options.commitMessage() : "";
        const result = await API.put("/api/files/content", {
          path: this.path, content,
          sha256: options.force ? null : (this.data ? this.data.sha256 : null),
          message: message || "",
        });
        if (!result.saved) {
          this.renderValidation(result.validation, this.renderForceButton(content));
          toast("Nicht gespeichert – die Prüfung hat Fehler gefunden", "error");
          return false;
        }
        this.clean = content;
        this.data.sha256 = result.sha256;
        this.data.size = result.size;
        this.onChange();
        const parts = [];
        if (result.git && result.git.committed) parts.push("Commit " + result.git.commit);
        else if (result.git && result.git.reason) parts.push(result.git.reason);
        if (result.backup) parts.push("Sicherung " + result.backup);
        toast("Gespeichert" + (parts.length ? " – " + parts.join(", ") : ""), "ok");
        if ((result.validation.warnings || []).length) this.renderValidation(result.validation);
        else AW.clear(this.panel);
        if (this.options.onSaved) this.options.onSaved(result);
        return true;
      } catch (err) {
        if (err.status === 409) {
          await this.handleConflict();
          return false;
        }
        fail(err, "Speichern fehlgeschlagen");
        return false;
      } finally {
        this.setBusy(false);
        this.setStatus("");
      }
    }

    renderForceButton(content) {
      if (!(AW.state.user && AW.state.user.capabilities["users.manage"])) return null;
      return h("div", { class: "mt" },
        h("button", { class: "small danger", onclick: async () => {
          const ok = await confirmDialog({
            title: "Prüfung übergehen?",
            body: "Die Datei wird gespeichert, obwohl Ansible sie als fehlerhaft ansieht.\n" +
                  "Playbooks oder Inventory können danach nicht mehr laufen.",
            confirmLabel: "Trotzdem speichern", danger: true,
          });
          if (!ok) return;
          try {
            const result = await API.put("/api/files/content", {
              path: this.path, content, sha256: this.data ? this.data.sha256 : null,
              skip_validation: true, message: "Gespeichert ohne Prüfung",
            });
            if (result.saved) {
              this.clean = content; this.data.sha256 = result.sha256;
              this.onChange(); AW.clear(this.panel);
              toast("Ohne Prüfung gespeichert", "warn");
            }
          } catch (err) { fail(err, "Speichern fehlgeschlagen"); }
        } }, "Prüfung übergehen und trotzdem speichern"));
    }

    async handleConflict() {
      const mine = this.cm.getValue();
      let theirs = "";
      try {
        const fresh = await API.get("/api/files/content?path=" + encodeURIComponent(this.path));
        theirs = fresh.content;
        this.data.sha256 = fresh.sha256;
      } catch (err) { /* egal, dann ohne Vergleich */ }
      const choice = await new Promise((resolve) => {
        let answered = false;
        modal({
          title: "Die Datei wurde zwischenzeitlich geändert",
          width: "620px",
          body: h("div", {},
            h("p", { text: "Jemand anderes (oder ein anderer Vorgang) hat " + this.path +
                           " gespeichert, während du bearbeitet hast." }),
            theirs ? h("details", { class: "box" },
              h("summary", { text: "Fremde Fassung ansehen" }),
              h("div", {}, h("pre", { class: "diff", text: theirs.slice(0, 20000) }))) : null,
            h("p", { class: "small faint", text: "Deine Fassung bleibt im Editor, bis du dich entscheidest." })),
          buttons: [
            { label: "Nichts tun", onClick: (close) => { answered = true; close(); resolve("none"); } },
            { label: "Fremde Fassung laden", onClick: (close) => { answered = true; close(); resolve("theirs"); } },
            { label: "Meine Fassung erzwingen", class: "danger",
              onClick: (close) => { answered = true; close(); resolve("mine"); } },
          ],
          onClose: () => { if (!answered) resolve("none"); },
        });
      });
      if (choice === "theirs") {
        this.cm.setValue(theirs);
        this.clean = theirs;
        this.onChange();
        toast("Fremde Fassung geladen", "ok");
      } else if (choice === "mine") {
        this.cm.setValue(mine);
        await this.save({ force: true });
      }
    }

    async reload() {
      if (this.isDirty()) {
        const ok = await confirmDialog({
          title: "Änderungen verwerfen?",
          body: "Die ungespeicherten Änderungen an " + this.path + " gehen verloren.",
          confirmLabel: "Verwerfen und neu laden", danger: true,
        });
        if (!ok) return;
      }
      await this.load();
      toast("Neu geladen", "ok", 2000);
    }

    setContent(text, markDirty) {
      if (!this.cm) return;
      this.cm.setValue(text);
      if (!markDirty) this.clean = text;
      this.onChange();
    }

    /* -- Historie und Sicherungen -------------------------------------- */

    async showHistory() {
      const body = h("div", {}, AW.spinner());
      const dialog = modal({ title: "Git-Historie: " + this.path, width: "820px", body });
      try {
        const data = await API.get("/api/git/log?limit=40&path=" + encodeURIComponent(this.path));
        if (!data.commits.length) {
          AW.mount(body, AW.emptyState("Für diese Datei gibt es noch keine Commits."));
          return;
        }
        const diffBox = h("pre", { class: "diff", text: "Commit links auswählen …" });
        const list = h("div", { class: "list list-scroll" });
        for (const commit of data.commits) {
          const row = h("button", { class: "list-item", onclick: async () => {
            for (const other of list.children) other.classList.remove("on");
            row.classList.add("on");
            AW.mount(diffBox, "lädt …");
            try {
              const diff = await API.text("/api/git/diff?revision=" + encodeURIComponent(commit.hash) +
                                          "&path=" + encodeURIComponent(this.path));
              AW.mount(diffBox, AW.diffToFragment(diff || "(keine Änderung an dieser Datei)"));
            } catch (err) { AW.mount(diffBox, h("span", { text: String(err.detail || err) })); }
          } },
            h("div", { class: "grow" },
              h("div", { class: "t", text: commit.subject }),
              h("div", { class: "s", text: commit.short + " · " + commit.author + " · " +
                                           commit.date.replace("T", " ").slice(0, 19) })),
            h("button", { class: "small", title: "Diese Fassung in den Editor laden",
              onclick: async (event) => {
                event.stopPropagation();
                try {
                  const file = await API.get("/api/git/file?revision=" + encodeURIComponent(commit.hash) +
                                             "&path=" + encodeURIComponent(this.path));
                  this.setContent(file.content, true);
                  dialog.close();
                  toast("Fassung " + commit.short + " geladen – noch nicht gespeichert", "warn", 7000);
                } catch (err) { fail(err, "Fassung nicht ladbar"); }
              } }, "laden"));
          list.appendChild(row);
        }
        AW.mount(body, h("div", { class: "split" }, h("div", { class: "card pad-0" }, list), diffBox));
      } catch (err) {
        AW.mount(body, h("div", { class: "notice error", text: err.detail || String(err) }));
      }
    }

    async showBackups() {
      const body = h("div", {}, AW.spinner());
      const dialog = modal({ title: "Sicherungskopien: " + this.path, width: "640px", body });
      try {
        const fresh = await API.get("/api/files/content?path=" + encodeURIComponent(this.path));
        const backups = fresh.backups || [];
        if (!backups.length) {
          AW.mount(body, AW.emptyState("Für diese Datei gibt es noch keine Sicherungskopien.",
            h("div", { class: "small faint mt", text:
              "Sie entstehen automatisch bei jedem Speichern über die Oberfläche." })));
          return;
        }
        const list = h("div", { class: "list" });
        for (const backup of backups) {
          list.appendChild(h("div", { class: "list-item", style: { cursor: "default" } },
            h("div", { class: "grow" },
              h("div", { class: "t", text: AW.fmtTime(backup.mtime) }),
              h("div", { class: "s", text: backup.stamp + " · " + AW.fmtBytes(backup.size) })),
            h("button", { class: "small", onclick: async () => {
              try {
                const data = await API.get("/api/files/backup?path=" + encodeURIComponent(this.path) +
                                           "&stamp=" + encodeURIComponent(backup.stamp));
                this.setContent(data.content, true);
                dialog.close();
                toast("Sicherung geladen – noch nicht gespeichert", "warn", 7000);
              } catch (err) { fail(err, "Sicherung nicht ladbar"); }
            } }, "in Editor laden")));
        }
        AW.mount(body, h("div", { class: "card pad-0" }, list));
      } catch (err) {
        AW.mount(body, h("div", { class: "notice error", text: err.detail || String(err) }));
      }
    }
  }

  AW.FileEditor = FileEditor;
  AW.miniEditor = miniEditor;
  AW.makeCodeMirror = makeCodeMirror;
  AW.cmMode = cmMode;
})(window.AW);

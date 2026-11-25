/* Dateimanager fuer den Daten-/Konfigordner neben den Playbooks. */
"use strict";

(function (AW) {
  const { h, icon, toast, modal, confirmDialog, promptDialog, API, fail } = AW;

  function parentOf(path) {
    if (!path) return null;
    const parts = path.split("/");
    parts.pop();
    return parts.join("/");
  }

  AW.views.files = {
    label: "Dateien", title: "Dateien", cap: "files.read",

    async mount(container, route) {
      const params = route.params || {};
      const dataDir = AW.state.config.data_dir_rel || "";
      let path = params.path !== undefined ? params.path : dataDir;
      let listing = null;

      const breadcrumb = h("div", { class: "breadcrumb" });
      const tbody = h("tbody");
      const infoNode = h("span", { class: "faint small" });
      const dropzone = h("div", { class: "dropzone mt" },
        icon("upload"), " Dateien hierher ziehen oder ",
        h("button", { class: "small", onclick: () => fileInput.click() }, "auswählen"));
      const fileInput = h("input", { type: "file", multiple: true, style: { display: "none" },
        onchange: () => { if (fileInput.files.length) uploadFiles(fileInput.files); } });

      const canWrite = AW.can("files.write");
      const canUpload = AW.can("files.upload");
      const canDelete = AW.can("files.delete");

      function renderBreadcrumb() {
        AW.clear(breadcrumb);
        breadcrumb.appendChild(h("button", { title: "Ansible-Verzeichnis",
          onclick: () => navigate("") }, icon("folder", 13), " /"));
        let accumulated = "";
        for (const part of (path || "").split("/").filter(Boolean)) {
          accumulated = accumulated ? accumulated + "/" + part : part;
          const target = accumulated;
          breadcrumb.appendChild(h("span", { class: "sep", text: "/" }));
          breadcrumb.appendChild(h("button", { onclick: () => navigate(target), text: part }));
        }
        if (path === dataDir && dataDir) {
          breadcrumb.appendChild(h("span", { class: "badge tiny plain",
            text: "Daten-/Konfigordner" }));
        }
      }

      function navigate(target) {
        path = target;
        history.replaceState(null, "", AW.linkTo("files", { path }));
        load();
      }

      function render() {
        renderBreadcrumb();
        AW.clear(tbody);
        if (!listing) return;
        infoNode.textContent = listing.count + " Einträge · " + listing.absolute;
        const parent = listing.parent;
        if (parent !== null && parent !== undefined) {
          tbody.appendChild(h("tr", { class: "clickable", onclick: () => navigate(parent) },
            h("td", {}, h("span", { class: "file-icon dir" }, icon("folder", 15))),
            h("td", { colspan: "5" }, h("span", { class: "muted", text: ".. (eine Ebene höher)" }))));
        }
        if (!listing.entries.length) {
          tbody.appendChild(h("tr", {}, h("td", { colspan: "6" },
            AW.emptyState("Dieser Ordner ist leer."))));
          return;
        }
        for (const entry of listing.entries) {
          const isDir = entry.type === "dir";
          const nameCell = h("td", {},
            h("div", { class: "btn-row", style: { gap: "6px" } },
              h("span", { class: isDir ? "" : "mono",
                          style: { cursor: isDir || entry.editable ? "pointer" : "default" },
                          text: entry.name,
                          onclick: () => isDir ? navigate(entry.path) : (entry.editable ? edit(entry) : null) }),
              entry.symlink ? h("span", { class: "badge tiny plain",
                title: entry.symlink_target || "", text: "Verweis" }) : null,
              entry.outside ? h("span", { class: "badge tiny failed",
                title: "Zeigt aus dem Ansible-Verzeichnis heraus und ist gesperrt.",
                text: "außerhalb" }) : null,
              entry.broken ? h("span", { class: "badge tiny changed", text: "defekt" }) : null));
          const actions = h("div", { class: "btn-row end" });
          if (!isDir && entry.editable) {
            actions.appendChild(h("button", { class: "small", title: "Bearbeiten",
              onclick: () => edit(entry) }, icon("edit", 13),
              h("span", { class: "hide-xs", text: canWrite ? "bearbeiten" : "ansehen" })));
          }
          if (!isDir && !entry.outside) {
            actions.appendChild(h("a", { class: "btn small", title: "Herunterladen", download: "",
              href: "/api/files/download?path=" + encodeURIComponent(entry.path) }, icon("download", 13)));
          }
          if (isDir) {
            actions.appendChild(h("a", { class: "btn small", title: "Ordner als ZIP herunterladen",
              href: "/api/files/zip?path=" + encodeURIComponent(entry.path) }, icon("download", 13),
              h("span", { class: "hide-xs", text: "ZIP" })));
          }
          if (canWrite) {
            actions.appendChild(h("button", { class: "small ghost", title: "Umbenennen",
              onclick: () => rename(entry) }, "umben."));
          }
          if (canDelete) {
            actions.appendChild(h("button", { class: "small ghost", title: "Löschen",
              onclick: () => remove(entry) }, icon("trash", 13)));
          }
          tbody.appendChild(h("tr", { class: "file-row" },
            h("td", {}, h("span", { class: "file-icon" + (isDir ? " dir" : "") },
              icon(isDir ? "folder" : "file", 15))),
            nameCell,
            h("td", { class: "num nowrap hide-xs", text: isDir ? "–" : AW.fmtBytes(entry.size) }),
            h("td", { class: "nowrap hide-xs", title: AW.fmtTime(entry.mtime),
                      text: AW.fmtTimeShort(entry.mtime) }),
            h("td", { class: "mono tiny faint hide-xs", text: entry.mode }),
            h("td", {}, actions)));
        }
      }

      async function load() {
        try {
          listing = await API.get("/api/files?path=" + encodeURIComponent(path));
          render();
        } catch (err) {
          listing = null;
          AW.clear(tbody);
          tbody.appendChild(h("tr", {}, h("td", { colspan: "6" },
            h("div", { class: "notice error" },
              h("strong", { text: "Ordner nicht lesbar" }),
              h("div", { class: "small mt", text: err.detail || String(err) }),
              h("div", { class: "mt" }, h("button", { onclick: () => navigate(dataDir) },
                "Zurück zum Datenordner"))))));
          renderBreadcrumb();
        }
      }

      /* -- Aktionen ----------------------------------------------------- */

      function edit(entry) {
        const editor = new AW.FileEditor({
          path: entry.path,
          onSaved: () => load(),
        });
        const dialog = modal({ title: entry.path, width: "1100px", body: editor.node,
          onClose: () => load() });
        editor.options.onClose = () => dialog.close();
        editor.load(entry.path).then(() => {
          if (editor.cm) setTimeout(() => editor.cm.refresh(), 60);
        });
      }

      async function rename(entry) {
        const name = await promptDialog({ title: "Umbenennen", label: "Neuer Name",
                                          value: entry.name });
        if (!name || name === entry.name) return;
        try {
          const result = await API.post("/api/files/rename", { path: entry.path, name });
          toast("Umbenannt zu " + result.path +
                (result.git && result.git.committed ? " (Commit " + result.git.commit + ")" : ""), "ok");
          load();
        } catch (err) { fail(err, "Umbenennen fehlgeschlagen"); }
      }

      async function remove(entry) {
        const isDir = entry.type === "dir";
        const ok = await confirmDialog({
          title: isDir ? "Ordner löschen?" : "Datei löschen?",
          body: entry.path + (isDir
            ? "\n\nDer Ordner wird mit allem Inhalt gelöscht. Vorher wird ein ZIP-Archiv " +
              "als Sicherung abgelegt."
            : "\n\nVorher wird eine Sicherungskopie abgelegt."),
          confirmLabel: "Löschen", danger: true,
        });
        if (!ok) return;
        try {
          await API.del("/api/files?path=" + encodeURIComponent(entry.path) +
                        (isDir ? "&recursive=true" : ""));
          toast("Gelöscht: " + entry.path, "ok");
          load();
        } catch (err) { fail(err, "Löschen fehlgeschlagen"); }
      }

      async function createDir() {
        const name = await promptDialog({ title: "Neuen Ordner anlegen", label: "Name" });
        if (!name) return;
        try {
          const result = await API.post("/api/files/dir", { path, name });
          toast("Ordner angelegt: " + result.path, "ok");
          load();
        } catch (err) { fail(err, "Ordner konnte nicht angelegt werden"); }
      }

      async function uploadFiles(files, overwrite) {
        const limit = AW.state.config.max_upload_bytes || 0;
        const tooBig = Array.from(files).filter((file) => limit && file.size > limit);
        if (tooBig.length) {
          toast("Zu groß (Grenze " + AW.fmtBytes(limit) + "): " +
                tooBig.map((f) => f.name).join(", "), "error");
          return;
        }
        const form = new FormData();
        form.append("path", path);
        form.append("overwrite", overwrite ? "true" : "false");
        for (const file of files) form.append("files", file, file.name);
        const note = toast("Lade " + files.length + " Datei(en) hoch …", "", 0);
        try {
          const result = await API.upload("/api/files/upload", form);
          note.remove();
          if (result.uploaded.length) {
            toast(result.uploaded.length + " Datei(en) hochgeladen", "ok");
          }
          const conflicts = (result.errors || []).filter((item) =>
            /schon eine Datei/.test(item.error));
          for (const item of (result.errors || [])) {
            if (!conflicts.includes(item)) toast(item.name + ": " + item.error, "error");
          }
          if (conflicts.length && !overwrite) {
            const ok = await confirmDialog({
              title: "Vorhandene Dateien überschreiben?",
              body: "Diese Dateien gibt es dort schon:\n" +
                    conflicts.map((item) => "· " + item.name).join("\n") +
                    "\n\nVon den alten Fassungen wird vorher eine Sicherungskopie angelegt.",
              confirmLabel: "Überschreiben", danger: true,
            });
            if (ok) {
              const names = new Set(conflicts.map((item) => item.name));
              const again = Array.from(files).filter((file) => names.has(file.name));
              await uploadFiles(again, true);
            }
          }
          fileInput.value = "";
          load();
        } catch (err) {
          note.remove();
          fail(err, "Hochladen fehlgeschlagen");
        }
      }

      /* -- Ziehen und Ablegen ------------------------------------------- */

      const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
      for (const name of ["dragenter", "dragover"]) {
        dropzone.addEventListener(name, (event) => { stop(event); dropzone.classList.add("over"); });
      }
      for (const name of ["dragleave", "drop"]) {
        dropzone.addEventListener(name, (event) => { stop(event); dropzone.classList.remove("over"); });
      }
      dropzone.addEventListener("drop", (event) => {
        const files = event.dataTransfer && event.dataTransfer.files;
        if (files && files.length) uploadFiles(files);
      });

      AW.mount(container,
        h("div", { class: "page-head" },
          h("div", { class: "grow" },
            h("h1", { text: "Dateien" }),
            h("div", { class: "sub", text:
              "Konfigurationen und Daten neben den Playbooks – ansehen, bearbeiten, " +
              "herunter- und hochladen." })),
          h("div", { class: "btn-row" },
            h("button", { onclick: () => load() }, icon("refresh"), "Aktualisieren"),
            canWrite ? h("button", { onclick: createDir }, icon("plus"), "Neuer Ordner") : null,
            canUpload ? h("button", { class: "primary", onclick: () => fileInput.click() },
              icon("upload"), "Hochladen") : null)),
        h("div", { class: "card pad-0" },
          h("div", { class: "card-head" }, breadcrumb, h("div", { class: "grow" }),
            h("a", { class: "btn small", title: "Diesen Ordner als ZIP herunterladen",
              href: "/api/files/zip?path=" + encodeURIComponent(path) },
              icon("download", 13), h("span", { class: "hide-xs", text: "Ordner als ZIP" })),
            infoNode),
          h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {},
                h("th", { text: "" }), h("th", { text: "Name" }),
                h("th", { class: "num hide-xs", text: "Größe" }),
                h("th", { class: "hide-xs", text: "Geändert" }),
                h("th", { class: "hide-xs", text: "Rechte" }),
                h("th", { text: "" }))),
              tbody))),
        canUpload ? h("div", {}, dropzone, fileInput) : null);

      await load();
      return () => {};
    },
  };
})(window.AW);

/* Grundgeruest: Anmeldezustand, Navigation, Seitenwechsel. */
"use strict";

(function (AW) {
  const { h, icon, toast, API } = AW;

  AW.state = { user: null, config: {}, needsSetup: false, roles: [] };
  AW.views = AW.views || {};

  const NAV = [
    { route: "runs", label: "Läufe", icon: "list", cap: "runs.view" },
    { route: "start", label: "Playbook starten", icon: "play", cap: "runs.start" },
    { route: "editor", label: "Playbooks & Inventory", icon: "file", cap: "files.read" },
    { route: "files", label: "Dateien", icon: "folder", cap: "files.read" },
    { route: "cron", label: "Zeitplan (Cron)", icon: "clock", cap: "cron.view" },
    { route: "admin", label: "Verwaltung", icon: "settings", cap: "system.view" },
  ];

  let current = { cleanup: null, route: null };
  let pageHost = null;
  let navHost = null;
  let activeBadge = null;
  let runsSocket = null;

  /* -- Adresse / Route -------------------------------------------------- */

  function parseHash() {
    const raw = (location.hash || "").replace(/^#\/?/, "");
    const [pathPart, queryPart] = raw.split("?");
    const segments = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
    const params = {};
    if (queryPart) {
      for (const [key, value] of new URLSearchParams(queryPart)) params[key] = value;
    }
    return { route: segments[0] || "runs", segments: segments.slice(1), params };
  }

  AW.go = function (route, params) {
    let hash = "#/" + route;
    if (params && params.segments) {
      hash += "/" + params.segments.map(encodeURIComponent).join("/");
    }
    const query = Object.assign({}, params || {});
    delete query.segments;
    const search = new URLSearchParams(query).toString();
    if (search) hash += "?" + search;
    if (location.hash === hash) handleRoute();
    else location.hash = hash;
  };

  AW.linkTo = function (route, params) {
    let hash = "#/" + route;
    if (params && params.segments) hash += "/" + params.segments.map(encodeURIComponent).join("/");
    const query = Object.assign({}, params || {});
    delete query.segments;
    const search = new URLSearchParams(query).toString();
    return hash + (search ? "?" + search : "");
  };

  async function handleRoute() {
    const { route, segments, params } = parseHash();
    const view = AW.views[route];
    if (!view) {
      // Nicht blind weiterleiten - sonst dreht sich die Weiterleitung im Kreis,
      // falls auch die Startseite fehlt.
      if (route !== "runs" && AW.views.runs) { AW.go("runs"); return; }
      AW.mount(pageHost, h("div", { class: "page" },
        h("div", { class: "notice error" },
          h("strong", { text: "Diese Seite gibt es nicht" }),
          h("div", { class: "small mt", text: "Unbekannter Bereich: " + route }))));
      return;
    }
    if (view.cap && !can(view.cap)) {
      AW.mount(pageHost, h("div", { class: "page" },
        h("div", { class: "notice error" },
          h("strong", { text: "Kein Zugriff" }),
          h("div", { class: "small mt", text:
            "Für diesen Bereich fehlt deiner Rolle (" + AW.state.user.role + ") die Berechtigung." }))));
      return;
    }
    if (current.cleanup) {
      try { current.cleanup(); } catch (err) { console.error(err); }
      current.cleanup = null;
    }
    markNav(route);
    closeSidebar();
    const container = h("div", { class: "page" });
    AW.mount(pageHost, container);
    document.title = (view.title || view.label || route) + " · Ansible Controller";
    try {
      current.route = route;
      current.cleanup = await view.mount(container, { segments, params }) || null;
    } catch (err) {
      console.error(err);
      AW.mount(container, h("div", { class: "notice error" },
        h("strong", { text: "Diese Seite konnte nicht geladen werden" }),
        h("div", { class: "small mt wrap-any", text: (err && (err.detail || err.message)) || String(err) }),
        h("div", { class: "mt" },
          h("button", { onclick: () => handleRoute() }, icon("refresh"), "Erneut versuchen"))));
    }
  }

  function can(capability) {
    return !!(AW.state.user && AW.state.user.capabilities[capability]);
  }
  AW.can = can;

  /* -- Grundgeruest ----------------------------------------------------- */

  function markNav(route) {
    if (!navHost) return;
    for (const link of navHost.querySelectorAll("a")) {
      link.classList.toggle("active", link.dataset.route === route);
    }
  }

  function closeSidebar() {
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) sidebar.classList.remove("open");
    const scrim = document.querySelector(".scrim");
    if (scrim) scrim.remove();
  }

  function toggleSidebar() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    const open = sidebar.classList.toggle("open");
    const existing = document.querySelector(".scrim");
    if (existing) existing.remove();
    if (open) {
      document.body.appendChild(h("div", { class: "scrim", onclick: closeSidebar }));
    }
  }

  function brand() {
    const logo = h("span");
    logo.innerHTML = '<svg width="26" height="26" viewBox="0 0 32 32">' +
      '<rect width="32" height="32" rx="7" fill="#21252e"/>' +
      '<path d="M9 22l7-13 7 13" fill="none" stroke="#4a9eff" stroke-width="2.6" ' +
      'stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="16" cy="19.5" r="2.1" fill="#3fb950"/></svg>';
    return h("div", { class: "brand" }, logo.firstChild,
      h("div", { class: "brand-text" },
        h("div", { class: "brand-title", text: AW.state.config.title || "Ansible Controller" }),
        h("div", { class: "brand-sub", title: AW.state.config.ansible_dir,
                   text: AW.state.config.ansible_dir || "" })));
  }

  function buildShell() {
    activeBadge = h("span", { class: "badge running", style: { display: "none" } },
                    h("span", { class: "dot pulse" }), "0");
    navHost = h("nav", { class: "nav" });
    for (const item of NAV) {
      if (item.cap && !can(item.cap)) continue;
      navHost.appendChild(h("a", { href: AW.linkTo(item.route), dataset: { route: item.route } },
        icon(item.icon), h("span", { text: item.label }),
        item.route === "runs" ? activeBadge : null));
    }
    const user = AW.state.user;
    const sidebar = h("aside", { class: "sidebar" }, brand(), navHost,
      h("div", { class: "sidebar-foot" },
        h("div", { class: "who" }, icon("users", 14),
          h("span", { class: "ellipsis", text: user.name }),
          h("span", { class: "badge plain tiny", text: user.role })),
        h("button", { class: "small ghost", onclick: logout }, icon("logout", 14), "Abmelden")));

    pageHost = h("div", {});
    const topbar = h("header", { class: "topbar" },
      h("button", { class: "ghost icon", title: "Menü", onclick: toggleSidebar }, icon("menu")),
      h("strong", { text: AW.state.config.title || "Ansible Controller" }));

    AW.mount(document.getElementById("app"),
      h("div", { class: "shell" }, sidebar, h("main", { class: "main" }, topbar, pageHost)));
  }

  async function logout() {
    try { await API.post("/api/logout"); } catch (err) { /* auch bei Fehler abmelden */ }
    API.csrf = null;
    AW.state.user = null;
    if (runsSocket) { runsSocket.close(); runsSocket = null; }
    AW.boot();
  }

  /* -- Zaehler der aktiven Laeufe in der Navigation --------------------- */

  function watchActiveRuns() {
    if (!can("runs.view")) return;
    const counts = new Map();
    const update = () => {
      const active = counts.size;
      if (!activeBadge) return;
      activeBadge.style.display = active ? "" : "none";
      AW.mount(activeBadge, h("span", { class: "dot pulse" }), String(active));
    };
    runsSocket = API.socket("/ws/runs", {
      onMessage: (message) => {
        if (message.type === "init") {
          counts.clear();
          for (const run of message.runs || []) {
            if (run.status === "running" || run.status === "queued") counts.set(run.id, run.status);
          }
        } else if (message.type === "run") {
          const meta = message.meta;
          if (meta.status === "running" || meta.status === "queued") counts.set(meta.id, meta.status);
          else counts.delete(meta.id);
        } else if (message.type === "run_deleted") {
          counts.delete(message.id);
        }
        update();
        if (AW.onRunsMessage) AW.onRunsMessage(message);
      },
    });
  }

  /* -- Start ------------------------------------------------------------ */

  AW.boot = async function () {
    const app = document.getElementById("app");
    try {
      const me = await API.get("/api/me", { allowAnonymous: true });
      AW.state.config = me.config || {};
      AW.state.needsSetup = !!me.needs_setup;
      AW.state.roles = me.roles || [];
      if (!me.authenticated) {
        if (runsSocket) { runsSocket.close(); runsSocket = null; }
        AW.renderLogin(app, { needsSetup: me.needs_setup, config: me.config });
        return;
      }
      AW.state.user = me.user;
      API.csrf = me.csrf_token;
    } catch (err) {
      AW.mount(app, h("div", { class: "boot" },
        h("div", { class: "notice error" },
          h("strong", { text: "Der Dienst antwortet nicht" }),
          h("div", { class: "small mt", text: (err && (err.detail || err.message)) || String(err) })),
        h("button", { onclick: () => AW.boot() }, icon("refresh"), "Erneut versuchen")));
      return;
    }
    buildShell();
    if (runsSocket) runsSocket.close();
    watchActiveRuns();
    await handleRoute();
  };

  API.onUnauthorized = () => {
    if (!AW.state.user) return;
    AW.state.user = null;
    toast("Die Sitzung ist abgelaufen. Bitte neu anmelden.", "warn");
    AW.boot();
  };

  window.addEventListener("hashchange", handleRoute);
  window.addEventListener("DOMContentLoaded", () => AW.boot());
  if (document.readyState !== "loading") AW.boot();
})(window.AW);

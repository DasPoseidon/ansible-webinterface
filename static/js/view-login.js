/* Anmeldung und Ersteinrichtung. */
"use strict";

(function (AW) {
  const { h, icon, toast, API, fail } = AW;

  function logoBrand(subtitle) {
    const logo = h("span");
    logo.innerHTML = '<svg width="34" height="34" viewBox="0 0 32 32">' +
      '<rect width="32" height="32" rx="7" fill="#21252e"/>' +
      '<path d="M9 22l7-13 7 13" fill="none" stroke="#4a9eff" stroke-width="2.6" ' +
      'stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="16" cy="19.5" r="2.1" fill="#3fb950"/></svg>';
    return h("div", { class: "brand" }, logo.firstChild,
      h("div", { class: "brand-text" },
        h("div", { class: "brand-title", text: "Ansible Controller" }),
        h("div", { class: "brand-sub", text: subtitle || "" })));
  }

  AW.renderLogin = function (container, options) {
    options = options || {};
    const setup = !!options.needsSetup;
    const name = h("input", { type: "text", autocomplete: "username",
                              placeholder: setup ? "z. B. admin" : "Benutzername" });
    const password = h("input", { type: "password",
      autocomplete: setup ? "new-password" : "current-password", placeholder: "Passwort" });
    const repeat = h("input", { type: "password", autocomplete: "new-password",
                                placeholder: "Passwort wiederholen" });
    const error = h("div", { class: "notice error", style: { display: "none" } });
    const button = h("button", { class: "primary", type: "submit", style: { width: "100%" } },
      setup ? "Administrator anlegen" : "Anmelden");

    const showError = (message) => {
      error.textContent = message;
      error.style.display = "";
    };

    const submit = async (event) => {
      event.preventDefault();
      error.style.display = "none";
      if (!name.value.trim() || !password.value) {
        showError("Bitte Benutzername und Passwort eingeben.");
        return;
      }
      if (setup && password.value !== repeat.value) {
        showError("Die beiden Passwörter stimmen nicht überein.");
        return;
      }
      if (setup && password.value.length < 10) {
        showError("Das Passwort muss mindestens 10 Zeichen haben.");
        return;
      }
      button.disabled = true;
      button.textContent = setup ? "wird angelegt …" : "melde an …";
      try {
        const result = await API.post(setup ? "/api/setup" : "/api/login",
          { name: name.value.trim(), password: password.value },
          { allowAnonymous: true });
        API.csrf = result.csrf_token;
        AW.state.user = result.user;
        toast(setup ? "Administrator angelegt – willkommen!" : "Angemeldet als " + result.user.name,
              "ok", 3000);
        AW.boot();
      } catch (err) {
        button.disabled = false;
        button.textContent = setup ? "Administrator anlegen" : "Anmelden";
        showError(err.detail || String(err));
        password.focus();
        password.select();
      }
    };

    const form = h("form", { class: "card login", onsubmit: submit },
      logoBrand(options.config && options.config.ansible_dir),
      setup ? h("div", { class: "notice" },
        h("strong", { text: "Ersteinrichtung" }),
        h("div", { class: "small mt", text:
          "Es gibt noch keinen Benutzer. Lege jetzt den ersten Administrator an – " +
          "danach ist diese Maske nicht mehr erreichbar." })) : null,
      error,
      h("div", { class: "field" }, h("label", { for: "aw-user", text: "Benutzername" }), name),
      h("div", { class: "field" }, h("label", { for: "aw-pass", text: "Passwort" }), password),
      setup ? h("div", { class: "field" },
        h("label", { text: "Passwort wiederholen" }), repeat) : null,
      button,
      h("div", { class: "tiny faint mt", text:
        "Dieses Werkzeug führt Playbooks mit Root-Rechten aus. Zugang nur für Berechtigte." }));
    name.id = "aw-user";
    password.id = "aw-pass";

    AW.mount(container, h("div", { class: "login-wrap" }, form));
    setTimeout(() => name.focus(), 50);
  };
})(window.AW);

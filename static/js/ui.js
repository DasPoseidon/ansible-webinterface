/* Kleine Helfer fuer DOM, Formatierung, Hinweise und Dialoge. */
"use strict";

const AW = window.AW || (window.AW = {});
// Muss hier stehen: die View-Dateien werden vor app.js geladen und
// registrieren sich in AW.views.
AW.views = AW.views || {};

/* -- DOM --------------------------------------------------------------- */

const PROP_KEYS = new Set(["value", "checked", "disabled", "selected", "indeterminate",
                           "multiple", "readOnly", "open", "hidden"]);

function append(node, kids) {
  for (const kid of kids.flat(4)) {
    if (kid === null || kid === undefined || kid === false || kid === true) continue;
    node.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    kids.unshift(attrs);
    attrs = null;
  }
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "for") node.htmlFor = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "style") Object.assign(node.style, value);
      else if (key.startsWith("on") && typeof value === "function")
        node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (PROP_KEYS.has(key)) node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
  }
  append(node, kids);
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
function mount(node, ...kids) { clear(node); append(node, kids); return node; }
function qs(sel, root) { return (root || document).querySelector(sel); }

/* -- Symbole ----------------------------------------------------------- */

const ICONS = {
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  list: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.2"/><circle cx="4" cy="12" r="1.2"/><circle cx="4" cy="18" r="1.2"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><polyline points="14 3 14 8 19 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/>',
  users: '<circle cx="9" cy="8" r="3.4"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5.2a3.4 3.4 0 0 1 0 6.6"/><path d="M18 20a6 6 0 0 0-2-4.5"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><circle cx="7" cy="7.5" r="1"/><circle cx="7" cy="16.5" r="1"/>',
  terminal: '<polyline points="5 8 9 12 5 16"/><line x1="12" y1="16" x2="19" y2="16"/><rect x="2" y="3" width="20" height="18" rx="2"/>',
  settings: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/><circle cx="9" cy="7" r="2.2"/><circle cx="15" cy="12" r="2.2"/><circle cx="8" cy="17" r="2.2"/>',
  logout: '<path d="M14 17v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v2"/><polyline points="17 8 21 12 17 16"/><line x1="10" y1="12" x2="21" y2="12"/>',
  menu: '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
  x: '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  alert: '<path d="M12 4 2.5 20h19z"/><line x1="12" y1="10" x2="12" y2="14"/><circle cx="12" cy="17" r=".9" fill="currentColor"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 11 12 16 17 11"/><line x1="12" y1="3" x2="12" y2="16"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 8 12 3 17 8"/><line x1="12" y1="3" x2="12" y2="16"/>',
  trash: '<polyline points="4 7 20 7"/><path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13"/><path d="M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L20 8l-4-4L4 16z"/><line x1="14" y1="6" x2="18" y2="10"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><polyline points="20 3 20 7 16 7"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><line x1="16" y1="16" x2="21" y2="21"/>',
  branch: '<circle cx="7" cy="5" r="2.2"/><circle cx="7" cy="19" r="2.2"/><circle cx="17" cy="9" r="2.2"/><path d="M7 7.2v9.6"/><path d="M17 11.2c0 3-4 2.8-7 4.4"/>',
  save: '<path d="M5 3h11l3 3v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><rect x="8" y="3" width="7" height="5"/><rect x="8" y="13" width="8" height="6"/>',
  chevronR: '<polyline points="9 6 15 12 9 18"/>',
  chevronD: '<polyline points="6 9 12 15 18 9"/>',
  eye: '<path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/>',
  history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3"/><polyline points="3 4 3 8.5 7.5 8.5"/><polyline points="12 8 12 12 15 14"/>',
};

function icon(name, size) {
  const span = h("span", { class: "ico" });
  span.innerHTML = '<svg width="' + (size || 16) + '" height="' + (size || 16) +
    '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round">' + (ICONS[name] || "") + "</svg>";
  return span.firstChild;
}

/* -- Formatierung ------------------------------------------------------ */

function pad(value) { return String(value).padStart(2, "0"); }

function fmtTime(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + "." + d.getFullYear() +
         " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function fmtTimeShort(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const clock = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  return sameDay ? clock : pad(d.getDate()) + "." + pad(d.getMonth() + 1) + ". " + clock;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const s = Number(seconds);
  if (s < 1) return s.toFixed(2) + " s";
  if (s < 60) return s.toFixed(1) + " s";
  const m = Math.floor(s / 60), rest = Math.round(s % 60);
  if (m < 60) return m + " min " + pad(rest) + " s";
  return Math.floor(m / 60) + " h " + pad(m % 60) + " min";
}

function fmtAgo(ts) {
  if (!ts) return "–";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "vor " + Math.max(1, Math.round(diff)) + " s";
  if (diff < 3600) return "vor " + Math.round(diff / 60) + " min";
  if (diff < 86400) return "vor " + Math.round(diff / 3600) + " h";
  return "vor " + Math.round(diff / 86400) + " Tagen";
}

function fmtBytes(value) {
  if (value === null || value === undefined) return "–";
  let size = Number(value);
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (let i = 0; i < units.length; i++) {
    if (size < 1024 || i === units.length - 1)
      return (i === 0 ? size + " " : size.toFixed(1) + " ") + units[i];
    size /= 1024;
  }
  return value + " B";
}

const STATUS_LABELS = {
  queued: "wartet", running: "läuft", success: "erfolgreich", failed: "fehlgeschlagen",
  cancelled: "abgebrochen", interrupted: "unterbrochen", error: "Fehler",
};

function statusBadge(status, extra) {
  const label = STATUS_LABELS[status] || status;
  const node = h("span", { class: "badge " + status },
    h("span", { class: "dot" + (status === "running" ? " pulse" : "") }), label);
  if (extra) node.appendChild(h("span", { class: "faint", text: " " + extra }));
  return node;
}

/* -- ANSI-Farben ------------------------------------------------------- */

const ANSI_RE = /\x1b\[([0-9;]*)m/g;

function ansiClasses(state) {
  const out = [];
  if (state.fg) out.push("a-" + state.fg);
  if (state.bold) out.push("a-b");
  if (state.italic) out.push("a-i");
  if (state.underline) out.push("a-u");
  return out.join(" ");
}

function ansiToFragment(text) {
  const frag = document.createDocumentFragment();
  const state = { fg: null, bold: false, italic: false, underline: false };
  let last = 0, match;
  const push = (chunk) => {
    if (!chunk) return;
    const cls = ansiClasses(state);
    frag.appendChild(cls ? h("span", { class: cls, text: chunk })
                         : document.createTextNode(chunk));
  };
  ANSI_RE.lastIndex = 0;
  while ((match = ANSI_RE.exec(text)) !== null) {
    push(text.slice(last, match.index));
    for (const raw of (match[1] || "0").split(";")) {
      const code = parseInt(raw || "0", 10);
      if (code === 0) { state.fg = null; state.bold = false; state.italic = false; state.underline = false; }
      else if (code === 1) state.bold = true;
      else if (code === 3) state.italic = true;
      else if (code === 4) state.underline = true;
      else if (code === 22) state.bold = false;
      else if (code === 23) state.italic = false;
      else if (code === 24) state.underline = false;
      else if (code === 39) state.fg = null;
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) state.fg = code;
    }
    last = ANSI_RE.lastIndex;
  }
  push(text.slice(last));
  return frag;
}

function stripAnsi(text) { return String(text).replace(/\x1b\[[0-9;]*m/g, ""); }

/* -- Diff-Darstellung -------------------------------------------------- */

function diffToFragment(text) {
  const frag = document.createDocumentFragment();
  for (const line of String(text || "").split("\n")) {
    let cls = "";
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") ||
        line.startsWith("index ")) cls = "meta";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    frag.appendChild(h("span", { class: cls, text: line + "\n" }));
  }
  return frag;
}

/* -- Hinweise ---------------------------------------------------------- */

function toast(message, kind, timeout) {
  const host = document.getElementById("toasts");
  const node = h("div", { class: "toast " + (kind || "") },
    h("div", { class: "grow", text: String(message) }),
    h("button", { title: "Schließen", onclick: () => node.remove() }, "×"));
  host.appendChild(node);
  const delay = timeout === undefined ? (kind === "error" ? 9000 : 4500) : timeout;
  if (delay) setTimeout(() => node.remove(), delay);
  return node;
}

/* -- Dialoge ----------------------------------------------------------- */

function modal(options) {
  const root = document.getElementById("modal-root");
  const buttons = [];
  let closed = false;
  const close = (value) => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    back.remove();
    if (options.onClose) options.onClose(value);
  };
  const onKey = (event) => {
    if (event.key === "Escape") { event.stopPropagation(); close(null); }
  };
  for (const spec of options.buttons || []) {
    buttons.push(h("button", {
      class: spec.class || "",
      disabled: !!spec.disabled,
      onclick: () => spec.onClick ? spec.onClick(close) : close(spec.value),
    }, spec.icon ? icon(spec.icon) : null, spec.label));
  }
  const box = h("div", { class: "modal", style: options.width ? { "--modal-w": options.width } : null },
    h("div", { class: "modal-head" },
      h("h2", { text: options.title || "" }),
      h("button", { class: "ghost icon", title: "Schließen", onclick: () => close(null) }, icon("x"))),
    h("div", { class: "modal-body" }, options.body),
    buttons.length ? h("div", { class: "modal-foot" }, buttons) : null);
  const back = h("div", { class: "modal-back", onclick: (event) => {
    if (event.target === back && options.dismissable !== false) close(null);
  } }, box);
  root.appendChild(back);
  document.addEventListener("keydown", onKey);
  setTimeout(() => {
    const focusable = box.querySelector("input, textarea, select, button.primary, button");
    if (focusable) focusable.focus();
  }, 30);
  return { close, box };
}

function confirmDialog(options) {
  return new Promise((resolve) => {
    let answered = false;
    const dialog = modal({
      title: options.title || "Bitte bestätigen",
      width: options.width || "480px",
      body: typeof options.body === "string"
        ? h("div", {}, ...String(options.body).split("\n").map((line) => h("p", { text: line })))
        : options.body,
      buttons: [
        { label: options.cancelLabel || "Abbrechen", onClick: (close) => { answered = true; close(); resolve(false); } },
        { label: options.confirmLabel || "Ja, ausführen",
          class: options.danger ? "danger" : "primary",
          onClick: (close) => { answered = true; close(); resolve(true); } },
      ],
      onClose: () => { if (!answered) resolve(false); },
    });
    return dialog;
  });
}

function promptDialog(options) {
  return new Promise((resolve) => {
    const input = h("input", { type: "text", value: options.value || "",
                               placeholder: options.placeholder || "" });
    let answered = false;
    const submit = (close) => { answered = true; close(); resolve(input.value); };
    const dialog = modal({
      title: options.title || "Eingabe",
      width: "440px",
      body: h("div", { class: "field" },
        options.label ? h("label", { text: options.label }) : null, input,
        options.hint ? h("div", { class: "hint", text: options.hint }) : null),
      buttons: [
        { label: "Abbrechen", onClick: (close) => { answered = true; close(); resolve(null); } },
        { label: options.confirmLabel || "Übernehmen", class: "primary", onClick: submit },
      ],
      onClose: () => { if (!answered) resolve(null); },
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submit(dialog.close);
    });
  });
}

/* -- Sonstiges --------------------------------------------------------- */

function debounce(fn, wait) {
  let handle = null;
  return function () {
    const args = arguments;
    clearTimeout(handle);
    handle = setTimeout(() => fn.apply(null, args), wait || 250);
  };
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(
      () => toast("In die Zwischenablage kopiert", "ok", 2000),
      () => toast("Kopieren nicht möglich – bitte von Hand markieren", "warn"));
    return;
  }
  const area = h("textarea", { value: text, style: { position: "fixed", opacity: "0" } });
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
    toast("In die Zwischenablage kopiert", "ok", 2000);
  } catch (err) {
    toast("Kopieren nicht möglich – bitte von Hand markieren", "warn");
  }
  area.remove();
}

function spinner(small) { return h("span", { class: "spinner" + (small ? " small" : "") }); }

function emptyState(message, extra) {
  return h("div", { class: "table-empty" }, h("div", { text: message }), extra);
}

Object.assign(AW, {
  h, append, clear, mount, qs, icon, toast, modal, confirmDialog, promptDialog,
  fmtTime, fmtTimeShort, fmtDuration, fmtAgo, fmtBytes, statusBadge, STATUS_LABELS,
  ansiToFragment, stripAnsi, diffToFragment, debounce, copyText, spinner, emptyState,
});

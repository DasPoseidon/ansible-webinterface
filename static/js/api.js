/* Zugriff auf die HTTP-API und die Live-Verbindungen. */
"use strict";

(function (AW) {
  class ApiError extends Error {
    constructor(status, detail, payload) {
      super(detail || ("HTTP " + status));
      this.status = status;
      this.detail = detail;
      this.payload = payload;
    }
  }

  const API = {
    csrf: null,
    onUnauthorized: null,

    async request(method, path, options) {
      options = options || {};
      const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
      let body = options.body;
      if (options.json !== undefined) {
        body = JSON.stringify(options.json);
        headers["Content-Type"] = "application/json";
      }
      if (method !== "GET" && method !== "HEAD" && API.csrf) headers["X-CSRF-Token"] = API.csrf;
      let response;
      try {
        response = await fetch(path, { method, headers, body, credentials: "same-origin" });
      } catch (err) {
        throw new ApiError(0, "Der Dienst ist nicht erreichbar. Läuft er noch?");
      }
      if (response.status === 401 && !options.allowAnonymous) {
        if (API.onUnauthorized) API.onUnauthorized();
        throw new ApiError(401, "Die Sitzung ist abgelaufen. Bitte neu anmelden.");
      }
      const type = response.headers.get("content-type") || "";
      let payload = null;
      if (options.asText) payload = await response.text();
      else if (type.includes("application/json")) payload = await response.json();
      else payload = await response.text();
      if (!response.ok) {
        const detail = payload && typeof payload === "object"
          ? (payload.detail || JSON.stringify(payload))
          : String(payload || "").slice(0, 400);
        throw new ApiError(response.status, detail || ("HTTP " + response.status), payload);
      }
      return payload;
    },

    get(path, options) { return API.request("GET", path, options); },
    post(path, json, options) { return API.request("POST", path, Object.assign({ json: json === undefined ? {} : json }, options || {})); },
    put(path, json, options) { return API.request("PUT", path, Object.assign({ json }, options || {})); },
    del(path, options) { return API.request("DELETE", path, options); },
    text(path) { return API.request("GET", path, { asText: true }); },

    async upload(path, formData) {
      const headers = {};
      if (API.csrf) headers["X-CSRF-Token"] = API.csrf;
      const response = await fetch(path, { method: "POST", headers, body: formData,
                                           credentials: "same-origin" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new ApiError(response.status, payload.detail || "Hochladen fehlgeschlagen", payload);
      return payload;
    },

    /* Eine Live-Verbindung mit automatischem Wiederaufbau. */
    socket(path, handlers) {
      const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
      let ws = null, closed = false, attempt = 0, timer = null;

      const connect = () => {
        if (closed) return;
        try { ws = new WebSocket(url); }
        catch (err) { schedule(); return; }
        ws.onopen = () => { attempt = 0; if (handlers.onOpen) handlers.onOpen(); };
        ws.onmessage = (event) => {
          let data;
          try { data = JSON.parse(event.data); } catch (err) { return; }
          if (handlers.onMessage) handlers.onMessage(data);
        };
        ws.onclose = (event) => {
          if (closed) return;
          if (handlers.onClose) handlers.onClose(event);
          if (event.code === 4401 || event.code === 4403) {
            if (API.onUnauthorized) API.onUnauthorized();
            return;
          }
          schedule();
        };
        ws.onerror = () => {};
      };

      const schedule = () => {
        if (closed) return;
        attempt += 1;
        const wait = Math.min(1000 * Math.pow(1.7, attempt - 1), 15000);
        if (handlers.onReconnect) handlers.onReconnect(attempt, wait);
        timer = setTimeout(connect, wait);
      };

      connect();
      return {
        close() {
          closed = true;
          clearTimeout(timer);
          if (ws && ws.readyState <= 1) ws.close();
        },
        get state() { return ws ? ws.readyState : 3; },
      };
    },
  };

  AW.API = API;
  AW.ApiError = ApiError;

  /* Fehler einheitlich melden. */
  AW.fail = function (err, prefix) {
    const message = err instanceof ApiError ? err.detail : (err && err.message) || String(err);
    AW.toast((prefix ? prefix + ": " : "") + message, "error");
    if (!(err instanceof ApiError)) console.error(err);
  };
})(window.AW);

// Haven als Seitenleisten-Eintrag in Home Assistant (Custom Panel).
//
// Ein Web Component ohne Abhängigkeiten. Home Assistant lädt dieses Modul
// (`module_url`), legt das Element <haven-panel> an und setzt `hass`,
// `narrow`, `panel` und `route`. Das Element startet die Flutter-Web-App in
// sich selbst (Embedding mit `hostElement`) und reicht Token, Dunkelmodus und
// Schmalansicht über `window.__havenPanel` an die App weiter (Dart liest das
// per `dart:js_interop`, siehe lib/core/panel/).
//
// Annahmen (HA-Frontend 2025/2026, nicht gegen ein laufendes HA geprüft):
// - `hass.auth` ist das Auth-Objekt von home-assistant-js-websocket:
//   `data.access_token`, `expired`, `refreshAccessToken()`.
// - `hass.themes.darkMode` ist `true` oder `false`.
// - Das Ereignis `hass-toggle-menu` (bubbles, composed) öffnet die Seitenleiste
//   von HA in der Schmalansicht.
// - Wechselt man den Eintrag, entfernt HA das Element und legt später ein neues
//   an. Flutter lässt sich nur einmal je Seite starten, deshalb wandert der
//   Host-Knoten mit der laufenden App in das neue Element.

const BASE = new URL("./", import.meta.url).href;
const VERSION = (BASE.match(/\/haven_static\/([^/]+)\/$/) || [])[1] || "dev";
const BG = { dark: "#0D0C12", light: "#F6F1EA" };
const FG = { dark: "#968FA5", light: "#6C6477" };

function bridge() {
  if (window.__havenPanel) return window.__havenPanel;
  const b = {
    token: null,
    darkMode: null,
    origin: window.location.origin,
    version: VERSION,
    narrow: false,
    language: null,
    _hass: null,
    _listeners: [],
    addListener(fn) {
      if (typeof fn === "function") b._listeners.push(fn);
    },
    // Gültiges Token für eine neue WebSocket-Verbindung der App.
    async getToken() {
      const auth = b._hass && b._hass.auth;
      if (!auth) return b.token || "";
      try {
        if (auth.expired && typeof auth.refreshAccessToken === "function") {
          await auth.refreshAccessToken();
        }
      } catch (e) {
        console.warn("Haven: Token konnte nicht erneuert werden", e);
      }
      const t = (auth.data && auth.data.access_token) || auth.accessToken;
      if (t) b.token = t;
      return b.token || "";
    },
    // Seitenleiste von HA öffnen (Schmalansicht, z. B. auf dem Handy).
    toggleMenu() {
      const el = b._element;
      if (el) {
        el.dispatchEvent(
          new Event("hass-toggle-menu", { bubbles: true, composed: true }),
        );
      }
    },
    _update(hass, narrow) {
      b._hass = hass;
      const auth = hass && hass.auth;
      const next = {
        token: (auth && auth.data && auth.data.access_token) || b.token,
        darkMode:
          hass && hass.themes && typeof hass.themes.darkMode === "boolean"
            ? hass.themes.darkMode
            : null,
        narrow: !!narrow,
        language: (hass && (hass.language || (hass.locale && hass.locale.language))) || null,
      };
      let changed = false;
      for (const k of Object.keys(next)) {
        if (b[k] !== next[k]) {
          b[k] = next[k];
          changed = true;
        }
      }
      if (!changed) return;
      for (const fn of b._listeners) {
        try {
          fn();
        } catch (e) {
          console.warn("Haven:", e);
        }
      }
    },
    // Wird von flutter_bootstrap.js gerufen, wenn es im Panel läuft.
    boot(flutter) {
      const host = b._host;
      const config = {
        hostElement: host,
        entrypointBaseUrl: BASE,
        assetBase: BASE,
        canvasKitBaseUrl: BASE + "canvaskit/",
        // Nur die volle CanvasKit liegt im Build (eine Datei für alle Browser).
        canvasKitVariant: "full",
      };
      flutter.loader.load({
        config,
        onEntrypointLoaded: async (engineInitializer) => {
          const appRunner = await engineInitializer.initializeEngine(config);
          await appRunner.runApp();
          const loading = host.querySelector(".haven-loading");
          if (loading) loading.remove();
        },
      });
    },
  };
  window.__havenPanel = b;
  return b;
}

function hostNode(dark) {
  const b = bridge();
  if (!b._host) {
    const host = document.createElement("div");
    host.className = "haven-host";
    host.style.cssText = "position:absolute;inset:0;overflow:hidden;";
    const loading = document.createElement("div");
    loading.className = "haven-loading";
    loading.textContent = "Haven lädt";
    loading.style.cssText =
      "position:absolute;inset:0;display:flex;align-items:center;" +
      "justify-content:center;font:600 15px system-ui,sans-serif;" +
      "letter-spacing:.2px;color:" + (dark ? FG.dark : FG.light) + ";";
    host.appendChild(loading);
    b._host = host;
  }
  return b._host;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Laden fehlgeschlagen: " + src));
    document.head.appendChild(s);
  });
}

class HavenPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._narrow = false;
  }

  set hass(hass) {
    this._hass = hass;
    bridge()._update(hass, this._narrow);
    this._paint();
    this._start();
  }

  get hass() {
    return this._hass;
  }

  set narrow(v) {
    this._narrow = !!v;
    if (this._hass) bridge()._update(this._hass, this._narrow);
  }

  get narrow() {
    return this._narrow;
  }

  set panel(p) {
    this._panel = p;
  }

  set route(r) {
    this._route = r;
  }

  connectedCallback() {
    this.style.cssText =
      "display:block;position:relative;width:100%;height:100%;min-height:100vh;";
    const b = bridge();
    b._element = this;
    const host = hostNode(b.darkMode === true);
    if (host.parentNode !== this) this.appendChild(host);
    this._paint();
    this._start();
  }

  disconnectedCallback() {
    const b = bridge();
    if (b._element === this) b._element = null;
  }

  _paint() {
    const dark = bridge().darkMode === true;
    this.style.background = dark ? BG.dark : BG.light;
  }

  async _start() {
    const b = bridge();
    if (b._started || !this._hass || !this.isConnected) return;
    b._started = true;
    try {
      await b.getToken();
      await loadScript(BASE + "flutter_bootstrap.js");
    } catch (e) {
      b._started = false;
      const loading = b._host && b._host.querySelector(".haven-loading");
      if (loading) loading.textContent = "Haven konnte nicht geladen werden";
      console.error("Haven:", e);
    }
  }
}

if (!customElements.get("haven-panel")) {
  customElements.define("haven-panel", HavenPanel);
}

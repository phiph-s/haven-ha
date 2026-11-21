// Haven als installierbare Web-App (PWA) unter `/haven_app/` von Home
// Assistant. Die Integration liefert die Seite, das Manifest und einen
// Service Worker aus (panel.py, `app_html`) und lädt dieses Modul aus dem
// versionierten Web-Build (/haven_static/<version>/haven-app.js).
//
// Anders als im Seitenleisten-Eintrag (haven-panel.js) gibt es hier kein
// `hass` von HA. Die Seite meldet sich selbst an, wie die Apps von HA:
// OAuth2 mit dem Login von HA (`/auth/authorize`), dann Tokens über
// `/auth/token`. Client-ID ist die Adresse dieser Seite. HA erlaubt die
// Rücksprung-Adresse ohne weitere Angaben, weil sie auf demselben Host liegt.
// Die Tokens liegen im localStorage dieser Herkunft (wie bei HA selbst).
//
// Die App spricht mit dieser Seite über dasselbe Objekt wie im Panel
// (`window.__havenPanel`, siehe lib/core/panel/), mit `standalone: true`.

const BASE = new URL("./", import.meta.url).href;
const VERSION = (BASE.match(/\/haven_static\/([^/]+)\/$/) || [])[1] || "dev";
const ORIGIN = window.location.origin;
const APP_PATH = "/haven_app/";
const CLIENT_ID = ORIGIN + APP_PATH;
const REDIRECT_URI = CLIENT_ID;
const STORE_KEY = "haven_app_tokens";
const STATE_KEY = "haven_app_oauth_state";
// So lange vor Ablauf wird das Zugriffstoken erneuert.
const REFRESH_MARGIN_MS = 60 * 1000;

function loadTokens() {
  try {
    const t = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    return t && t.refresh_token ? t : null;
  } catch (e) {
    return null;
  }
}

function saveTokens(t) {
  try {
    if (t) localStorage.setItem(STORE_KEY, JSON.stringify(t));
    else localStorage.removeItem(STORE_KEY);
  } catch (e) {
    console.warn("Haven: Anmeldung nicht gespeichert", e);
  }
}

function randomState() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

/// Zum Login von HA. Danach kommt HA mit `?code=…&state=…` zurück.
function redirectToLogin() {
  const state = randomState();
  try {
    sessionStorage.setItem(STATE_KEY, state);
  } catch (e) {
    // Ohne sessionStorage prüfen wir `state` nicht (kein Angriff möglich,
    // der Code gilt nur für diese Client-ID).
  }
  const q = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    state,
  });
  window.location.replace(ORIGIN + "/auth/authorize?" + q.toString());
}

class AuthError extends Error {}

async function tokenRequest(params) {
  const body = new URLSearchParams({ client_id: CLIENT_ID, ...params });
  let res;
  try {
    res = await fetch(ORIGIN + "/auth/token", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } catch (e) {
    throw new Error("Home Assistant nicht erreichbar");
  }
  // 400/403: Code oder Refresh-Token ungültig (abgemeldet, widerrufen).
  if (res.status === 400 || res.status === 401 || res.status === 403) {
    throw new AuthError("Anmeldung abgelaufen");
  }
  if (!res.ok) throw new Error("Home Assistant antwortet mit " + res.status);
  const data = await res.json();
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires: Date.now() + (Number(data.expires_in) || 1800) * 1000,
  };
}

const auth = {
  tokens: loadTokens(),
  _refreshing: null,

  get valid() {
    const t = this.tokens;
    return !!(t && t.access_token && t.expires - REFRESH_MARGIN_MS > Date.now());
  },

  /// Code aus dem Rücksprung von HA einlösen. `true`, wenn angemeldet.
  async fromRedirect() {
    const q = new URLSearchParams(window.location.search);
    const code = q.get("code");
    if (!code) return false;
    let expected = null;
    try {
      expected = sessionStorage.getItem(STATE_KEY);
      sessionStorage.removeItem(STATE_KEY);
    } catch (e) {
      // siehe redirectToLogin
    }
    // Adresse aufräumen, damit ein Neuladen den Code nicht noch mal schickt.
    window.history.replaceState(null, "", APP_PATH);
    if (expected && q.get("state") !== expected) return false;
    const t = await tokenRequest({ grant_type: "authorization_code", code });
    this.tokens = t;
    saveTokens(t);
    return true;
  },

  /// Gültiges Zugriffstoken, erneuert es bei Bedarf (nur eine Anfrage
  /// gleichzeitig). Bei widerrufener Anmeldung: zum Login.
  async accessToken() {
    if (this.valid) return this.tokens.access_token;
    if (!this.tokens) {
      redirectToLogin();
      return "";
    }
    if (!this._refreshing) {
      const refresh = this.tokens.refresh_token;
      this._refreshing = tokenRequest({
        grant_type: "refresh_token",
        refresh_token: refresh,
      })
        .then((t) => {
          this.tokens = { ...t, refresh_token: t.refresh_token || refresh };
          saveTokens(this.tokens);
          bridge()._changed();
          return this.tokens.access_token;
        })
        .catch((e) => {
          if (e instanceof AuthError) {
            this.tokens = null;
            saveTokens(null);
            redirectToLogin();
            return "";
          }
          // Offline: das letzte Token versuchen, die App verbindet neu.
          console.warn("Haven:", e.message);
          return (this.tokens && this.tokens.access_token) || "";
        })
        .finally(() => {
          this._refreshing = null;
        });
    }
    return this._refreshing;
  },

  /// Anmeldung bei HA widerrufen und von vorn.
  async logout() {
    const refresh = this.tokens && this.tokens.refresh_token;
    this.tokens = null;
    saveTokens(null);
    if (refresh) {
      try {
        await fetch(ORIGIN + "/auth/revoke", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ token: refresh }),
        });
      } catch (e) {
        console.warn("Haven: Abmelden bei HA fehlgeschlagen", e);
      }
    }
    redirectToLogin();
  },
};

function bridge() {
  if (window.__havenPanel) return window.__havenPanel;
  const b = {
    token: null,
    // Unbekannt: die App folgt ihrer Einstellung bzw. dem System.
    darkMode: null,
    origin: ORIGIN,
    version: VERSION,
    narrow: false,
    language: navigator.language || null,
    standalone: true,
    _listeners: [],
    addListener(fn) {
      if (typeof fn === "function") b._listeners.push(fn);
    },
    async getToken() {
      const t = await auth.accessToken();
      if (t) b.token = t;
      return t || b.token || "";
    },
    toggleMenu() {},
    logout() {
      auth.logout();
    },
    _changed() {
      const t = auth.tokens && auth.tokens.access_token;
      if (!t || t === b.token) return;
      b.token = t;
      for (const fn of b._listeners) {
        try {
          fn();
        } catch (e) {
          console.warn("Haven:", e);
        }
      }
    },
    // Von flutter_bootstrap.js gerufen: die App füllt die ganze Seite.
    boot(flutter) {
      const config = {
        entrypointBaseUrl: BASE,
        assetBase: BASE,
        canvasKitBaseUrl: BASE + "canvaskit/",
        canvasKitVariant: "full",
      };
      flutter.loader.load({
        config,
        onEntrypointLoaded: async (engineInitializer) => {
          const appRunner = await engineInitializer.initializeEngine(config);
          await appRunner.runApp();
          const loading = document.getElementById("haven-loading");
          if (loading) loading.remove();
        },
      });
    },
  };
  window.__havenPanel = b;
  return b;
}

function say(text) {
  const el = document.getElementById("haven-loading");
  if (el) el.textContent = text;
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

/// Token rechtzeitig erneuern, solange die Seite offen ist (die App holt
/// es nur beim Verbinden). Nach dem Aufwachen aus dem Hintergrund sofort.
function keepFresh() {
  const tick = () => {
    if (auth.tokens && !auth.valid) bridge().getToken();
  };
  setInterval(tick, 30 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });
}

async function start() {
  if ("serviceWorker" in navigator) {
    // Nur für „Installieren“: der Worker reicht alles ans Netz durch.
    navigator.serviceWorker
      .register(APP_PATH + "sw.js", { scope: APP_PATH })
      .catch((e) => console.warn("Haven: Service Worker", e));
  }
  try {
    await auth.fromRedirect();
  } catch (e) {
    if (!(e instanceof AuthError)) {
      say(e.message + ". Seite neu laden zum erneuten Versuch.");
      return;
    }
  }
  if (!auth.tokens) {
    say("Anmelden bei Home Assistant");
    redirectToLogin();
    return;
  }
  const b = bridge();
  await b.getToken();
  if (!auth.tokens) return; // zum Login unterwegs
  keepFresh();
  try {
    await loadScript(BASE + "flutter_bootstrap.js");
  } catch (e) {
    say("Haven konnte nicht geladen werden");
    console.error("Haven:", e);
  }
}

start();

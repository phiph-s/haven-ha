"""Seitenleisten-Eintrag „Haven“: Konfiguration für Panel und statische Dateien.

Rein, ohne Home Assistant (testbar). Die Anmeldung bei HA steht in
``panel_setup.py``. Annahmen zur HA-API (geprüft am Quelltext von HA 2026.2):
``panel_custom.async_register_panel(hass, frontend_url_path, webcomponent_name,
sidebar_title, sidebar_icon, module_url=..., embed_iframe=..., trust_external=...,
config=..., require_admin=...)`` und
``hass.http.async_register_static_paths([StaticPathConfig(url_path, path, cache_headers)])``.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

PANEL_URL_PATH = "haven"
PANEL_COMPONENT = "haven-panel"
PANEL_TITLE = "Haven"
PANEL_ICON = "mdi:home-heart"
PANEL_MODULE = "haven-panel.js"

# Präfix der statischen Dateien. Die Version im Pfad sorgt fürs Cache-Busting:
# Jede neue Version hat eine neue URL, HA darf lange cachen.
STATIC_PREFIX = "/haven_static"

# Ordner mit dem Web-Build in der Integration (von app/tool/build_ha_panel.sh).
FRONTEND_DIR = "frontend"

# Die Flutter-App läuft direkt im Panel-Element (kein iframe). Falls Eingabe
# oder Fokus im Shadow DOM von HA Probleme machen: auf True stellen, dann
# lädt HA das Modul in einem eigenen iframe.
EMBED_IFRAME = False

# Pflichtdateien eines brauchbaren Builds.
REQUIRED_FILES = (PANEL_MODULE, "flutter.js", "main.dart.js")

_SAFE = re.compile(r"[^0-9A-Za-z._-]")


def safe_version(version: str | None) -> str:
    """Version als Pfadsegment (``0.3.0`` bleibt, Fremdzeichen werden ``_``)."""
    v = _SAFE.sub("_", (version or "").strip())
    return v or "dev"


def static_url(version: str | None) -> str:
    """URL-Präfix der Dateien, ohne Schrägstrich am Ende."""
    return f"{STATIC_PREFIX}/{safe_version(version)}"


def frontend_dir(component_dir: Path | str) -> Path:
    return Path(component_dir) / FRONTEND_DIR


def frontend_available(directory: Path | str) -> bool:
    """Liegt ein Web-Build im Ordner? Ohne Build gibt es keinen Eintrag."""
    d = Path(directory)
    return d.is_dir() and all((d / f).is_file() for f in REQUIRED_FILES)


def static_path_config(version: str | None, directory: Path | str) -> dict[str, Any]:
    """Argumente für ``StaticPathConfig``: der ganze Ordner unter der Versions-URL."""
    return {"url_path": static_url(version), "path": str(directory), "cache_headers": True}


def panel_registration(version: str | None, *, require_admin: bool = False) -> dict[str, Any]:
    """Argumente für ``panel_custom.async_register_panel`` (ohne ``hass``).

    ``config`` landet im Element unter ``panel.config``: Basis-URL der Dateien und
    Version. Das Modul liest die Basis zur Not auch aus ``import.meta.url``.
    """
    base = static_url(version)
    return {
        "frontend_url_path": PANEL_URL_PATH,
        "webcomponent_name": PANEL_COMPONENT,
        "sidebar_title": PANEL_TITLE,
        "sidebar_icon": PANEL_ICON,
        "module_url": f"{base}/{PANEL_MODULE}",
        "embed_iframe": EMBED_IFRAME,
        "trust_external": False,
        "require_admin": require_admin,
        "config": {"base_url": f"{base}/", "version": safe_version(version)},
    }


# ---- Installierbare Web-App (PWA) --------------------------------------
# Feste Adresse (Startadresse und Geltungsbereich der installierten App
# dürfen sich nicht mit jeder Version ändern). Die Seite lädt den
# versionierten Build und meldet sich selbst bei HA an (haven-app.js).
APP_URL_PATH = "/haven_app"
APP_MODULE = "haven-app.js"
APP_BG = {"light": "#F6F1EA", "dark": "#0D0C12"}


def app_html(version: str | None) -> str:
    """Startseite der Web-App. Enthält nichts Geheimes (ohne Anmeldung)."""
    base = static_url(version)
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Haven</title>
<meta name="description" content="Haven für Home Assistant">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="{APP_BG["light"]}">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="{APP_BG["dark"]}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Haven">
<link rel="manifest" href="{APP_URL_PATH}/manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="{base}/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="{base}/favicon.png">
<link rel="apple-touch-icon" href="{base}/apple-touch-icon.png">
<style>
html,body{{margin:0;height:100%;background:{APP_BG["light"]};color:#6C6477}}
@media (prefers-color-scheme: dark){{html,body{{background:{APP_BG["dark"]};color:#968FA5}}}}
#haven-loading{{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
font:600 15px system-ui,sans-serif;letter-spacing:.2px;padding:24px;text-align:center}}
</style>
</head>
<body>
<div id="haven-loading">Haven lädt</div>
<noscript>Haven braucht JavaScript.</noscript>
<script type="module" src="{base}/{APP_MODULE}"></script>
</body>
</html>
"""


def app_manifest(version: str | None) -> dict[str, Any]:
    """Web-App-Manifest. Symbole aus dem versionierten Build."""
    base = static_url(version)
    icons = [
        {"src": f"{base}/icons/Icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": f"{base}/icons/Icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {
            "src": f"{base}/icons/Icon-maskable-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "maskable",
        },
        {
            "src": f"{base}/icons/Icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    return {
        "id": f"{APP_URL_PATH}/",
        "name": "Haven",
        "short_name": "Haven",
        "description": "Haven für Home Assistant",
        "start_url": f"{APP_URL_PATH}/",
        "scope": f"{APP_URL_PATH}/",
        "display": "standalone",
        "background_color": APP_BG["light"],
        "theme_color": APP_BG["light"],
        "prefer_related_applications": False,
        "icons": icons,
    }


# Service Worker nur fürs Installieren: reicht jede Anfrage ans Netz durch
# und speichert nichts (Zustände und Bilder kommen immer frisch aus HA, neue
# Versionen haben eine neue URL).
APP_SERVICE_WORKER = """// Haven: nichts zwischenspeichern, alles ans Netz.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
"""


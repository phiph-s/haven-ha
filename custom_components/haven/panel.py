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

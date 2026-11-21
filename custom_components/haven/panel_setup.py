"""Meldet den Seitenleisten-Eintrag „Haven“ bei Home Assistant an (Custom Panel).

Nur aktiv, wenn im Ordner ``frontend/`` ein Web-Build liegt (siehe README).
Statische Pfade lassen sich in HA nicht wieder abmelden, deshalb einmal je
Laufzeit. Das Panel selbst kommt und geht mit dem Config Entry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import json

from aiohttp import web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .panel import (
    APP_MODULE,
    APP_SERVICE_WORKER,
    APP_URL_PATH,
    PANEL_URL_PATH,
    app_html,
    app_manifest,
    frontend_available,
    frontend_dir,
    panel_registration,
    static_path_config,
)

_LOGGER = logging.getLogger(__name__)
_DATA = f"{DOMAIN}_panel"


async def async_setup_panel(hass: HomeAssistant, version: str | None) -> bool:
    """Statische Dateien und Panel anmelden. ``False``, wenn kein Build da ist."""
    directory = frontend_dir(Path(__file__).parent)
    if not await hass.async_add_executor_job(frontend_available, directory):
        _LOGGER.info("Haven: kein Web-Build in %s, kein Seitenleisten-Eintrag", directory)
        return False
    state = hass.data.setdefault(_DATA, {"static": set(), "panel": False, "app": False})
    static = static_path_config(version, directory)
    if static["url_path"] not in state["static"]:
        await hass.http.async_register_static_paths([StaticPathConfig(**static)])
        state["static"].add(static["url_path"])
    app_built = await hass.async_add_executor_job((directory / APP_MODULE).is_file)
    if app_built and not state.get("app"):
        _register_app(hass, version)
        state["app"] = True
    if not state["panel"]:
        await panel_custom.async_register_panel(hass, **panel_registration(version))
        state["panel"] = True
    return True


class _AppFile(HomeAssistantView):
    """Eine feste Datei der Web-App, ohne Anmeldung (enthält nichts Geheimes;
    angemeldet wird in der Seite über den Login von HA)."""

    requires_auth = False

    def __init__(self, url: str, name: str, body: str, content_type: str) -> None:
        self.url = url
        self.name = name
        self._body = body
        self._type = content_type

    async def get(self, request: web.Request) -> web.Response:
        # Kurz cachen: nach einem Update zeigt die Seite auf die neue Version.
        return web.Response(
            text=self._body,
            content_type=self._type,
            headers={"Cache-Control": "no-cache"},
        )


def _register_app(hass: HomeAssistant, version: str | None) -> None:
    """Installierbare Web-App unter ``/haven_app/`` (Seite, Manifest, Worker).
    Views lassen sich nicht abmelden: einmal je Laufzeit, die Version gilt
    bis zum Neustart (wie die statischen Pfade)."""
    html = app_html(version)
    files = [
        (APP_URL_PATH, "app", html, "text/html"),
        (f"{APP_URL_PATH}/", "app_index", html, "text/html"),
        (
            f"{APP_URL_PATH}/manifest.webmanifest",
            "app_manifest",
            json.dumps(app_manifest(version), ensure_ascii=False),
            "application/manifest+json",
        ),
        (f"{APP_URL_PATH}/sw.js", "app_sw", APP_SERVICE_WORKER, "text/javascript"),
    ]
    for url, name, body, content_type in files:
        hass.http.register_view(_AppFile(url, f"api:{DOMAIN}:{name}", body, content_type))


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Seitenleisten-Eintrag entfernen (Entry entladen)."""
    state = hass.data.get(_DATA)
    if not state or not state["panel"]:
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    state["panel"] = False

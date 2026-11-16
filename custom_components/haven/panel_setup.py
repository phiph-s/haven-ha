"""Meldet den Seitenleisten-Eintrag „Haven“ bei Home Assistant an (Custom Panel).

Nur aktiv, wenn im Ordner ``frontend/`` ein Web-Build liegt (siehe README).
Statische Pfade lassen sich in HA nicht wieder abmelden, deshalb einmal je
Laufzeit. Das Panel selbst kommt und geht mit dem Config Entry.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .panel import (
    PANEL_URL_PATH,
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
    state = hass.data.setdefault(_DATA, {"static": set(), "panel": False})
    static = static_path_config(version, directory)
    if static["url_path"] not in state["static"]:
        await hass.http.async_register_static_paths([StaticPathConfig(**static)])
        state["static"].add(static["url_path"])
    if not state["panel"]:
        await panel_custom.async_register_panel(hass, **panel_registration(version))
        state["panel"] = True
    return True


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Seitenleisten-Eintrag entfernen (Entry entladen)."""
    state = hass.data.get(_DATA)
    if not state or not state["panel"]:
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    state["panel"] = False

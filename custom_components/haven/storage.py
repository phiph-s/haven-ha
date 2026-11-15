"""Persistenz der App-Daten in ``.storage`` (``haven.floorplan``, ``haven.widgets``, ``haven.prefs``)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .appdata import normalize_prefs
from .const import DOMAIN, STORAGE_VERSION

_KEY = f"{DOMAIN}_storage"


class HavenStorage:
    """Kleine Stores: ``{"plan": {...}}``, ``{"widgets": [...]}`` und ``{"prefs": {...}}``."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._plan = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.floorplan")
        self._widgets = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.widgets")
        self._prefs = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.prefs")

    async def async_get_plan(self) -> dict[str, Any] | None:
        data = await self._plan.async_load()
        return data.get("plan") if data else None

    async def async_set_plan(self, plan: dict[str, Any]) -> None:
        await self._plan.async_save({"plan": plan})

    async def async_get_widgets(self) -> list[dict[str, Any]] | None:
        data = await self._widgets.async_load()
        return data.get("widgets") if data else None

    async def async_set_widgets(self, widgets: list[dict[str, Any]]) -> None:
        await self._widgets.async_save({"widgets": widgets})

    async def async_get_prefs(self) -> dict[str, Any]:
        """Nie gespeichert: ``{"excluded_lights": []}``."""
        data = await self._prefs.async_load()
        prefs = data.get("prefs") if data else None
        return normalize_prefs(prefs if isinstance(prefs, dict) else {})

    async def async_set_prefs(self, prefs: dict[str, Any]) -> None:
        await self._prefs.async_save({"prefs": prefs})


def get_storage(hass: HomeAssistant) -> HavenStorage:
    if _KEY not in hass.data:
        hass.data[_KEY] = HavenStorage(hass)
    return hass.data[_KEY]

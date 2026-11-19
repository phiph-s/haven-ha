"""Bilder der Wohnung als Image-Entities (je Perspektive und Design ein Bild).

Für Widgets und Displays: ``entity_picture`` liefert das Bild im aktuellen Lichtzustand,
``image_last_updated`` ändert sich, sobald eine Lampe der Perspektive schaltet.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .render_runtime import RenderRuntime, get_render

THEMES = {"light": "hell", "dark": "dunkel"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = get_render(hass)
    entities: dict[str, HavenMapImage] = {}

    @callback
    def sync() -> None:
        new = [HavenMapImage(hass, runtime, name) for name in runtime.sets if name not in entities]
        for e in new:
            entities[e.set_name] = e
        if new:
            async_add_entities(new)
        registry = er.async_get(hass)
        for name in [n for n in entities if n not in runtime.sets]:
            gone = entities.pop(name)
            if gone.entity_id and registry.async_get(gone.entity_id):
                registry.async_remove(gone.entity_id)
        for e in entities.values():
            e.refresh()

    entry.async_on_unload(runtime.add_listener(sync))
    entry.async_on_unload(runtime.async_unload)
    await runtime.async_load()
    sync()


class HavenMapImage(ImageEntity):
    """Ein Bildsatz (Perspektive × Design) als Bild."""

    _attr_content_type = "image/png"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, runtime: RenderRuntime, set_name: str) -> None:
        super().__init__(hass)
        self._runtime = runtime
        self.set_name = set_name
        info = runtime.info.get(set_name, {})
        view = info.get("name") or info.get("view") or set_name
        theme = THEMES.get(info.get("theme", ""), info.get("theme", ""))
        self._attr_unique_id = f"{DOMAIN}_render_{set_name}"
        self._attr_name = f"Haven Karte {view} ({theme})" if theme else f"Haven Karte {view}"
        self._attr_image_last_updated = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.watch(self.set_name, self.refresh))

    @callback
    def refresh(self) -> None:
        """Bild hat sich geändert (Lampe, neuer Bildsatz)."""
        self._attr_image_last_updated = dt_util.utcnow()
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._runtime.info.get(self.set_name, {})
        return {k: info[k] for k in ("view", "room", "theme", "width", "height") if k in info}

    async def async_image(self) -> bytes | None:
        return await self._runtime.async_image(self.set_name)

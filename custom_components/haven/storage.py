"""Persistenz der App-Daten in ``.storage`` (``haven.floorplan``, ``haven.widgets``, ``haven.prefs``, ``haven.networkmap``, ``haven.scenes``)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .appdata import (
    VersionedDoc,
    build_plan_doc,
    build_widgets_doc,
    deserialize_networkmap_cache,
    read_plan_doc,
    read_prefs_doc,
    read_widgets_doc,
    serialize_networkmap_cache,
)
from .const import DOMAIN, STORAGE_VERSION

_KEY = f"{DOMAIN}_storage"
# Teile von haven.scenes: Looks, Soll und letzte Messung je Szene
_SCENE_PARTS = ("looks", "targets", "checks", "extras", "room_scenes")


def _versioned(store: Store[dict[str, Any]], what: str) -> VersionedDoc:
    return VersionedDoc(store.async_load, store.async_save, what)


class HavenStorage:
    """Kleine Stores: ``{"plan": {...}}``, ``{"widgets": [...]}`` und ``{"prefs": {...}}``.

    Die drei App-Dokumente tragen dazu ``revision`` (siehe ``appdata.VersionedDoc``):
    ``set`` mit ``base_revision`` lehnt einen veralteten Stand mit ``conflict`` ab.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._plan = _versioned(Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.floorplan"), "Der Grundriss")
        self._widgets = _versioned(Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.widgets"), "Die Übersicht")
        self._prefs = _versioned(Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.prefs"), "Die Einstellungen")
        self._networkmap = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.networkmap")
        self._scenes = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.scenes")
        self._scene_doc: dict[str, dict[str, Any]] | None = None

    async def async_get_plan_doc(self) -> dict[str, Any]:
        """``{"plan": {...}|None, "updated_at": ...|None, "revision": n}``."""
        return read_plan_doc(await self._plan.async_load())

    async def async_set_plan(
        self, plan: dict[str, Any], updated_at: Any = None, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Speichert den Grundriss, gibt ``{"plan", "updated_at", "revision"}`` zurück.

        Ohne ``furniture`` im Plan (ältere App) bleiben die gespeicherten Möbel erhalten.
        Wirft ``Z2MError("conflict")``, wenn ``base_revision`` veraltet ist.
        """
        doc = await self._plan.async_update(
            lambda previous: build_plan_doc(plan, updated_at, previous=previous), base_revision
        )
        return read_plan_doc(doc)

    async def async_get_widgets_doc(self) -> dict[str, Any]:
        """``{"widgets": [...]|None, "layout": {...}|None, "updated_at": ...|None, "revision": n}``."""
        return read_widgets_doc(await self._widgets.async_load())

    async def async_set_widgets(
        self,
        widgets: list[dict[str, Any]],
        layout: Any = None,
        updated_at: Any = None,
        *,
        layout_given: bool = False,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        doc = await self._widgets.async_update(
            lambda previous: build_widgets_doc(
                widgets, layout, updated_at, previous=previous, layout_given=layout_given
            ),
            base_revision,
        )
        return read_widgets_doc(doc)

    async def async_load_networkmap(self) -> dict[str, Any] | None:
        return deserialize_networkmap_cache(await self._networkmap.async_load())

    def schedule_save_networkmap(self, networkmap: dict[str, Any]) -> None:
        """Schreibt den Cache verzögert (Store bündelt mehrere Aufrufe)."""
        self._networkmap.async_delay_save(lambda: serialize_networkmap_cache(networkmap), 1)

    async def async_get_prefs(self) -> dict[str, Any]:
        """Nie gespeichert: ``{"excluded_lights": []}``."""
        return (await self.async_get_prefs_doc())["prefs"]

    async def async_get_prefs_doc(self) -> dict[str, Any]:
        """``{"prefs": {...}, "revision": n}``."""
        return read_prefs_doc(await self._prefs.async_load())

    async def async_set_prefs(self, prefs: dict[str, Any], base_revision: int | None = None) -> dict[str, Any]:
        """Speichert die Prefs, gibt ``{"prefs", "revision"}`` zurück (``conflict`` wie beim Plan)."""
        return read_prefs_doc(await self._prefs.async_update(lambda _previous: {"prefs": prefs}, base_revision))

    async def _async_scene_doc(self) -> dict[str, dict[str, Any]]:
        """``{looks, targets, checks}``, je ``{"<group_id>:<scene_id>": ...}`` (einmal geladen)."""
        if self._scene_doc is None:
            data = await self._scenes.async_load()
            doc: dict[str, dict[str, Any]] = {}
            for part in _SCENE_PARTS:
                value = data.get(part) if isinstance(data, dict) else None
                doc[part] = dict(value) if isinstance(value, dict) else {}
            self._scene_doc = doc
        return self._scene_doc

    async def _async_set_scene_part(self, part: str, values: dict[str, Any]) -> None:
        doc = await self._async_scene_doc()
        doc[part] = dict(values)
        await self._scenes.async_save({p: doc[p] for p in _SCENE_PARTS})

    async def async_get_scene_looks(self) -> dict[str, Any]:
        """Looks der Szenen, ``{"<group_id>:<scene_id>": {colors, brightness, kelvin, stored_at}}``."""
        return (await self._async_scene_doc())["looks"]

    def scene_looks_cached(self) -> dict[str, Any]:
        """Ohne Laden (für synchrone Callbacks), leer bis zum ersten Laden."""
        return (self._scene_doc or {}).get("looks", {})

    async def async_set_scene_looks(self, looks: dict[str, Any]) -> None:
        await self._async_set_scene_part("looks", looks)

    async def async_get_scene_targets(self) -> dict[str, Any]:
        """Soll je Szene (siehe ``scenesync``): ``{members, ieees, stored_at, source}``."""
        return (await self._async_scene_doc())["targets"]

    async def async_set_scene_targets(self, targets: dict[str, Any]) -> None:
        await self._async_set_scene_part("targets", targets)

    async def async_get_scene_checks(self) -> dict[str, Any]:
        """Letzte Messung je Szene (Ergebnis von ``scene_check``)."""
        return (await self._async_scene_doc())["checks"]

    async def async_set_scene_checks(self, checks: dict[str, Any]) -> None:
        await self._async_set_scene_part("checks", checks)

    async def async_get_scene_extras(self) -> dict[str, Any]:
        """Zusatzgeräte der Gruppenszenen (siehe ``room_scenes``): ``{"<g>:<s>": [device]}``."""
        return (await self._async_scene_doc())["extras"]

    async def async_set_scene_extras(self, extras: dict[str, Any]) -> None:
        await self._async_set_scene_part("extras", extras)

    async def async_get_room_scenes(self) -> dict[str, Any]:
        """Szenen der Räume ohne Gruppe: ``{area_id: [{id, name, look, devices, stored_at}]}``."""
        return (await self._async_scene_doc())["room_scenes"]

    async def async_set_room_scenes(self, room_scenes: dict[str, Any]) -> None:
        await self._async_set_scene_part("room_scenes", room_scenes)

    def scene_extras_cached(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(extras, room_scenes)`` ohne Laden, leer bis zum ersten Laden."""
        doc = self._scene_doc or {}
        return doc.get("extras", {}), doc.get("room_scenes", {})

    def scene_sync_cached(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(targets, checks)`` ohne Laden, leer bis zum ersten Laden."""
        doc = self._scene_doc or {}
        return doc.get("targets", {}), doc.get("checks", {})


def get_storage(hass: HomeAssistant) -> HavenStorage:
    if _KEY not in hass.data:
        hass.data[_KEY] = HavenStorage(hass)
    return hass.data[_KEY]

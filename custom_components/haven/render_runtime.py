"""Bilder der Wohnung für Widgets und Displays: Bildsätze laden und je Lichtzustand zusammensetzen.

Die Bilder entstehen erst, wenn jemand sie abruft (``async_image``), im Executor und mit
Cache je Lichtzustand. Ändert sich eine Lampe eines Satzes, meldet die Laufzeit das
gedrosselt den Image-Entities (``image_last_updated``), damit Clients neu laden.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import DOMAIN
from .render_store import RenderStore

if TYPE_CHECKING:
    from .render_compose import LayerSet

_LOGGER = logging.getLogger(__name__)
_KEY = f"{DOMAIN}_render"

# Lampenwechsel höchstens so oft an die Entities melden (Sekunden)
THROTTLE = 1.0


def render_available() -> bool:
    """numpy und Pillow vorhanden (sonst kann HA keine Bilder zusammensetzen)."""
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "PIL"))


class RenderRuntime:
    """Bildsätze in ``.storage/haven_render`` und ihre zusammengesetzten Bilder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = RenderStore(Path(hass.config.path(".storage", "haven_render")))
        self.sets: dict[str, LayerSet] = {}
        self.info: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._cache: dict[str, tuple[tuple[Any, ...], bytes]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[], None]] = []
        self._watchers: dict[str, Callable[[], None]] = {}
        self._pending: set[str] = set()
        self._timer: CALLBACK_TYPE | None = None
        self._unsub: CALLBACK_TYPE | None = None
        # Upload und Austausch nacheinander (ein Upload zur Zeit)
        self.upload_lock = asyncio.Lock()

    # --- Laden ------------------------------------------------------------------------

    async def async_load(self) -> None:
        if not self._loaded:
            await self.async_reload()

    async def async_reload(self) -> None:
        """Sätze neu lesen (nach einem Upload), Lampen neu abonnieren, Entities anpassen."""

        def load() -> tuple[dict[str, LayerSet], dict[str, Any]]:
            if not render_available():
                return {}, {}
            from .render_compose import LayerSet, LayerSetError  # noqa: PLC0415

            out: dict[str, LayerSet] = {}
            for name in self.store.set_names():
                try:
                    out[name] = LayerSet(self.store.sets_dir / name)
                except LayerSetError as err:
                    _LOGGER.warning("Haven: Bildsatz %s unbrauchbar: %s", name, err)
            info = self.store.index().get("sets", {})
            return out, info if isinstance(info, dict) else {}

        self.sets, self.info = await self.hass.async_add_executor_job(load)
        self._loaded = True
        self._cache.clear()
        self._subscribe()
        for listener in list(self._listeners):
            listener()

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """``listener`` nach jedem Neuladen der Sätze aufrufen."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def watch(self, name: str, on_change: Callable[[], None]) -> CALLBACK_TYPE:
        """``on_change`` (gedrosselt) aufrufen, wenn sich eine Lampe des Satzes ``name`` ändert."""
        self._watchers[name] = on_change

        def remove() -> None:
            if self._watchers.get(name) is on_change:
                del self._watchers[name]

        return remove

    # --- Lampen -----------------------------------------------------------------------

    @callback
    def _subscribe(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        lamps = sorted({lamp for s in self.sets.values() for lamp in s.lamp_ids})
        if lamps:
            self._unsub = async_track_state_change_event(self.hass, lamps, self._on_state)

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for name, s in self.sets.items():
            if entity_id in s.lamp_rooms:
                self._pending.add(name)
        if self._pending and self._timer is None:
            self._timer = async_call_later(self.hass, THROTTLE, self._flush)

    @callback
    def _flush(self, _now: Any) -> None:
        self._timer = None
        names, self._pending = self._pending, set()
        for name in names:
            watcher = self._watchers.get(name)
            if watcher is not None:
                watcher()

    def _lights(self, s: LayerSet) -> dict[str, Any]:
        from .render_compose import lamp_light  # noqa: PLC0415

        out: dict[str, Any] = {}
        for lamp in s.lamp_ids:
            state = self.hass.states.get(lamp)
            out[lamp] = None if state is None else lamp_light(state.state, dict(state.attributes))
        return out

    # --- Bilder -----------------------------------------------------------------------

    async def async_image(self, name: str) -> bytes | None:
        """PNG des Satzes ``name`` im aktuellen Lichtzustand (``None``: unbekannt)."""
        s = self.sets.get(name)
        if s is None:
            return None
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            lights = self._lights(s)
            sig = tuple(sorted((k, v) for k, v in lights.items() if v is not None))
            cached = self._cache.get(name)
            if cached is not None and cached[0] == sig:
                return cached[1]
            from .render_compose import encode_png  # noqa: PLC0415

            try:
                data = await self.hass.async_add_executor_job(lambda: encode_png(s.compose(lights)))
            except Exception:
                _LOGGER.exception("Haven: Bild %s konnte nicht zusammengesetzt werden", name)
                return None
            self._cache[name] = (sig, data)
            return data

    @callback
    def async_unload(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._timer is not None:
            self._timer()
            self._timer = None


def get_render(hass: HomeAssistant) -> RenderRuntime:
    if _KEY not in hass.data:
        hass.data[_KEY] = RenderRuntime(hass)
    return hass.data[_KEY]

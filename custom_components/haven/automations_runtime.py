"""Laufzeit der Haven-Automationen in Home Assistant.

Hört über den Listener der :class:`~.z2m.Z2MBridge` auf Gerätemeldungen
(``<base>/<friendly_name>``, Abos für Namen mit ``/`` pflegt die Bridge selbst).
Die Geräte stehen über ihre IEEE in der Automation, das Topic löst die Bridge
über den aktuellen ``friendly_name`` auf. Nach einem neuen ``bridge/devices``
(Umbenennung) gilt deshalb sofort der neue Name, ohne eigenes Neu-Abonnieren.

Die Logik (Normalisieren, Planen, Entprellen, Validieren) liegt HA-frei in
``automations.py``. Hier passieren nur Speichern, Senden und Ereignisse.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .automations import (
    STORE_KEY,
    STORE_MINOR_VERSION,
    STORE_VERSION,
    Gesture,
    Plan,
    RoomState,
    RoomTarget,
    StepCoalescer,
    build_room_targets,
    default_name,
    device_owner,
    empty_store,
    event_data,
    migrate_store,
    mqtt_payload,
    normalize_action,
    plan_gesture,
    public_automation,
    read_store,
    room_state_from,
    validate_automation,
)
from .commands import Z2MError
from .const import DOMAIN, EVENT_DATA_UPDATED
from .motion_runtime import MotionRunner

if TYPE_CHECKING:
    from .z2m import Z2MBridge

_LOGGER = logging.getLogger(__name__)

EVENT_AUTOMATION_TRIGGERED = "haven_automation_triggered"
_KEY = f"{DOMAIN}_automations"
# So lange gilt der selbst erwartete Zustand eines Raums mehr als der von HA
# (HA meldet die neue Helligkeit erst nach dem Gruppen-Update von Z2M).
_ESTIMATE_TTL = 1.5


class _AutomationStore(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: Any) -> dict[str, Any]:
        return migrate_store(old_major_version, old_minor_version, old_data)


class AutomationEngine:
    """Hält die Automationen, hört auf die Bridge und führt Gesten aus."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.bridge: Z2MBridge | None = None
        self._store = _AutomationStore(hass, STORE_VERSION, STORE_KEY, minor_version=STORE_MINOR_VERSION)
        self._data: dict[str, Any] = empty_store()
        self._loaded = False
        self._remove_listener: Callable[[], None] | None = None
        self._coalescer = StepCoalescer()
        self._timers: dict[str, Any] = {}
        self._pending_rotate: dict[str, tuple[str, str]] = {}  # key -> (automation_id, ieee)
        self._estimates: dict[str, tuple[float, RoomState]] = {}
        self._rooms_cache: tuple[float, dict[str, RoomTarget]] | None = None
        self._excluded: list[str] = []
        self.motion = MotionRunner(self)

    # --- Lebenszyklus ------------------------------------------------------------

    async def async_load(self) -> None:
        if self._loaded:
            return
        self._data = read_store(await self._store.async_load())
        self._loaded = True

    @callback
    def attach(self, bridge: Z2MBridge) -> None:
        """Mit der Bridge eines geladenen Entries verbinden."""
        self.detach()
        self.bridge = bridge
        self._remove_listener = bridge.add_listener(self._on_bridge)
        self.motion.sync()

    @callback
    def detach(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        self.motion.stop()
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._pending_rotate.clear()
        self._coalescer.clear()
        self.bridge = None

    # --- Daten ---------------------------------------------------------------------

    @property
    def automations(self) -> list[dict[str, Any]]:
        return self._data["automations"]

    @property
    def runtime(self) -> dict[str, Any]:
        return self._data["runtime"]

    def get(self, automation_id: str) -> dict[str, Any] | None:
        return next((a for a in self.automations if a["id"] == automation_id), None)

    def list_public(self) -> list[dict[str, Any]]:
        out = [public_automation(a, self.runtime) for a in self.automations]
        for item in out:
            if item.get("type") == "room_motion":
                item["motion"] = self.motion.status(item)
        return out

    def _public(self, automation: dict[str, Any]) -> dict[str, Any]:
        item = public_automation(automation, self.runtime)
        if automation.get("type") == "room_motion":
            item["motion"] = self.motion.status(automation)
        return item

    def _schedule_save(self) -> None:
        self._store.async_delay_save(lambda: self._data, 2)

    async def async_set(self, doc: dict[str, Any], rooms: dict[str, RoomTarget] | None) -> dict[str, Any]:
        known = (
            {ieee: d.get("friendly_name") or ieee for ieee, d in self.bridge.devices.items()}
            if self.bridge is not None and self.bridge.devices_received
            else None
        )
        automation = validate_automation(doc, self.automations, rooms, known)
        if not automation["name"]:
            room = (rooms or {}).get(automation["room_area_id"])
            automation["name"] = default_name(room, automation["room_area_id"], automation["type"])
        items = [a for a in self.automations if a["id"] != automation["id"]]
        index = next((i for i, a in enumerate(self.automations) if a["id"] == automation["id"]), len(items))
        items.insert(index, automation)
        self._data["automations"] = items
        await self._store.async_save(self._data)
        self.motion.sync()
        return self._public(automation)

    async def async_delete(self, automation_id: str) -> bool:
        before = len(self.automations)
        self._data["automations"] = [a for a in self.automations if a["id"] != automation_id]
        if len(self.automations) == before:
            return False
        self.runtime["last_triggered"].pop(automation_id, None)
        self.runtime.setdefault("motion", {}).pop(automation_id, None)
        self.motion.sync()
        await self._store.async_save(self._data)
        return True

    # --- Räume -----------------------------------------------------------------------

    def room_targets(self, *, max_age: float = 5.0) -> dict[str, RoomTarget]:
        """Räume mit Gruppe, Lichtern und Szenen. Kurz zwischengespeichert (Registry-Abfragen)."""
        now = time.monotonic()
        if self._rooms_cache and now - self._rooms_cache[0] < max_age:
            return self._rooms_cache[1]
        if self.bridge is None:
            return {}
        from .websocket_api import build_rooms  # noqa: PLC0415 - vermeidet Importzyklus

        excluded = self._excluded
        rooms = build_room_targets(build_rooms(self.hass, self.bridge)["rooms"], self.bridge.groups, excluded)
        self._rooms_cache = (now, rooms)
        return rooms

    def invalidate_rooms(self) -> None:
        self._rooms_cache = None

    async def async_refresh_excluded(self) -> None:
        from .storage import get_storage  # noqa: PLC0415

        prefs = await get_storage(self.hass).async_get_prefs()
        # Gesperrte Geräte („immer an“) schaltet keine Automation.
        self._excluded = list(dict.fromkeys([*prefs.get("excluded_lights", []), *prefs.get("locked", [])]))
        self.invalidate_rooms()

    def _room_state(self, room: RoomTarget) -> RoomState:
        estimate = self._estimates.get(room.area_id)
        if estimate and time.monotonic() - estimate[0] < _ESTIMATE_TTL:
            return estimate[1]
        ids = [*room.lights, *([room.group_entity] if room.group_entity else [])]
        states: dict[str, dict[str, Any]] = {}
        for entity_id in ids:
            if (s := self.hass.states.get(entity_id)) is not None:
                states[entity_id] = {"state": s.state, "attributes": dict(s.attributes)}
        return room_state_from(states, room)

    # --- Ereignisse der Bridge ------------------------------------------------------

    @callback
    def _on_bridge(self, kind: str, payload: Any) -> None:
        if kind in ("devices", "groups"):
            self.invalidate_rooms()
            self.motion.sync()
            return
        if kind != "device_state":
            return
        self.motion.handle_device_state(payload["ieee"], payload.get("values") or {})
        gesture = normalize_action(payload.get("values"))
        if gesture is None:
            return
        ieee = payload["ieee"]
        automation = device_owner(self.automations, ieee, type="room_knob")
        if automation is None:
            return
        self.handle_gesture(automation, ieee, gesture)

    @callback
    def handle_gesture(self, automation: dict[str, Any], ieee: str | None, gesture: Gesture, source: str = "device") -> None:
        """Geste einer Automation: entprellen (Drehen), planen, senden, Ereignis feuern."""
        room = self.room_targets().get(automation["room_area_id"])
        self._record(automation, gesture, room, ieee, source)
        if room is None:
            _LOGGER.warning("Haven-Automation %s: Raum %s unbekannt", automation["id"], automation["room_area_id"])
            return
        if gesture.kind != "rotate":
            self._execute(automation, gesture, room)
            return
        key = automation["room_area_id"]
        now = self.hass.loop.time()
        steps = self._coalescer.push(key, gesture.direction * gesture.steps, now)
        if steps is not None:
            self._execute(automation, _rotation(steps), room)
            return
        self._pending_rotate[key] = (automation["id"], ieee or "")
        if key not in self._timers and (delay := self._coalescer.delay(key, now)) is not None:
            self._timers[key] = self.hass.loop.call_later(delay, self._flush_rotate, key)

    @callback
    def _flush_rotate(self, key: str) -> None:
        self._timers.pop(key, None)
        now = self.hass.loop.time()
        steps = self._coalescer.flush(key, now)
        automation_id, _ = self._pending_rotate.pop(key, ("", ""))
        automation = self.get(automation_id)
        if steps is not None and automation is not None:
            room = self.room_targets().get(automation["room_area_id"])
            if room is not None:
                self._execute(automation, _rotation(steps), room)
        elif (delay := self._coalescer.delay(key, now)) is not None:
            self._timers[key] = self.hass.loop.call_later(delay, self._flush_rotate, key)

    def plan(self, automation: dict[str, Any], gesture: Gesture, room: RoomTarget) -> Plan:
        base = self.bridge.base_topic if self.bridge is not None else "zigbee2mqtt"
        return plan_gesture(
            automation,
            gesture,
            room,
            self._room_state(room),
            base_topic=base,
            last_scene=self.runtime["last_scene"].get(room.area_id),
        )

    @callback
    def _execute(self, automation: dict[str, Any], gesture: Gesture, room: RoomTarget) -> Plan:
        plan = self.plan(automation, gesture, room)
        if plan.expected is not None:
            self._estimates[room.area_id] = (time.monotonic(), plan.expected)
        if plan.scene is not None:
            self.runtime["last_scene"][room.area_id] = plan.scene
            self._schedule_save()
        if not plan.empty:
            self.hass.async_create_task(self._async_send(plan))
            # Bewegungslicht im selben Raum: der Mensch hat das Licht übernommen
            self.motion.handle_knob(room.area_id, plan.expected.on if plan.expected is not None else None)
        return plan

    async def _async_send(self, plan: Plan, context: Context | None = None) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        for topic, payload in plan.mqtt:
            try:
                await mqtt.async_publish(self.hass, topic, mqtt_payload(payload), qos=0, retain=False)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Haven-Automation: MQTT-Senden auf %s fehlgeschlagen", topic)
        for domain, service, data in plan.services:
            try:
                await self.hass.services.async_call(domain, service, data, blocking=False, context=context)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Haven-Automation: %s.%s fehlgeschlagen", domain, service)

    def _record(
        self, automation: dict[str, Any], gesture: Gesture, room: RoomTarget | None, ieee: str | None, source: str
    ) -> None:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.runtime["last_triggered"][automation["id"]] = {"at": at, "action": gesture.name}
        self._schedule_save()
        self.hass.bus.async_fire(
            EVENT_AUTOMATION_TRIGGERED,
            event_data(automation, gesture, room, device=ieee, source=source, at=at),
        )

    def test(self, automation_id: str, gesture: Gesture) -> Plan:
        """Geste testweise auslösen (wie vom Gerät, ohne Entprellen)."""
        automation = self.get(automation_id)
        if automation is None:
            raise Z2MError("not_found", f"Unbekannte Automation {automation_id}")
        if automation.get("type") == "room_motion":
            raise Z2MError("invalid_format", "Bewegungslicht testet man mit motion, clear oder takeover")
        room = self.room_targets().get(automation["room_area_id"])
        self._record(automation, gesture, room, None, "test")
        if room is None:
            raise Z2MError("not_found", f"Unbekannter Raum {automation['room_area_id']}")
        return self._execute(automation, gesture, room)


def _rotation(steps: int) -> Gesture:
    return Gesture("rotate", 1 if steps > 0 else -1, abs(steps), "coalesced")


def get_engine(hass: HomeAssistant) -> AutomationEngine:
    if _KEY not in hass.data:
        hass.data[_KEY] = AutomationEngine(hass)
    return hass.data[_KEY]


async def async_setup_automations(hass: HomeAssistant, bridge: Z2MBridge) -> Callable[[], None]:
    """Beim Laden des Entries: Store lesen, an die Bridge hängen. Liefert die Abmeldung."""
    engine = get_engine(hass)
    await engine.async_load()
    await engine.async_refresh_excluded()
    engine.attach(bridge)

    async def _on_data_updated(event: Any) -> None:
        if event.data.get("kind") == "prefs":
            await engine.async_refresh_excluded()

    remove_bus = hass.bus.async_listen(EVENT_DATA_UPDATED, _on_data_updated)

    @callback
    def _unload() -> None:
        remove_bus()
        engine.detach()

    return _unload

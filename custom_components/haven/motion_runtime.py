"""Laufzeit des Bewegungslichts (``room_motion``) in Home Assistant.

Die Logik liegt HA-frei in ``motion.py`` (Besitz-Automat ``step``). Hier passiert nur:

- Melder: direkt aus den Z2M-Meldungen (``presence``/``occupancy``, über die Bridge wie bei den
  Reglern). Die HA-Entities (``binary_sensor`` je IEEE) dienen nur noch der Anzeige und als
  Rückfall beim Start, wenn Z2M noch nichts gemeldet hat,
- State-Abos auf die Lichter des Raums (Gruppe und Lampen),
- der Zeitgeber je Automation (``async_call_later``),
- Befehle senden (MQTT auf die Raumgruppe, sonst HA-Dienste mit eigenem ``Context``),
- Ereignis ``haven_automation_triggered`` und der Zustand im Store (``runtime.motion``).

Eigene Änderungen erkennt der Automat am Erwartungsfenster und am Kontext: Dienstaufrufe der Engine
tragen einen eigenen ``Context``, ein ``state_changed`` mit fremdem ``context.user_id`` gilt als manuell.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .automations import Plan, RoomState, RoomTarget, room_state_from
from .commands import Z2MError
from . import motion as mo

if TYPE_CHECKING:
    from .automations_runtime import AutomationEngine

_LOGGER = logging.getLogger(__name__)

EVENT_AUTOMATION_TRIGGERED = "haven_automation_triggered"
_RETRY_SECONDS = 30
_MAX_RETRIES = 20
TEST_ACTIONS = ("motion", "clear", "takeover")


def light_color(attrs: dict[str, Any]) -> tuple[Any, ...] | None:
    """Farbe eines Lichts für den Vergleich: ``("ct", mired)``, ``("hs", h, s)`` oder ``("xy", x, y)``."""
    mode = attrs.get("color_mode")
    try:
        if mode == "color_temp" and attrs.get("color_temp") is not None:
            return ("ct", float(attrs["color_temp"]))
        if mode == "hs" and attrs.get("hs_color"):
            return ("hs", *map(float, attrs["hs_color"][:2]))
        if mode == "xy" and attrs.get("xy_color"):
            return ("xy", *map(float, attrs["xy_color"][:2]))
    except (TypeError, ValueError):
        return None
    return None


class MotionRunner:
    """Hält Zustand und Abos aller Bewegungslichter."""

    def __init__(self, engine: AutomationEngine) -> None:
        self.engine = engine
        self.hass: HomeAssistant = engine.hass
        self.states: dict[str, mo.MotionState] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._timers: dict[str, Callable[[], None]] = {}
        self._resolved: dict[str, dict[str, Any]] = {}  # id -> {detectors, lux}
        self._own: deque[str] = deque(maxlen=64)
        self._z2m: dict[str, dict[str, bool]] = {}  # id -> {ieee: aktiv} aus Z2M-Meldungen
        self._retry: Callable[[], None] | None = None
        self._retries = 0

    # --- Lebenszyklus ---------------------------------------------------------------

    def automations(self) -> list[dict[str, Any]]:
        return [a for a in self.engine.automations if a.get("type") == "room_motion"]

    @callback
    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        if self._retry:
            self._retry()
            self._retry = None

    @callback
    def sync(self) -> None:
        """Abos, Melder-Entities und Zeitgeber neu aufbauen (nach Laden, Speichern, Löschen)."""
        self.stop()
        saved = self.engine.runtime.setdefault("motion", {})
        active = {a["id"]: a for a in self.automations() if a.get("enabled", True)}
        for gone in [k for k in self.states if k not in active]:
            self.states.pop(gone, None)
            saved.pop(gone, None)
        for k in [k for k in saved if k not in {a["id"] for a in self.automations()}]:
            saved.pop(k, None)
        if self.engine.bridge is None or not active:
            return
        from .websocket_api import _ha_info  # noqa: PLC0415

        ha = _ha_info(self.hass)
        rooms = self.engine.room_targets(max_age=0)
        unresolved = False
        for aid, automation in active.items():
            state = self.states.get(aid) or mo.MotionState.from_dict(saved.get(aid))
            self.states[aid] = state
            entities = sorted({e for ieee in automation["devices"] for e in (ha.get(ieee) or {}).get("ha_entities", [])})
            snapshot = {e: {"attributes": dict(s.attributes)} for e in entities if (s := self.hass.states.get(e))}
            detectors = [
                d for ieee in automation["devices"]
                if (d := mo.pick_detector_entity(snapshot, (ha.get(ieee) or {}).get("ha_entities", [])))
            ]
            lux_entity = (automation["options"].get("lux") or {}).get("entity")
            if not lux_entity:
                for ieee in automation["devices"]:
                    lux_entity = mo.pick_lux_entity(snapshot, (ha.get(ieee) or {}).get("ha_entities", []))
                    if lux_entity:
                        break
            self._resolved[aid] = {"detectors": detectors, "lux": lux_entity}
            unresolved = unresolved or len(detectors) < len(automation["devices"])
            room = rooms.get(automation["room_area_id"])
            if room is not None:
                lights = [*room.lights, *([room.group_entity] if room.group_entity else [])]
                if lights:
                    self._unsubs.append(
                        async_track_state_change_event(self.hass, lights, lambda e, aid=aid: self._on_light(aid, e))
                    )
            self._restore(aid, automation, room, detectors)
        if unresolved and self._retries < _MAX_RETRIES:
            self._retries += 1
            self._retry = async_call_later(self.hass, _RETRY_SECONDS, lambda _now: self.sync())
        elif not unresolved:
            self._retries = 0

    def _restore(self, aid: str, automation: dict[str, Any], room: RoomTarget | None, detectors: list[str]) -> None:
        """Nach Neustart: Melderzustand lesen, Nachlauf neu ansetzen, Zeitgeber setzen."""
        state = self.states[aid]
        state.occupied = any(self._device_active(aid, ieee) for ieee in automation["devices"]) or any(
            mo.detector_active((s := self.hass.states.get(d)) and s.state) for d in detectors
        )
        if room is not None and state.owner == "manual" and self.engine._room_state(room).on is False:  # noqa: SLF001
            # Licht ist aus (z. B. während des Neustarts ausgeschaltet): niemandem mehr
            state.owner, state.deadline, state.warn_at, state.warned, state.applied = "none", None, None, False, None
            self.engine.runtime.setdefault("motion", {})[aid] = state.to_dict()
            self.engine._schedule_save()  # noqa: SLF001
        if room is not None and state.owner == "motion" and not state.occupied:
            self._apply(automation, room, mo.MotionEvent("clear"), source="restore")
        else:
            self._schedule(aid)

    # --- Umgebung -------------------------------------------------------------------

    def _env(self, automation: dict[str, Any], room: RoomTarget) -> mo.MotionEnv:
        light: RoomState = self.engine._room_state(room)  # noqa: SLF001
        lux_entity = (self._resolved.get(automation["id"]) or {}).get("lux")
        lux = mo.parse_lux(s.state) if lux_entity and (s := self.hass.states.get(lux_entity)) else None
        now = dt_util.now()
        sunrise = sunset = None
        try:
            from homeassistant.helpers.sun import get_astral_event_date  # noqa: PLC0415

            sunrise = get_astral_event_date(self.hass, "sunrise", now.date())
            sunset = get_astral_event_date(self.hass, "sunset", now.date())
        except Exception:  # noqa: BLE001 - ohne Standort keine Sonnenzeiten
            pass
        return mo.MotionEnv(now, light.on, light.brightness, lux, sunrise, sunset)

    # --- Ereignisse -------------------------------------------------------------------

    def _context(self, aid: str) -> tuple[dict[str, Any], mo.MotionState] | None:
        automation = self.engine.get(aid)
        state = self.states.get(aid)
        if automation is None or state is None or not automation.get("enabled", True):
            return None
        return automation, state

    def _device_active(self, aid: str, ieee: str) -> bool:
        """Letzter bekannter Melderzustand eines Geräts (Z2M), ``False`` wenn unbekannt."""
        known = (self._z2m.get(aid) or {}).get(ieee)
        if known is not None:
            return known
        bridge = self.engine.bridge
        cached = bridge.state_cache.states.get(ieee) if bridge is not None else None
        return bool(mo.detector_value(cached))

    @callback
    def handle_device_state(self, ieee: str, values: dict[str, Any]) -> None:
        """Z2M-Meldung eines Geräts: bei Melderwechsel ``motion`` bzw. ``clear`` an die Automationen."""
        active = mo.detector_value(values)
        if active is None:
            return
        for automation in self.automations():
            aid = automation["id"]
            if ieee not in automation["devices"] or self._context(aid) is None:
                continue
            seen = self._z2m.setdefault(aid, {})
            changed = seen.get(ieee) is not active
            seen[ieee] = active
            # Z2M sendet bei jeder Meldung (z. B. Helligkeit) den ganzen Zustand: nur Wechsel zählen
            if not changed:
                continue
            # Mehrere Melder: aktiv, solange einer aktiv ist
            if not active and any(self._device_active(aid, d) for d in automation["devices"] if d != ieee):
                continue
            room = self.engine.room_targets().get(automation["room_area_id"])
            if room is not None:
                self._apply(automation, room, mo.MotionEvent("motion" if active else "clear"), source="device")

    @callback
    def _on_light(self, aid: str, event: Event) -> None:
        ctx = self._context(aid)
        new = event.data.get("new_state")
        if ctx is None or new is None or new.state not in ("on", "off"):
            return
        automation, _ = ctx
        room = self.engine.room_targets().get(automation["room_area_id"])
        if room is None:
            return
        # Zustand des Raums aus HA (nicht aus der Schätzung der Knob-Engine)
        ids = [*room.lights, *([room.group_entity] if room.group_entity else [])]
        snap = {e: {"state": s.state, "attributes": dict(s.attributes)} for e in ids if (s := self.hass.states.get(e))}
        rs = room_state_from(snap, room)
        main = snap.get(room.group_entity) if room.group_entity else None
        attrs = (main or {"attributes": dict(new.attributes)})["attributes"]
        context = new.context
        own = context.id in self._own or (context.parent_id in self._own if context.parent_id else False)
        user = bool(context.user_id) and not own
        self._apply(
            automation,
            room,
            mo.MotionEvent("light", rs.on, rs.brightness if rs.on else None, light_color(attrs) if rs.on else None, own, user),
            source="device",
            quiet=True,
        )

    @callback
    def handle_knob(self, area_id: str, light_on: bool | None) -> None:
        """Ein Haven-Regler im selben Raum hat das Licht geändert: Übernahme direkt melden."""
        room = self.engine.room_targets().get(area_id)
        if room is None:
            return
        for automation in self.automations():
            if automation.get("enabled", True) and automation["room_area_id"] == area_id and automation["id"] in self.states:
                self._apply(automation, room, mo.MotionEvent("knob", light_on), source="knob", quiet=True)

    @callback
    def _on_timer(self, aid: str, _now: Any) -> None:
        self._timers.pop(aid, None)
        ctx = self._context(aid)
        if ctx is None:
            return
        automation, _ = ctx
        room = self.engine.room_targets().get(automation["room_area_id"])
        if room is not None:
            self._apply(automation, room, mo.MotionEvent("timer"), source="timer", quiet=True)

    # --- Anwenden ---------------------------------------------------------------------

    def _apply(
        self,
        automation: dict[str, Any],
        room: RoomTarget,
        event: mo.MotionEvent,
        *,
        source: str,
        quiet: bool = False,
    ) -> list[Plan]:
        aid = automation["id"]
        state = self.states.setdefault(aid, mo.MotionState())
        env = self._env(automation, room)
        new, commands = mo.step(automation["options"], state, event, env)
        self.states[aid] = new
        self.engine.runtime.setdefault("motion", {})[aid] = new.to_dict()
        if new != state:
            self.engine._schedule_save()  # noqa: SLF001
        plans = self._send(room, commands, env)
        self._schedule(aid)
        for cmd in commands:
            if cmd.kind == "notify":
                self._fire(automation, room, cmd.action or "", source)
        return plans

    def _send(self, room: RoomTarget, commands: list[mo.Command], env: mo.MotionEnv) -> list[Plan]:
        base = self.engine.bridge.base_topic if self.engine.bridge is not None else "zigbee2mqtt"
        context = Context()
        self._own.append(context.id)
        plans: list[Plan] = []
        for cmd in commands:
            if cmd.kind == "notify":
                continue
            plan = mo.plan_command(cmd, room, self.engine._room_state(room), base)  # noqa: SLF001
            if plan.expected is not None:
                self.engine._estimates[room.area_id] = (time.monotonic(), plan.expected)  # noqa: SLF001
            if plan.scene is not None:
                self.engine.runtime["last_scene"][room.area_id] = plan.scene
            if not plan.empty:
                self.hass.async_create_task(self.engine._async_send(plan, context))  # noqa: SLF001
            plans.append(plan)
        return plans

    def _schedule(self, aid: str) -> None:
        if cancel := self._timers.pop(aid, None):
            cancel()
        state = self.states.get(aid)
        wake = mo.next_wake(state) if state else None
        if wake is None:
            return
        delay = max(0.0, wake - time.time())
        self._timers[aid] = async_call_later(self.hass, delay + 0.05, lambda now, aid=aid: self._on_timer(aid, now))

    def _fire(self, automation: dict[str, Any], room: RoomTarget, action: str, source: str) -> None:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.engine.runtime["last_triggered"][automation["id"]] = {"at": at, "action": action}
        self.hass.bus.async_fire(
            EVENT_AUTOMATION_TRIGGERED,
            {
                "id": automation["id"],
                "action": action,
                "room": automation["room_area_id"],
                "room_name": room.name,
                "device": None,
                "source": source,
                "at": at,
                "state": self.status(automation, room),
            },
        )

    # --- Abfragen ---------------------------------------------------------------------

    def status(self, automation: dict[str, Any], room: RoomTarget | None = None) -> dict[str, Any]:
        """Zustand für die App (Liste und Ereignis)."""
        room = room or self.engine.room_targets().get(automation["room_area_id"])
        state = self.states.get(automation["id"]) or mo.MotionState()
        resolved = self._resolved.get(automation["id"]) or {}
        base = {"detectors": resolved.get("detectors", []), "lux_entity": resolved.get("lux")}
        if room is None:
            return {**base, "owner": state.owner, "phase": "idle", "off_at": None, "inactive": None}
        out = mo.status(automation["options"], state, self._env(automation, room))
        if out.get("off_at") is not None:
            out["off_at"] = datetime.fromtimestamp(out["off_at"], timezone.utc).isoformat(timespec="seconds")
        return {**base, **out}

    def test(self, automation: dict[str, Any], action: str) -> tuple[Plan, dict[str, Any]]:
        """Auslösen wie vom Melder (``motion``, ``clear``) bzw. Übernahme erzwingen (``takeover``)."""
        room = self.engine.room_targets().get(automation["room_area_id"])
        if room is None:
            raise Z2MError("not_found", f"Unbekannter Raum {automation['room_area_id']}")
        aid = automation["id"]
        self.states.setdefault(aid, mo.MotionState())
        plans = self._apply(automation, room, mo.MotionEvent(action), source="test")
        plan = next((p for p in plans if not p.empty), plans[0] if plans else Plan(note="nichts zu tun"))
        if not plans:
            status = self.status(automation, room)
            plan.note = {"idle": "Bedingungen nicht erfüllt", "inactive": "Inaktiv"}.get(status["phase"], "kein Befehl nötig")
            if status.get("inactive"):
                plan.note = {"time": "außerhalb des Zeitfensters", "sun": "außerhalb des Sonnenfensters", "lux": "zu hell"}[status["inactive"]]
        return plan, self.status(automation, room)

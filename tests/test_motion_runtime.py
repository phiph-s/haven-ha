"""Laufzeit des Bewegungslichts (``motion_runtime.py``) mit nachgebautem Home Assistant.

HA führt Rückrufe, die nicht mit ``@callback`` markiert sind, im Thread-Pool aus. Aus einem fremden
Thread lehnt HA ``hass.async_create_task`` und ``hass.bus.async_fire`` mit ``RuntimeError`` ab. Der
Nachbau hier verhält sich genauso: So bleibt sichtbar, wenn ein Zeitgeber oder ein State-Abo wieder
als nackte Funktion übergeben wird. Dann schaltete der Automat auf „niemand“, das Aus kam aber nie
beim Licht an und die App blieb bei „Licht vom Melder, Bewegung erkannt“.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib
import sys
import threading
import types
from typing import Any

import pytest

auto = importlib.import_module("haven_nohass.automations")
mo = importlib.import_module("haven_nohass.motion")

PIR = "0x00000000000000a1"
RADAR = "0x00000000000000a2"
ROOM = auto.RoomTarget("waschraum", "Waschraum", group="ZGWaschraum", group_entity="light.zgwaschraum", lights=["light.lampe_waschraum"])


# --- Nachbau von Home Assistant ------------------------------------------------------------


def _callback(func):
    func._hass_callback = True  # noqa: SLF001 - wie homeassistant.core.callback
    return func


def _is_callback(func) -> bool:
    return getattr(func, "_hass_callback", False)


class FakeContext:
    _n = 0

    def __init__(self, user_id: str | None = None, parent_id: str | None = None) -> None:
        FakeContext._n += 1
        self.id = f"ctx{FakeContext._n}"
        self.user_id = user_id
        self.parent_id = parent_id


class FakeState:
    def __init__(self, state: str, attributes: dict[str, Any] | None = None, context: FakeContext | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}
        self.context = context or FakeContext()


class FakeEvent:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class FakeBus:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.hass.check_loop("hass.bus.async_fire")
        self.fired.append((event_type, data))


class FakeHass:
    """Ereignisschleife = Testthread. Nicht markierte Rückrufe laufen wie in HA in einem Worker-Thread."""

    def __init__(self) -> None:
        self.loop_thread = threading.current_thread()
        self.bus = FakeBus(self)
        self.states = types.SimpleNamespace(get=self._get)
        self._states: dict[str, FakeState] = {}
        self.timers: list[dict[str, Any]] = []
        self.trackers: list[tuple[list[str], Any]] = []
        self.tasks: list[Any] = []
        self.errors: list[BaseException] = []

    def _get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    def check_loop(self, what: str) -> None:
        if threading.current_thread() is not self.loop_thread:
            raise RuntimeError(f"calls {what} from a thread other than the event loop")

    def async_create_task(self, coro: Any) -> None:
        if threading.current_thread() is not self.loop_thread:
            coro.close()
            raise RuntimeError("calls hass.async_create_task from a thread other than the event loop")
        self.tasks.append(coro)

    def run_job(self, func: Any, *args: Any) -> None:
        """Wie ``hass.async_run_hass_job``: Callback direkt, sonst im Executor (Fehler landen im Log)."""
        if _is_callback(func):
            func(*args)
            return

        def _worker() -> None:
            try:
                func(*args)
            except BaseException as err:  # noqa: BLE001 - HA: „Future exception was never retrieved“
                self.errors.append(err)

        t = threading.Thread(target=_worker)
        t.start()
        t.join()

    def drain(self) -> None:
        """Gesendete Befehle (``engine._async_send``) ausführen."""
        while self.tasks:
            asyncio.run(self.tasks.pop(0))

    def set_light(self, on: bool, brightness: int | None = 204, user_id: str | None = None) -> None:
        """Licht des Raums setzen und die State-Abos auslösen."""
        attrs = {"brightness": brightness, "color_mode": "brightness"} if on else {}
        ctx = FakeContext(user_id=user_id)
        for eid in [ROOM.group_entity, *ROOM.lights]:
            old = self._states.get(eid)
            new = FakeState("on" if on else "off", attrs, ctx)
            self._states[eid] = new
            for entities, action in list(self.trackers):
                if eid in entities:
                    self.run_job(action, FakeEvent({"entity_id": eid, "old_state": old, "new_state": new}))

    def fire_due_timers(self, now: float) -> None:
        for timer in [t for t in self.timers if not t["cancelled"] and t["at"] <= now]:
            timer["cancelled"] = True
            self.run_job(timer["action"], datetime.fromtimestamp(now, timezone.utc))


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


def _ha_modules(clock: Clock) -> dict[str, types.ModuleType]:
    core = types.ModuleType("homeassistant.core")
    core.Context = FakeContext
    core.Event = FakeEvent
    core.HomeAssistant = FakeHass
    core.callback = _callback

    event = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass: FakeHass, delay: float, action: Any):
        timer = {"at": clock.t + delay, "action": action, "cancelled": False}
        hass.timers.append(timer)

        def _cancel() -> None:
            timer["cancelled"] = True

        return _cancel

    def async_track_state_change_event(hass: FakeHass, entity_ids: list[str], action: Any):
        entry = (list(entity_ids), action)
        hass.trackers.append(entry)
        return lambda: hass.trackers.remove(entry) if entry in hass.trackers else None

    event.async_call_later = async_call_later
    event.async_track_state_change_event = async_track_state_change_event

    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = clock.now
    util = types.ModuleType("homeassistant.util")
    util.dt = dt
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.event = event
    ha = types.ModuleType("homeassistant")
    ha.core, ha.helpers, ha.util = core, helpers, util
    return {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
    }


class FakeEngine:
    def __init__(self, hass: FakeHass, automation: dict[str, Any]) -> None:
        self.hass = hass
        self.automations = [automation]
        self.runtime: dict[str, Any] = {"motion": {}, "last_triggered": {}, "last_scene": {}}
        self.bridge = types.SimpleNamespace(base_topic="zigbee2mqtt", state_cache=types.SimpleNamespace(states={}))
        self._estimates: dict[str, Any] = {}
        self.sent: list[Any] = []

    def get(self, aid: str) -> dict[str, Any] | None:
        return next((a for a in self.automations if a["id"] == aid), None)

    def room_targets(self, max_age: float | None = None) -> dict[str, Any]:
        return {ROOM.area_id: ROOM}

    def _room_state(self, room: Any) -> Any:
        snap = {e: {"state": s.state, "attributes": dict(s.attributes)} for e in [room.group_entity, *room.lights] if (s := self.hass.states.get(e))}
        return auto.room_state_from(snap, room)

    def _schedule_save(self) -> None:
        pass

    async def _async_send(self, plan: Any, context: Any) -> None:
        self.sent.append(plan)
        # Z2M schaltet die Gruppe: neuer Zustand kommt ohne HA-Kontext zurück
        if plan.action == "off":
            self.hass.set_light(False)
        elif plan.action == "on":
            self.hass.set_light(True, 204)


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    for name, module in _ha_modules(clock).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "haven_nohass.motion_runtime", raising=False)
    runtime = importlib.import_module("haven_nohass.motion_runtime")
    monkeypatch.setattr(runtime.time, "time", lambda: clock.t)
    yield runtime, clock
    sys.modules.pop("haven_nohass.motion_runtime", None)


def _setup(runtime, clock, devices=(RADAR,), **options):
    hass = FakeHass()
    automation = {
        "id": "m1",
        "type": "room_motion",
        "enabled": True,
        "room_area_id": ROOM.area_id,
        "devices": list(devices),
        "options": mo.normalize_motion_options({"delay": 30, **options}),
    }
    engine = FakeEngine(hass, automation)
    runner = runtime.MotionRunner(engine)
    runner.states["m1"] = mo.MotionState()
    hass.set_light(False)
    # wie ``sync``: State-Abo auf die Lichter des Raums
    lights = [*ROOM.lights, ROOM.group_entity]
    runtime.async_track_state_change_event(hass, lights, runtime.callback(lambda e: runner._on_light("m1", e)))  # noqa: SLF001
    return hass, engine, runner


def _advance(hass: FakeHass, clock: Clock, seconds: float) -> None:
    clock.t += seconds
    hass.fire_due_timers(clock.t)
    hass.drain()


def test_callbacks_handed_to_ha_are_marked(rig):
    """Zeitgeber und State-Abos müssen ``@callback`` sein, sonst laufen sie in HA im Thread-Pool."""
    runtime, clock = rig
    hass, engine, runner = _setup(runtime, clock)
    runner.handle_device_state(RADAR, {"presence": True})
    hass.drain()
    runner._retry = None  # noqa: SLF001
    runner._schedule("m1")  # noqa: SLF001
    live = [t for t in hass.timers if not t["cancelled"]]
    assert live and all(_is_callback(t["action"]) for t in live)

    hass.trackers.clear()
    engine.automations[0]["devices"] = []  # sync ohne Melder: kein _ha_info nötig
    websocket_api = types.ModuleType("haven_nohass.websocket_api")
    websocket_api._ha_info = lambda _hass: {}  # noqa: SLF001
    sys.modules["haven_nohass.websocket_api"] = websocket_api
    try:
        runner.sync()
    finally:
        sys.modules.pop("haven_nohass.websocket_api", None)
    assert hass.trackers and all(_is_callback(action) for _, action in hass.trackers)


def test_light_turns_off_after_presence_clears(rig):
    """Melder an -> Licht an, Melder aus -> nach ``delay`` Licht aus und Zustand ``idle``."""
    runtime, clock = rig
    hass, engine, runner = _setup(runtime, clock)

    runner.handle_device_state(RADAR, {"presence": True, "illuminance": 12})
    hass.drain()
    assert [p.action for p in engine.sent] == ["on"]
    assert runner.status(engine.automations[0])["phase"] == "motion"
    assert [d["action"] for _, d in hass.bus.fired] == ["motion_on"]

    _advance(hass, clock, 5)
    runner.handle_device_state(RADAR, {"presence": False, "illuminance": 12})
    status = runner.status(engine.automations[0])
    assert status["occupied"] is False and status["off_at"] is not None

    _advance(hass, clock, 31)
    assert hass.errors == []
    assert [p.action for p in engine.sent] == ["on", "off"]
    assert hass.states.get(ROOM.group_entity).state == "off"
    assert runner.states["m1"].owner == "none"
    assert runner.status(engine.automations[0])["phase"] == "idle"
    fired = [d for _, d in hass.bus.fired]
    assert [d["action"] for d in fired] == ["motion_on", "motion_off"]
    # Die App übernimmt den Zustand aus dem Ereignis: zuletzt „idle“, nicht „Bewegung erkannt“
    assert fired[-1]["state"]["phase"] == "idle" and fired[-1]["state"]["occupied"] is False


def test_two_detectors_off_only_when_both_clear(rig):
    runtime, clock = rig
    hass, engine, runner = _setup(runtime, clock, devices=(RADAR, PIR))

    runner.handle_device_state(PIR, {"occupancy": True})
    runner.handle_device_state(RADAR, {"presence": True})
    hass.drain()
    _advance(hass, clock, 10)
    runner.handle_device_state(PIR, {"occupancy": False})
    _advance(hass, clock, 60)
    assert [p.action for p in engine.sent] == ["on"]  # Radar sieht noch jemanden

    runner.handle_device_state(RADAR, {"presence": False})
    _advance(hass, clock, 31)
    assert hass.errors == []
    assert [p.action for p in engine.sent] == ["on", "off"]
    assert runner.status(engine.automations[0])["phase"] == "idle"


def test_takeover_from_light_change_fires_event(rig):
    """Licht von Hand verstellt: Übernahme läuft im Event-Loop und meldet ``takeover``."""
    runtime, clock = rig
    hass, engine, runner = _setup(runtime, clock)
    runner.handle_device_state(RADAR, {"presence": True})
    hass.drain()
    _advance(hass, clock, 5)
    hass.set_light(True, 60, user_id="someone")
    assert hass.errors == []
    assert runner.states["m1"].owner == "manual"
    assert [d["action"] for _, d in hass.bus.fired] == ["motion_on", "takeover"]

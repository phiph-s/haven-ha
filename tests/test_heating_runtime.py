"""Laufzeit der Heizungsregelung (``heating_runtime.py``) mit nachgebautem Home Assistant.

Wie beim Bewegungslicht: Der Nachbau führt nicht mit ``@callback`` markierte Rückrufe in einem
Worker-Thread aus und lehnt dort ``async_create_task``/``bus.async_fire`` ab. So fällt sofort auf, wenn ein
Zeitgeber oder State-Abo als nackte Funktion übergeben wird.
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

ht = importlib.import_module("haven_nohass.heating")

TRV_A = "0x00000000000000c1"
TRV_B = "0x00000000000000c2"
SENSOR = "sensor.thermometer_salon_temperature"
WINDOW = "binary_sensor.fenster_salon_contact"

EXPOSES = [
    {"type": "climate", "features": [
        {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 35, "value_step": 0.5},
        {"property": "local_temperature", "access": 1},
        {"property": "local_temperature_calibration", "access": 7, "value_min": -10, "value_max": 10, "value_step": 0.1},
    ]},
    {"type": "enum", "property": "preset", "access": 3, "values": ["off", "antifrost", "eco", "comfort", "auto", "on"]},
]


def _callback(func):
    func._hass_callback = True  # noqa: SLF001
    return func


def _is_callback(func) -> bool:
    return getattr(func, "_hass_callback", False)


class FakeState:
    def __init__(self, state: str, attributes: dict[str, Any] | None = None, when: datetime | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = self.last_reported = when


class FakeBus:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.hass.check_loop("hass.bus.async_fire")
        self.fired.append((event_type, data))


class FakeHass:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.loop_thread = threading.current_thread()
        self.bus = FakeBus(self)
        self._states: dict[str, FakeState] = {}
        self.states = types.SimpleNamespace(get=self._states.get)
        self.timers: list[dict[str, Any]] = []
        self.trackers: list[tuple[list[str], Any]] = []
        self.tasks: list[Any] = []
        self.errors: list[BaseException] = []
        self.data: dict[str, Any] = {}

    def check_loop(self, what: str) -> None:
        if threading.current_thread() is not self.loop_thread:
            raise RuntimeError(f"calls {what} from a thread other than the event loop")

    def async_create_task(self, coro: Any) -> None:
        if threading.current_thread() is not self.loop_thread:
            coro.close()
            raise RuntimeError("calls hass.async_create_task from a thread other than the event loop")
        self.tasks.append(coro)

    def run_job(self, func: Any, *args: Any) -> None:
        if _is_callback(func):
            func(*args)
            return

        def _worker() -> None:
            try:
                func(*args)
            except BaseException as err:  # noqa: BLE001
                self.errors.append(err)

        t = threading.Thread(target=_worker)
        t.start()
        t.join()

    def drain(self) -> None:
        while self.tasks:
            asyncio.run(self.tasks.pop(0))

    def set_state(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        old = self._states.get(entity_id)
        new = FakeState(state, attributes, self.clock.now())
        self._states[entity_id] = new
        for entities, action in list(self.trackers):
            if entity_id in entities:
                self.run_job(action, types.SimpleNamespace(data={"entity_id": entity_id, "old_state": old, "new_state": new}))

    def fire_due_timers(self) -> None:
        for timer in [t for t in self.timers if not t["cancelled"] and t["at"] <= self.clock.t]:
            timer["cancelled"] = True
            self.run_job(timer["action"], self.clock.now())


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


class FakeStore:
    def __init__(self, hass: Any, version: int, key: str) -> None:
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self.saved

    async def async_save(self, data: Any) -> None:
        self.saved = data

    def async_delay_save(self, func: Any, delay: float) -> None:
        self.saved = func()


class FakeBridge:
    def __init__(self) -> None:
        self.devices = {
            ieee: {"ieee_address": ieee, "friendly_name": name, "definition": {"exposes": EXPOSES}}
            for ieee, name in ((TRV_A, "Heizung Salon"), (TRV_B, "Heizung Salon 2"))
        }
        self.devices_received = True
        self.availability: dict[str, str] = {}
        self.state_cache = types.SimpleNamespace(states={})
        self.listeners: list[Any] = []
        self.published: list[tuple[str, dict[str, Any]]] = []

    def add_listener(self, listener: Any):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def device_type(self, ieee: str) -> str:
        return "thermostat" if ieee in self.devices else "unknown"

    async def async_publish_set(self, name: str, values: dict[str, Any]) -> None:
        self.published.append((name, values))

    def report(self, ieee: str, **values: Any) -> None:
        """Z2M-Meldung: ganzer Zustand wie bei Z2M, dann an die Listener."""
        state = {**self.state_cache.states.get(ieee, {}), **values}
        self.state_cache.states[ieee] = state
        for listener in list(self.listeners):
            listener("device_state", {"ieee": ieee, "values": state})


def _ha_modules(clock: Clock) -> dict[str, types.ModuleType]:
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = FakeHass
    core.callback = _callback
    event = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass: FakeHass, delay: float, action: Any):
        timer = {"at": clock.t + delay, "action": action, "cancelled": False}
        hass.timers.append(timer)
        return lambda: timer.update(cancelled=True)

    def async_track_state_change_event(hass: FakeHass, entity_ids: list[str], action: Any):
        entry = (list(entity_ids), action)
        hass.trackers.append(entry)
        return lambda: hass.trackers.remove(entry) if entry in hass.trackers else None

    event.async_call_later = async_call_later
    event.async_track_state_change_event = async_track_state_change_event
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = FakeStore
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.event, helpers.storage = event, storage
    ha = types.ModuleType("homeassistant")
    ha.core, ha.helpers = core, helpers
    return {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.storage": storage,
    }


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    for name, module in _ha_modules(clock).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "haven_nohass.heating_runtime", raising=False)
    runtime = importlib.import_module("haven_nohass.heating_runtime")
    monkeypatch.setattr(runtime.time, "time", lambda: clock.t)
    hass = FakeHass(clock)
    bridge = FakeBridge()
    runner = runtime.HeatingRunner(hass)
    yield runtime, clock, hass, bridge, runner
    sys.modules.pop("haven_nohass.heating_runtime", None)


def _advance(hass: FakeHass, clock: Clock, seconds: float) -> None:
    clock.t += seconds
    hass.fire_due_timers()
    hass.drain()


def _setup(runner, hass, bridge, **config):
    hass.set_state(SENSOR, "20.0")
    hass.set_state(WINDOW, "off")
    bridge.report(TRV_A, local_temperature=24.0, current_heating_setpoint=21.0, local_temperature_calibration=0, preset="comfort")
    asyncio.run(runner.async_load())
    runner.attach(bridge)
    doc = {"area_id": "salon", "trvs": [TRV_A], "sensors": [SENSOR], "windows": [WINDOW], **config}
    asyncio.run(runner.async_set(doc, {"salon"}))
    hass.drain()


def test_callbacks_handed_to_ha_are_marked(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge)
    assert hass.trackers and all(_is_callback(a) for _, a in hass.trackers)
    hass.set_state(WINDOW, "on")
    live = [t for t in hass.timers if not t["cancelled"]]
    assert live and all(_is_callback(t["action"]) for t in live)
    assert hass.errors == []


def test_calibration_sent_and_status_event(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge)
    assert bridge.published == [("Heizung Salon", {"local_temperature_calibration": -4.0})]
    fired = [d for e, d in hass.bus.fired if e == runtime.EVENT_HEATING_UPDATED]
    assert fired and fired[-1]["area_id"] == "salon"
    status = fired[-1]["status"]
    assert status["room_temperature"] == 20.0 and status["trvs"][0]["correction"] == -4.0
    assert runner.list_public()[0]["config"]["trvs"] == [TRV_A]


def test_window_pause_through_state_change_and_timer(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge, calibration="none", window_open_delay=60, window_close_delay=120)
    bridge.published.clear()
    hass.set_state(WINDOW, "on")
    hass.drain()
    assert bridge.published == []
    _advance(hass, clock, 61)
    assert hass.errors == []
    assert bridge.published == [("Heizung Salon", {"preset": "off"})]
    assert runner.status("salon")["reason"] == "window"
    bridge.report(TRV_A, preset="off")
    hass.set_state(WINDOW, "off")
    status = runner.status("salon")
    assert status["window"]["resume_at"] is not None
    _advance(hass, clock, 121)
    assert bridge.published[-1] == ("Heizung Salon", {"preset": "comfort"})
    assert runner.status("salon")["reason"] is None


def test_knob_on_device_reaches_second_trv(rig):
    runtime, clock, hass, bridge, runner = rig
    bridge.report(TRV_B, local_temperature=23.0, current_heating_setpoint=21.0, local_temperature_calibration=0, preset="comfort")
    _setup(runner, hass, bridge, calibration="none", trvs=[TRV_A, TRV_B])
    bridge.published.clear()
    clock.t += 300
    bridge.report(TRV_A, current_heating_setpoint=23.0)
    hass.drain()
    assert runner.states["salon"].target == 23.0
    assert bridge.published == [("Heizung Salon 2", {"current_heating_setpoint": 23.0})]


def test_control_and_tick(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge, calibration="none")
    status = asyncio.run(runner.async_control("salon", mode="eco"))
    hass.drain()
    assert status["mode"] == "eco" and status["effective_target"] == 18.0
    assert bridge.published[-1] == ("Heizung Salon", {"current_heating_setpoint": 18.0})
    # unbestätigt: der Takt schickt es nach RESEND erneut
    _advance(hass, clock, ht.RESEND + runtime.TICK_SECONDS)
    assert bridge.published[-1] == ("Heizung Salon", {"current_heating_setpoint": 18.0})
    assert len([p for p in bridge.published if p[1] == {"current_heating_setpoint": 18.0}]) == 2
    assert hass.errors == []


def test_delete_resets_calibration(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge)
    assert asyncio.run(runner.async_delete("salon")) is True
    hass.drain()
    assert bridge.published[-1] == ("Heizung Salon", {"local_temperature_calibration": 0})
    assert runner.list_public() == [] and hass.trackers == []


def test_open_valve_on_device_is_released_and_resumed_from_app(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge, calibration="none")
    bridge.published.clear()
    clock.t += 100
    bridge.report(TRV_A, preset="on")
    hass.drain()
    assert bridge.published == []
    assert runner.status("salon")["released"] is True
    # Ende der Freigabe ist als Wecker geplant
    assert any(not t["cancelled"] for t in hass.timers)
    status = asyncio.run(runner.async_control("salon", resume=True))
    hass.drain()
    assert status["released"] is False
    assert bridge.published[-1] == ("Heizung Salon", {"preset": "comfort"})
    assert hass.errors == []


# --- Display ---------------------------------------------------------------------------------

DISPLAY = "0x00000000000000d1"
DISPLAY_EXPOSES = [
    {"type": "climate", "features": [
        {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 35, "value_step": 0.5},
        {"property": "local_temperature", "access": 1},
        {"property": "system_mode", "access": 7, "values": ["off", "heat"]},
        {"property": "preset", "access": 7, "values": ["manual", "auto"]},
        {"property": "running_state", "access": 1, "values": ["idle", "heat"]},
        {"property": "local_temperature_calibration", "access": 7, "value_min": -9, "value_max": 9, "value_step": 1},
    ]},
]


def _with_display(bridge) -> None:
    bridge.devices[DISPLAY] = {
        "ieee_address": DISPLAY, "friendly_name": "Wand Salon", "definition": {"exposes": DISPLAY_EXPOSES},
    }
    bridge.report(DISPLAY, local_temperature=22.0, current_heating_setpoint=21.0, system_mode="heat", preset="manual",
                  running_state="heat", local_temperature_calibration=0)


def test_display_mark_errors(rig):
    runtime, clock, hass, bridge, runner = rig
    _setup(runner, hass, bridge, calibration="none")
    with pytest.raises(runtime.Z2MError) as err:
        asyncio.run(runner.async_display("0xunknown", True))
    assert err.value.code == "not_found"
    with pytest.raises(runtime.Z2MError) as err:
        asyncio.run(runner.async_display(TRV_A, True))
    assert err.value.code == "device_taken" and "Heizung Salon wird als Heizkörperthermostat" in err.value.message
    # nicht markiert: als Display eines Raums abgelehnt
    _with_display(bridge)
    with pytest.raises(runtime.Z2MError) as err:
        asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A], "display": DISPLAY}, {"salon"}))
    assert err.value.code == "invalid_format"
    # Geräteliste noch nicht da: Markieren ohne Prüfung
    bridge.devices_received = False
    assert asyncio.run(runner.async_display("0xlater", True)) == {"displays": ["0xlater"]}


def test_display_room_end_to_end(rig):
    runtime, clock, hass, bridge, runner = rig
    _with_display(bridge)
    _setup(runner, hass, bridge, calibration="none")
    assert asyncio.run(runner.async_display(DISPLAY, True)) == {"displays": [DISPLAY]}
    assert asyncio.run(runner.async_display(DISPLAY, True)) == {"displays": [DISPLAY]}  # doppelt: bleibt einmal
    # markiertes Display darf nicht als TRV in einen Raum
    with pytest.raises(runtime.Z2MError) as err:
        asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A, DISPLAY]}, {"salon"}))
    assert err.value.code == "device_taken"
    hass.set_state(SENSOR, "unavailable")
    bridge.published.clear()
    result = asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A], "sensors": [SENSOR], "display": DISPLAY}, {"salon"}))
    hass.drain()
    assert result["config"]["display"] == DISPLAY
    status = result["status"]
    assert status["room_source"] == "display" and status["room_temperature"] == 22.0
    assert status["display"]["name"] == "Wand Salon" and status["display"]["setpoint"] == 21.0
    # TRV misst 24, Display 22: TRV wird nach dem Display korrigiert (geglättet), das Display nie kalibriert
    assert status["trvs"][0]["method"] == "offset" and status["trvs"][0]["correction"] == -2.0
    assert [n for n, v in bridge.published if "local_temperature_calibration" in v] == ["Heizung Salon"]
    assert all(name != "Wand Salon" for name, _ in bridge.published)
    # Drehen am Display: Ziel des Raums, das TRV folgt
    bridge.published.clear()
    clock.t += 300
    bridge.report(DISPLAY, current_heating_setpoint=23.5)
    hass.drain()
    assert runner.states["salon"].target == 23.5
    assert bridge.published == [("Heizung Salon", {"current_heating_setpoint": 23.5})]
    # Aus am Display: Raum aus
    clock.t += 60
    bridge.report(DISPLAY, system_mode="off")
    hass.drain()
    assert runner.states["salon"].mode == "off"
    assert bridge.published[-1] == ("Heizung Salon", {"preset": "off"})
    # Aus der App wieder an: das Display wird eingeschaltet und zeigt das Ziel
    bridge.report(TRV_A, preset="off")
    asyncio.run(runner.async_control("salon", mode="eco"))
    hass.drain()
    assert ("Wand Salon", {"system_mode": "heat", "current_heating_setpoint": 18.0}) in bridge.published
    # Store kennt die Displays
    assert asyncio.run(runner._store.async_load())["displays"] == [DISPLAY]  # noqa: SLF001
    assert hass.errors == []


def test_display_unmark_removes_it_from_rooms(rig):
    runtime, clock, hass, bridge, runner = rig
    _with_display(bridge)
    _setup(runner, hass, bridge, calibration="none")
    asyncio.run(runner.async_display(DISPLAY, True))
    asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A], "display": DISPLAY}, {"salon"}))
    hass.drain()
    assert runner.status("salon")["display"]["ieee"] == DISPLAY
    assert runner.states["salon"].display_ieee == DISPLAY
    assert asyncio.run(runner.async_display(DISPLAY, False)) == {"displays": []}
    hass.drain()
    assert runner.configs["salon"]["display"] is None
    assert runner.status("salon")["display"] is None
    assert runner.states["salon"].display is None
    saved = asyncio.run(runner._store.async_load())  # noqa: SLF001
    assert saved["displays"] == [] and saved["rooms"]["salon"]["display"] is None
    # danach als TRV erlaubt
    asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A, DISPLAY]}, {"salon"}))
    # nicht markiert: Aufheben ist harmlos
    assert asyncio.run(runner.async_display("0xnichts", False)) == {"displays": []}


# Beok BOT-R9V-ZB (Auszug): Ein/Aus über system_mode, preset auto/manual/eco, eigene Eco-Temperatur
BEOK_EXPOSES = [
    {"type": "climate", "features": [
        {"type": "numeric", "property": "current_heating_setpoint", "access": 3, "value_min": 5, "value_max": 60, "value_step": 0.5},
        {"type": "numeric", "property": "local_temperature", "access": 1},
        {"type": "enum", "property": "system_mode", "access": 3, "values": ["off", "heat"]},
        {"type": "enum", "property": "running_state", "access": 1, "values": ["idle", "heat"]},
        {"type": "enum", "property": "preset", "access": 3, "values": ["auto", "manual", "eco"]},
    ]},
    {"type": "numeric", "property": "battery", "access": 1, "value_min": 0, "value_max": 100},
    {"type": "numeric", "property": "eco_temperature", "access": 3, "value_min": 5, "value_max": 30},
]


def test_display_window_override_end_to_end_and_persisted(rig):
    runtime, clock, hass, bridge, runner = rig
    bridge.devices[DISPLAY] = {"ieee_address": DISPLAY, "friendly_name": "Wand Salon", "definition": {"exposes": BEOK_EXPOSES}}
    bridge.report(DISPLAY, local_temperature=22.0, current_heating_setpoint=21.0, system_mode="heat", preset="manual",
                  eco_temperature=18, battery=80)
    _setup(runner, hass, bridge, calibration="none", window_open_delay=60, window_close_delay=120)
    asyncio.run(runner.async_display(DISPLAY, True))
    asyncio.run(runner.async_set({"area_id": "salon", "trvs": [TRV_A], "sensors": [SENSOR], "windows": [WINDOW],
                                  "display": DISPLAY, "calibration": "none", "window_open_delay": 60}, {"salon"}))
    hass.drain()
    status = runner.status("salon")
    assert {k: status["display"][k] for k in ("preset", "eco_temperature", "battery")} == {
        "preset": "manual", "eco_temperature": 18.0, "battery": 80}
    # Fenster auf: nach der Verzögerung TRV und Display aus
    bridge.published.clear()
    hass.set_state(WINDOW, "on")
    _advance(hass, clock, 61)
    assert ("Wand Salon", {"system_mode": "off"}) in bridge.published
    clock.t += 2
    bridge.report(DISPLAY, system_mode="off")  # Bestätigung
    hass.drain()
    assert runner.states["salon"].mode == "heat"  # eigenes Aus ist kein Raum-Aus
    assert runner.status("salon")["reason"] == "window"
    # am Display wieder an: trotzdem heizen, gespeichert
    clock.t += 30
    bridge.report(DISPLAY, system_mode="heat")
    hass.drain()
    status = runner.status("salon")
    assert status["reason"] is None and status["window"]["override"] is True and status["window"]["pause_at"] is None
    saved = asyncio.run(runner._store.async_load())  # noqa: SLF001
    assert saved["state"]["salon"]["window_override"] is True
    # nach einem Neustart weiter übersteuert
    again = runtime.HeatingRunner(hass)
    again._store.saved = saved  # noqa: SLF001
    asyncio.run(again.async_load())
    assert again.states["salon"].window_override is True and again.states["salon"].paused is False
    # Fenster zu: Übersteuerung vorbei
    hass.set_state(WINDOW, "off")
    hass.drain()
    assert runner.status("salon")["window"]["override"] is False
    assert asyncio.run(runner._store.async_load())["state"]["salon"]["window_override"] is False  # noqa: SLF001
    assert hass.errors == []

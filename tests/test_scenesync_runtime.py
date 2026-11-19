"""Szenen-Abgleich (``scenesync_runtime.py``) mit nachgebautem Home Assistant und Lampen.

Die Bridge ist die echte ``Z2MBridge``, nur das MQTT-Senden ist ersetzt: Ein kleiner Nachbau
spielt Lampen mit eigenem Szenenspeicher. Nach ``scene_recall`` meldet „Z2M“ für alle Mitglieder
den Zustand aus seinem Gedächtnis (auch für Lampen, die die Szene gar nicht kennen); erst das
``get`` bringt den echten Zustand der Lampe nach HA. So fällt auf, wenn die Messung dem Gedächtnis traut.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from typing import Any

import pytest

ss = importlib.import_module("haven_nohass.scenesync")
z2m = importlib.import_module("haven_nohass.z2m")
Z2MError = importlib.import_module("haven_nohass.commands").Z2MError

BASE = "zigbee2mqtt"
GROUP_ID = 3
GROUP_NAME = "Gruppe Test"
SCENE = 1
OFF = {"on": False, "brightness": None, "kelvin": None, "rgb": None}

LIGHT_EXPOSES = [
    {
        "type": "light",
        "features": [
            {"type": "binary", "name": "state", "property": "state", "access": 7, "value_on": "ON", "value_off": "OFF"},
            {"type": "numeric", "name": "brightness", "property": "brightness", "access": 7, "value_min": 0, "value_max": 254},
            {"type": "numeric", "name": "color_temp", "property": "color_temp", "access": 7, "value_min": 153, "value_max": 500},
            {
                "type": "composite",
                "name": "color_xy",
                "property": "color",
                "access": 7,
                "features": [
                    {"type": "numeric", "name": "x", "property": "x", "access": 7},
                    {"type": "numeric", "name": "y", "property": "y", "access": 7},
                ],
            },
        ],
    }
]
BUTTON_EXPOSES = [{"type": "enum", "name": "action", "property": "action", "access": 1, "values": ["single"]}]

# Erfundene Lampen: Name in Z2M, IEEE, HA-Licht
LAMPS = {
    "Lampe A": ("0x00000000000000d1", "light.lampe_a"),
    "Lampe B": ("0x00000000000000d2", "light.lampe_b"),
    "Lampe C": ("0x00000000000000d3", "light.lampe_c"),
    "Lampe D": ("0x00000000000000d4", "light.lampe_d"),
}
BUTTON = ("Taster", "0x00000000000000e1")


def lamp(brightness=80, kelvin=None, rgb=None):
    return {"on": True, "brightness": brightness, "kelvin": kelvin, "rgb": rgb}


WARM = lamp(100, kelvin=4000)
RED = lamp(80, rgb="#ff0000")
BLUE = lamp(40, rgb="#0000ff")


# --- Nachbau von Home Assistant ------------------------------------------------------------


def _callback(func):
    func._hass_callback = True  # noqa: SLF001 - wie homeassistant.core.callback
    return func


class FakeState:
    def __init__(self, state: str, attributes: dict[str, Any]) -> None:
        self.state = state
        self.attributes = attributes


class FakeEvent:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def ha_state(entity_id: str, value: dict[str, Any] | None) -> FakeState:
    """HA-Zustand einer Lampe aus ``LampState`` (``None`` = nicht erreichbar)."""
    name = {"friendly_name": entity_id.split(".", 1)[1].replace("_", " ").title()}
    if value is None:
        return FakeState("unavailable", name)
    if not value["on"]:
        return FakeState("off", name)
    attrs: dict[str, Any] = {**name, "brightness": round(value["brightness"] * 255 / 100), "color_mode": "brightness"}
    if value["kelvin"] is not None:
        attrs.update(color_mode="color_temp", color_temp_kelvin=value["kelvin"])
    elif value["rgb"] is not None:
        hexv = value["rgb"].lstrip("#")
        attrs.update(color_mode="rgb", rgb_color=(int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)))
    return FakeState("on", attrs)


class FakeHass:
    def __init__(self, world: World) -> None:
        self.world = world
        self._states: dict[str, FakeState] = {}
        self.states = types.SimpleNamespace(get=self._states.get)
        self.services = types.SimpleNamespace(async_call=self._async_call)
        self.trackers: list[tuple[list[str], Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def set(self, entity_id: str, value: dict[str, Any] | None) -> None:
        old = self._states.get(entity_id)
        new = ha_state(entity_id, value)
        self._states[entity_id] = new
        for entities, action in list(self.trackers):
            if entity_id in entities:
                assert getattr(action, "_hass_callback", False), "State-Abo ohne @callback"
                action(FakeEvent({"entity_id": entity_id, "old_state": old, "new_state": new}))

    async def _async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False) -> None:
        assert domain == "light"
        self.calls.append((service, dict(data)))
        self.world.apply_service(service, data)


class World:
    """Lampen mit echtem Zustand, eigenem Szenenspeicher und Erreichbarkeit."""

    def __init__(self, members: list[str]) -> None:
        self.members = list(members)
        self.real: dict[str, dict[str, Any]] = {}
        self.scenes: dict[str, dict[int, dict[str, Any]]] = {}
        self.online: dict[str, bool] = {}
        self.z2m_memory: dict[int, dict[str, dict[str, Any]]] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.hass = FakeHass(self)
        self.bridge = z2m.Z2MBridge(None, BASE)
        devices = [
            {"ieee_address": ieee, "friendly_name": name, "type": "Router", "definition": {"model": "L1", "exposes": LIGHT_EXPOSES}}
            for name, (ieee, _) in LAMPS.items()
        ]
        devices.append({"ieee_address": BUTTON[1], "friendly_name": BUTTON[0], "type": "EndDevice", "definition": {"model": "B1", "exposes": BUTTON_EXPOSES}})
        self.bridge.handle_devices(json.dumps(devices))
        self.set_members(members)
        self.bridge._async_publish = self._publish  # noqa: SLF001

    # Einrichtung

    def set_members(self, members: list[str]) -> None:
        self.members = list(members)
        refs = [{"ieee_address": LAMPS[m][0], "endpoint": 11} for m in members] + [{"ieee_address": BUTTON[1], "endpoint": 1}]
        self.bridge.handle_groups(json.dumps([{"id": GROUP_ID, "friendly_name": GROUP_NAME, "members": refs, "scenes": [{"id": SCENE, "name": "Abend"}]}]))

    def lamp(self, name: str, state: dict[str, Any], online: bool = True, ha_unavailable: bool = False) -> None:
        eid = LAMPS[name][1]
        self.real[eid] = dict(state)
        self.online[eid] = online
        self.scenes.setdefault(eid, {})
        self.hass.set(eid, None if ha_unavailable else state)

    def ha_lights(self) -> dict[str, list[str]]:
        return {ieee: [eid, f"sensor.{eid[6:]}_linkquality"] for ieee, eid in LAMPS.values()}

    def ha(self, name: str) -> dict[str, Any] | None:
        s = self.hass.states.get(LAMPS[name][1])
        return ss.lamp_state_from(s.state, s.attributes)

    # MQTT an Z2M

    async def _publish(self, topic: str, payload: str) -> None:
        data = json.loads(payload)
        self.published.append((topic, data))
        name, action = topic[len(BASE) + 1 :].rsplit("/", 1)
        if name == GROUP_NAME and action == "set":
            eids = [LAMPS[m][1] for m in self.members]
            if "scene_recall" in data:
                sid = data["scene_recall"]
                for eid in eids:
                    if self.online[eid] and sid in self.scenes[eid]:
                        self.real[eid] = dict(self.scenes[eid][sid])
                # Z2M meldet seinen gemerkten Szenenzustand für alle Mitglieder
                for eid, value in self.z2m_memory.get(sid, {}).items():
                    if eid in eids and self.hass.states.get(eid).state != "unavailable":
                        self.hass.set(eid, value)
            if "scene_store" in data:
                sid = data["scene_store"]["ID"]
                for eid in eids:
                    if self.online[eid]:
                        self.scenes[eid][sid] = dict(self.real[eid])
        elif action == "get":
            eid = LAMPS[name][1]
            assert set(data) <= {"state", "brightness", "color", "color_temp"}
            if self.online[eid]:
                self.hass.set(eid, self.real[eid])
                self.bridge.handle_device_message(f"{BASE}/{name}", json.dumps({"state": "ON" if self.real[eid]["on"] else "OFF"}))

    # light.turn_on / turn_off

    def apply_service(self, service: str, data: dict[str, Any]) -> None:
        eid = data["entity_id"]
        if not self.online[eid]:
            return
        if service == "turn_off":
            new = dict(OFF)
        else:
            old = self.real[eid] if self.real[eid]["on"] else lamp(100)
            new = dict(old)
            if "brightness" in data:
                new["brightness"] = round(data["brightness"] * 100 / 255)
            if "brightness_pct" in data:
                new["brightness"] = data["brightness_pct"]
            if "color_temp_kelvin" in data:
                new.update(kelvin=data["color_temp_kelvin"], rgb=None)
            if "rgb_color" in data:
                new.update(rgb="#" + "".join(f"{c:02x}" for c in data["rgb_color"]), kelvin=None)
        self.real[eid] = new
        self.hass.set(eid, new)


class FakeStorage:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {"looks": {}, "targets": {}, "checks": {}}

    async def async_get_scene_looks(self):
        return self.data["looks"]

    async def async_set_scene_looks(self, looks):
        self.data["looks"] = dict(looks)

    async def async_get_scene_targets(self):
        return self.data["targets"]

    async def async_set_scene_targets(self, targets):
        self.data["targets"] = dict(targets)

    async def async_get_scene_checks(self):
        return self.data["checks"]

    async def async_set_scene_checks(self, checks):
        self.data["checks"] = dict(checks)


def _ha_modules() -> dict[str, types.ModuleType]:
    core = types.ModuleType("homeassistant.core")
    core.callback = _callback
    event = types.ModuleType("homeassistant.helpers.event")

    def async_track_state_change_event(hass: FakeHass, entity_ids, action):
        entry = (list(entity_ids), action)
        hass.trackers.append(entry)
        return lambda: hass.trackers.remove(entry) if entry in hass.trackers else None

    event.async_track_state_change_event = async_track_state_change_event
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.event = event
    ha = types.ModuleType("homeassistant")
    ha.core, ha.helpers = core, helpers
    return {"homeassistant": ha, "homeassistant.core": core, "homeassistant.helpers": helpers, "homeassistant.helpers.event": event}


@pytest.fixture
def runtime(monkeypatch):
    for name, module in _ha_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "haven_nohass.scenesync_runtime", raising=False)
    rt = importlib.import_module("haven_nohass.scenesync_runtime")
    for const, value in (
        ("RECALL_SETTLE", 0.0),
        ("GET_TIMEOUT", 0.1),
        ("GET_QUIET", 0.01),
        ("GET_QUIET_MAX", 0.05),
        ("CONFIRM_TIMEOUT", 0.1),
        ("STORE_SETTLE", 0.0),
    ):
        monkeypatch.setattr(rt, const, value)
    rt._LOCKS.clear()  # noqa: SLF001 - Locks hängen an der Ereignisschleife des Tests
    yield rt
    sys.modules.pop("haven_nohass.scenesync_runtime", None)


def _setup(rt, members=("Lampe A", "Lampe B", "Lampe C")):
    world = World(list(members))
    storage = FakeStorage()
    sync = rt.SceneSync(world.hass, world.bridge, storage, world.ha_lights)
    return world, storage, sync


def _target(storage: FakeStorage) -> dict[str, Any]:
    return storage.data["targets"][f"{GROUP_ID}:{SCENE}"]


def _statuses(result: dict[str, Any]) -> dict[str, str]:
    return {m["entity_id"]: m["status"] for m in result["members"]}


# --- Soll beim Speichern ------------------------------------------------------------------------


def test_capture_target_on_store(runtime):
    world, storage, sync = _setup(runtime)
    world.lamp("Lampe A", RED)
    world.lamp("Lampe B", lamp(50, kelvin=2700))
    world.lamp("Lampe C", OFF)
    target = asyncio.run(sync.async_capture_target(GROUP_ID, SCENE, "store"))
    assert target is _target(storage)
    assert target["members"] == {"light.lampe_a": RED, "light.lampe_b": lamp(50, kelvin=2700), "light.lampe_c": OFF}
    assert target["source"] == "store"
    # Nur Licht-Mitglieder, der Taster zählt nicht
    assert target["ieees"] == sorted(LAMPS[n][0] for n in ("Lampe A", "Lampe B", "Lampe C"))
    assert target["stored_at"]


def test_capture_target_skips_unavailable_lamp(runtime):
    world, storage, sync = _setup(runtime)
    world.lamp("Lampe A", RED)
    world.lamp("Lampe B", RED)
    world.lamp("Lampe C", RED, ha_unavailable=True)
    target = asyncio.run(sync.async_capture_target(GROUP_ID, SCENE))
    assert set(target["members"]) == {"light.lampe_a", "light.lampe_b"}
    assert len(target["ieees"]) == 3


def test_members_map_to_ha_lights(runtime):
    world, _, sync = _setup(runtime)
    for name in ("Lampe A", "Lampe B", "Lampe C"):
        world.lamp(name, WARM)
    members = sync.members(world.bridge.groups[0])
    assert [m.entity_id for m in members] == ["light.lampe_a", "light.lampe_b", "light.lampe_c"]
    assert members[0].friendly_name == "Lampe A" and members[0].name == "Lampe A"
    assert members[0].get_payload == {"state": "", "brightness": "", "color": "", "color_temp": ""}


# --- Messen -------------------------------------------------------------------------------------


def _scene_world(rt):
    """Soll: A rot, B blau, C aus. B kennt die Szene nicht, Z2M „meldet“ sie trotzdem richtig."""
    world, storage, sync = _setup(rt)
    for name in ("Lampe A", "Lampe B", "Lampe C"):
        world.lamp(name, WARM)
    world.scenes["light.lampe_a"][SCENE] = RED
    world.scenes["light.lampe_c"][SCENE] = OFF
    soll = {"light.lampe_a": RED, "light.lampe_b": BLUE, "light.lampe_c": OFF}
    world.z2m_memory[SCENE] = dict(soll)
    storage.data["targets"][f"{GROUP_ID}:{SCENE}"] = ss.build_target(
        soll, world.bridge.group_light_members(world.bridge.groups[0]), "store", "2026-11-19T20:00:00+00:00"
    )
    return world, storage, sync


def test_check_finds_missing_lamp_and_restores(runtime):
    world, storage, sync = _scene_world(runtime)
    result = asyncio.run(sync.async_check(GROUP_ID, SCENE))
    assert result["status"] == "differs" and result["has_target"] is True and result["name"] == "Abend"
    assert _statuses(result) == {"light.lampe_a": "ok", "light.lampe_b": "missing", "light.lampe_c": "ok"}
    row_b = next(m for m in result["members"] if m["entity_id"] == "light.lampe_b")
    assert row_b["expected"] == BLUE and row_b["measured"] == WARM  # gelesen, nicht aus dem Gedächtnis
    assert row_b["name"] == "Lampe B"
    # Jede Lampe wurde gelesen, mit dem, was sie kann
    gets = [(t, p) for t, p in world.published if t.endswith("/get")]
    assert sorted(t for t, _ in gets) == [f"{BASE}/Lampe {x}/get" for x in "ABC"]
    assert all(p == {"state": "", "brightness": "", "color": "", "color_temp": ""} for _, p in gets)
    # Vorheriger Zustand ist wieder da
    assert all(world.ha(n) == WARM and world.real[LAMPS[n][1]] == WARM for n in ("Lampe A", "Lampe B", "Lampe C"))
    assert not world.hass.trackers  # Abos wieder abgemeldet
    assert storage.data["checks"][f"{GROUP_ID}:{SCENE}"] == result
    assert result["measured_look"]["colors"][0] == "#ff0000"


def test_check_differs_and_unreachable(runtime):
    world, _, sync = _scene_world(runtime)
    world.scenes["light.lampe_b"][SCENE] = lamp(40, rgb="#00ff00")  # kennt die Szene, aber falsch
    world.online["light.lampe_c"] = False  # antwortet nicht auf get
    result = asyncio.run(sync.async_check(GROUP_ID, SCENE))
    assert _statuses(result) == {"light.lampe_a": "ok", "light.lampe_b": "differs", "light.lampe_c": "unreachable"}
    row_c = next(m for m in result["members"] if m["entity_id"] == "light.lampe_c")
    assert row_c["measured"] is None and row_c["expected"] == OFF


def test_check_unavailable_lamp_is_not_asked(runtime):
    world, _, sync = _scene_world(runtime)
    world.lamp("Lampe C", WARM, ha_unavailable=True)
    result = asyncio.run(sync.async_check(GROUP_ID, SCENE))
    assert _statuses(result)["light.lampe_c"] == "unreachable"
    assert f"{BASE}/Lampe C/get" not in [t for t, _ in world.published]
    assert world.hass.states.get("light.lampe_c").state == "unavailable"  # nicht „wiederhergestellt“


def test_check_without_target(runtime):
    world, storage, sync = _scene_world(runtime)
    storage.data["targets"].clear()
    result = asyncio.run(sync.async_check(GROUP_ID, SCENE))
    assert result["status"] == "unknown" and result["has_target"] is False
    # A und C reagierten, B blieb unverändert
    assert _statuses(result) == {"light.lampe_a": "ok", "light.lampe_b": "missing", "light.lampe_c": "ok"}
    assert all(m["expected"] is None for m in result["members"])


def test_check_restores_even_on_error(runtime):
    world, _, sync = _scene_world(runtime)

    async def boom(friendly_name, values):
        raise Z2MError("mqtt_error", "weg")

    world.bridge.async_publish_get = boom
    with pytest.raises(Z2MError):
        asyncio.run(sync.async_check(GROUP_ID, SCENE))
    assert all(world.real[LAMPS[n][1]] == WARM for n in ("Lampe A", "Lampe B", "Lampe C"))


def test_check_unknown_scene(runtime):
    _, _, sync = _setup(runtime)
    with pytest.raises(Z2MError) as err:
        asyncio.run(sync.async_check(GROUP_ID, 9))
    assert err.value.code == "not_found"


# --- Übernehmen ----------------------------------------------------------------------------------


def test_adopt_uses_last_check(runtime):
    world, storage, sync = _scene_world(runtime)

    async def run():
        check = await sync.async_check(GROUP_ID, SCENE)
        published = len(world.published)
        result = await sync.async_adopt(GROUP_ID, SCENE)
        assert len(world.published) == published  # keine neue Messung
        return check, result

    check, result = asyncio.run(run())
    target = _target(storage)
    assert target["source"] == "adopt"
    assert target["members"] == {"light.lampe_a": RED, "light.lampe_b": WARM, "light.lampe_c": OFF}
    assert result == {"ok": True, "look": storage.data["looks"][f"{GROUP_ID}:{SCENE}"]}
    assert {k: result["look"][k] for k in ("colors", "brightness", "kelvin")} == check["measured_look"]
    # Messung ist älter als das neue Soll: Status ok
    summary = ss.attach_sync(world.bridge.groups_summary(), storage.data["targets"], storage.data["checks"])
    assert summary[0]["scenes"][0]["sync"] == "ok"
    assert summary[0]["scenes"][0]["checked_at"] == check["checked_at"]


def test_adopt_without_check_measures(runtime):
    world, storage, sync = _scene_world(runtime)
    result = asyncio.run(sync.async_adopt(GROUP_ID, SCENE))
    assert result["ok"] is True
    assert any(t.endswith("/get") for t, _ in world.published)
    assert f"{GROUP_ID}:{SCENE}" in storage.data["checks"]


# --- Abgleichen -----------------------------------------------------------------------------------


def test_repair_writes_target_to_all_lamps(runtime):
    world, storage, sync = _scene_world(runtime)
    world.lamp("Lampe D", lamp(30, kelvin=3000))  # neu in der Gruppe, ohne Soll
    world.set_members(["Lampe A", "Lampe B", "Lampe C", "Lampe D"])
    summary = ss.attach_sync(world.bridge.groups_summary(), storage.data["targets"], {})
    assert summary[0]["scenes"][0]["sync"] == "stale"

    result = asyncio.run(sync.async_repair(GROUP_ID, SCENE))

    assert result["status"] == "ok"
    assert _statuses(result) == dict.fromkeys(("light.lampe_a", "light.lampe_b", "light.lampe_c", "light.lampe_d"), "ok")
    # Die Lampen kennen die Szene jetzt, D mit „aus“
    assert world.scenes["light.lampe_b"][SCENE] == BLUE
    assert world.scenes["light.lampe_d"][SCENE] == OFF
    store = [p for t, p in world.published if t == f"{BASE}/{GROUP_NAME}/set" and "scene_store" in p]
    assert store == [{"scene_store": {"ID": SCENE, "name": "Abend"}}]
    # Vorheriger Zustand zurück
    assert world.ha("Lampe D") == lamp(30, kelvin=3000)
    assert all(world.ha(n) == WARM for n in ("Lampe A", "Lampe B", "Lampe C"))
    # Soll umfasst jetzt D, Status in der Gruppenliste ok
    target = _target(storage)
    assert target["members"]["light.lampe_d"] == OFF and target["source"] == "store"
    summary = ss.attach_sync(world.bridge.groups_summary(), storage.data["targets"], storage.data["checks"])
    assert summary[0]["scenes"][0]["sync"] == "ok"


def test_repair_sets_lamps_to_target_before_store(runtime):
    world, _, sync = _scene_world(runtime)
    order: list[str] = []
    original = world._publish  # noqa: SLF001

    async def tracking(topic, payload):
        data = json.loads(payload)
        if "scene_store" in data:
            order.append("store:" + ",".join(f"{k}={world.real[k]['on']}" for k in sorted(world.real)))
        await original(topic, payload)

    world.bridge._async_publish = tracking  # noqa: SLF001
    asyncio.run(sync.async_repair(GROUP_ID, SCENE))
    assert order == ["store:light.lampe_a=True,light.lampe_b=True,light.lampe_c=False"]
    services = [s for s, d in world.hass.calls if d["entity_id"] == "light.lampe_b"]
    assert services[0] == "turn_on"
    assert next(d for s, d in world.hass.calls if d["entity_id"] == "light.lampe_b") == {
        "entity_id": "light.lampe_b", "brightness_pct": 40, "rgb_color": [0, 0, 255]
    }


def test_repair_without_target(runtime):
    world, _, sync = _setup(runtime)
    for name in ("Lampe A", "Lampe B", "Lampe C"):
        world.lamp(name, WARM)
    with pytest.raises(Z2MError) as err:
        asyncio.run(sync.async_repair(GROUP_ID, SCENE))
    assert err.value.code == "no_target"
    assert not world.published and not world.hass.calls


def test_one_flow_per_group(runtime):
    world, _, sync = _scene_world(runtime)
    events: list[str] = []
    original = sync._check  # noqa: SLF001

    async def tracked(group_id, scene_id):
        events.append("start")
        await asyncio.sleep(0.01)
        result = await original(group_id, scene_id)
        events.append("end")
        return result

    sync._check = tracked  # noqa: SLF001

    async def run():
        await asyncio.gather(sync.async_check(GROUP_ID, SCENE), sync.async_check(GROUP_ID, SCENE))

    asyncio.run(run())
    assert events == ["start", "end", "start", "end"]
    assert runtime.group_lock(GROUP_ID) is runtime.group_lock(GROUP_ID)
    assert runtime.group_lock(GROUP_ID) is not runtime.group_lock(GROUP_ID + 1)

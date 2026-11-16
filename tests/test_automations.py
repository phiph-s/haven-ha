"""Haven-Automationen (Drehregler je Raum): Normalisierung, Planung, Entprellen, Validierung, Store."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import py_compile

import pytest

from test_rooms import _build

auto = importlib.import_module("haven_nohass.automations")
Z2MError = importlib.import_module("haven_nohass.commands").Z2MError

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "haven"


def _exposed_actions(device: dict) -> list[str]:
    found: list[str] = []

    def walk(items):
        for e in items:
            if e.get("property") == "action":
                found.extend(e.get("values") or [])
            walk(e.get("features") or [])

    walk(device["exposes"])
    return found


@pytest.fixture()
def targets(inventory):
    bridge, rooms = _build(inventory)
    return bridge, auto.build_room_targets(rooms["rooms"], bridge.groups, ["light.lampe_salon"])


def _knob(**options) -> dict:
    return auto.normalize_automation(
        {"id": "k1", "type": "room_knob", "name": "Salon", "room_area_id": "salon", "devices": ["0x00124b0000000016"], "options": options}
    )


# --- Normalisierung --------------------------------------------------------------------


def test_normalize_event_mode():
    n = auto.normalize_action
    assert n({"action": "rotate_right"}) == auto.Gesture("rotate", 1, 1, "rotate_right")
    assert n({"action": "rotate_left"}).direction == -1
    assert n({"action": "rotate_left"}).name == "rotate_left"
    assert n({"action": "single", "battery": 90}).kind == "single"
    assert n({"action": "double"}).kind == "double"
    assert n({"action": "hold"}).kind == "hold"


def test_normalize_command_mode():
    n = auto.normalize_action
    assert n({"action": "brightness_step_up", "action_step_size": 13}).name == "rotate_right"
    assert n({"action": "brightness_step_down", "action_rate": 5}).name == "rotate_left"
    assert n({"action": "toggle"}).kind == "single"
    assert n({"action": "on"}).kind == "single" and n({"action": "off"}).kind == "single"
    assert n({"action": "color_temperature_step_up"}) is None
    assert n({"action": "hue_move"}) is None


def test_normalize_ignores_empty_and_unknown():
    n = auto.normalize_action
    assert n({"action": ""}) is None
    assert n({"action": None}) is None
    assert n({"battery": 100}) is None
    assert n({"action": "irgendwas"}) is None
    assert n("kein dict") is None


def test_every_exposed_knob_action_is_known(inventory):
    """Jede Aktion der Drehregler und Taster im Bestand ist entweder eine Geste oder bewusst ignoriert."""
    for device in inventory["z2m_devices"]:
        if device["model"] not in ("ZG-101ZD", "ZG-101ZL"):
            continue
        for action in _exposed_actions(device):
            known = auto.normalize_action({"action": action}) is not None or action in auto.IGNORED_ACTIONS
            assert known, (device["model"], action)


# --- Räume ------------------------------------------------------------------------------


def test_room_targets(targets):
    _, rooms = targets
    salon = rooms["salon"]
    assert salon.group == "ZGSalon"
    assert [s["id"] for s in salon.scenes] == list(range(8))
    assert "light.lampe_salon" not in salon.lights  # ausgenommen
    assert rooms["kochbereich"].group == "ZGKochbereich" and rooms["kochbereich"].scenes == []


def test_room_state_prefers_group_entity():
    room = auto.RoomTarget("salon", "Salon", group="ZGSalon", group_entity="light.zgsalon", lights=["light.a"])
    states = {"light.zgsalon": {"state": "on", "attributes": {"brightness": 120}}, "light.a": {"state": "on", "attributes": {"brightness": 200}}}
    assert auto.room_state_from(states, room) == auto.RoomState(True, 120, ["light.a"])
    no_group = auto.RoomTarget("x", "X", lights=["light.a", "light.b"])
    states = {"light.a": {"state": "off", "attributes": {}}, "light.b": {"state": "on", "attributes": {"brightness": 80}}}
    assert auto.room_state_from(states, no_group) == auto.RoomState(True, 80, ["light.b"])
    assert auto.room_state_from({}, no_group).on is None


# --- Planung ------------------------------------------------------------------------------


def test_plan_rotate_group(targets):
    _, rooms = targets
    plan = auto.plan_gesture(_knob(), auto.Gesture("rotate", 1, 1), rooms["salon"], auto.RoomState(True, 100))
    assert plan.mqtt == [("zigbee2mqtt/ZGSalon/set", {"brightness_step": 35, "transition": 0.2})]
    plan = auto.plan_gesture(_knob(), auto.Gesture("rotate", -1, 2), rooms["salon"], auto.RoomState(True, 200), base_topic="z2m")
    assert plan.mqtt == [("z2m/ZGSalon/set", {"brightness_step": -70, "transition": 0.2})]
    assert plan.expected.brightness == 130


def test_plan_rotate_respects_minimum_and_off_at_min(targets):
    _, rooms = targets
    salon = rooms["salon"]
    down = auto.Gesture("rotate", -1, 1)
    plan = auto.plan_gesture(_knob(min_brightness=20), down, salon, auto.RoomState(True, 40))
    assert plan.mqtt[0][1]["brightness_step"] == -20
    assert auto.plan_gesture(_knob(min_brightness=20), down, salon, auto.RoomState(True, 20)).empty
    off = auto.plan_gesture(_knob(min_brightness=20, off_at_min=True), down, salon, auto.RoomState(True, 20))
    assert off.mqtt == [("zigbee2mqtt/ZGSalon/set", {"state": "OFF"})]
    # Aus: Runterdrehen tut nichts, Hochdrehen schaltet gedimmt ein
    assert auto.plan_gesture(_knob(), down, salon, auto.RoomState(False, 0)).empty
    up = auto.plan_gesture(_knob(), auto.Gesture("rotate", 1, 1), salon, auto.RoomState(False, 0))
    assert up.mqtt[0][1] == {"state": "ON", "brightness": 35, "transition": 0.2}
    # Oben begrenzt
    top = auto.plan_gesture(_knob(), auto.Gesture("rotate", 1, 1), salon, auto.RoomState(True, 240))
    assert top.mqtt[0][1]["brightness_step"] == 14


def test_plan_toggle_and_on_action(targets):
    _, rooms = targets
    salon = rooms["salon"]
    single = auto.Gesture("single")
    assert auto.plan_gesture(_knob(), single, salon, auto.RoomState(True, 100)).mqtt[0][1] == {"state": "OFF"}
    assert auto.plan_gesture(_knob(), single, salon, auto.RoomState(False, 0)).mqtt[0][1] == {"state": "ON"}
    assert auto.plan_gesture(_knob(), single, salon, auto.RoomState()).mqtt[0][1] == {"state": "TOGGLE"}
    with_scene = _knob(on_action="scene", on_scene=3)
    plan = auto.plan_gesture(with_scene, single, salon, auto.RoomState(False, 0))
    assert plan.mqtt[0][1] == {"scene_recall": 3} and plan.scene == 3 and plan.action == "on"


def test_plan_hold_options(targets):
    _, rooms = targets
    salon = rooms["salon"]
    hold = auto.Gesture("hold")
    assert auto.plan_gesture(_knob(), hold, salon, auto.RoomState(True, 9)).empty  # Standard: keine
    off = _knob(gestures={"hold": {"action": "off"}})
    assert auto.plan_gesture(off, hold, salon, auto.RoomState(True, 9)).mqtt[0][1] == {"state": "OFF"}
    scene = _knob(gestures={"hold": {"action": "scene", "scene": 7}})
    assert auto.plan_gesture(scene, hold, salon, auto.RoomState(True, 9)).mqtt[0][1] == {"scene_recall": 7}


def test_plan_fallback_services_without_group():
    room = auto.RoomTarget("diele", "Diele", lights=["light.a", "light.b"])
    knob = _knob()
    plan = auto.plan_gesture(knob, auto.Gesture("rotate", 1, 1), room, auto.RoomState(True, 100, ["light.a"]))
    assert plan.services == [("light", "turn_on", {"entity_id": ["light.a"], "brightness_step": 35, "transition": 0.2})]
    plan = auto.plan_gesture(knob, auto.Gesture("single"), room, auto.RoomState(True, 100, ["light.a"]))
    assert plan.services == [("light", "turn_off", {"entity_id": ["light.a", "light.b"]})]
    plan = auto.plan_gesture(knob, auto.Gesture("single"), room, auto.RoomState())
    assert plan.services == [("light", "toggle", {"entity_id": ["light.a", "light.b"]})]
    # Szenen gibt es ohne Gruppe nicht
    assert auto.plan_gesture(knob, auto.Gesture("double"), room, auto.RoomState()).empty


def test_fallback_respects_excluded_lights(targets):
    bridge, _ = targets
    rooms = auto.build_room_targets(
        [{"area_id": "x", "name": "X", "lights": ["light.a", "light.b"], "group": None}], bridge.groups, ["light.b"]
    )
    plan = auto.plan_gesture(_knob(), auto.Gesture("single"), rooms["x"], auto.RoomState(True, 10, ["light.a"]))
    assert plan.services == [("light", "turn_off", {"entity_id": ["light.a"]})]


# --- Szenen --------------------------------------------------------------------------------


def test_next_scene_wraps(targets):
    _, rooms = targets
    ruhe = auto.RoomTarget("ruheraum", "Ruheraum", group="ZGRuheraum", scenes=[{"id": i, "name": str(i)} for i in range(4)])
    knob = _knob()
    double = auto.Gesture("double")
    seen = []
    last = None
    for _ in range(5):
        plan = auto.plan_gesture(knob, double, ruhe, auto.RoomState(True, 50), last_scene=last)
        last = plan.scene
        seen.append(last)
    assert seen == [0, 1, 2, 3, 0]


def test_scene_cycle_custom_order_and_missing():
    room = auto.RoomTarget("r", "R", group="G", scenes=[{"id": i, "name": str(i)} for i in range(4)])
    assert auto.scene_cycle({"scenes": [3, 1, 9, 3]}, room) == [3, 1]
    assert auto.scene_cycle({"scenes": None}, room) == [0, 1, 2, 3]
    assert auto.next_scene([3, 1], 1) == 3
    assert auto.next_scene([3, 1], 2) == 3  # unbekannt: erste
    assert auto.next_scene([], 1) is None


# --- Entprellen ------------------------------------------------------------------------------


def test_coalescer_first_immediate_then_batched():
    c = auto.StepCoalescer(0.12)
    assert c.push("salon", 1, 0.0) == 1
    assert c.push("salon", 1, 0.03) is None
    assert c.push("salon", 1, 0.06) is None
    assert c.flush("salon", 0.08) is None  # zu früh
    assert c.delay("salon", 0.08) == pytest.approx(0.04)
    assert c.flush("salon", 0.12) == 2
    assert c.delay("salon", 0.13) is None
    # Nächstes Ereignis im Fenster nach dem Flush wird wieder gesammelt
    assert c.push("salon", -1, 0.15) is None
    assert c.flush("salon", 0.24) == -1
    # nach Pause sofort
    assert c.push("salon", 1, 1.0) == 1
    # andere Räume unabhängig
    assert c.push("kochbereich", -1, 1.01) == -1


def test_coalescer_opposite_steps_cancel():
    c = auto.StepCoalescer(0.1)
    c.push("r", 1, 0.0)
    c.push("r", 1, 0.02)
    c.push("r", -1, 0.04)
    assert c.flush("r", 0.1) is None  # +1 und -1 im Fenster heben sich auf


# --- Validierung -------------------------------------------------------------------------------


def test_validate_device_taken(targets):
    _, rooms = targets
    existing = [_knob()]
    other = {"type": "room_knob", "name": "Zweit", "room_area_id": "kochbereich", "devices": ["0x00124b0000000016"]}
    with pytest.raises(Z2MError) as err:
        auto.validate_automation(other, existing, rooms, {"0x00124b0000000016": "Drehknopf Salon 1"})
    assert err.value.code == "device_taken"
    assert "Drehknopf Salon 1 steuert schon Salon" in err.value.message
    # Deaktiviert darf es doppelt sein
    saved = auto.validate_automation({**other, "enabled": False}, existing, rooms)
    assert saved["enabled"] is False and saved["id"]
    # Dieselbe Automation ändern ist kein Konflikt
    assert auto.validate_automation({**existing[0], "name": "Neu"}, existing, rooms)["name"] == "Neu"


def test_validate_room_without_lights(targets):
    _, rooms = targets
    rooms = {**rooms, "leer": auto.RoomTarget("leer", "Abstellraum")}
    with pytest.raises(Z2MError) as err:
        auto.validate_automation({"room_area_id": "leer", "devices": ["0x1"]}, [], rooms)
    assert err.value.code == "no_lights"
    with pytest.raises(Z2MError) as err:
        auto.validate_automation({"room_area_id": "fehlt", "devices": ["0x1"]}, [], rooms)
    assert err.value.code == "not_found"


def test_validate_shape_errors():
    v = auto.validate_shape
    assert v({"room_area_id": "salon", "devices": ["0x1"]}) == []
    assert v({"room_area_id": "salon", "devices": []})
    assert v({"type": "motion_light", "room_area_id": "salon", "devices": ["0x1"]})
    assert v({"room_area_id": "salon", "devices": ["0x1"], "options": {"step": 0}})
    assert v({"room_area_id": "salon", "devices": ["0x1"], "options": {"gestures": {"hold": {"action": "scene"}}}})
    assert v({"room_area_id": "salon", "devices": ["0x1"], "options": {"gestures": {"rotate": "toggle"}}})
    assert v({"room_area_id": "salon", "devices": ["0x1"], "options": {"on_action": "scene"}})
    assert v({"room_area_id": "salon", "devices": ["0x1"], "options": {"transition": 20}})
    with pytest.raises(Z2MError) as err:
        auto.validate_automation({"devices": ["0x1"]}, [])
    assert err.value.code == "invalid_format"


def test_unknown_device(targets):
    _, rooms = targets
    with pytest.raises(Z2MError) as err:
        auto.validate_automation({"room_area_id": "salon", "devices": ["0xdead"]}, [], rooms, {"0x1": "a"})
    assert err.value.code == "not_found"


# --- Store ----------------------------------------------------------------------------------------


def test_store_migration_from_1_1():
    old = {
        "automations": [
            {
                "id": "a",
                "room_area_id": "salon",
                "devices": ["0x1", "0x1"],
                "options": {"hold_action": "scene", "hold_scene": 2, "step": 20},
            }
        ],
        "scene_index": {"salon": 4, "kaputt": "x"},
    }
    data = auto.migrate_store(1, 1, old)
    a = data["automations"][0]
    assert a["options"]["gestures"]["hold"] == {"action": "scene", "scene": 2}
    assert a["options"]["gestures"]["single"] == {"action": "toggle"}
    assert a["options"]["step"] == 20 and a["options"]["min_brightness"] == 3
    assert a["devices"] == ["0x1"] and a["enabled"] is True and a["type"] == "room_knob"
    assert data["runtime"]["last_scene"] == {"salon": 4}
    assert auto.validate_shape(a) == []


def test_store_migration_bare_list_and_garbage():
    assert auto.migrate_store(1, 1, [{"id": "x", "room_area_id": "r", "devices": []}])["automations"][0]["id"] == "x"
    assert auto.migrate_store(1, 1, "kaputt") == auto.empty_store()
    assert auto.read_store(None) == auto.empty_store()
    current = auto.read_store({"automations": [_knob()], "runtime": {"last_scene": {"salon": 1}, "last_triggered": {}}})
    assert current["automations"][0] == _knob() and current["runtime"]["last_scene"] == {"salon": 1}
    json.dumps(current)  # speicherbar


def test_public_and_event_data(targets):
    _, rooms = targets
    knob = _knob()
    runtime = {"last_triggered": {"k1": {"at": "2026-11-16T10:00:00+00:00", "action": "single"}}}
    assert auto.public_automation(knob, runtime)["last_triggered"]["action"] == "single"
    data = auto.event_data(knob, auto.Gesture("rotate", -1), rooms["salon"], source="test")
    assert data == {"id": "k1", "action": "rotate_left", "room": "salon", "room_name": "Salon", "source": "test"}
    assert auto.gesture_from_name("rotate_right").direction == 1
    assert auto.gesture_from_name("toggle") is None


def test_ha_modules_compile():
    for name in ("automations_runtime.py", "automations_ws.py", "websocket_api.py", "__init__.py"):
        py_compile.compile(str(PKG / name), doraise=True)

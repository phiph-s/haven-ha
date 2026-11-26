"""Szenen mit Zusatzgeräten und Szenen von Räumen ohne Gruppe (Feature ``room_scenes``)."""

from __future__ import annotations

import importlib

import pytest

rs = importlib.import_module("haven_nohass.room_scenes")
auto = importlib.import_module("haven_nohass.automations")
Z2MError = importlib.import_module("haven_nohass.commands").Z2MError

NOW = "2026-11-26T18:00:00+00:00"


def test_capture_device():
    on = {"state": "on", "attributes": {"brightness": 128, "color_mode": "color_temp", "color_temp_kelvin": 2700}}
    assert rs.capture_device("light.a", on) == {"entity_id": "light.a", "on": True, "brightness": 50, "kelvin": 2700}
    rgb = {"state": "on", "attributes": {"brightness": 255, "color_mode": "hs", "rgb_color": [255, 100, 0]}}
    assert rs.capture_device("light.b", rgb) == {"entity_id": "light.b", "on": True, "brightness": 100, "rgb": [255, 100, 0]}
    assert rs.capture_device("light.c", {"state": "off", "attributes": {}}) == {"entity_id": "light.c", "on": False}
    assert rs.capture_device("switch.d", {"state": "on", "attributes": {}}) == {"entity_id": "switch.d", "on": True}
    assert rs.capture_device("light.e", {"state": "unavailable"}) is None
    assert rs.capture_device("cover.f", {"state": "on"}) is None
    assert rs.capture_device("light.g", None) is None


def test_validate_device_ids():
    assert rs.validate_device_ids(["light.a", "switch.b", "light.a"]) == ["light.a", "switch.b"]
    for bad in (["cover.x"], "light.a", [1], ["light"]):
        with pytest.raises(Z2MError):
            rs.validate_device_ids(bad)


def test_plan_devices_lights_and_switches():
    devices = [
        {"entity_id": "light.a", "on": True, "brightness": 40},
        {"entity_id": "light.b", "on": False},
        {"entity_id": "switch.s", "on": True},
        {"entity_id": "switch.t", "on": False},
        {"entity_id": "switch.locked", "on": False},
    ]
    services = rs.plan_devices(devices, skip={"switch.locked"})
    assert ("light", "turn_on", {"entity_id": ["light.a"], "brightness_pct": 40, "transition": 0.0}) in services
    assert ("light", "turn_off", {"entity_id": ["light.b"], "transition": 0.0}) in services
    assert ("switch", "turn_on", {"entity_id": ["switch.s"]}) in services
    assert ("switch", "turn_off", {"entity_id": ["switch.t"]}) in services


def test_room_scene_store_rename_remove():
    dev = [{"entity_id": "light.a", "on": True}]
    scenes, s0 = rs.store_room_scene([], " Abend ", None, dev, None, NOW)
    assert s0["id"] == 0 and s0["name"] == "Abend"
    scenes, s1 = rs.store_room_scene(scenes, "Hell", None, dev, None, NOW)
    assert s1["id"] == 1
    with pytest.raises(Z2MError) as err:
        rs.store_room_scene(scenes, "abend", None, dev, None, NOW)
    assert err.value.code == "name_taken"
    with pytest.raises(Z2MError) as err:
        rs.store_room_scene(scenes, "Leer", None, [], None, NOW)
    assert err.value.code == "no_lights"
    # Überschreiben behält den Namen
    scenes, again = rs.store_room_scene(scenes, None, 0, [{"entity_id": "light.a", "on": False}], None, NOW)
    assert again["name"] == "Abend" and again["devices"][0]["on"] is False
    scenes, name = rs.rename_room_scene(scenes, 1, "Lesen")
    assert name == "Lesen"
    with pytest.raises(Z2MError):
        rs.rename_room_scene(scenes, 1, "Abend")
    scenes = rs.remove_room_scene(scenes, 0)
    assert [s["id"] for s in scenes] == [1]
    # freie ID wird wiederverwendet
    _, s = rs.store_room_scene(scenes, "Neu", None, dev, None, NOW)
    assert s["id"] == 0
    with pytest.raises(Z2MError):
        rs.remove_room_scene(scenes, 9)


def test_keep_devices_and_public():
    devices = [{"entity_id": "light.a", "on": True}, {"entity_id": "switch.b", "on": True}]
    assert rs.keep_devices(devices, ["switch.b"]) == [devices[1]]
    pub = rs.public_room_scene({"id": 2, "name": "X", "look": {"colors": ["#ffffff"], "brightness": 50, "kelvin": None}, "devices": devices, "stored_at": NOW})
    assert pub["look"]["stored_at"] == NOW and pub["devices"] == devices


def test_room_targets_with_scene_devices():
    rooms = [
        {"area_id": "g", "name": "G", "lights": ["light.a", "light.w"], "light_ieee": {"light.a": "0x1"},
         "group": {"id": 5, "friendly_name": "ZG", "missing": []}},
        {"area_id": "n", "name": "N", "lights": ["light.w2"], "group": None},
    ]
    groups = [{"id": 5, "friendly_name": "ZG", "scenes": [{"id": 0, "name": "Abend"}]}]
    extras = {"5:0": [{"entity_id": "light.w", "on": True, "brightness": 30}]}
    room_scenes = {"n": [{"id": 3, "name": "Nacht", "devices": [{"entity_id": "light.w2", "on": True, "brightness": 5}]}]}
    targets = auto.build_room_targets(rooms, groups, [], extras, room_scenes, ["switch.locked"])
    g, n = targets["g"], targets["n"]
    assert g.scene_devices == {0: extras["5:0"]}
    assert n.scenes == [{"id": 3, "name": "Nacht"}] and n.has_scenes

    plan = auto._plan_scene(g, 0, "zigbee2mqtt")
    assert plan.mqtt == [("zigbee2mqtt/ZG/set", {"scene_recall": 0})]
    assert plan.services == [("light", "turn_on", {"entity_id": ["light.w"], "brightness_pct": 30, "transition": 0.0})]
    plan = auto._plan_scene(n, 3, "zigbee2mqtt")
    assert plan.mqtt == [] and plan.services[0][2]["entity_id"] == ["light.w2"] and plan.scene == 3
    assert auto._plan_scene(n, 9, "zigbee2mqtt").note == "Szene fehlt"


def test_knob_next_scene_in_room_without_group():
    room = auto.RoomTarget(
        "n", "N", lights=["light.w2"], scenes=[{"id": 0, "name": "A"}, {"id": 1, "name": "B"}],
        scene_devices={0: [{"entity_id": "light.w2", "on": True}], 1: [{"entity_id": "light.w2", "on": False}]},
    )
    knob = auto.normalize_automation({"id": "k", "room_area_id": "n", "devices": ["0x1"], "options": {}})
    plan = auto.plan_gesture(knob, auto.Gesture("double"), room, auto.RoomState(True, 50), last_scene=0)
    assert plan.scene == 1 and plan.services == [("light", "turn_off", {"entity_id": ["light.w2"], "transition": 0.0})]


def test_locked_devices_skipped_on_recall():
    room = auto.RoomTarget(
        "n", "N", scenes=[{"id": 0, "name": "A"}], locked=["switch.s"],
        scene_devices={0: [{"entity_id": "switch.s", "on": False}, {"entity_id": "switch.t", "on": False}]},
    )
    plan = auto._plan_scene(room, 0, "zigbee2mqtt")
    assert plan.services == [("switch", "turn_off", {"entity_id": ["switch.t"]})]

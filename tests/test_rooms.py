"""Raum-Zusammenstellung (haven/rooms) ohne Home Assistant."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from test_z2m import _bridge

rooms_mod = importlib.import_module("haven_nohass.rooms")
AreaData, DeviceData, EntityData = rooms_mod.AreaData, rooms_mod.DeviceData, rooms_mod.EntityData

STATES = {
    s["entity_id"]: s["attributes"]
    for s in json.loads((Path(__file__).parent / "data" / "light_states.json").read_text(encoding="utf-8"))
}


def _build(inventory):
    bridge = _bridge(inventory)
    ref = bridge.ieee_for
    areas = [AreaData("kochbereich", "Kochbereich"), AreaData("salon", "Salon", icon="mdi:television"), AreaData("waschraum", "Waschraum")]
    devices = {
        d.device_id: d
        for d in (
            DeviceData("d_spule", "Kochbereich Arbeitslicht 2", "Kochbereich Arbeitslicht 2", "Tuya", "TS0502B", ref("Kochbereich Arbeitslicht 2"), "kochbereich"),
            # Gerät steht im Salon, die Entity hat den eigenen Bereich Kochbereich
            DeviceData("d_kochen", "Kochbereich Arbeitslicht 1", "Kochbereich Arbeitslicht 1", "Tuya", "TS0502B", ref("Kochbereich Arbeitslicht 1"), "salon"),
            DeviceData("d_lampe", "Lampe Salon", "Lampe Salon", "Philips", "X", ref("Lampe Salon"), "salon"),
            DeviceData("d_sensor", "Sensor Kochbereich", "Sensor Kochbereich", "SONOFF", "SNZB-02", "0xabc", "kochbereich"),
            DeviceData("d_wetter", "Wetter", "Wetter", None, None, None, "kochbereich", service=True),
            # Z2M-Gruppen als MQTT-Geräte, ohne Bereich bzw. im Salon
            DeviceData("g_kochbereich", "ZGKochbereich", "ZGKochbereich", "Zigbee2MQTT", "Group", None, None),
            DeviceData("g_wohn", "ZGSalon", "ZGSalon", "Zigbee2MQTT", "Group", None, "salon"),
            DeviceData("g_bad", "zgwaschraum", "zgwaschraum", "Zigbee2MQTT", "Group", None, "waschraum"),
            DeviceData("d_frei", "Sensor Flur", "Sensor Flur", "SONOFF", "SNZB-02", "0xdef", None),
        )
    }
    entities = [
        EntityData("light.kochbereich_arbeitslicht_2", "d_spule", platform="mqtt"),
        EntityData("light.kochbereich_arbeitslicht_1", "d_kochen", area_id="kochbereich", platform="mqtt"),
        EntityData("sensor.kochbereich_arbeitslicht_1_power", "d_kochen", platform="mqtt"),
        EntityData("light.lampe_salon", "d_lampe", platform="mqtt", attributes=STATES["light.lampe_salon"]),
        EntityData("sensor.sensor_kochbereich_temperature", "d_sensor", device_class="temperature"),
        EntityData("sensor.sensor_kochbereich_battery", "d_sensor", has_category=True, device_class="battery"),
        EntityData("sensor.sensor_kochbereich_versteckt", "d_sensor", hidden=True, device_class="humidity"),
        EntityData("weather.home", "d_wetter"),
        EntityData("light.salon", None, area_id="salon", platform="group", attributes=STATES["light.salon"]),
        EntityData("light.zgkochbereich", "g_kochbereich", platform="mqtt", attributes=STATES["light.zgkochbereich"]),
        EntityData("light.zgsalon", "g_wohn", platform="mqtt", attributes=STATES["light.zgsalon"]),
        # Restored ohne Attribute: nur die Namensregel greift
        EntityData("light.zgwaschraum", "g_bad", platform="mqtt"),
        EntityData("sensor.sensor_flur_temperature", "d_frei", device_class="temperature"),
    ]
    return bridge, rooms_mod.assemble_rooms(areas, devices, entities, bridge)


def test_rooms(inventory):
    bridge, result = _build(inventory)
    rooms = {r["area_id"]: r for r in result["rooms"]}
    assert [r["name"] for r in result["rooms"]] == ["Kochbereich", "Salon", "Waschraum"]

    kochbereich = rooms["kochbereich"]
    # Entity-Bereich schlägt Gerätebereich
    assert kochbereich["lights"] == ["light.kochbereich_arbeitslicht_1", "light.kochbereich_arbeitslicht_2"]
    assert set(kochbereich["zigbee_lights"]) == {bridge.ieee_for("Kochbereich Arbeitslicht 2"), bridge.ieee_for("Kochbereich Arbeitslicht 1")}
    assert kochbereich["group"]["friendly_name"] == "ZGKochbereich" and kochbereich["group"]["match"] == "exact"
    # Gruppen-Entity ohne Bereich wird über den Namen gefunden
    assert kochbereich["group_entity"] == "light.zgkochbereich"
    assert kochbereich["light_groups"] == []
    assert kochbereich["climate"]["temperature"] == ["sensor.sensor_kochbereich_temperature"]
    assert [(d["device_id"], d["entities"]) for d in kochbereich["devices"]] == [
        ("d_kochen", ["light.kochbereich_arbeitslicht_1"]),
        ("d_spule", ["light.kochbereich_arbeitslicht_2"]),
        ("d_sensor", ["sensor.sensor_kochbereich_temperature"]),
    ]

    wohn = rooms["salon"]
    assert wohn["lights"] == ["light.lampe_salon"]
    assert wohn["light_groups"] == [
        {
            "entity_id": "light.salon",
            "type": "ha_group",
            "name": "Salon",
            "members": sorted(STATES["light.salon"]["entity_id"]),
            "z2m_group_id": None,
        },
        {
            "entity_id": "light.zgsalon",
            "type": "z2m_group",
            "name": "ZGSalon",
            "members": sorted(STATES["light.zgsalon"]["group_entities"]),
            "z2m_group_id": 1,
        },
    ]
    assert wohn["group"]["friendly_name"] == "ZGSalon"
    assert wohn["group_entity"] == "light.zgsalon"
    # Gerät d_kochen bleibt mit seiner übrigen Entity im Salon, Gruppen-Geräte nicht
    assert [d["device_id"] for d in wohn["devices"]] == ["d_kochen", "d_lampe"]
    assert wohn["devices"][0]["entities"] == ["sensor.kochbereich_arbeitslicht_1_power"]

    waschraum = rooms["waschraum"]
    assert waschraum["lights"] == [] and waschraum["group"] is None and waschraum["group_entity"] is None
    assert waschraum["light_groups"] == [
        {"entity_id": "light.zgwaschraum", "type": "z2m_group", "name": None, "members": [], "z2m_group_id": 4}
    ]
    assert waschraum["devices"] == []

    # Z2M-Gruppen sind keine freien Geräte
    assert result["unassigned"] == [
        {
            "device_id": "d_frei",
            "name": "Sensor Flur",
            "manufacturer": "SONOFF",
            "model": "SNZB-02",
            "ieee": "0xdef",
            "entities": ["sensor.sensor_flur_temperature"],
        }
    ]


def test_room_keys(inventory):
    _, result = _build(inventory)
    assert set(result["rooms"][0]) == {
        "area_id", "name", "floor_id", "icon", "lights", "light_groups", "zigbee_lights", "light_ieee", "outside_lights", "group_missing_lights", "switches",
        "climate", "motion", "group", "group_entity", "devices",
    }


def test_protocol_of_identifiers():
    protocol_of_identifiers = rooms_mod.protocol_of_identifiers

    assert protocol_of_identifiers({("matter", "deviceid_ABC-1")}) == "matter"
    assert protocol_of_identifiers({("zwave_js", "123-4")}) == "zwave"
    assert protocol_of_identifiers({("mqtt", "zigbee2mqtt_0x1")}) is None
    assert protocol_of_identifiers(None) is None


def test_matter_device_item(inventory):
    DeviceData, _device_item = rooms_mod.DeviceData, rooms_mod._device_item

    item = _device_item(DeviceData("d_m", "Lampe", "Lampe", "Eve", "Light Strip", None, "salon", protocol="matter", sw_version="1.2"))
    assert item["protocol"] == "matter" and item["sw_version"] == "1.2"
    plain = _device_item(DeviceData("d_x", "X", "X", None, None, "0x1", None, sw_version="9"))
    assert "protocol" not in plain and "sw_version" not in plain

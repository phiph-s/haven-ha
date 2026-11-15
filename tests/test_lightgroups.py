"""Lichtgruppen-Erkennung mit echten Zustandsattributen (verkleinert aus HA 0.1.0)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

lightgroups = importlib.import_module("haven_nohass.lightgroups")

STATES = {
    s["entity_id"]: s["attributes"]
    for s in json.loads((Path(__file__).parent / "data" / "light_states.json").read_text(encoding="utf-8"))
}
GROUPS = ["ZGWaschraum", "ZGKochbereich", "ZGRuheraum", "ZGSalon"]


def _classify(entity_id, platform="mqtt", device_name=None, **kw):
    return lightgroups.classify_light(entity_id, platform, STATES.get(entity_id), device_name, GROUPS, **kw)


def test_real_attributes():
    # Alte HA-Lichtgruppe: Attribut entity_id als Liste, auch ohne Registry-Plattform
    assert _classify("light.salon", platform=None) == "ha_group"
    assert _classify("light.ruheraum", platform=None) == "ha_group"
    # Z2M-Gruppe über MQTT: Attribut group_entities
    assert _classify("light.zgsalon") == "z2m_group"
    assert _classify("light.zgkochbereich") == "z2m_group"
    # Echte Lampen
    assert _classify("light.lampe_salon", device_name="Lampe Salon", ieee="0x1") is None
    assert _classify("light.zusatzlicht_1", platform="moonraker", device_name="Snapmaker U1") is None


def test_platform_and_name_rules():
    assert lightgroups.classify_light("light.x", "group", {}, None, GROUPS) == "ha_group"
    # Namensregel ohne Attribute (z. B. Zustand restored), ohne Groß/klein
    assert lightgroups.classify_light("light.y", "mqtt", {}, "zgkochbereich", GROUPS) == "z2m_group"
    assert lightgroups.classify_light("light.y", "mqtt", {"friendly_name": "ZGKOCHBEREICH"}, None, GROUPS) == "z2m_group"
    # Echtes Zigbee-Gerät (IEEE) ist nie per Name eine Gruppe
    assert lightgroups.classify_light("light.y", "mqtt", {}, "ZGKochbereich", GROUPS, ieee="0x1") is None
    assert lightgroups.classify_light("light.y", "mqtt", None, None, GROUPS) is None
    assert lightgroups.classify_light("switch.zgkochbereich", "mqtt", {}, "ZGKochbereich", GROUPS) is None


def test_members_and_group_id():
    assert lightgroups.group_members(STATES["light.zgkochbereich"]) == ["light.kochbereich_arbeitslicht_1", "light.kochbereich_arbeitslicht_2"]
    assert lightgroups.group_members(STATES["light.salon"])[0] == "light.lampe_salon"
    assert lightgroups.group_members(STATES["light.lampe_salon"]) == []
    groups = [{"id": 3, "friendly_name": "ZGKochbereich"}, {"id": 1, "friendly_name": "ZGSalon"}]
    assert lightgroups.z2m_group_id(["zgsalon"], groups) == 1
    assert lightgroups.z2m_group_id([None, "ZGKochbereich"], groups) == 3
    assert lightgroups.z2m_group_id(["Salon"], groups) is None

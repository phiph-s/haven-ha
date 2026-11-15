"""Tests für die HA-freie Z2M-Logik (Gruppen-Zuordnung) mit dem Bestand."""

from __future__ import annotations

import importlib
import json

z2m = importlib.import_module("haven_nohass.z2m")


def _bridge(inventory) -> "z2m.Z2MBridge":
    bridge = z2m.Z2MBridge(None, "zigbee2mqtt")
    devs = [
        {
            "ieee_address": d["ieee"],
            "friendly_name": d["name"],
            "type": d["type"],
            "power_source": d["power"],
            "supported": True,
            "definition": {
                "model": d["model"],
                "vendor": d["vendor"],
                "description": d["description"],
                "exposes": d["exposes"],
            },
        }
        for d in inventory["z2m_devices"]
    ]
    bridge.handle_devices(json.dumps(devs))
    # Inventar nennt Mitglieder beim Namen, echtes bridge/groups mit ieee_address.
    groups = [
        {
            "id": g["id"],
            "friendly_name": g["name"],
            "members": [{"ieee_address": bridge.ieee_for(m), "endpoint": 11} for m in g["members"]],
            "scenes": g["scenes"],
        }
        for g in inventory["z2m_groups"]
    ]
    bridge.handle_groups(json.dumps(groups))
    return bridge


def test_devices_and_groups(inventory):
    bridge = _bridge(inventory)
    assert len(bridge.devices_summary()) == 42
    groups = {g["friendly_name"]: g for g in bridge.groups_summary()}
    assert len(groups["ZGSalon"]["scenes"]) == 8
    assert groups["ZGKochbereich"]["members"][0]["friendly_name"] == "Kochbereich Arbeitslicht 2"


def test_match_group_exact(inventory):
    bridge = _bridge(inventory)
    kochbereich = {bridge.ieee_for("Kochbereich Arbeitslicht 2"), bridge.ieee_for("Kochbereich Arbeitslicht 1")}
    match = bridge.match_group(kochbereich)
    assert match["friendly_name"] == "ZGKochbereich" and match["match"] == "exact"


def test_match_group_partial_and_none(inventory):
    bridge = _bridge(inventory)
    only_one = {bridge.ieee_for("Lampe Waschraum")}
    assert bridge.match_group(only_one)["friendly_name"] == "ZGWaschraum"
    assert bridge.match_group(set()) is None
    two = {bridge.ieee_for("Lampe Waschraum"), bridge.ieee_for("Lampe Ruhebett")}
    assert bridge.match_group(two)["match"] == "partial"


def test_device_detail_and_bad_json(inventory):
    bridge = _bridge(inventory)
    ieee = bridge.ieee_for("Drehknopf Kochbereich")
    detail = bridge.device_detail(ieee)
    assert detail["device_type"] == "knob"
    assert detail["classified"]["primary"]
    bridge.handle_devices("kein json")  # darf nichts zerstören
    assert len(bridge.devices) == 42

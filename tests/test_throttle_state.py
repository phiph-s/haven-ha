"""Drosselung und Zustands-Cache."""

from __future__ import annotations

import importlib
import json

throttle = importlib.import_module("haven_nohass.throttle")
statecache = importlib.import_module("haven_nohass.statecache")
from test_z2m import _bridge  # noqa: E402


def test_throttle_first_goes_through_rest_merged():
    t = throttle.Throttle(4)
    assert t.push("a", {"x": 1}, 0.0) == {"x": 1}
    assert t.push("a", {"x": 2, "y": 1}, 0.05) is None
    assert t.push("a", {"x": 3}, 0.10) is None
    assert abs(t.delay("a", 0.10) - 0.15) < 1e-9
    assert t.flush("a", 0.10) is None  # zu früh
    assert t.flush("a", 0.25) == {"x": 3, "y": 1}
    assert t.delay("a", 0.25) is None
    assert t.push("a", {"x": 4}, 0.30) is None  # nur 0.05 s nach dem Flush
    assert t.push("a", {"x": 5}, 1.0) is None  # es wartet noch etwas
    assert t.flush("a", 1.0) == {"x": 5}


def test_throttle_max_four_per_second_and_independent_keys():
    t = throttle.Throttle(4)
    emitted = 0
    for i in range(100):  # 100 Meldungen in 1 s
        now = i / 100
        if t.push("a", {"v": i}, now) is not None:
            emitted += 1
        if (d := t.delay("a", now)) is not None and d == 0 and t.flush("a", now) is not None:
            emitted += 1
        if t.flush("a", now) is not None:
            emitted += 1
    assert emitted <= 5  # 0, 0.25, 0.5, 0.75, 1.0 (Intervallgrenzen)
    assert t.push("b", {"v": 1}, 0.0) == {"v": 1}


def test_statecache_topics_and_filter(inventory):
    c = statecache.StateCache("zigbee2mqtt")
    c.set_names({"Lampe": "0x1", "Flur/Decke": "0x2"})
    assert c.wildcard_topic == "zigbee2mqtt/+"
    assert c.slash_topics() == {"Flur/Decke": "zigbee2mqtt/Flur/Decke"}
    assert c.ingest("zigbee2mqtt/Lampe", json.dumps({"state": "ON"})) == ("0x1", {"state": "ON"})
    assert c.ingest("zigbee2mqtt/Flur/Decke", '{"brightness": 5}') == ("0x2", {"brightness": 5})
    assert c.ingest("zigbee2mqtt/bridge", "{}") is None
    assert c.ingest("zigbee2mqtt/bridge/info", "{}") is None
    assert c.ingest("zigbee2mqtt/Gruppe", "{}") is None
    assert c.ingest("zigbee2mqtt/Lampe", "online") is None
    assert c.ingest("zigbee2mqtt/Lampe", "[1]") is None
    assert c.ingest("anders/Lampe", "{}") is None
    c.ingest("zigbee2mqtt/Lampe", '{"brightness": 9}')
    assert c.states["0x1"] == {"state": "ON", "brightness": 9}
    c.set_names({"Lampe neu": "0x1"})  # Umbenennung behält den Zustand
    assert "0x1" in c.states and "0x2" not in c.states


def test_bridge_state_events_with_inventory(inventory):
    bridge = _bridge(inventory)
    events = []
    bridge.add_listener(lambda k, p: events.append((k, p)))
    name = "Kochbereich Arbeitslicht 2"
    ieee = bridge.ieee_for(name)
    bridge.handle_device_message(f"zigbee2mqtt/{name}", json.dumps({"state": "ON", "linkquality": 80}))
    bridge.handle_device_message("zigbee2mqtt/ZGKochbereich", json.dumps({"state": "ON"}))  # Gruppe
    assert events == [("device_state", {"ieee": ieee, "values": {"state": "ON", "linkquality": 80}})]
    assert bridge.state_cache.states[ieee]["linkquality"] == 80
    # Namen mit "/" aus dem Bestand: alle wären exakt zu abonnieren
    assert all("/" in n for n in bridge.state_cache.slash_topics())

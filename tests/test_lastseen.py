"""Zuletzt gesehen, Verfügbarkeit und Option last_seen (ohne Home Assistant)."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json

lastseen = importlib.import_module("haven_nohass.lastseen")
z2m = importlib.import_module("haven_nohass.z2m")
commands = importlib.import_module("haven_nohass.commands")
statecache = importlib.import_module("haven_nohass.statecache")

MS = 1_777_450_500_000  # 2026-04-29T08:15:00Z
ISO = "2026-04-29T08:15:00+00:00"


def test_parse_formats():
    want = datetime(2026, 4, 29, 8, 15, tzinfo=timezone.utc)
    assert lastseen.parse_last_seen(MS) == want
    assert lastseen.parse_last_seen(str(MS)) == want
    assert lastseen.parse_last_seen(MS // 1000) == want
    assert lastseen.parse_last_seen("2026-04-29T08:15:00.000Z") == want
    assert lastseen.parse_last_seen("2026-04-29T10:15:00+02:00") == want
    assert lastseen.parse_last_seen("2026-04-29T08:15:00") == want
    for bad in (None, "", "gestern", 0, -5, True, [], {}):
        assert lastseen.parse_last_seen(bad) is None


def test_resolve_priority():
    nm = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert lastseen.resolve_last_seen(MS, nm) == (ISO, "z2m")
    # der jüngste Zeitpunkt gewinnt: Empfang nach der Netzkarte
    later = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert lastseen.resolve_last_seen(None, nm, later) == ("2026-05-01T00:00:00+00:00", "message")
    assert lastseen.resolve_last_seen(MS, nm, later) == (ISO, "z2m")  # Z2M-Zeit gilt
    assert lastseen.resolve_last_seen(None, later, nm)[1] == "networkmap"
    assert lastseen.resolve_last_seen(None, nm) == ("2026-01-01T00:00:00+00:00", "networkmap")
    assert lastseen.resolve_last_seen("kaputt", nm)[1] == "networkmap"
    assert lastseen.resolve_last_seen(None, None) == (None, None)


def test_networkmap_last_seen():
    nodes = [{"ieeeAddr": "0x1", "lastSeen": MS}, {"ieeeAddr": "0x2"}, {"lastSeen": MS}, "x"]
    assert list(lastseen.networkmap_last_seen(nodes)) == ["0x1"]
    assert lastseen.networkmap_last_seen(None) == {}


def test_availability_parse():
    assert lastseen.parse_availability('{"state":"offline"}') == "offline"
    assert lastseen.parse_availability(b'{"state":"online"}') == "online"
    assert lastseen.parse_availability("online") == "online"
    assert lastseen.parse_availability('"offline"') == "offline"
    assert lastseen.parse_availability("kaputt") is None
    assert lastseen.parse_availability('{"state":1}') is None


def test_info_flags():
    assert lastseen.last_seen_enabled({"config": {"advanced": {"last_seen": "ISO_8601"}}}) is True
    assert lastseen.last_seen_enabled({"config": {"advanced": {"last_seen": "disable"}}}) is False
    assert lastseen.last_seen_enabled({"config": {"advanced": {}}}) is False
    assert lastseen.last_seen_enabled({}) is None
    assert lastseen.availability_enabled({"config": {"availability": {"enabled": True}}}) is True
    assert lastseen.availability_enabled({"config": {"availability": True}}) is True
    # Z2M 2.x: Objekt mit enabled false = aus
    assert lastseen.availability_enabled({"config": {"availability": {"enabled": False, "active": {"timeout": 10}}}}) is False
    assert lastseen.availability_enabled({"config": {"availability": {"active": {"timeout": 10}}}}) is True
    assert lastseen.availability_enabled({"config": {}}) is False
    assert lastseen.availability_enabled({}) is None


def test_option_payload():
    assert commands.build_last_seen_option() == {"options": {"advanced": {"last_seen": "ISO_8601"}}}


def _bridge():
    bridge = z2m.Z2MBridge(None, "zigbee2mqtt")
    bridge.handle_devices(
        json.dumps(
            [
                {"ieee_address": "0x1", "friendly_name": "Lampe", "type": "Router"},
                {"ieee_address": "0x2", "friendly_name": "Flur/Melder", "type": "EndDevice"},
            ]
        )
    )
    return bridge


def test_bridge_sources_and_availability():
    bridge = _bridge()
    seen = {s["ieee"]: s for s in bridge.devices_summary()}
    assert seen["0x1"]["last_seen"] is None and seen["0x1"]["last_seen_source"] is None
    assert seen["0x1"]["availability"] is None

    # Gespeicherte (retained) Meldung: kein Lebenszeichen
    bridge.handle_device_message("zigbee2mqtt/Lampe", json.dumps({"state": "ON"}), retained=True)
    assert bridge.device_summary("0x1")["last_seen"] is None

    bridge.networkmap = {"nodes": [{"ieeeAddr": "0x1", "lastSeen": MS}], "links": [], "timestamp": "x"}
    assert bridge.device_summary("0x1")["last_seen_source"] == "networkmap"
    assert bridge.device_summary("0x1")["networkmap_at"] == "x"

    bridge.handle_device_message("zigbee2mqtt/Lampe", json.dumps({"last_seen": "2026-05-01T00:00:00Z"}))
    summary = bridge.device_summary("0x1")
    assert summary["last_seen_source"] == "z2m" and summary["last_seen"].startswith("2026-05-01")

    events = []
    bridge.add_listener(lambda k, p: events.append((k, p)))
    bridge.handle_availability_message("zigbee2mqtt/Lampe/availability", '{"state":"offline"}')
    bridge.handle_availability_message("zigbee2mqtt/Flur/Melder/availability", "online")
    bridge.handle_availability_message("zigbee2mqtt/Lampe/availability", '{"state":"offline"}')  # unverändert
    bridge.handle_availability_message("zigbee2mqtt/Gruppe/availability", "online")  # unbekannt
    assert bridge.device_summary("0x1")["availability"] == "offline"
    assert bridge.device_summary("0x2")["availability"] == "online"
    assert [e[0] for e in events] == ["availability", "availability"]

    bridge.handle_info(json.dumps({"config": {"advanced": {"last_seen": "disable"}}}))
    info = bridge.info_summary()
    assert info["last_seen_enabled"] is False and info["availability_enabled"] is False


def test_availability_topics():
    cache = statecache.StateCache("zigbee2mqtt")
    cache.set_names({"Lampe": "0x1", "Flur/Melder": "0x2"})
    assert cache.availability_wildcard == "zigbee2mqtt/+/availability"
    assert cache.slash_availability_topics() == {"Flur/Melder": "zigbee2mqtt/Flur/Melder/availability"}
    assert cache.availability_ieee("zigbee2mqtt/Flur/Melder/availability") == "0x2"
    assert cache.availability_ieee("zigbee2mqtt/bridge/availability") is None
    assert cache.ingest("zigbee2mqtt/Lampe/availability", '{"state":"online"}') is None


def test_live_message_counts_as_seen_except_startup():
    bridge = _bridge()
    # Netzkarte von gestern: Schnappschuss
    bridge.networkmap = {"nodes": [{"ieeeAddr": "0x1", "lastSeen": MS}], "links": [], "timestamp": "x"}
    now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    bridge.handle_device_message("zigbee2mqtt/Lampe", json.dumps({"power": 12}), now=now)
    summary = bridge.device_summary("0x1")
    assert summary["last_seen"] == "2026-05-02T12:00:00+00:00" and summary["last_seen_source"] == "message"

    # Z2M startet: gespeicherte Zustände in der ersten Minute zählen nicht
    bridge2 = _bridge()
    bridge2.handle_state(json.dumps({"state": "online"}))
    bridge2.handle_device_message("zigbee2mqtt/Lampe", json.dumps({"power": 12}), now=now)
    assert bridge2.device_summary("0x1")["last_seen"] is None
    bridge2._quiet_until = 0.0  # Minute vorbei
    bridge2.handle_device_message("zigbee2mqtt/Lampe", json.dumps({"power": 13}), now=now)
    assert bridge2.device_summary("0x1")["last_seen_source"] == "message"


def test_availability_before_device_list_is_kept():
    bridge = z2m.Z2MBridge(None, "zigbee2mqtt")
    # retained Verfügbarkeit kommt vor bridge/devices
    bridge.handle_availability_message("zigbee2mqtt/Lampe/availability", '{"state":"online"}')
    bridge.handle_availability_message("zigbee2mqtt/Gruppe/availability", '{"state":"online"}')
    bridge.handle_availability_message("zigbee2mqtt/bridge/availability", '{"state":"online"}')
    assert bridge.availability == {}
    bridge.handle_devices(json.dumps([
        {"ieee_address": "0x1", "friendly_name": "Lampe", "type": "Router"},
    ]))
    assert bridge.availability == {"0x1": "online"}
    assert bridge.device_summary("0x1")["availability"] == "online"

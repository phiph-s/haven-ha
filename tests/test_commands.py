"""Payload-Bauer, Transaktions-Abgleich und Request-Ablauf der Bridge."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

commands = importlib.import_module("haven_nohass.commands")
z2m = importlib.import_module("haven_nohass.z2m")
from test_z2m import _bridge  # noqa: E402


def test_builders():
    assert commands.build_rename("A", " B ", True) == {"from": "A", "to": "B", "homeassistant_rename": True}
    assert commands.build_remove("0x1", True) == {"id": "0x1", "force": True}
    assert commands.build_interview("0x1") == {"id": "0x1"}
    assert commands.build_permit_join(240) == {"time": 240}
    assert commands.build_permit_join(60, "0x1") == {"time": 60, "device": "0x1"}
    assert commands.build_permit_join(0) == {"time": 0}
    assert commands.build_networkmap() == {"type": "raw", "routes": False}
    assert commands.build_group_member("G", "0x1") == {"group": "G", "device": "0x1"}
    assert commands.build_group_member("G", "0x1", 11)["endpoint"] == 11
    assert commands.set_topic("zigbee2mqtt", "a/b") == "zigbee2mqtt/a/b/set"
    assert commands.request_topic("zigbee2mqtt", "device/rename") == "zigbee2mqtt/bridge/request/device/rename"


@pytest.mark.parametrize("bad", [-1, 255, True, "5"])
def test_permit_join_range(bad):
    with pytest.raises(commands.Z2MError) as err:
        commands.build_permit_join(bad)
    assert err.value.code == "invalid_format"


def test_empty_values_and_name():
    with pytest.raises(commands.Z2MError):
        commands.build_set({})
    with pytest.raises(commands.Z2MError):
        commands.build_rename("A", "  ")


def test_parse_response():
    assert commands.parse_response({"status": "ok", "data": {"a": 1}}) == {"a": 1}
    with pytest.raises(commands.Z2MError) as err:
        commands.parse_response({"status": "error", "error": "kaputt"})
    assert err.value.code == "z2m_error" and "kaputt" in err.value.message


def test_tracker_match_and_timeout():
    async def run():
        t = commands.TransactionTracker()
        tid, fut = t.new()
        tid2, fut2 = t.new()
        assert tid != tid2
        assert not t.resolve({"transaction": "fremd", "status": "ok"})
        assert not t.resolve("kein dict")
        assert t.resolve({"transaction": tid2, "status": "ok", "data": {"x": 2}})
        assert await t.wait(tid2, fut2, 1, "x") == {"x": 2}
        with pytest.raises(commands.Z2MError) as err:
            await t.wait(tid, fut, 0.05, "device/rename")
        assert err.value.code == "timeout" and "device/rename" in err.value.message
        assert t.pending == 0
        assert not t.resolve({"transaction": tid, "status": "ok"})  # zu spät

    asyncio.run(run())


def _fake_z2m(bridge, answer):
    """Ersetzt MQTT: protokolliert Publishes und lässt ``answer`` antworten."""
    sent = []

    async def publish(topic, payload):
        sent.append((topic, json.loads(payload)))
        if "/request/" in topic and answer is not None:
            req = json.loads(payload)
            bridge.handle_response(json.dumps({**answer, "transaction": req["transaction"]}))

    bridge._async_publish = publish
    return sent


def test_bridge_request_roundtrip(inventory):
    async def run():
        bridge = _bridge(inventory)
        sent = _fake_z2m(bridge, {"status": "ok", "data": {"from": "A", "to": "B"}})
        data = await bridge.async_request("device/rename", commands.build_rename("A", "B"))
        assert data["to"] == "B"
        topic, payload = sent[0]
        assert topic == "zigbee2mqtt/bridge/request/device/rename"
        assert payload["from"] == "A" and payload["transaction"].startswith("haven-")

    asyncio.run(run())


def test_bridge_request_error_timeout_offline(inventory):
    async def run():
        bridge = _bridge(inventory)
        _fake_z2m(bridge, {"status": "error", "error": "Device 'x' does not exist"})
        with pytest.raises(commands.Z2MError) as err:
            await bridge.async_request("device/remove", {"id": "x"})
        assert "does not exist" in err.value.message

        _fake_z2m(bridge, None)
        with pytest.raises(commands.Z2MError) as err:
            await bridge.async_request("device/remove", {"id": "x"}, timeout=0.05)
        assert err.value.code == "timeout"

        bridge.handle_state(json.dumps({"state": "offline"}))
        with pytest.raises(commands.Z2MError) as err:
            await bridge.async_request("permit_join", {"time": 1})
        assert err.value.code == "bridge_offline"

    asyncio.run(run())


def test_networkmap_cache_and_single_flight(inventory):
    async def run():
        bridge = _bridge(inventory)
        assert await bridge.async_networkmap(False) is None
        value = {"nodes": [{"ieeeAddr": "0x1"}], "links": [{"source": {}, "target": {}}]}
        sent = _fake_z2m(bridge, {"status": "ok", "data": {"type": "raw", "routes": False, "value": value}})
        result = await bridge.async_networkmap(True)
        assert result["nodes"] == value["nodes"] and result["timestamp"]
        assert sent[0][1]["type"] == "raw" and sent[0][1]["routes"] is False
        assert await bridge.async_networkmap(False) is result

        _fake_z2m(bridge, None)
        first = asyncio.create_task(bridge.async_networkmap(True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(commands.Z2MError) as err:
            await bridge.async_networkmap(True)
        assert err.value.code == "busy"
        first.cancel()

    asyncio.run(run())


def test_supports_identify(devices):
    with_identify = [d for d in devices.values() if commands.supports_identify({"definition": {"exposes": d["exposes"]}})]
    without = [d for d in devices.values() if d not in with_identify]
    assert with_identify and without
    assert not commands.supports_identify({})
    assert commands.supports_identify(
        {"definition": {"exposes": [{"type": "light", "features": [{"property": "identify", "access": 2}]}]}}
    )
    assert not commands.supports_identify(
        {"definition": {"exposes": [{"property": "identify", "access": 1}]}}
    )


def test_info_channel_pan_id(inventory):
    bridge = _bridge(inventory)
    bridge.handle_info(json.dumps({"version": "2.0.0", "network": {"channel": 15, "pan_id": 6754}}))
    info = bridge.info_summary()
    assert info["channel"] == 15 and info["pan_id"] == 6754
    bridge.handle_info(json.dumps({"version": "2.0.0"}))
    assert bridge.info_summary()["channel"] is None

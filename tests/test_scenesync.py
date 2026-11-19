"""Szenen-Abgleich ohne Home Assistant: Lampenzustand, Vergleich, Look, Status in der Gruppenliste."""

from __future__ import annotations

import importlib

ss = importlib.import_module("haven_nohass.scenesync")

OFF = {"on": False, "brightness": None, "kelvin": None, "rgb": None}


def lamp(brightness=80, kelvin=None, rgb=None):
    return {"on": True, "brightness": brightness, "kelvin": kelvin, "rgb": rgb}


# --- Lampenzustand aus HA -------------------------------------------------------------------


def test_lamp_state_from_ha():
    assert ss.lamp_state_from("off", {"brightness": 100}) == OFF
    assert ss.lamp_state_from("unavailable", {}) is None
    assert ss.lamp_state_from(None, None) is None
    white = {"brightness": 128, "color_mode": "color_temp", "color_temp_kelvin": 2702, "rgb_color": (255, 167, 87)}
    assert ss.lamp_state_from("on", white) == lamp(50, kelvin=2702)
    color = {"brightness": 255, "color_mode": "xy", "rgb_color": [255, 0, 16], "xy_color": [0.7, 0.3]}
    assert ss.lamp_state_from("on", color) == lamp(100, rgb="#ff0010")
    assert ss.lamp_state_from("on", {"color_mode": "hs", "hs_color": [120, 100]}) == lamp(None, rgb="#00ff00")
    assert ss.lamp_state_from("on", {"brightness": 51, "color_mode": "brightness"}) == lamp(20)
    assert ss.lamp_state_from("on", {"color_mode": "color_temp", "color_temp": 250}) == lamp(None, kelvin=4000)


# --- Vergleich ------------------------------------------------------------------------------------


def test_lamp_differs_thresholds():
    assert not ss.lamp_differs(lamp(50), lamp(58))
    assert ss.lamp_differs(lamp(50), lamp(59))
    assert not ss.lamp_differs(lamp(kelvin=2700), lamp(kelvin=3000))
    assert ss.lamp_differs(lamp(kelvin=2700), lamp(kelvin=3001))
    assert not ss.lamp_differs(lamp(rgb="#ff0000"), lamp(rgb="#d72828"))
    assert ss.lamp_differs(lamp(rgb="#ff0000"), lamp(rgb="#ff2900"))
    assert ss.lamp_differs(lamp(), OFF)
    assert not ss.lamp_differs(OFF, dict(OFF))
    # Weiß gegen Farbe: über die Farbe der Farbtemperatur
    assert not ss.lamp_differs(lamp(kelvin=2700), lamp(rgb="#ffa757"))
    assert ss.lamp_differs(lamp(kelvin=2700), lamp(rgb="#0000ff"))
    # Nur dimmbar (keine Farbe) gegen Farbe: Farbe zählt nicht
    assert not ss.lamp_differs(lamp(80), lamp(80, rgb="#0000ff"))
    # Helligkeit unbekannt: zählt nicht
    assert not ss.lamp_differs(lamp(None), lamp(10))


def test_compare_member_with_target():
    red, blue = lamp(80, rgb="#ff0000"), lamp(80, rgb="#0000ff")
    warm = lamp(100, kelvin=4000)
    assert ss.compare_member(red, warm, red, True) == "ok"
    assert ss.compare_member(red, warm, warm, True) == "missing"  # unverändert, Soll weicht ab
    assert ss.compare_member(red, warm, blue, True) == "differs"  # reagiert, aber falsch
    assert ss.compare_member(red, warm, lamp(70, rgb="#ff0000"), True) == "differs"  # 10 Prozentpunkte
    assert ss.compare_member(red, warm, None, True) == "unreachable"
    assert ss.compare_member(red, red, red, False) == "ok"  # stand schon richtig
    assert ss.compare_member(OFF, warm, warm, True) == "missing"
    assert ss.compare_member(red, None, blue, True) == "differs"  # vorher unbekannt


def test_compare_member_without_target():
    warm, red = lamp(100, kelvin=4000), lamp(80, rgb="#ff0000")
    assert ss.compare_member(None, warm, warm, True) == "missing"
    assert ss.compare_member(None, warm, warm, False) == "ok"  # keine Lampe reagierte
    assert ss.compare_member(None, warm, red, True) == "ok"
    assert ss.compare_member(None, warm, None, True) == "unreachable"


def test_compare_members_uses_other_lamps():
    warm, red = lamp(100, kelvin=4000), lamp(80, rgb="#ff0000")
    rows = [
        {"expected": None, "before": warm, "measured": red},
        {"expected": None, "before": warm, "measured": warm},
        {"expected": None, "before": warm, "measured": None},
    ]
    assert ss.compare_members(rows) == ["ok", "missing", "unreachable"]
    # Nur die Lampe selbst reagierte: sie ist nicht „andere“
    assert ss.compare_members(rows[:1]) == ["ok"]


def test_scene_status():
    assert ss.scene_status(["ok", "ok"], True) == "ok"
    assert ss.scene_status(["ok", "unreachable"], True) == "ok"
    assert ss.scene_status(["ok", "missing"], True) == "differs"
    assert ss.scene_status(["differs"], True) == "differs"
    assert ss.scene_status(["missing"], False) == "unknown"


def test_measured_look():
    look = ss.measured_look([lamp(80, rgb="#ff0000"), lamp(60, rgb="#f01010"), lamp(40, rgb="#0000ff"), OFF, None])
    assert look == {"colors": ["#ff0000", "#0000ff"], "brightness": 60, "kelvin": None}
    white = ss.measured_look([lamp(100, kelvin=2700), lamp(50, kelvin=3100)])
    assert white["kelvin"] == 2900 and white["brightness"] == 75 and len(white["colors"]) == 1
    assert ss.measured_look([OFF, None]) == {"colors": ["#000000"], "brightness": 0, "kelvin": None}
    many = ss.measured_look([lamp(rgb=c) for c in ("#ff0000", "#00ff00", "#0000ff", "#ffffff", "#ffffff")])
    assert many["colors"][0] == "#ffffff" and len(many["colors"]) == 3
    dimmable = ss.measured_look([lamp(None)])
    assert dimmable["brightness"] == 100 and dimmable["kelvin"] is None
    # Der Look passt zur Validierung der App-Looks
    scenes = importlib.import_module("haven_nohass.scenes")
    for candidate in (look, white, many, dimmable, ss.measured_look([])):
        assert scenes.validate_look(candidate) == candidate


def test_build_check():
    red, warm = lamp(80, rgb="#ff0000"), lamp(100, kelvin=4000)
    rows = [
        {"entity_id": "light.a", "name": "A", "expected": red, "before": warm, "measured": red},
        {"entity_id": "light.b", "name": "B", "expected": red, "before": warm, "measured": warm},
    ]
    result = ss.build_check(3, 2, "Abend", rows, True, "2026-11-19T20:00:00+00:00")
    assert result["status"] == "differs" and result["has_target"] is True
    assert [m["status"] for m in result["members"]] == ["ok", "missing"]
    assert result["members"][1] == {"entity_id": "light.b", "name": "B", "expected": red, "measured": warm, "status": "missing"}
    assert set(result) == {"group_id", "scene_id", "name", "has_target", "status", "members", "measured_look", "checked_at"}


# --- Soll und Status ---------------------------------------------------------------------------


def test_build_target():
    target = ss.build_target({"light.b": lamp(), "light.a": OFF, "light.c": None}, ["0x2", "0x1"], "store", "t")
    assert target == {"members": {"light.a": OFF, "light.b": lamp()}, "ieees": ["0x1", "0x2"], "stored_at": "t", "source": "store"}


def test_sync_status():
    target = {"members": {}, "ieees": ["0x1", "0x2"], "stored_at": "2026-11-19T20:00:00+00:00", "source": "store"}
    later = {"status": "differs", "checked_at": "2026-11-19T21:00:00+00:00"}
    earlier = {"status": "differs", "checked_at": "2026-11-19T19:00:00+00:00"}
    assert ss.sync_status(None, later, ["0x1"]) == "unknown"
    assert ss.sync_status(target, None, ["0x1", "0x2"]) == "ok"
    assert ss.sync_status(target, None, ["0x1"]) == "stale"  # Lampe weg
    assert ss.sync_status(target, None, ["0x1", "0x2", "0x3"]) == "stale"  # Lampe dazu
    assert ss.sync_status(target, later, ["0x2", "0x1"]) == "differs"
    assert ss.sync_status(target, earlier, ["0x1", "0x2"]) == "ok"  # Soll ist neuer als die Messung
    assert ss.sync_status(target, {**later, "status": "ok"}, ["0x1", "0x2"]) == "ok"
    assert ss.sync_status(target, later, ["0x1"]) == "stale"  # stale geht vor


def test_attach_sync():
    summary = [
        {
            "id": 3,
            "friendly_name": "Gruppe",
            "members": [
                {"ieee": "0x1", "device_type": "light"},
                {"ieee": "0x2", "device_type": "light"},
                {"ieee": "0x9", "device_type": "remote"},  # Taster zählt nicht
            ],
            "scenes": [{"id": 0, "name": "Hell", "look": None}, {"id": 1, "name": "Abend", "look": None}, {"id": 2, "name": "Kino", "look": None}],
        }
    ]
    target = {"members": {}, "ieees": ["0x1", "0x2"], "stored_at": "2026-11-19T20:00:00+00:00", "source": "store"}
    checks = {"3:1": {"status": "differs", "checked_at": "2026-11-19T21:00:00+00:00"}}
    out = ss.attach_sync(summary, {"3:0": target, "3:1": target, "3:2": {**target, "ieees": ["0x1"]}}, checks)
    scenes = out[0]["scenes"]
    assert [(s["sync"], s["checked_at"]) for s in scenes] == [
        ("ok", None),
        ("differs", "2026-11-19T21:00:00+00:00"),
        ("stale", None),
    ]
    assert scenes[0]["look"] is None  # bestehende Felder bleiben
    assert "sync" not in summary[0]["scenes"][0]
    assert ss.attach_sync(summary, {}, {})[0]["scenes"][0]["sync"] == "unknown"


# --- Dienste und get --------------------------------------------------------------------------


def test_target_call():
    assert ss.target_call(OFF) == ("turn_off", {})
    assert ss.target_call(lamp(40, rgb="#ff8000")) == ("turn_on", {"brightness_pct": 40, "rgb_color": [255, 128, 0]})
    assert ss.target_call(lamp(0, kelvin=2700)) == ("turn_on", {"brightness_pct": 1, "color_temp_kelvin": 2700})
    assert ss.target_call(lamp(None)) == ("turn_on", {})


def test_restore_call():
    assert ss.restore_call("off", {}) == ("turn_off", {})
    assert ss.restore_call("unavailable", {}) is None
    attrs = {"brightness": 200, "color_mode": "xy", "xy_color": (0.5, 0.4), "rgb_color": (255, 200, 100)}
    assert ss.restore_call("on", attrs) == ("turn_on", {"brightness": 200, "xy_color": [0.5, 0.4]})
    white = {"brightness": 10, "color_mode": "color_temp", "color_temp_kelvin": 3000}
    assert ss.restore_call("on", white) == ("turn_on", {"brightness": 10, "color_temp_kelvin": 3000})
    assert ss.restore_call("on", {"color_mode": "onoff"}) == ("turn_on", {})


def test_get_payload_and_member_mapping():
    assert ss.get_payload(["color_xy", "brightness", "color_temp"]) == {
        "state": "", "brightness": "", "color": "", "color_temp": ""
    }
    assert ss.get_payload(["brightness"]) == {"state": "", "brightness": ""}
    assert ss.get_payload([]) == {"state": ""}
    ha = {
        "0x1": ["sensor.a_linkquality", "light.a"],
        "0x2": ["light.b_l2", "light.b_l1"],  # zwei Endpunkte: der erste nach Namen
        "0x3": ["switch.c"],  # kein Licht in HA
    }
    assert ss.map_members(["0x2", "0x1", "0x3", "0x4"], ha) == [("0x1", "light.a"), ("0x2", "light.b_l1")]

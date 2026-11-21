"""Validierung von Grundriss und Widgets."""

from __future__ import annotations

import importlib

appdata = importlib.import_module("haven_nohass.appdata")
commands = importlib.import_module("haven_nohass.commands")

PLAN = {
    "version": 1,
    "grid": {"cols": 20, "rows": 26, "cellMeters": 0.5},
    "rooms": [{"id": "kochbereich", "name": "Kochbereich", "areaId": "kochbereich", "x": 12, "y": 0, "w": 8, "h": 7}],
    "placements": [{"entityId": "light.kochbereich_arbeitslicht_2", "roomId": "kochbereich", "rx": 0.5, "ry": 0.3}],
}


def test_floorplan_ok_and_errors():
    assert appdata.validate_floorplan(PLAN) == []
    assert appdata.validate_floorplan({**PLAN, "coordinator": {"roomId": "kochbereich", "rx": 0.2, "ry": 0.2}}) == []
    assert appdata.validate_floorplan([]) != []
    assert "version fehlt" in appdata.validate_floorplan({"rooms": [], "placements": []})
    assert "rooms fehlt" in appdata.validate_floorplan({"version": 1, "placements": []})
    assert "placements fehlt" in appdata.validate_floorplan({"version": 1, "rooms": []})
    assert appdata.validate_floorplan({**PLAN, "rooms": "x"}) != []
    assert appdata.validate_floorplan({**PLAN, "version": "1"}) != []
    assert appdata.validate_floorplan({**PLAN, "placements": [{"roomId": "k"}]}) != []


def test_floorplan_version_2_passes():
    # App 0.5.0: Räume als Polygone, Platzierungen als Rasterzellen, Draußen.
    plan_v2 = {
        "version": 2,
        "grid": {"cols": 27, "rows": 28, "cellMeters": 0.5},
        "rooms": [
            {"id": "salon", "name": "Salon", "areaId": "salon",
             "points": [[3, 3], [15, 3], [15, 10], [18, 10], [18, 15], [3, 15]]},
        ],
        "placements": [
            {"entityId": "light.tischlampe_salon", "x": 12, "y": 5, "kind": "light"},
            {"entityId": "sensor.sensor_terrasse_temperature", "x": 24, "y": 8, "deviceId": "0x00124b000000000d"},
        ],
        "coordinator": [19, 11],
        "outside": {"margin": 3},
        "updatedAt": 1763208000000,
    }
    assert appdata.validate_floorplan(plan_v2) == []
    doc = appdata.build_plan_doc(plan_v2, 1763208000000)
    assert appdata.read_plan_doc(doc)["plan"]["version"] == 2


def test_widgets():
    ok = [
        {"id": "power", "type": "power", "entities": [], "options": {}},
        {"id": "weather", "type": "weather", "entities": ["weather.forecast_home"], "options": {"a": 1}},
    ]
    assert appdata.validate_widgets(ok) == []
    assert appdata.validate_widgets([]) == []
    assert appdata.validate_widgets({}) != []
    assert appdata.validate_widgets([{"id": "a"}]) != []
    assert appdata.validate_widgets([ok[0], ok[0]]) != []
    assert appdata.validate_widgets([{"id": "a", "type": "x", "entities": "e"}]) != []


def test_require_valid_german_error():
    appdata.require_valid([], "X")
    try:
        appdata.require_valid(["version fehlt"], "Der Grundriss")
    except commands.Z2MError as err:
        assert err.code == "invalid_format" and "version fehlt" in err.message
    else:
        raise AssertionError


def test_prefs():
    ok = {"excluded_lights": ["light.salon", "light.zgkochbereich"]}
    assert appdata.validate_prefs(ok) == []
    assert appdata.validate_prefs({}) == []
    assert appdata.normalize_prefs({}) == {"excluded_lights": [], "locked": []}
    # Falsche Typen
    assert appdata.validate_prefs([]) != []
    assert appdata.validate_prefs({"excluded_lights": "light.a"}) != []
    assert appdata.validate_prefs({"excluded_lights": [1]}) != []
    # Keine Licht-IDs
    assert appdata.validate_prefs({"excluded_lights": ["switch.a"]}) != []
    assert appdata.validate_prefs({"excluded_lights": ["light."]}) != []
    # Duplikate werden still entfernt, Reihenfolge bleibt
    dup = {"excluded_lights": ["light.b", "light.a", "light.b"]}
    assert appdata.validate_prefs(dup) == []
    assert appdata.normalize_prefs(dup) == {"excluded_lights": ["light.b", "light.a"], "locked": []}
    # Unbekannte Schlüssel bleiben erhalten
    extra = {"excluded_lights": ["light.a"], "theme": {"dark": True}}
    assert appdata.validate_prefs(extra) == []
    assert appdata.normalize_prefs(extra) == {**extra, "locked": []}


def test_prefs_locked_devices():
    prefs = {"locked": ["0x00124b0000000041", "switch.steckdose_server", "switch.steckdose_server"]}
    assert appdata.validate_prefs(prefs) == []
    assert appdata.normalize_prefs(prefs)["locked"] == ["0x00124b0000000041", "switch.steckdose_server"]
    assert appdata.validate_prefs({"locked": "x"}) != []
    assert appdata.validate_prefs({"locked": [1]}) != []
    norm = appdata.normalize_prefs(prefs)
    assert appdata.is_locked(norm, "0x00124b0000000041")
    assert appdata.is_locked(norm, entities=["switch.steckdose_server"])
    assert not appdata.is_locked(norm, "0x00124b0000000042")
    # Schalten wird abgelehnt, Einstellen nicht
    assert appdata.controls({"state": "OFF"}) and appdata.controls({"state_l2": "OFF"})
    assert appdata.controls({"brightness": 10}) and appdata.controls({"color_temp": 300})
    assert not appdata.controls({"power_on_behavior": "on"}) and not appdata.controls({"child_lock": "LOCK"})


# --- 0.3.0: Events, updated_at/layout, Networkmap-Cache ---------------------------------


def test_event_data():
    assert appdata.build_event_data("prefs") == {"kind": "prefs"}
    assert appdata.build_event_data("widgets", "2026-11-15T12:00:00Z") == {
        "kind": "widgets",
        "updated_at": "2026-11-15T12:00:00Z",
    }
    assert appdata.build_event_data("floorplan", 1763208000000)["updated_at"] == 1763208000000


def test_updated_at_validation_and_resolution():
    assert appdata.validate_updated_at(None) == []
    assert appdata.validate_updated_at("2026-11-15T12:00:00Z") == []
    assert appdata.validate_updated_at(1763208000000) == []
    assert appdata.validate_updated_at(True) != []
    assert appdata.validate_updated_at({}) != []
    assert appdata.validate_layout(None) == [] and appdata.validate_layout({"a": 1}) == []
    assert appdata.validate_layout([]) != []
    assert appdata.resolve_updated_at("x", {"updatedAt": "y"}) == "x"
    assert appdata.resolve_updated_at(None, {"updatedAt": "y"}) == "y"
    assert appdata.resolve_updated_at(None, {"updatedAt": True}) is None
    assert appdata.resolve_updated_at(None, {}) is None


def test_plan_doc_keeps_unknown_fields():
    plan = {**PLAN, "updatedAt": "2026-11-15T12:00:00Z", "neu": {"x": 1}}
    stored = appdata.build_plan_doc(plan)
    assert stored["plan"] == plan and stored["updated_at"] == "2026-11-15T12:00:00Z"
    assert appdata.read_plan_doc(stored) == {"plan": plan, "updated_at": "2026-11-15T12:00:00Z", "revision": 0}
    assert appdata.build_plan_doc(plan, 5)["updated_at"] == 5
    # Alter Speicherstand ohne updated_at
    assert appdata.read_plan_doc({"plan": PLAN}) == {"plan": PLAN, "updated_at": None, "revision": 0}
    assert appdata.read_plan_doc(None) == {"plan": None, "updated_at": None, "revision": 0}


def test_widgets_doc():
    widgets = [{"id": "power", "type": "power", "entities": [], "options": {}, "extra": 1}]
    assert appdata.read_widgets_doc(None) == {"widgets": None, "layout": None, "updated_at": None, "revision": 0}
    # Alter Speicherstand (0.2.0)
    assert appdata.read_widgets_doc({"widgets": widgets}) == {
        "widgets": widgets,
        "layout": None,
        "updated_at": None,
        "revision": 0,
    }
    doc = appdata.build_widgets_doc(widgets, {"cols": 2}, "t", layout_given=True)
    assert appdata.read_widgets_doc(doc) == {"widgets": widgets, "layout": {"cols": 2}, "updated_at": "t", "revision": 0}
    # Alter Client sendet kein layout: bisheriges bleibt
    kept = appdata.build_widgets_doc(widgets, None, None, previous=doc)
    assert kept["layout"] == {"cols": 2} and kept["updated_at"] is None
    # layout: null löscht
    assert appdata.build_widgets_doc(widgets, None, None, previous=doc, layout_given=True)["layout"] is None


def test_networkmap_cache_roundtrip():
    nm = {"nodes": [{"ieeeAddr": "0x1"}], "links": [], "timestamp": "2026-11-15T12:00:00+00:00", "extra": 1}
    stored = appdata.serialize_networkmap_cache(nm)
    assert stored == {"networkmap": {"nodes": nm["nodes"], "links": [], "timestamp": nm["timestamp"]}}
    assert appdata.deserialize_networkmap_cache(stored) == stored["networkmap"]
    assert appdata.deserialize_networkmap_cache(None) is None
    assert appdata.deserialize_networkmap_cache({"networkmap": {"nodes": "x", "links": [], "timestamp": "t"}}) is None
    assert appdata.deserialize_networkmap_cache({"networkmap": {"nodes": [], "links": []}}) is None
    assert appdata.serialize_networkmap_cache(None) == {"networkmap": None}


def test_prefs_weather_sources():
    ok = {"weather": {"entity": "weather.forecast_home", "temperature": "sensor.aussen_temperature"}}
    assert appdata.validate_prefs(ok) == []
    assert appdata.normalize_prefs(ok)["weather"] == ok["weather"]
    assert appdata.validate_prefs({"weather": "x"}) != []
    assert appdata.validate_prefs({"weather": {"entity": 3}}) != []
    assert appdata.validate_prefs({"weather": {"wind": "sensor.x"}}) != []


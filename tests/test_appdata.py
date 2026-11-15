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
    assert appdata.normalize_prefs({}) == {"excluded_lights": []}
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
    assert appdata.normalize_prefs(dup) == {"excluded_lights": ["light.b", "light.a"]}
    # Unbekannte Schlüssel bleiben erhalten
    extra = {"excluded_lights": ["light.a"], "theme": {"dark": True}}
    assert appdata.validate_prefs(extra) == []
    assert appdata.normalize_prefs(extra) == extra

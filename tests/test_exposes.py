"""Tests für exposes.classify mit dem echten Bestand (42 Z2M-Geräte)."""

from __future__ import annotations

import importlib

import pytest

exposes = importlib.import_module("haven_nohass.exposes")
classify = exposes.classify


def _classified(devices, name):
    d = devices[name]
    return classify(d["exposes"], d["model"])


def _find(result, prop):
    return next((f for f in exposes.iter_features(result) if f["property"] == prop), None)


def test_inventory_size(inventory):
    assert len(inventory["z2m_devices"]) == 42


def test_all_devices_have_known_type(inventory):
    for d in inventory["z2m_devices"]:
        result = classify(d["exposes"], d["model"])
        assert result["device_type"] in exposes.DEVICE_TYPES
        assert result["device_type"] != "unknown", d["name"]


def test_every_feature_has_area(inventory):
    for d in inventory["z2m_devices"]:
        result = classify(d["exposes"], d["model"])
        for feat in exposes.iter_features(result):
            assert feat["area"] in exposes.AREAS, (d["name"], feat["property"])
        # Jedes Blatt-Expose kommt genau einmal vor (nichts verloren, nichts doppelt).
        leaves = [e.get("property") or e.get("name") for e in exposes._walk_leaves(d["exposes"])]
        out_leaves = [
            f["property"]
            for f in exposes.iter_features(result)
            if "features" not in f
        ]
        # color-Composites werden zu einem Eintrag mit Kindern (x, y, hue, saturation)
        assert sorted(leaves) == sorted(out_leaves), d["name"]


def test_areas_are_disjoint(inventory):
    for d in inventory["z2m_devices"]:
        result = classify(d["exposes"], d["model"])
        for area in exposes.AREAS:
            assert all(f["area"] == area for f in result[area])


def test_all_settings_have_german_label(inventory):
    for d in inventory["z2m_devices"]:
        result = classify(d["exposes"], d["model"])
        for feat in exposes.iter_features(result):
            if feat["area"] != "settings":
                continue
            assert feat["label"], (d["name"], feat["property"])
            assert feat["source"] == "table", (d["name"], feat["property"])
            assert feat["label"] != feat["property"]
            assert feat["help"], (d["name"], feat["property"])
            assert feat["group"], (d["name"], feat["property"])
            # Enum-Werte haben Labels
            for v in feat.get("values", []):
                assert v["label"], (d["name"], feat["property"], v)


def test_inventory_fully_covered_by_table(inventory):
    """Kein Inventar-Property läuft über den generischen Rückfall."""
    generic = set()
    for d in inventory["z2m_devices"]:
        generic.update(classify(d["exposes"], d["model"])["generic"])
    assert generic == set()


def test_sensitivity_is_detection_setting(devices):
    r = _classified(devices, "Bewegungsmelder Waschraum")
    assert r["device_type"] == "motion"
    feat = _find(r, "sensitivity")
    assert feat["area"] == "settings"
    assert feat["group"] == "Erkennung"
    assert feat["label"] == "Empfindlichkeit"
    assert [v["label"] for v in feat["values"]] == ["Niedrig", "Mittel", "Hoch"]
    assert _find(r, "keep_time")["group"] == "Erkennung"
    assert _find(r, "occupancy")["area"] == "primary"


def test_hue_lamp(devices):
    r = _classified(devices, "Tischlampe Salon")
    assert r["device_type"] == "light"
    assert {"brightness", "color_temp", "color_xy"} <= set(r["capabilities"])
    prim = {f["property"] for f in r["primary"]}
    assert {"state", "brightness", "color_temp", "color_xy"} <= prim
    hidden = {f["property"] for f in r["hidden"]}
    assert "color_hs" in hidden
    assert _find(r, "effect_speed") is None or _find(r, "effect_speed")["area"] == "hidden"
    assert _find(r, "power_on_behavior")["group"] == "Verhalten nach Stromausfall"


def test_tuya_hs_lamp_promotes_hs(devices):
    r = _classified(devices, "Lampe Waschraum")
    assert r["device_type"] == "light"
    assert "color_hs" in r["capabilities"]
    assert _find(r, "color_hs")["area"] == "primary"


def test_dimmer_has_no_color(devices):
    r = _classified(devices, "Kochbereich Arbeitslicht 1")
    assert r["device_type"] == "light"
    assert r["capabilities"] == ["brightness"]


def test_plug_meter(devices):
    r = _classified(devices, "Steckdose Messung 4")
    assert r["device_type"] == "plug_meter"
    prim = {f["property"] for f in r["primary"]}
    assert {"state", "power", "energy", "current", "voltage"} <= prim
    assert _find(r, "child_lock")["group"] == "Schutz"
    assert _find(r, "power_outage_memory")["group"] == "Verhalten nach Stromausfall"
    labels = {v["value"]: v["label"] for v in _find(r, "power_outage_memory")["values"]}
    assert labels["restore"] == "Letzter Zustand"


def test_voltage_of_battery_device_is_diagnostic(devices):
    r = _classified(devices, "Kontaktsensor Schrank")
    assert r["device_type"] == "contact"
    assert _find(r, "voltage")["area"] == "diagnostic"
    assert _find(r, "battery")["area"] == "diagnostic"
    assert _find(r, "contact")["area"] == "primary"


def test_knob(devices):
    r = _classified(devices, "Drehknopf Kochbereich")
    assert r["device_type"] == "knob"
    assert _find(r, "action")["area"] == "primary"
    assert _find(r, "action_step_size")["area"] == "hidden"
    assert _find(r, "operation_mode")["area"] == "settings"
    assert _find(r, "linkquality")["area"] == "diagnostic"


def test_button_is_remote(devices):
    assert _classified(devices, "Taster Medienwand")["device_type"] == "remote"


def test_climate_and_presence(devices):
    assert _classified(devices, "Sensor Waschraum")["device_type"] == "climate_sensor"
    r = _classified(devices, "Präsenz Waschraum")
    assert r["device_type"] == "presence"
    assert _find(r, "temperature_calibration")["group"] == "Kalibrierung"
    assert _find(r, "temperature_unit")["group"] == "Anzeige"
    assert _find(r, "fading_time")["group"] == "Erkennung"


def test_relay_and_switch(devices):
    assert _classified(devices, "Lüfter Waschraum")["device_type"] == "relay"
    assert _classified(devices, "Dekolampe")["device_type"] == "switch"


def test_composite_settings_have_labeled_children(devices):
    r = _classified(devices, "Steckdose Messung 2")
    prot = _find(r, "overload_protection")
    assert prot["area"] == "settings" and prot["group"] == "Schutz"
    assert all(c["label"] and c["group"] == "Schutz" for c in prot["features"])


def test_generic_fallback():
    unknown = [
        {"type": "numeric", "property": "foo_bar_level", "access": 7, "label": "Foo", "description": "x"},
        {"type": "numeric", "property": "foo_reading", "access": 1},
        {"type": "binary", "property": "foo_diag", "access": 1, "category": "diagnostic"},
        {"type": "enum", "property": "foo_mode", "access": 3, "values": ["a_b", "c"], "category": "config"},
    ]
    r = classify(unknown)
    assert r["device_type"] == "unknown"
    assert set(r["generic"]) == {"foo_bar_level", "foo_reading", "foo_diag", "foo_mode"}
    assert _find(r, "foo_bar_level")["area"] == "settings"
    assert _find(r, "foo_reading")["area"] == "primary"
    assert _find(r, "foo_diag")["area"] == "diagnostic"
    mode = _find(r, "foo_mode")
    assert mode["area"] == "settings" and mode["label"] == "Foo mode"
    assert [v["label"] for v in mode["values"]] == ["A b", "C"]


def test_endpoint_suffix_uses_table():
    r = classify(
        [{"type": "binary", "property": "child_lock_l1", "access": 7, "category": "config"}]
    )
    feat = _find(r, "child_lock_l1")
    assert feat["source"] == "table" and feat["label"].startswith("Kindersicherung")

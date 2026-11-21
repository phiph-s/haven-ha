"""Heizungsregelung (``heating.py``): Fähigkeiten, Validierung, Fenster, Sommer, Korrektur, Übernahme vom Gerät."""

from __future__ import annotations

import importlib

import pytest

ht = importlib.import_module("haven_nohass.heating")
Z2MError = importlib.import_module("haven_nohass.commands").Z2MError

A = "0x00000000000000c1"
B = "0x00000000000000c2"
T0 = 1_800_000_000.0

# Ausschnitt der Exposes eines Tuya TRV602Z (Zigbee2MQTT 2.x)
TRV602Z_EXPOSES = [
    {
        "type": "climate",
        "features": [
            {"name": "current_heating_setpoint", "property": "current_heating_setpoint", "access": 7,
             "value_min": 5, "value_max": 35, "value_step": 0.5},
            {"name": "local_temperature", "property": "local_temperature", "access": 1},
            {"name": "system_mode", "property": "system_mode", "access": 1, "values": ["auto", "heat", "off"]},
            {"name": "local_temperature_calibration", "property": "local_temperature_calibration", "access": 7,
             "value_min": -10, "value_max": 10, "value_step": 0.1},
            {"name": "running_state", "property": "running_state", "access": 1, "values": ["idle", "heat"]},
        ],
    },
    {"type": "enum", "name": "preset", "property": "preset", "access": 3,
     "values": ["off", "antifrost", "eco", "comfort", "auto", "on"]},
    {"type": "numeric", "name": "position", "property": "position", "access": 1},
]

# Dazu Komfort-Temperatur und Ventilverhalten (wie am echten Gerät)
TRV602Z_FULL = TRV602Z_EXPOSES + [
    {"type": "numeric", "name": "comfort_temperature", "property": "comfort_temperature", "access": 3,
     "value_min": 5, "value_max": 35},
    {"type": "enum", "name": "hysteresis", "property": "hysteresis", "access": 3, "values": ["comfort", "eco"]},
]

# Generisches Thermostat ohne Kalibrierung, Modus über system_mode
GENERIC_EXPOSES = [
    {
        "type": "climate",
        "features": [
            {"property": "occupied_heating_setpoint", "access": 7, "value_min": 7, "value_max": 30, "value_step": 0.5},
            {"property": "local_temperature", "access": 1},
            {"property": "system_mode", "access": 7, "values": ["off", "heat"]},
        ],
    }
]


def opts(**kw):
    return ht.normalize_options({"area_id": "salon", "trvs": [A], "sensors": ["sensor.salon_temperature"], **kw})


def trv(local=None, setpoint=None, calibration=0.0, mode="comfort", exposes=TRV602Z_EXPOSES, available=True, heating=None):
    caps = ht.trv_caps(exposes)
    values = {"local_temperature": local, caps.setpoint: setpoint, "preset": mode, "system_mode": mode}
    if caps.calibration:
        values[caps.calibration] = calibration
    if heating is not None:
        values["running_state"] = "heat" if heating else "idle"
    return ht.TrvInput(caps, ht.trv_reading(values, caps, available=available), "TRV")


def inputs(now=T0, trvs=None, sensors=None, windows=None, outdoor=None, display=None):
    return ht.HeatingInputs(
        now=now,
        trvs=trvs if trvs is not None else {A: trv(24.0, 21.0)},
        sensors=sensors if sensors is not None else {"sensor.salon_temperature": 20.0},
        windows=windows or {},
        outdoor=outdoor,
        display=display,
    )


def run(o, state, inp):
    state, events = ht.observe(o, state, inp)
    state, commands = ht.plan(o, state, inp)
    return state, commands, events


# --- Fähigkeiten -----------------------------------------------------------------------


def test_caps_trv602z():
    caps = ht.trv_caps(TRV602Z_EXPOSES)
    assert caps.setpoint == "current_heating_setpoint"
    assert (caps.min, caps.max, caps.step) == (5, 35, 0.5)
    assert caps.calibration == "local_temperature_calibration" and caps.cal_step == 0.1
    # system_mode ist beim TRV602Z nur lesbar: Modus über preset. Nie „on“ (Ventil dauerhaft offen),
    # sondern Komfort; „on“ und „auto“ (Wochenplan) heißen: das Gerät steuert selbst.
    assert (caps.mode_prop, caps.on_value, caps.off_value) == ("preset", "comfort", "off")
    assert caps.release_values == ("auto", "on") and caps.boost_values == ("on",)
    assert caps.comfort_temp is None and caps.hysteresis is None


def test_caps_trv602z_full():
    caps = ht.trv_caps(TRV602Z_FULL)
    assert caps.comfort_temp == "comfort_temperature" and caps.hysteresis == "hysteresis"


def test_caps_prefer_manual_and_system_mode_over_comfort():
    manual = [{"type": "enum", "property": "preset", "access": 3, "values": ["auto", "manual", "on", "off"]}]
    caps = ht.trv_caps(manual)
    assert (caps.mode_prop, caps.on_value) == ("preset", "manual")
    assert caps.release_values == ("auto", "on")
    both = GENERIC_EXPOSES + [{"type": "enum", "property": "preset", "access": 3, "values": ["comfort", "on"]}]
    assert (ht.trv_caps(both).mode_prop, ht.trv_caps(both).on_value) == ("system_mode", "heat")
    only_on = [{"type": "enum", "property": "preset", "access": 3, "values": ["on", "off"]}]
    assert ht.trv_caps(only_on).mode_prop is None


def test_caps_generic_system_mode():
    caps = ht.trv_caps(GENERIC_EXPOSES)
    assert caps.setpoint == "occupied_heating_setpoint"
    assert caps.calibration is None
    assert (caps.mode_prop, caps.on_value, caps.off_value) == ("system_mode", "heat", "off")


def test_caps_empty():
    caps = ht.trv_caps(None)
    assert caps.mode_prop is None and caps.calibration is None


# --- Validierung ------------------------------------------------------------------------


def test_validate_ok_and_defaults():
    c = ht.validate_config({"area_id": "salon", "trvs": [A]}, {}, thermostats={A: "TRV"}, known_areas={"salon"})
    assert c["calibration"] == "offset" and c["presets"] == ht.DEFAULT_PRESETS
    assert c["window_open_delay"] == 60 and c["window_close_delay"] == 120


@pytest.mark.parametrize(
    "doc",
    [
        {"area_id": "salon", "trvs": []},
        {"area_id": "salon", "trvs": [A, A]},
        {"area_id": "salon", "trvs": [A], "sensors": ["binary_sensor.x"]},
        {"area_id": "salon", "trvs": [A], "windows": ["sensor.x"]},
        {"area_id": "salon", "trvs": [A], "calibration": "pid"},
        {"area_id": "salon", "trvs": [A], "outdoor": {"entity": "light.x"}},
        {"area_id": "salon", "trvs": [A], "outdoor": {"entity": "weather.home", "threshold": 40}},
        {"area_id": "salon", "trvs": [A], "presets": {"eco": 2}},
        {"area_id": "salon", "trvs": [A], "window_open_delay": -1},
        {"trvs": [A]},
    ],
)
def test_validate_rejects(doc):
    with pytest.raises(Z2MError) as err:
        ht.validate_config(doc, {})
    assert err.value.code == "invalid_format"


def test_validate_trv_in_other_room():
    other = {"ruheraum": ht.normalize_options({"area_id": "ruheraum", "trvs": [A]})}
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [A]}, other, thermostats={A: "Heizung Ruheraum"})
    assert err.value.code == "device_taken" and "Heizung Ruheraum" in err.value.message
    # derselbe Raum darf sich selbst ersetzen
    ht.validate_config({"area_id": "ruheraum", "trvs": [A]}, other)


def test_validate_unknown_thermostat_and_area():
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [B]}, {}, thermostats={A: "TRV"})
    assert err.value.code == "not_found"
    with pytest.raises(Z2MError):
        ht.validate_config({"area_id": "keller", "trvs": [A]}, {}, known_areas={"salon"})


# --- Raumtemperatur ------------------------------------------------------------------------


def test_room_temperature_average_and_fallback():
    inp = inputs(sensors={"sensor.a": 20.0, "sensor.b": 21.0, "sensor.c": None})
    assert ht.room_temperature(inp) == (20.5, "sensors")
    inp = inputs(sensors={"sensor.a": None})
    assert ht.room_temperature(inp) == (24.0, "trv")


def test_sensor_value_rejects_stale_and_invalid():
    assert ht.sensor_value("20.4", 60) == 20.4
    assert ht.sensor_value("unavailable", 0) is None
    assert ht.sensor_value("20.4", ht.SENSOR_MAX_AGE + 1) is None
    assert ht.sensor_value("85", 0) is None
    assert ht.outdoor_value("weather", "sunny", {"temperature": 12.5}) == 12.5
    assert ht.contact_open("on") is True and ht.contact_open("unavailable") is None


# --- Offset-Korrektur ------------------------------------------------------------------------


def test_offset_calibration_written():
    o = opts()
    state, commands, _ = run(o, ht.HeatingState(target=21.0), inputs())
    # TRV misst 24 am Heizkörper, Raum hat 20: Kalibrierung -4, Sollwert bleibt das Ziel
    assert commands == {A: {"local_temperature_calibration": -4.0}}
    assert state.trvs[A].sent_calibration == -4.0


def test_offset_uses_raw_value_with_existing_calibration():
    o = opts()
    # TRV zeigt 22 bei Kalibrierung -2 (roh 24), Raum 20: neue Kalibrierung -4
    _, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(22.0, 21.0, calibration=-2.0)}))
    assert commands[A] == {"local_temperature_calibration": -4.0}


def test_offset_deadband_interval_and_settle():
    o = opts()
    st = ht.HeatingState(target=21.0)
    # kleine Abweichung: nichts
    _, commands, _ = run(o, st, inputs(trvs={A: trv(20.2, 21.0, calibration=0.0)}))
    assert commands == {}
    # gesendet, Gerät bestätigt, Messung steht noch aus: nicht nachlegen
    st, commands, _ = run(o, st, inputs(trvs={A: trv(24.0, 21.0, calibration=0.0)}))
    assert commands[A] == {"local_temperature_calibration": -4.0}
    st, commands, _ = run(o, st, inputs(now=T0 + 700, trvs={A: trv(24.0, 21.0, calibration=-4.0)}))
    assert commands == {}
    assert st.trvs[A].cal_changed_at == T0 + 700
    # neue Messung mit Kalibrierung (20,0): passt, nichts zu tun
    st, commands, _ = run(o, st, inputs(now=T0 + 800, trvs={A: trv(20.0, 21.0, calibration=-4.0)}))
    assert commands == {} and st.trvs[A].cal_changed_at is None
    # Raum kühlt dauerhaft ab, TRV merkt es nicht: gedämpft nachführen, je Schritt höchstens 0,5
    now = T0 + 800
    for _ in range(4):
        now += 900
        st, commands, _ = run(o, st, inputs(now=now, trvs={A: trv(20.0, 21.0, calibration=-4.0)}, sensors={"s": 18.0}))
    assert commands == {A: {"local_temperature_calibration": -4.5}}
    # gleich danach: erst nach 10 min wieder
    st, commands, _ = run(o, st, inputs(now=now + 50, trvs={A: trv(20.0, 21.0, calibration=-4.5)}, sensors={"s": 18.0}))
    assert commands == {}


def test_calibration_is_smoothed_and_holds_while_valve_switches():
    o = opts()
    st, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(22.0, 21.0, calibration=0.0)}))
    assert commands[A] == {"local_temperature_calibration": -2.0}
    st, _, _ = run(o, st, inputs(now=T0 + 60, trvs={A: trv(20.0, 21.0, calibration=-2.0)}))
    # Kurzer Ausreißer am Heizkörper (Ventil geht auf): die Glättung folgt kaum
    hot = trv(23.0, 21.0, calibration=-2.0)
    hot.reading.valve = 100
    st, _, _ = run(o, st, inputs(now=T0 + 700, trvs={A: trv(20.0, 21.0, calibration=-2.0)}))
    st.trvs[A].seen_valve = 0
    st, commands, _ = run(o, st, inputs(now=T0 + 760, trvs={A: hot}))
    assert st.trvs[A].valve_changed_at == T0 + 760
    # Ventil hat gerade umgeschaltet: nicht nachkalibrieren
    assert commands == {}
    assert -2.0 > st.trvs[A].corr_ema > -3.0


def test_device_open_valve_releases_trv_and_returns_after_a_while():
    o = opts(calibration="none")
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    # Jemand stellt am Gerät „on“ (Ventil offen): Haven gibt frei statt zurückzustellen
    st, commands, events = run(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 21.0, mode="on")}))
    assert events == ["released:on"] and commands == {}
    assert st.mode == "heat" and st.target == 21.0
    s = ht.status(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 21.0, mode="on")}))
    assert s["released"] is True and s["trvs"][0]["released_mode"] == "on"
    assert s["trvs"][0]["release_until"] == T0 + 100 + ht.BOOST_RETURN
    assert ht.next_wake(o, st) == T0 + 100 + ht.BOOST_RETURN
    # Sollwert am Gerät ändert sich: keine Übernahme, solange freigegeben
    st, commands, events = run(o, st, inputs(now=T0 + 200, trvs={A: trv(20.0, 30.0, mode="on")}))
    assert events == [] and commands == {} and st.target == 21.0
    # Nach BOOST_RETURN übernimmt Haven wieder
    later = T0 + 100 + ht.BOOST_RETURN
    st, commands, events = run(o, st, inputs(now=later, trvs={A: trv(20.0, 30.0, mode="on")}))
    assert events == ["resume_boost"]
    assert commands == {A: {"preset": "comfort", "current_heating_setpoint": 21.0}}


def test_device_schedule_releases_until_app_takes_over():
    o = opts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0, calibration=0.0)}))
    st, commands, events = run(o, st, inputs(now=T0 + 100, trvs={A: trv(24.0, 21.0, calibration=-4.0, mode="auto")}))
    assert events == ["released:auto"] and st.trvs[A].release_until is None
    assert "preset" not in commands.get(A, {}) and "current_heating_setpoint" not in commands.get(A, {})
    # Kalibrierung läuft weiter (Wochenplan regelt dann nach dem Raum)
    assert ht.desired(o, st, inputs(now=T0 + 100, trvs={A: trv(24.0, 21.0, calibration=-4.0, mode="auto")}))[A].calibration == -4.0
    # Wochenplan ändert den Sollwert: keine Übernahme
    st, _, events = run(o, st, inputs(now=T0 + 3000, trvs={A: trv(20.0, 15.0, calibration=-4.0, mode="auto")}))
    assert events == [] and st.target == 21.0
    # „Wieder regeln“ in der App
    st = ht.control(st, now=T0 + 3100)
    assert st.trvs[A].released_at is None
    _, commands, _ = run(o, st, inputs(now=T0 + 3100, trvs={A: trv(20.0, 15.0, calibration=-4.0, mode="auto")}))
    assert commands[A]["preset"] == "comfort" and commands[A]["current_heating_setpoint"] == 21.0


def test_released_without_calibration_resets_it():
    o = opts(calibrate_released=False)
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0, calibration=0.0)}))
    st, commands, _ = run(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 21.0, calibration=-4.0, mode="auto")}))
    assert commands == {A: {"local_temperature_calibration": 0.0}}


def test_device_back_to_comfort_ends_release_and_sets_room_mode():
    o = opts(calibration="none")
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    st, _, _ = run(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 21.0, mode="auto")}))
    st, _, events = run(o, st, inputs(now=T0 + 200, trvs={A: trv(20.0, 21.0, mode="eco")}))
    assert events == ["resume_device", "manual_mode:eco"] and st.trvs[A].released_at is None


def test_lock_manual_never_releases():
    o = opts(calibration="none", lock_manual=True)
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    st, commands, events = run(o, st, inputs(now=T0 + 400, trvs={A: trv(20.0, 21.0, mode="on")}))
    assert events == [] and commands == {A: {"preset": "comfort"}}


def test_comfort_temperature_and_hysteresis_follow():
    o = opts(calibration="none")
    caps = ht.trv_caps(TRV602Z_FULL)
    values = {"local_temperature": 20.0, "current_heating_setpoint": 21.0, "preset": "comfort",
              "comfort_temperature": 20.0, "hysteresis": "eco"}
    t = ht.TrvInput(caps, ht.trv_reading(values, caps), "TRV")
    st, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: t}))
    assert commands == {A: {"comfort_temperature": 21.0, "hysteresis": "comfort"}}
    # nicht bei jeder Meldung erneut
    _, commands, _ = run(o, st, inputs(now=T0 + 60, trvs={A: t}))
    assert commands == {}


def test_detects_whether_reported_temperature_includes_calibration():
    o = opts()
    # Gerät A meldet kalibriert: nach -4 springt die Messung von 24 auf 20
    st, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0, calibration=0.0)}))
    assert commands[A] == {"local_temperature_calibration": -4.0}
    st, _, _ = run(o, st, inputs(now=T0 + 30, trvs={A: trv(24.0, 21.0, calibration=-4.0)}))
    st, commands, _ = run(o, st, inputs(now=T0 + 60, trvs={A: trv(20.1, 21.0, calibration=-4.0)}))
    assert st.trvs[A].cal_included is True and commands == {}


def test_device_reporting_raw_temperature_does_not_run_away():
    o = opts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0, calibration=0.0)}))
    st, _, _ = run(o, st, inputs(now=T0 + 30, trvs={A: trv(24.0, 21.0, calibration=-4.0)}))
    # Messung bleibt roh (24,1): Kalibrierung steckt nicht drin
    st, _, _ = run(o, st, inputs(now=T0 + 60, trvs={A: trv(24.1, 21.0, calibration=-4.0)}))
    assert st.trvs[A].cal_included is False
    # Später: ohne Erkennung hielte Haven 24,1 − (−4) = 28,1 für roh und wollte −8,1
    st, commands, _ = run(o, st, inputs(now=T0 + 3000, trvs={A: trv(24.1, 21.0, calibration=-4.0)}))
    assert commands == {}
    assert ht.desired(o, st, inputs(trvs={A: trv(24.1, 21.0, calibration=-4.0)}))[A].calibration == -4.1


def test_offset_clamped_to_device_limits():
    o = opts()
    _, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(35.0, 21.0)}, sensors={"s": 19.0}))
    assert commands[A]["local_temperature_calibration"] == -10.0


def test_offset_without_calibration_falls_back_to_target():
    o = opts()
    inp = inputs(trvs={A: trv(24.0, 21.0, exposes=GENERIC_EXPOSES, mode="heat")})
    state, commands, _ = run(o, ht.HeatingState(target=21.0), inp)
    assert commands == {A: {"occupied_heating_setpoint": 25.0}}
    assert ht.status(o, state, inp)["trvs"][0]["method"] == "target"


def test_sensor_failure_keeps_calibration():
    o = opts()
    st, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(22.0, 21.0, calibration=-2.0)}, sensors={"s": None}))
    assert commands == {}
    status = ht.status(o, st, inputs(trvs={A: trv(22.0, 21.0, calibration=-2.0)}, sensors={"s": None}))
    assert status["room_source"] == "trv" and status["trvs"][0]["method"] == "none"


# --- Sollwert-Korrektur -------------------------------------------------------------------------


def test_target_mode_shifts_setpoint_and_throttles():
    o = opts(calibration="target")
    st = ht.HeatingState(target=21.0)
    st, commands, _ = run(o, st, inputs(trvs={A: trv(24.0, 21.0)}))
    assert commands == {A: {"current_heating_setpoint": 25.0}}
    assert st.trvs[A].shift == 4.0
    # Gerät bestätigt, TRV wird wärmer: kleine Änderung erst nach 10 min
    st, commands, _ = run(o, st, inputs(now=T0 + 60, trvs={A: trv(25.0, 25.0)}))
    assert commands == {}
    st, commands, _ = run(o, st, inputs(now=T0 + 700, trvs={A: trv(25.0, 25.0)}))
    assert commands == {A: {"current_heating_setpoint": 26.0}}


def test_target_mode_user_change_is_immediate():
    o = opts(calibration="target")
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0)}))
    st = ht.control(st, target=22.0, now=T0 + 30)
    st, commands, _ = run(o, st, inputs(now=T0 + 30, trvs={A: trv(24.0, 25.0)}))
    assert commands == {A: {"current_heating_setpoint": 26.0}}


def test_none_mode_groups_trvs():
    o = opts(calibration="none", trvs=[A, B])
    inp = inputs(trvs={A: trv(24.0, 21.0), B: trv(23.0, 19.0)})
    _, commands, _ = run(o, ht.HeatingState(target=21.0), inp)
    assert commands == {B: {"current_heating_setpoint": 21.0}}


def test_switching_away_from_offset_resets_calibration():
    st, _, _ = run(opts(), ht.HeatingState(target=21.0), inputs())
    assert st.trvs[A].sent_calibration == -4.0
    st, commands, _ = run(opts(calibration="none"), st, inputs(now=T0 + 60, trvs={A: trv(20.0, 21.0, calibration=-4.0)}))
    assert commands == {A: {"local_temperature_calibration": 0.0}}


# --- Betriebsarten -----------------------------------------------------------------------------------


def test_presets_and_off():
    o = opts(calibration="none", presets={"eco": 17.5})
    st = ht.control(ht.HeatingState(), mode="eco", now=T0)
    _, commands, _ = run(o, st, inputs(trvs={A: trv(20.0, 21.0)}))
    assert commands == {A: {"current_heating_setpoint": 17.5}}
    st = ht.control(st, mode="off", now=T0 + 1)
    _, commands, _ = run(o, st, inputs(now=T0 + 1, trvs={A: trv(20.0, 17.5)}))
    assert commands == {A: {"preset": "off"}}


def test_device_in_schedule_mode_is_taken_over_at_setup():
    o = opts(calibration="none")
    _, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0, mode="auto")}))
    assert commands == {A: {"preset": "comfort"}}


def test_old_open_valve_preset_is_replaced():
    # Frühere Versionen haben „on“ gesendet: das Gerät steht noch so, ohne dass es jemand gestellt hat
    o = opts(calibration="none")
    st = ht.HeatingState(target=21.0, trvs={A: ht.TrvMemo(seen_mode="on", seen_setpoint=21.0, sent_mode="on")})
    st, commands, events = run(o, st, inputs(trvs={A: trv(20.0, 21.0, mode="on")}))
    assert events == [] and commands == {A: {"preset": "comfort"}}


def test_off_without_off_value_uses_minimum():
    exposes = [{"type": "climate", "features": [
        {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 30, "value_step": 0.5}]}]
    o = opts(calibration="none")
    st = ht.control(ht.HeatingState(), mode="off", now=T0)
    _, commands, _ = run(o, st, inputs(trvs={A: trv(20.0, 21.0, exposes=exposes)}))
    assert commands == {A: {"current_heating_setpoint": 5.0}}


def test_control_validation():
    with pytest.raises(Z2MError):
        ht.control(ht.HeatingState(), mode="party", now=T0)
    with pytest.raises(Z2MError):
        ht.control(ht.HeatingState(), target=40, now=T0)
    st = ht.control(ht.HeatingState(mode="eco"), target=22.3, now=T0)
    assert (st.mode, st.target) == ("heat", 22.5)


# --- Fenster ---------------------------------------------------------------------------------------


def test_window_pause_after_delay_and_resume():
    o = opts(windows=["binary_sensor.fenster"], window_open_delay=60, window_close_delay=120, calibration="none")
    st = ht.HeatingState(target=21.0)
    st, commands, events = run(o, st, inputs(trvs={A: trv(20.0, 21.0)}, windows={"binary_sensor.fenster": True}))
    assert commands == {} and events == [] and ht.next_wake(o, st) == T0 + 60
    st, commands, events = run(o, st, inputs(now=T0 + 60, trvs={A: trv(20.0, 21.0)}, windows={"binary_sensor.fenster": True}))
    assert events == ["window_pause"] and commands == {A: {"preset": "off"}}
    assert ht.status(o, st, inputs(windows={"binary_sensor.fenster": True}))["reason"] == "window"
    # zu, aber erst nach der Verzögerung wieder heizen
    st, commands, _ = run(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 21.0, mode="off")}, windows={"binary_sensor.fenster": False}))
    assert commands == {} and ht.next_wake(o, st) == T0 + 220
    st, commands, events = run(o, st, inputs(now=T0 + 220, trvs={A: trv(20.0, 21.0, mode="off")}, windows={"binary_sensor.fenster": False}))
    assert events == ["window_resume"] and commands == {A: {"preset": "comfort"}}


def test_short_window_opening_is_ignored():
    o = opts(windows=["binary_sensor.fenster"], window_open_delay=60)
    st, _, _ = run(o, ht.HeatingState(), inputs(windows={"binary_sensor.fenster": True}))
    st, _, _ = run(o, st, inputs(now=T0 + 30, windows={"binary_sensor.fenster": False}))
    assert not st.paused and st.window_open_since is None and ht.next_wake(o, st) is None


def test_unavailable_contact_counts_as_closed():
    o = opts(windows=["binary_sensor.fenster"], window_open_delay=0)
    st, _, _ = run(o, ht.HeatingState(), inputs(windows={"binary_sensor.fenster": None}))
    assert not st.paused


# --- Sommer ---------------------------------------------------------------------------------------


def test_summer_pause_with_smoothing_and_hysteresis():
    o = opts(outdoor={"entity": "sensor.aussen", "threshold": 16}, calibration="none")
    st, _, _ = run(o, ht.HeatingState(), inputs(outdoor=14.0))
    assert st.outdoor_ema == 14.0 and not st.summer
    # Kurzer Ausreißer auf 25 °C reicht nicht (geglättet)
    st, _, _ = run(o, st, inputs(now=T0 + 600, outdoor=25.0))
    assert not st.summer and 14 < st.outdoor_ema < 16
    # Lange warm: Sommerpause
    t = T0 + 600
    for _ in range(12):
        t += 3600
        st, commands, events = run(o, st, inputs(now=t, trvs={A: trv(20.0, 21.0)}, outdoor=20.0))
        if st.summer:
            break
    assert st.summer and "summer_start" in events and commands == {A: {"preset": "off"}}
    # knapp unter der Schwelle bleibt es Sommer (Hysterese)
    st.outdoor_ema = 15.5
    st, _, _ = run(o, st, inputs(now=t + 1, outdoor=15.5))
    assert st.summer
    st.outdoor_ema = 14.9
    st, _, events = run(o, st, inputs(now=t + 2, outdoor=14.9))
    assert not st.summer and events == ["summer_end"]


# --- Änderungen am Gerät ------------------------------------------------------------------------


def test_knob_on_trv_sets_room_target_for_all():
    o = opts(calibration="none", trvs=[A, B])
    st = ht.HeatingState(target=21.0)
    st, _, _ = run(o, st, inputs(trvs={A: trv(20.0, 21.0), B: trv(20.0, 21.0)}))
    st, commands, events = run(o, st, inputs(now=T0 + 300, trvs={A: trv(20.0, 23.0), B: trv(20.0, 21.0)}))
    assert events == ["manual_target:23.0"]
    assert st.target == 23.0 and st.mode == "heat"
    assert commands == {B: {"current_heating_setpoint": 23.0}}


def test_own_confirmation_is_not_a_manual_change():
    o = opts(calibration="none", trvs=[A])
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    st = ht.control(st, target=22.0, now=T0 + 10)
    st, commands, _ = run(o, st, inputs(now=T0 + 10, trvs={A: trv(20.0, 21.0)}))
    assert commands == {A: {"current_heating_setpoint": 22.0}}
    st = ht.control(st, target=22.5, now=T0 + 12)
    st, commands, _ = run(o, st, inputs(now=T0 + 12, trvs={A: trv(20.0, 21.0)}))
    # späte Bestätigung des vorigen Werts ist keine Handarbeit
    st, _, events = run(o, st, inputs(now=T0 + 15, trvs={A: trv(20.0, 22.0)}))
    assert events == [] and st.target == 22.5
    st, _, events = run(o, st, inputs(now=T0 + 20, trvs={A: trv(20.0, 22.5)}))
    assert events == [] and st.target == 22.5


def test_target_mode_adoption_removes_shift():
    o = opts(calibration="target")
    st, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(24.0, 21.0)}))
    assert commands[A] == {"current_heating_setpoint": 25.0}
    st, _, _ = run(o, st, inputs(now=T0 + 30, trvs={A: trv(24.0, 25.0)}))
    # Hand am Gerät: 26 am TRV heißt 22 im Raum
    st, _, events = run(o, st, inputs(now=T0 + 400, trvs={A: trv(24.0, 26.0)}))
    assert st.target == 22.0 and events == ["manual_target:22.0"]


def test_preset_button_on_trv_switches_room_mode():
    o = opts(calibration="none")
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    # Taste am TRV: Gerät geht auf seine eigene Eco-Temperatur (hier 19)
    st, commands, events = run(o, st, inputs(now=T0 + 100, trvs={A: trv(20.0, 19.0, mode="eco")}))
    assert st.mode == "eco" and events == ["manual_mode:eco"]
    # Haven setzt das Gerät zurück auf Komfort mit der Eco-Temperatur des Raums
    assert commands == {A: {"preset": "comfort", "current_heating_setpoint": 18.0}}


def test_lock_manual_reverts():
    o = opts(calibration="none", lock_manual=True)
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0)}))
    st, commands, events = run(o, st, inputs(now=T0 + 400, trvs={A: trv(20.0, 25.0)}))
    assert events == [] and st.target == 21.0
    assert commands == {A: {"current_heating_setpoint": 21.0}}


def test_resend_when_unconfirmed():
    o = opts(calibration="none")
    st, commands, _ = run(o, ht.HeatingState(target=22.0), inputs(trvs={A: trv(20.0, 21.0)}))
    assert commands
    st, commands, _ = run(o, st, inputs(now=T0 + 60, trvs={A: trv(20.0, 21.0)}))
    assert commands == {}
    st, commands, _ = run(o, st, inputs(now=T0 + ht.RESEND + 1, trvs={A: trv(20.0, 21.0)}))
    assert commands == {A: {"current_heating_setpoint": 22.0}}


def test_unavailable_trv_gets_no_commands():
    o = opts(calibration="none")
    _, commands, _ = run(o, ht.HeatingState(target=22.0), inputs(trvs={A: trv(20.0, 21.0, available=False)}))
    assert commands == {}


def test_disabled_does_nothing():
    o = opts(enabled=False)
    _, commands, _ = run(o, ht.HeatingState(target=22.0), inputs())
    assert commands == {}


# --- Status und Store ----------------------------------------------------------------------------


def test_status_shape():
    o = opts(windows=["binary_sensor.fenster"], outdoor={"entity": "weather.home", "threshold": 16})
    st, _, _ = run(o, ht.HeatingState(target=21.0), inputs(outdoor=8.0, windows={"binary_sensor.fenster": False}))
    s = ht.status(o, st, inputs(outdoor=8.0, trvs={A: trv(24.0, 21.0, heating=True)}))
    assert s["room_temperature"] == 20.0 and s["room_source"] == "sensors"
    assert s["effective_target"] == 21.0 and s["reason"] is None and s["heating"] is True
    t = s["trvs"][0]
    assert t["method"] == "offset" and t["correction"] == -4.0 and t["desired_calibration"] == -4.0
    assert s["outdoor"]["value"] == 8.0 and s["outdoor"]["summer"] is False


def test_state_roundtrip_and_store():
    st, _, _ = run(opts(), ht.HeatingState(target=21.0), inputs())
    again = ht.HeatingState.from_dict(st.to_dict())
    assert again == st
    assert ht.HeatingState.from_dict({"mode": "party", "target": "x"}).mode == "heat"
    store = ht.read_store({"rooms": {"salon": {"trvs": [A]}}, "state": {"salon": st.to_dict(), "weg": {}}})
    assert store["rooms"]["salon"]["area_id"] == "salon" and list(store["state"]) == ["salon"]
    assert ht.read_store(None) == ht.empty_store()


# --- Display (Wandthermostat als Bedienung des Raums) ----------------------------------------------

D = "0x00000000000000d1"

# Wie ein Beok BOT-R9V-ZB (Tuya): Ein/Aus über system_mode, preset nur manual/auto
BEOK_EXPOSES = [
    {
        "type": "climate",
        "features": [
            {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 35, "value_step": 0.5},
            {"property": "local_temperature", "access": 1},
            {"property": "system_mode", "access": 7, "values": ["off", "heat"]},
            {"property": "preset", "access": 7, "values": ["manual", "auto"]},
            {"property": "running_state", "access": 1, "values": ["idle", "heat"]},
            {"property": "local_temperature_calibration", "access": 7, "value_min": -9, "value_max": 9, "value_step": 1},
        ],
    },
    {"type": "binary", "property": "child_lock", "access": 3, "value_on": "LOCK", "value_off": "UNLOCK"},
]

# Display mit Modus über preset, inklusive Aus und eigenem Wochenplan; Sollwert in ganzen Grad, 10 bis 25
PRESET_DISPLAY = [
    {
        "type": "climate",
        "features": [
            {"property": "current_heating_setpoint", "access": 7, "value_min": 10, "value_max": 25, "value_step": 1},
            {"property": "local_temperature", "access": 1},
        ],
    },
    {"type": "enum", "property": "preset", "access": 3, "values": ["manual", "auto", "off"]},
]


def disp(local=21.0, setpoint=21.0, mode="heat", exposes=BEOK_EXPOSES, available=True, heating=None,
         preset="manual", eco=18.0, battery=None):
    caps = ht.display_caps(exposes)
    values = {"local_temperature": local, caps.setpoint: setpoint, "local_temperature_calibration": 0}
    if caps.mode_prop:
        values[caps.mode_prop] = mode
    if caps.preset_prop:
        values[caps.preset_prop] = preset
    if caps.eco_temp_prop:
        values[caps.eco_temp_prop] = eco
    if battery is not None:
        values["battery"] = battery
    if heating is not None:
        values["running_state"] = "heat" if heating else "idle"
    return ht.TrvInput(caps, ht.trv_reading(values, caps, available=available), "Display Salon")


def dopts(**kw):
    return opts(**{"display": D, "calibration": "none", **kw})


def dinp(now=T0, display=None, trvs=None, **kw):
    return inputs(now=now, trvs=trvs if trvs is not None else {A: trv(20.0, 21.0)},
                  display=display if display is not None else disp(), **kw)


def test_display_caps_power_over_system_mode():
    assert (ht.trv_caps(BEOK_EXPOSES).mode_prop, ht.trv_caps(BEOK_EXPOSES).off_value) == ("preset", None)
    caps = ht.display_caps(BEOK_EXPOSES)
    assert (caps.mode_prop, caps.on_value, caps.off_value) == ("system_mode", "heat", "off")
    assert caps.release_values == ()
    caps = ht.display_caps(PRESET_DISPLAY)
    assert (caps.mode_prop, caps.on_value, caps.off_value, caps.release_values) == ("preset", "manual", "off", ("auto",))
    assert (caps.min, caps.max, caps.step) == (10, 25, 1)


def test_display_store_roundtrip():
    assert ht.empty_store()["displays"] == [] and ht.read_store(None)["displays"] == []
    assert ht.read_store({"rooms": {}})["displays"] == []
    assert ht.read_store({"displays": "x"})["displays"] == []
    store = ht.read_store(
        {
            "displays": [D, D, 5, "", None, B],
            "rooms": {
                "salon": {"trvs": [A], "display": D},
                "bad": {"trvs": [A], "display": "0xunmarked"},
                "alt": {"trvs": [A]},
            },
        }
    )
    assert store["displays"] == [D, B]
    assert store["rooms"]["salon"]["display"] == D
    assert store["rooms"]["bad"]["display"] is None and store["rooms"]["alt"]["display"] is None
    assert ht.default_options()["display"] is None


def test_validate_display_ok():
    doc = {"area_id": "salon", "trvs": [A], "display": D}
    c = ht.validate_config(doc, {}, thermostats={A: "TRV", D: "Display"}, displays={D})
    assert c["display"] == D
    assert ht.validate_config({"area_id": "salon", "trvs": [A], "display": None}, {}, displays={D})["display"] is None
    # trvs bleibt Pflicht, auch mit Display
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [], "display": D}, {}, displays={D})
    assert err.value.code == "invalid_format"


@pytest.mark.parametrize("display", [5, "", ["x"], A])
def test_validate_display_shape(display):
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [A], "display": display}, {}, displays={A, D})
    assert err.value.code == "invalid_format"


def test_validate_display_must_be_marked():
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [A], "display": D}, {}, thermostats={A: "TRV", D: "Wand"}, displays=set())
    assert err.value.code == "invalid_format" and "Wand ist nicht als Display markiert" in err.value.message


def test_validate_display_taken_by_other_room():
    other = {"ruheraum": ht.normalize_options({"area_id": "ruheraum", "trvs": [B], "display": D})}
    with pytest.raises(Z2MError) as err:
        ht.validate_config(
            {"area_id": "salon", "trvs": [A], "display": D}, other, thermostats={A: "TRV", D: "Wand"}, displays={D}
        )
    assert err.value.code == "device_taken" and "Wand" in err.value.message
    # derselbe Raum darf sein Display behalten
    ht.validate_config({"area_id": "ruheraum", "trvs": [B], "display": D}, other, displays={D})


def test_validate_trvs_must_not_contain_display():
    with pytest.raises(Z2MError) as err:
        ht.validate_config({"area_id": "salon", "trvs": [A, D]}, {}, thermostats={A: "TRV", D: "Wand"}, displays={D})
    assert err.value.code == "device_taken" and "Wand ist als Display markiert" in err.value.message


def test_display_counts_as_room_thermometer():
    inp = inputs(sensors={"sensor.a": 20.0, "sensor.b": None}, display=disp(local=22.0))
    assert ht.room_temperature(inp) == (21.0, "sensors")
    inp = inputs(sensors={"sensor.a": None}, display=disp(local=22.0))
    assert ht.room_temperature(inp) == (22.0, "display")
    inp = inputs(sensors={}, display=disp(local=22.0, available=False))
    assert ht.room_temperature(inp) == (24.0, "trv")
    inp = inputs(sensors={}, display=disp(local=85.0))
    assert ht.room_temperature(inp) == (24.0, "trv")
    assert ht.method_for({"calibration": "offset"}, ht.trv_caps(TRV602Z_EXPOSES), "display") == "offset"


def test_display_drives_calibration_but_is_never_calibrated():
    o = dopts(calibration="offset", sensors=[])
    st = ht.HeatingState(target=21.0)
    for i in range(6):
        now = T0 + i * (ht.CAL_SETTLE + 1)
        st, commands, _ = run(o, st, dinp(now=now, sensors={}, trvs={A: trv(24.0, 21.0)}, display=disp(local=20.0, setpoint=21.0)))
        assert "local_temperature_calibration" not in commands.get(D, {})
        if i == 0:
            # TRV misst 24, Display (Raum) 20: Kalibrierung -4 wie mit einem Thermometer
            assert commands[A] == {"local_temperature_calibration": -4.0}
    assert st.trvs[A].corr_ema == -4.0
    s = ht.status(o, st, dinp(sensors={}, trvs={A: trv(24.0, 21.0)}, display=disp(local=20.0)))
    assert s["room_source"] == "display" and s["trvs"][0]["method"] == "offset"


def test_display_setpoint_sets_room_target():
    o = dopts(trvs=[A, B])
    trvs = {A: trv(20.0, 21.0), B: trv(20.0, 21.0)}
    st, commands, events = run(o, ht.HeatingState(target=21.0), dinp(trvs=trvs))
    assert commands == {} and events == []  # erstes Sehen: nur merken
    st, commands, events = run(o, st, dinp(now=T0 + 300, trvs=trvs, display=disp(setpoint=23.0)))
    assert events == ["display_target:23.0"] and st.target == 23.0 and st.mode == "heat"
    assert st.changed_at == T0 + 300
    assert commands == {A: {"current_heating_setpoint": 23.0}, B: {"current_heating_setpoint": 23.0}}


def test_display_setpoint_switches_preset_room_to_heat():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="eco"), dinp(display=disp(setpoint=18.0)))
    st, _, events = run(o, st, dinp(now=T0 + 100, display=disp(setpoint=19.5)))
    assert st.mode == "heat" and st.target == 19.5 and events == ["display_target:19.5"]


def test_display_own_commands_are_not_adopted_but_turning_back_is():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st = ht.control(st, target=22.0, now=T0 + 10)
    st, commands, _ = run(o, st, dinp(now=T0 + 10))
    assert commands[D] == {"current_heating_setpoint": 22.0}
    # Bestätigung: keine Bedienung
    st, commands, events = run(o, st, dinp(now=T0 + 12, trvs={A: trv(20.0, 22.0)}, display=disp(setpoint=22.0)))
    assert events == [] and commands == {}
    # Zurückdrehen auf den alten Wert gilt wieder als Bedienung
    st, commands, events = run(o, st, dinp(now=T0 + 400, trvs={A: trv(20.0, 22.0)}, display=disp(setpoint=21.0)))
    assert events == ["display_target:21.0"] and st.target == 21.0
    assert commands == {A: {"current_heating_setpoint": 21.0}}


def test_display_late_confirmation_of_previous_value():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st = ht.control(st, target=22.0, now=T0 + 10)
    st, _, _ = run(o, st, dinp(now=T0 + 10))
    st = ht.control(st, target=22.5, now=T0 + 12)
    st, commands, _ = run(o, st, dinp(now=T0 + 12))
    assert commands[D] == {"current_heating_setpoint": 22.5}
    st, _, events = run(o, st, dinp(now=T0 + 15, display=disp(setpoint=22.0)))
    assert events == [] and st.target == 22.5
    st, _, events = run(o, st, dinp(now=T0 + 20, display=disp(setpoint=22.5)))
    assert events == [] and st.target == 22.5


def test_display_adopted_with_lock_manual_and_pauses():
    # Sommerpause: das Display bleibt an, zeigt das Ziel und wird auch mit lock_manual bedient
    o = dopts(lock_manual=True, outdoor={"entity": "weather.home", "threshold": 16})
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp(outdoor=20.0))
    assert st.summer
    st, commands, events = run(o, st, dinp(now=T0 + 300, display=disp(setpoint=23.0), outdoor=20.0))
    assert events == ["display_target:23.0"] and st.target == 23.0
    assert D not in commands
    s = ht.status(o, st, dinp(now=T0 + 300, display=disp(setpoint=23.0), outdoor=20.0))
    assert s["reason"] == "summer" and s["display"]["pending"] is False
    # Fensterpause, das Display ist noch an (Aus unterwegs): Drehen gilt trotzdem
    o = dopts(lock_manual=True, windows=["binary_sensor.fenster"], window_open_delay=0)
    st, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(windows={"binary_sensor.fenster": True}))
    assert st.paused and commands[D] == {"system_mode": "off"}
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=disp(setpoint=23.0), windows={"binary_sensor.fenster": True}))
    assert events == ["display_target:23.0"] and st.target == 23.0 and st.paused
    assert D not in commands  # Aus ist schon unterwegs, kein Sollwert
    # Ein TRV dagegen wird mit lock_manual zurückgestellt
    o = dopts(lock_manual=True)
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st, commands, events = run(o, st, dinp(now=T0 + 300, trvs={A: trv(20.0, 25.0)}))
    assert events == [] and commands == {A: {"current_heating_setpoint": 21.0}}


def test_display_power_switches_room_mode():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="eco"), dinp(display=disp(setpoint=18.0)))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=disp(setpoint=18.0, mode="off")))
    assert st.mode == "off" and events == ["display_mode:off"]
    # TRV aus, Display ist schon aus: nichts an das Display
    assert commands == {A: {"preset": "off"}}
    st, commands, events = run(
        o, st, dinp(now=T0 + 200, trvs={A: trv(20.0, 21.0, mode="off")}, display=disp(setpoint=18.0, mode="heat"))
    )
    # Einschalten aus Aus führt zu Manuell mit dem letzten Ziel
    assert st.mode == "heat" and events == ["display_mode:heat"]
    assert commands == {A: {"preset": "comfort"}, D: {"current_heating_setpoint": 21.0}}


def test_display_on_keeps_manual_room():
    o = dopts(display=D)
    st, _, _ = run(o, ht.HeatingState(target=22.0), dinp(display=disp(setpoint=22.0, mode="off")))
    # Display war aus, Raum steht auf Manuell (z. B. Haven hat das Display noch nicht eingeschaltet)
    st, _, events = run(o, st, dinp(now=T0 + 100, display=disp(setpoint=22.0, mode="heat")))
    assert st.mode == "heat" and events == []


def test_display_mode_echo_is_ignored_then_manual_off_counts():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st = ht.control(st, mode="off", now=T0 + 10)
    st, commands, _ = run(o, st, dinp(now=T0 + 10))
    assert commands[D] == {"system_mode": "off"}
    st, _, events = run(o, st, dinp(now=T0 + 12, display=disp(mode="off")))
    assert events == [] and st.mode == "off"
    st = ht.control(st, mode="heat", now=T0 + 20)
    st, commands, _ = run(o, st, dinp(now=T0 + 20, display=disp(mode="off")))
    assert commands[D] == {"system_mode": "heat"}
    st, _, events = run(o, st, dinp(now=T0 + 22, display=disp(mode="heat")))
    assert events == [] and st.mode == "heat"
    # Gerät meldet kurz nach dem Einschalten einen eigenen Sollwert: keine Bedienung
    st, _, events = run(o, st, dinp(now=T0 + 30, display=disp(setpoint=19.0)))
    assert events == [] and st.target == 21.0
    # Später von Hand aus: gilt
    st, _, events = run(o, st, dinp(now=T0 + 500, display=disp(setpoint=21.0, mode="off")))
    assert events == ["display_mode:off"] and st.mode == "off"


def test_display_schedule_mode_is_ignored_but_setpoint_counts():
    o = dopts()
    d = lambda **kw: disp(exposes=PRESET_DISPLAY, **{"mode": "manual", **kw})  # noqa: E731
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp(display=d()))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=d(mode="auto")))
    assert events == [] and st.mode == "heat"
    assert commands == {}  # nicht zurückstellen
    assert st.trvs[A].released_at is None
    st, commands, events = run(o, st, dinp(now=T0 + 200, display=d(mode="auto", setpoint=19.0)))
    assert events == ["display_target:19.0"] and st.target == 19.0
    assert commands == {A: {"current_heating_setpoint": 19.0}}
    # Aus am Display (preset off) schaltet den Raum aus, manual wieder ein
    st, _, events = run(o, st, dinp(now=T0 + 300, display=d(mode="off", setpoint=19.0)))
    assert events == ["display_mode:off"]
    st, _, events = run(o, st, dinp(now=T0 + 400, display=d(mode="manual", setpoint=19.0)))
    assert events == ["display_mode:heat"] and st.mode == "heat"


def test_display_setpoint_ignored_while_display_off():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="off"), dinp(display=disp(mode="off")))
    st, _, events = run(o, st, dinp(now=T0 + 100, display=disp(mode="off", setpoint=25.0)))
    assert events == [] and st.mode == "off"


def test_display_mirrors_effective_target():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st = ht.control(st, mode="eco", now=T0 + 10)
    st, commands, _ = run(o, st, dinp(now=T0 + 10))
    assert commands[D] == {"current_heating_setpoint": 18.0}
    # gerundet auf den Schritt des Displays und in dessen Grenzen
    d = lambda **kw: disp(exposes=PRESET_DISPLAY, **{"mode": "manual", **kw})  # noqa: E731
    _, commands, _ = run(o, ht.HeatingState(target=21.5), dinp(display=d()))
    assert commands[D] == {"current_heating_setpoint": 22.0}
    _, commands, _ = run(o, ht.HeatingState(target=28.0), dinp(display=d()))
    assert commands[D] == {"current_heating_setpoint": 25.0}
    # Aus: Display aus, kein Sollwert
    _, commands, _ = run(o, ht.HeatingState(target=21.0, mode="off"), dinp(display=disp(setpoint=19.0)))
    assert commands[D] == {"system_mode": "off"}
    _, commands, _ = run(o, ht.HeatingState(target=21.0, mode="off"), dinp(display=d(setpoint=19.0)))
    assert commands[D] == {"preset": "off"}


def test_display_off_in_window_pause_but_on_in_summer_pause():
    o = dopts(windows=["binary_sensor.fenster"], window_open_delay=0, outdoor={"entity": "weather.home", "threshold": 16})
    st = ht.HeatingState(target=22.0)
    st, commands, _ = run(o, st, dinp(display=disp(setpoint=21.0, mode="off"), windows={"binary_sensor.fenster": True}, outdoor=20.0))
    assert st.paused and st.summer
    assert commands == {A: {"preset": "off"}}  # Display ist schon aus, kein Sollwert
    # nur Sommer: Display an und mit dem Ziel
    st = ht.HeatingState(target=22.0)
    st, commands, _ = run(o, st, dinp(display=disp(setpoint=21.0, mode="off"), outdoor=20.0))
    assert st.summer and not st.paused
    assert commands[D] == {"system_mode": "heat", "current_heating_setpoint": 22.0}


def test_display_without_off_never_switched():
    exposes = [{"type": "climate", "features": [
        {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 30, "value_step": 0.5},
        {"property": "local_temperature", "access": 1},
    ]}]
    o = dopts()
    _, commands, _ = run(o, ht.HeatingState(target=21.0, mode="off"), dinp(display=disp(exposes=exposes, setpoint=19.0)))
    assert D not in commands
    _, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(display=disp(exposes=exposes, setpoint=19.0)))
    assert commands[D] == {"current_heating_setpoint": 21.0}


def test_display_resend_and_unavailable():
    o = dopts()
    st, commands, _ = run(o, ht.HeatingState(target=22.0), dinp())
    assert commands[D] == {"current_heating_setpoint": 22.0}
    st, commands, _ = run(o, st, dinp(now=T0 + 60, trvs={A: trv(20.0, 22.0)}))
    assert commands == {}
    st, commands, _ = run(o, st, dinp(now=T0 + ht.RESEND + 1, trvs={A: trv(20.0, 22.0)}))
    assert commands == {D: {"current_heating_setpoint": 22.0}}
    _, commands, _ = run(o, ht.HeatingState(target=22.0), dinp(display=disp(available=False), trvs={A: trv(20.0, 22.0)}))
    assert commands == {}


def test_display_ignored_when_disabled():
    o = dopts(enabled=False)
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st, commands, events = run(o, st, dinp(now=T0 + 300, display=disp(setpoint=24.0)))
    assert events == [] and commands == {} and st.target == 21.0


def test_display_status_shape():
    o = dopts(sensors=["sensor.salon_temperature"])
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    s = ht.status(o, st, dinp(sensors={"sensor.salon_temperature": 20.0},
                              display=disp(local=22.0, setpoint=20.0, heating=True, battery=87)))
    assert s["display"] == {
        "ieee": D,
        "name": "Display Salon",
        "available": True,
        "local_temperature": 22.0,
        "setpoint": 20.0,
        "mode": "heat",
        "preset": "manual",
        "eco_temperature": None,  # BEOK_EXPOSES ohne Eco
        "battery": 87,
        "pending": True,
    }
    assert s["room_temperature"] == 21.0 and s["room_source"] == "sensors"
    assert s["sensors"] == [{"entity_id": "sensor.salon_temperature", "value": 20.0}]
    # running_state des Displays zählt nicht als Heizen
    assert s["heating"] is None
    assert [t["ieee"] for t in s["trvs"]] == [A]
    assert ht.status(opts(), ht.HeatingState(), inputs())["display"] is None
    # Display konfiguriert, aber kein Eingang: kein Display im Status
    assert ht.status(o, st, inputs())["display"] is None


def test_display_state_roundtrip_and_reset_on_change():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp())
    st = ht.control(st, target=22.0, now=T0 + 10)
    st, _, _ = run(o, st, dinp(now=T0 + 10))
    assert st.display_ieee == D and st.display.sent_setpoint == 22.0
    again = ht.HeatingState.from_dict(st.to_dict())
    assert again == st
    # alte Stores ohne Display, kaputte Einträge
    old = st.to_dict()
    old.pop("display")
    old.pop("display_ieee")
    assert ht.HeatingState.from_dict(old).display is None
    assert ht.HeatingState.from_dict({**st.to_dict(), "display": "x"}).display is None
    # anderes Display: neu anfangen (erstes Sehen, keine Übernahme)
    o2 = dopts(display=B, trvs=[A])
    st2, events = ht.observe(o2, st, dinp(now=T0 + 400, display=disp(setpoint=26.0)))
    assert st2.display_ieee == B and events == [] and st2.display.sent_setpoint is None
    assert st2.display.seen_setpoint == 26.0 and st2.target == 22.0
    # Display entfernt: Zustand weg
    st3, _, _ = run(opts(calibration="none"), st, inputs(trvs={A: trv(20.0, 22.0)}))
    assert st3.display is None and st3.display_ieee is None


# --- Display: Fensterpause, Eco und eigener Wochenplan (Beok BOT-R9V-ZB) ----------------------------

# Exposes wie beim Beok BOT-R9V-ZB in Zigbee2MQTT (Klima-Verbund und Einzelwerte, Auszug)
BEOK_R9V = [
    {
        "type": "climate",
        "features": [
            {"type": "numeric", "name": "current_heating_setpoint", "property": "current_heating_setpoint", "access": 3,
             "value_min": 5, "value_max": 60, "value_step": 0.5, "unit": "°C"},
            {"type": "numeric", "name": "local_temperature", "property": "local_temperature", "access": 1, "unit": "°C"},
            {"type": "numeric", "name": "local_temperature_calibration", "property": "local_temperature_calibration",
             "access": 3, "value_min": -9.9, "value_max": 9.9, "value_step": 0.1, "unit": "°C"},
            {"type": "enum", "name": "system_mode", "property": "system_mode", "access": 3, "values": ["off", "heat"]},
            {"type": "enum", "name": "running_state", "property": "running_state", "access": 1, "values": ["idle", "heat"]},
            {"type": "enum", "name": "preset", "property": "preset", "access": 3, "values": ["auto", "manual", "eco"]},
        ],
    },
    {"type": "binary", "name": "child_lock", "property": "child_lock", "access": 3, "value_on": "LOCK", "value_off": "UNLOCK"},
    {"type": "numeric", "name": "battery", "property": "battery", "access": 1, "value_min": 0, "value_max": 100, "unit": "%"},
    {"type": "binary", "name": "valve_state", "property": "valve_state", "access": 1, "value_on": "open", "value_off": "close"},
    {"type": "binary", "name": "frost", "property": "frost", "access": 3, "value_on": "ON", "value_off": "OFF"},
    {"type": "numeric", "name": "deadzone_temperature", "property": "deadzone_temperature", "access": 3,
     "value_min": 0, "value_max": 5, "value_step": 1, "unit": "°C"},
    {"type": "numeric", "name": "min_temperature_limit", "property": "min_temperature_limit", "access": 3,
     "value_min": 5, "value_max": 30, "unit": "°C"},
    {"type": "numeric", "name": "max_temperature_limit", "property": "max_temperature_limit", "access": 3,
     "value_min": 15, "value_max": 60, "unit": "°C"},
    {"type": "numeric", "name": "eco_temperature", "property": "eco_temperature", "access": 3,
     "value_min": 5, "value_max": 30, "unit": "°C"},
    *[{"type": "text", "name": f"schedule_{d}", "property": f"schedule_{d}", "access": 3}
      for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
]

W = "binary_sensor.fenster"


def bk(**kw):
    return disp(exposes=BEOK_R9V, **kw)


def wopts(**kw):
    return dopts(**{"windows": [W], "window_open_delay": 60, "window_close_delay": 120, **kw})


def winp(now, open_, display=None, trv_mode="comfort", setpoint=22.0, **kw):
    return dinp(now=now, display=display if display is not None else bk(setpoint=setpoint),
                trvs={A: trv(20.0, setpoint, mode=trv_mode)}, windows={W: open_}, **kw)


def paused_room(mode="heat", target=22.0):
    """Raum mit Beok, Fenster offen, Pause läuft, Display hat das eigene Aus bestätigt (t = T0 + 75)."""
    o = wopts()
    d0 = bk(mode="off", setpoint=22.0) if mode == "off" else None  # Raum aus: Display ist schon aus
    st, commands, _ = run(o, ht.HeatingState(target=target, mode=mode), winp(T0, False, display=d0))
    st, commands, events = run(o, st, winp(T0 + 10, True, display=d0))
    assert not st.paused
    st, commands, events = run(o, st, winp(T0 + 70, True, display=d0))
    assert st.paused and events == ["window_pause"]
    if mode != "off":
        assert commands[D] == {"system_mode": "off"}  # kein Sollwert, kein Preset
    st, commands, events = run(o, st, winp(T0 + 75, True, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    assert events == [] and st.mode == mode and commands == {}
    return o, st


def test_display_caps_beok_r9v():
    caps = ht.display_caps(BEOK_R9V)
    assert (caps.mode_prop, caps.on_value, caps.off_value) == ("system_mode", "heat", "off")
    assert (caps.preset_prop, caps.preset_manual, caps.preset_eco, caps.preset_self) == ("preset", "manual", "eco", ("auto",))
    assert (caps.eco_temp_prop, caps.eco_min, caps.eco_max, caps.eco_step) == ("eco_temperature", 5, 30, 0.5)
    assert (caps.min, caps.max, caps.step, caps.release_values) == (5, 60, 0.5, ())
    # als TRV unverändert (kein eigenes Preset), ebenso Displays mit Modus über preset
    t = ht.trv_caps(BEOK_R9V)
    assert (t.mode_prop, t.on_value, t.preset_prop, t.eco_temp_prop) == ("preset", "manual", None, None)
    assert ht.display_caps(PRESET_DISPLAY).preset_prop is None
    # ohne Eco im Preset: keine Eco-Temperatur
    caps = ht.display_caps(BEOK_EXPOSES + [BEOK_R9V[-8]])
    assert (caps.preset_prop, caps.preset_eco, caps.eco_temp_prop) == ("preset", None, None)
    r = ht.trv_reading({"preset": "eco", "eco_temperature": 17, "battery": 86.6, "system_mode": "heat"}, ht.display_caps(BEOK_R9V))
    assert (r.preset, r.eco_temp, r.battery, r.mode) == ("eco", 17.0, 87, "heat")
    r = ht.trv_reading({"preset": "eco", "eco_temperature": 17, "battery": "x"}, ht.trv_caps(TRV602Z_EXPOSES))
    assert (r.preset, r.eco_temp, r.battery) == (None, None, None)


def test_display_window_pause_switches_it_off_and_resume_on_again():
    o, st = paused_room()
    # Ziel aus der App während der Pause: nicht an das ausgeschaltete Display
    st = ht.control(st, target=23.0, now=T0 + 80)
    st, commands, _ = run(o, st, winp(T0 + 80, True, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    assert D not in commands
    s = ht.status(o, st, winp(T0 + 80, True, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    assert s["reason"] == "window" and s["display"]["pending"] is False and s["window"]["override"] is False
    # Fenster zu, nach der Verzögerung weiter: Display ein, mit dem Ziel
    st, _, _ = run(o, st, winp(T0 + 100, False, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    st, commands, events = run(o, st, winp(T0 + 220, False, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    assert events == ["window_resume"]
    assert commands[D] == {"system_mode": "heat", "current_heating_setpoint": 23.0}
    # Bestätigung ist keine Bedienung
    st, _, events = run(o, st, winp(T0 + 225, False, display=bk(mode="heat", setpoint=23.0)))
    assert events == [] and st.mode == "heat" and st.target == 23.0


def test_display_on_during_window_pause_heats_until_windows_closed():
    o, st = paused_room()
    st, commands, events = run(o, st, winp(T0 + 100, True, display=bk(mode="heat", setpoint=22.0), trv_mode="off"))
    assert events == ["display_window_override"]
    assert not st.paused and st.window_override and st.mode == "heat" and st.target == 22.0
    assert commands == {A: {"preset": "comfort"}}  # TRV heizt wieder, das Display ist schon richtig
    s = ht.status(o, st, winp(T0 + 100, True))
    assert s["window"]["override"] is True and s["window"]["paused"] is False and s["window"]["open"] is True
    assert s["window"]["pause_at"] is None and s["reason"] is None
    assert ht.next_wake(o, st) is None
    # Fenster bleibt offen: keine neue Pause
    st, commands, events = run(o, st, winp(T0 + 1000, True))
    assert events == [] and not st.paused and commands == {}
    # alle zu: Übersteuerung endet, die nächste Öffnung pausiert wieder
    st, _, events = run(o, st, winp(T0 + 1100, False))
    assert events == ["window_override_end"] and not st.window_override and not st.paused
    st, _, events = run(o, st, winp(T0 + 1200, True))
    assert events == [] and ht.next_wake(o, st) == T0 + 1260
    st, commands, events = run(o, st, winp(T0 + 1260, True))
    assert events == ["window_pause"] and commands[D] == {"system_mode": "off"}


def test_display_on_during_pause_keeps_room_mode_and_off_room_turns_heat():
    o, st = paused_room(mode="eco", target=21.0)
    st, _, events = run(o, st, winp(T0 + 100, True, display=bk(mode="heat", preset="eco", setpoint=18.0), trv_mode="off"))
    assert events == ["display_window_override"] and st.mode == "eco" and st.window_override
    # Raum aus und Fenster offen: Einschalten am Display = heat, trotz Fenster
    o, st = paused_room(mode="off")
    st, _, events = run(o, st, winp(T0 + 100, True, display=bk(mode="heat", setpoint=22.0), trv_mode="off"))
    assert events == ["display_window_override", "display_mode:heat"]
    assert st.mode == "heat" and st.window_override and not st.paused
    # Raum aus (App) beendet die Übersteuerung, das offene Fenster pausiert wieder
    st = ht.control(st, mode="off", now=T0 + 200)
    assert not st.window_override
    st = ht.control(st, mode="heat", now=T0 + 210)
    st, _, events = run(o, st, winp(T0 + 210, True))
    assert events == ["window_pause"] and st.paused


def test_display_off_at_device_ends_override_and_turns_room_off():
    o, st = paused_room()
    st, _, _ = run(o, st, winp(T0 + 100, True, display=bk(mode="heat", setpoint=22.0), trv_mode="off"))
    assert st.window_override
    st, _, events = run(o, st, winp(T0 + 900, True, display=bk(mode="off", setpoint=22.0)))
    assert events == ["display_mode:off", "window_override_end"] and st.mode == "off" and not st.window_override


def test_display_own_off_during_pause_is_never_room_off():
    # die Bestätigung des eigenen Aus prüft schon paused_room; hier ein Aus nach der Bestätigungsfrist
    o, st = paused_room()
    st.display.sent_mode = "off"
    st.display.seen_mode = "heat"
    st, events = ht.observe(o, st, winp(T0 + 2000, True, display=bk(mode="off", setpoint=22.0), trv_mode="off"))
    assert events == [] and st.mode == "heat" and st.paused
    # Setpoint am ausgeschalteten Display zählt nicht
    st, events = ht.observe(o, st, winp(T0 + 2100, True, display=bk(mode="off", setpoint=26.0), trv_mode="off"))
    assert events == [] and st.target == 22.0


def test_display_without_off_keeps_target_during_window_pause():
    exposes = [{"type": "climate", "features": [
        {"property": "current_heating_setpoint", "access": 7, "value_min": 5, "value_max": 30, "value_step": 0.5},
        {"property": "local_temperature", "access": 1},
    ]}]
    o = dopts(windows=[W], window_open_delay=0)
    st, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(display=disp(exposes=exposes, setpoint=19.0), windows={W: True}))
    assert st.paused and commands[D] == {"current_heating_setpoint": 21.0}


def test_display_eco_at_device_switches_room_to_eco():
    o = dopts()
    st, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(display=bk()))
    assert commands == {}
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(preset="eco", setpoint=18.0)))
    assert events == ["display_mode:eco"] and st.mode == "eco" and st.target == 21.0
    assert commands == {A: {"current_heating_setpoint": 18.0}}  # Display regelt nach seiner Eco-Temperatur
    # Drehen in Eco (das Gerät zeigt seine Eco-Temperatur): keine Übernahme
    st, commands, events = run(o, st, dinp(now=T0 + 500, trvs={A: trv(20.0, 18.0)}, display=bk(preset="eco", setpoint=19.0)))
    assert events == [] and st.mode == "eco" and commands == {}


def test_display_room_eco_from_app_sets_device_eco():
    o = dopts(presets={"eco": 17.3})
    st, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(display=bk(eco=17.5)))
    assert commands == {}  # 17.3 auf den Schritt 0.5 gerundet = 17.5
    st = ht.control(st, mode="eco", now=T0 + 10)
    st, commands, _ = run(o, st, dinp(now=T0 + 10, display=bk(eco=17.5)))
    assert commands[D] == {"preset": "eco"}  # kein Sollwert in Eco
    # Bestätigung, das Gerät meldet seine Eco-Temperatur als Sollwert: keine Bedienung
    st, commands, events = run(o, st, dinp(now=T0 + 12, trvs={A: trv(20.0, 17.5)}, display=bk(preset="eco", eco=17.5, setpoint=17.5)))
    assert events == [] and st.mode == "eco" and commands == {}
    # zurück auf heat aus der App: Manuell und das Ziel
    st = ht.control(st, mode="heat", now=T0 + 500)
    st, commands, _ = run(o, st, dinp(now=T0 + 500, display=bk(preset="eco", eco=17.5, setpoint=17.5)))
    assert commands[D] == {"preset": "manual", "current_heating_setpoint": 21.0}
    # Zwischenmeldung manual mit altem Sollwert gleich nach dem eigenen Preset: keine Bedienung
    st, _, events = run(o, st, dinp(now=T0 + 502, display=bk(preset="manual", eco=17.5, setpoint=23.0)))
    assert events == [] and st.target == 21.0 and st.mode == "heat"


def test_display_eco_to_manual_at_device_takes_setpoint():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="eco"), dinp(display=bk(preset="eco", setpoint=18.0)))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(preset="manual", setpoint=22.5)))
    assert events == ["display_mode:heat", "display_target:22.5"] and st.mode == "heat" and st.target == 22.5
    assert D not in commands
    # ohne gemeldeten Sollwert: das bisherige Ziel
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="eco"), dinp(display=bk(preset="eco", setpoint=18.0)))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(preset="manual", setpoint=None)))
    assert events == ["display_mode:heat"] and st.target == 21.0
    assert commands[D] == {"current_heating_setpoint": 21.0}


def test_display_own_schedule_is_switched_back_to_manual():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0), dinp(display=bk()))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(preset="auto", setpoint=19.0)))
    assert events == [] and st.mode == "heat" and st.target == 21.0
    assert commands == {D: {"preset": "manual", "current_heating_setpoint": 21.0}}
    # der Plan ändert den Sollwert, bevor das Manuell ankommt: keine Übernahme
    st, commands, events = run(o, st, dinp(now=T0 + 400, display=bk(preset="auto", setpoint=17.0)))
    assert events == [] and st.target == 21.0
    # Raum in Eco: zurück auf Eco statt Manuell
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="eco"), dinp(display=bk(preset="eco")))
    _, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(preset="auto", setpoint=19.0)))
    assert events == [] and commands == {D: {"preset": "eco"}}


def test_display_eco_temperature_kept_equal_to_room():
    o = dopts(presets={"eco": 17.0})
    st, commands, _ = run(o, ht.HeatingState(target=21.0), dinp(display=bk(eco=18.0)))
    assert commands == {D: {"eco_temperature": 17.0}}
    st, commands, _ = run(o, st, dinp(now=T0 + 60, display=bk(eco=18.0)))
    assert commands == {}  # Bestätigung steht aus
    st, _, _ = run(o, st, dinp(now=T0 + 70, display=bk(eco=17.0)))
    # am Gerät geändert: nicht übernehmen, sofort zurückstellen
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(eco=20.0)))
    assert events == [] and commands == {D: {"eco_temperature": 17.0}} and st.mode == "heat"
    # auch bei ausgeschaltetem Raum (Preset wird dann nicht gesendet)
    _, commands, _ = run(o, ht.HeatingState(mode="off"), dinp(display=bk(mode="off", eco=18.0, preset="auto")))
    assert commands[D] == {"eco_temperature": 17.0}


def test_display_preset_ignored_while_off():
    o = dopts()
    st, _, _ = run(o, ht.HeatingState(target=21.0, mode="off"), dinp(display=bk(mode="off")))
    st, commands, events = run(o, st, dinp(now=T0 + 100, display=bk(mode="off", preset="eco")))
    assert events == [] and st.mode == "off" and D not in commands


def test_display_status_preset_eco_battery_pending():
    o = dopts()
    st = ht.HeatingState(target=21.0, mode="eco")
    s = ht.status(o, st, dinp(display=bk(preset="eco", eco=18.0, setpoint=25.0, battery=55)))
    assert {k: s["display"][k] for k in ("preset", "eco_temperature", "battery", "pending")} == {
        "preset": "eco", "eco_temperature": 18.0, "battery": 55, "pending": False,  # Sollwert zählt in Eco nicht
    }
    assert ht.status(o, st, dinp(display=bk(preset="manual", eco=18.0, setpoint=18.0)))["display"]["pending"] is True
    assert ht.status(o, st, dinp(display=bk(preset="eco", eco=19.0)))["display"]["pending"] is True
    st = ht.HeatingState(target=21.0)
    assert ht.status(o, st, dinp(display=bk(preset="auto")))["display"]["pending"] is True
    assert ht.status(o, st, dinp(display=bk()))["display"]["pending"] is False
    assert ht.status(o, st, dinp(display=bk(setpoint=19.0)))["display"]["pending"] is True
    assert ht.status(o, st, dinp())["window"]["override"] is False


def test_window_override_and_display_memo_roundtrip():
    o, st = paused_room()
    st, _, _ = run(o, st, winp(T0 + 100, True, display=bk(mode="heat", setpoint=22.0), trv_mode="off"))
    st = ht.control(st, mode="eco", now=T0 + 110)
    st, _, _ = run(o, st, winp(T0 + 110, True))
    assert st.window_override and st.display.sent_preset == "eco" and st.display.seen_preset == "manual"
    again = ht.HeatingState.from_dict(st.to_dict())
    assert again == st
    old = st.to_dict()
    old.pop("window_override")
    for k in ("seen_preset", "sent_preset", "sent_preset_at", "sent_eco_temp", "sent_eco_temp_at"):
        old["display"].pop(k)
    loaded = ht.HeatingState.from_dict(old)
    assert loaded.window_override is False and loaded.display.sent_preset is None
    assert ht.HeatingState.from_dict({**st.to_dict(), "window_override": "ja"}).window_override is False

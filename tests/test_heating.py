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


def trv(local=None, setpoint=None, calibration=0.0, mode="on", exposes=TRV602Z_EXPOSES, available=True, heating=None):
    caps = ht.trv_caps(exposes)
    values = {"local_temperature": local, caps.setpoint: setpoint, "preset": mode, "system_mode": mode}
    if caps.calibration:
        values[caps.calibration] = calibration
    if heating is not None:
        values["running_state"] = "heat" if heating else "idle"
    return ht.TrvInput(caps, ht.trv_reading(values, caps, available=available), "TRV")


def inputs(now=T0, trvs=None, sensors=None, windows=None, outdoor=None):
    return ht.HeatingInputs(
        now=now,
        trvs=trvs if trvs is not None else {A: trv(24.0, 21.0)},
        sensors=sensors if sensors is not None else {"sensor.salon_temperature": 20.0},
        windows=windows or {},
        outdoor=outdoor,
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
    # system_mode ist beim TRV602Z nur lesbar: Modus über preset
    assert (caps.mode_prop, caps.on_value, caps.off_value) == ("preset", "on", "off")


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
    # Raum kühlt ab, TRV merkt es nicht: neu kalibrieren (letztes Senden ist 15 min her)
    st, commands, _ = run(o, st, inputs(now=T0 + 900, trvs={A: trv(20.0, 21.0, calibration=-4.0)}, sensors={"s": 19.0}))
    assert commands == {A: {"local_temperature_calibration": -5.0}}
    # gleich danach noch kälter: erst nach 10 min wieder
    st, commands, _ = run(o, st, inputs(now=T0 + 950, trvs={A: trv(20.0, 21.0, calibration=-4.0)}, sensors={"s": 18.0}))
    assert commands == {}
    assert ht.desired(o, st, inputs(trvs={A: trv(20.0, 21.0, calibration=-4.0)}, sensors={"s": 18.0}))[A].calibration == -6.0


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


def test_device_in_schedule_mode_is_put_to_manual():
    o = opts(calibration="none")
    _, commands, _ = run(o, ht.HeatingState(target=21.0), inputs(trvs={A: trv(20.0, 21.0, mode="auto")}))
    assert commands == {A: {"preset": "on"}}


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
    assert events == ["window_resume"] and commands == {A: {"preset": "on"}}


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
    # Haven setzt das Gerät zurück auf Manuell mit der Eco-Temperatur des Raums
    assert commands == {A: {"preset": "on", "current_heating_setpoint": 18.0}}


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

"""Bewegungslicht (``room_motion``): Bedingungen, Besitz-Automat, Befehle, Validierung, Migration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json

import pytest

from test_rooms import _build

auto = importlib.import_module("haven_nohass.automations")
mo = importlib.import_module("haven_nohass.motion")
Z2MError = importlib.import_module("haven_nohass.commands").Z2MError

TZ = timezone(timedelta(hours=1))
DETECTOR = "0x00124b0000000030"


def at(hh: int, mm: int = 0, ss: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=TZ)


def env(now: datetime, **kw) -> mo.MotionEnv:
    kw.setdefault("light_on", False)
    return mo.MotionEnv(now=now, **kw)


def opts(**kw) -> dict:
    return mo.normalize_motion_options(kw)


def kinds(cmds) -> list[str]:
    return [c.action if c.kind == "notify" else c.kind for c in cmds]


def run(options, state, kind, now, **kw):
    """Schritt. ``light_on``/``brightness`` gelten für die Umgebung, bei ``light``/``knob`` auch im Ereignis."""
    ev_kw = {k: kw[k] for k in ("color", "own", "user") if k in kw}
    for k in ("color", "own", "user"):
        kw.pop(k, None)
    if kind in ("light", "knob"):
        ev_kw.update({k: kw[k] for k in ("light_on", "brightness") if k in kw})
    return mo.step(options, state, mo.MotionEvent(kind, **ev_kw), env(now, **kw))


@pytest.fixture()
def targets(inventory):
    bridge, rooms = _build(inventory)
    return bridge, auto.build_room_targets(rooms["rooms"], bridge.groups, [])


# --- Bedingungen -----------------------------------------------------------------------------


def test_window_over_midnight():
    assert mo.in_window(at(23, 30), "18:00", "07:00")
    assert mo.in_window(at(3), "18:00", "07:00")
    assert mo.in_window(at(18, 0), "18:00", "07:00")
    assert not mo.in_window(at(7, 0), "18:00", "07:00")
    assert not mo.in_window(at(12), "18:00", "07:00")
    assert mo.in_window(at(9), "08:00", "10:00") and not mo.in_window(at(10), "08:00", "10:00")
    assert mo.in_window(at(5), "00:00", "00:00")  # gleich = ganzer Tag


def test_multiple_windows_and_empty():
    o = opts(windows=[{"from": "06:00", "to": "08:00"}, {"from": "18:00", "to": "23:00"}])
    assert mo.check_conditions(o, env(at(7))) == (True, None)
    assert mo.check_conditions(o, env(at(20))) == (True, None)
    assert mo.check_conditions(o, env(at(12))) == (False, "time")
    assert mo.check_conditions(opts(), env(at(12))) == (True, None)


def _sun(**kw):
    return env(at(12), sunrise=at(7, 45), sunset=at(16, 30), **kw)


def test_sun_night_with_offsets():
    o = opts(sun={"from": {"event": "sunset", "offset": -30}, "until": {"event": "sunrise", "offset": 60}})
    def ok(h, m=0):
        e = _sun()
        e.now = at(h, m)
        return mo.check_conditions(o, e)
    assert ok(15, 55) == (False, "sun")  # vor Sonnenuntergang - 30 min (16:00)
    assert ok(16, 5) == (True, None)
    assert ok(2) == (True, None)
    assert ok(8, 30) == (True, None)  # Sonnenaufgang 7:45 + 60 = 8:45
    assert ok(8, 50) == (False, "sun")


def test_sun_one_sided_and_unknown():
    after = opts(sun={"from": {"event": "sunset", "offset": 0}})
    e = _sun()
    e.now = at(17)
    assert mo.check_conditions(after, e)[0]
    e.now = at(12)
    assert mo.check_conditions(after, e) == (False, "sun")
    # unbekannte Sonnenzeiten blockieren nicht
    assert mo.check_conditions(after, env(at(12)))[0]


def test_lux_only_when_light_off():
    o = opts(lux={"below": 30})
    assert mo.check_conditions(o, env(at(12), lux=12.0)) == (True, None)
    assert mo.check_conditions(o, env(at(12), lux=80.0)) == (False, "lux")
    assert mo.check_conditions(o, env(at(12), lux=30.0)) == (False, "lux")  # Grenze: < below
    assert mo.check_conditions(o, env(at(12), lux=None))[0]
    # Licht an: Sensor misst das eigene Licht, nicht prüfen
    assert mo.check_conditions(o, env(at(12), lux=500.0), light_off=False)[0]


def test_all_conditions_must_hold():
    o = opts(windows=[{"from": "18:00", "to": "07:00"}], lux={"below": 30})
    assert mo.check_conditions(o, env(at(20), lux=5.0))[0]
    assert mo.check_conditions(o, env(at(20), lux=50.0)) == (False, "lux")
    assert mo.check_conditions(o, env(at(12), lux=5.0)) == (False, "time")


def test_night_light_selection():
    night = {"from": "23:00", "to": "06:00", "light": {"mode": "brightness", "brightness": 25}}
    o = opts(night=night)
    assert mo.select_light(o, env(at(23, 30)))[1] is True
    assert mo.select_light(o, env(at(23, 30)))[0]["brightness"] == 25
    assert mo.select_light(o, env(at(20))) == (o["light"], False)


# --- Besitz-Automat ---------------------------------------------------------------------------------


def _on(options=None, now=at(20), **kw):
    options = options or opts()
    s, cmds = run(options, mo.MotionState(), "motion", now, **kw)
    return options, s, cmds


def test_motion_turns_on_and_arms_delay():
    o, s, cmds = _on()
    assert kinds(cmds) == ["on", "motion_on"]
    assert s.owner == "motion" and s.applied == o["light"]
    assert s.occupied and s.deadline == pytest.approx(at(20).timestamp() + mo.HOLD_SAFETY)
    s, cmds = run(o, s, "clear", at(20, 1))
    assert s.deadline == pytest.approx(at(20, 1).timestamp() + 120)
    assert cmds == []


def test_extend_never_sets_scene_again():
    o = opts(light={"mode": "scene", "scene": 2})
    o, s, _ = _on(o)
    s, _ = run(o, s, "clear", at(20, 1))
    first = s.deadline
    # Zustandsänderung durch uns ist durch (Fenster vorbei), Nutzer dreht heller
    s, cmds = run(o, s, "motion", at(20, 1, 30), light_on=True)
    assert kinds(cmds) == ["motion_extend"] and all(c.kind != "on" for c in cmds)
    assert s.owner == "motion" and s.deadline > first


def test_light_already_on_is_not_touched_nor_switched_off():
    o = opts()
    s, cmds = run(o, mo.MotionState(), "motion", at(20), light_on=True)
    assert cmds == [] and s.owner == "none"
    s, cmds = run(o, s, "clear", at(20, 1), light_on=True)
    assert cmds == [] and s.deadline is None
    s, cmds = run(o, s, "timer", at(23), light_on=True)
    assert cmds == []


def test_conditions_block_start():
    o = opts(windows=[{"from": "18:00", "to": "07:00"}])
    s, cmds = run(o, mo.MotionState(), "motion", at(12))
    assert cmds == [] and s.owner == "none" and s.occupied
    assert mo.status(o, s, env(at(12)))["inactive"] == "time"


def test_off_after_delay_and_release():
    o, s, _ = _on(opts(delay=120))
    s, _ = run(o, s, "clear", at(20, 1))
    s, cmds = run(o, s, "timer", at(20, 1, 1))
    assert cmds == []  # noch nicht fällig
    s, cmds = run(o, s, "timer", at(20, 3), light_on=True)
    assert kinds(cmds) == ["off", "motion_off"]
    assert s.owner == "none" and s.deadline is None and s.last_off == pytest.approx(at(20, 3).timestamp())


def test_presence_holds_while_occupied():
    o, s, _ = _on()
    s, _ = run(o, s, "motion", at(20, 10), light_on=True)
    assert s.deadline > at(20, 10).timestamp() + 3600  # nur Sicherung
    s, cmds = run(o, s, "timer", at(20, 12), light_on=True)
    assert cmds == []


def test_takeover_by_brightness_change():
    o = opts(light={"mode": "brightness", "brightness": 100})
    o, s, _ = _on(o)
    # innerhalb des Fensters: eigene Meldung
    s, cmds = run(o, s, "light", at(20, 0, 1), light_on=True, brightness=100)
    assert cmds == [] and s.owner == "motion"
    # kleine Abweichung (<10 %): keine Übernahme
    s, cmds = run(o, s, "light", at(20, 1), light_on=True, brightness=115)
    assert s.owner == "motion"
    # deutlich heller
    s, cmds = run(o, s, "light", at(20, 2), light_on=True, brightness=200)
    assert kinds(cmds) == ["takeover"] and s.owner == "manual" and s.deadline is None


def test_takeover_by_color_and_user_context():
    o = opts(light={"mode": "brightness", "brightness": 100, "color_temp": 2700})
    o, s, _ = _on(o)
    s, _ = run(o, s, "light", at(20, 0, 1), light_on=True, brightness=100, color=("ct", 370))
    s, cmds = run(o, s, "light", at(20, 1), light_on=True, brightness=100, color=("ct", 250))
    assert s.owner == "manual"
    # fremder Benutzer im Kontext, auch innerhalb des Fensters und ohne Abweichung
    o, s, _ = _on(o)
    s, cmds = run(o, s, "light", at(20, 0, 1), light_on=True, brightness=100, user=True)
    assert s.owner == "manual" and kinds(cmds) == ["takeover"]


def test_scene_takeover_needs_baseline_first():
    o = opts(light={"mode": "scene", "scene": 1})
    o, s, _ = _on(o)
    s, _ = run(o, s, "light", at(20, 0, 1), light_on=True, brightness=60)  # Szene meldet sich
    assert s.baseline == 60 and s.owner == "motion"
    s, _ = run(o, s, "light", at(20, 1), light_on=True, brightness=180)
    assert s.owner == "manual"


def test_own_context_is_ignored():
    o, s, _ = _on(opts(light={"mode": "brightness", "brightness": 100}))
    s, cmds = run(o, s, "light", at(21), light_on=True, brightness=250, own=True)
    assert s.owner == "motion" and cmds == []


def test_takeover_by_knob():
    o, s, _ = _on()
    s, cmds = run(o, s, "knob", at(20, 0, 30), light_on=True)
    assert kinds(cmds) == ["takeover"] and s.owner == "manual"
    # Knob schaltet aus: Licht gehört niemandem
    o, s, _ = _on()
    s, cmds = run(o, s, "knob", at(20, 0, 30), light_on=False)
    assert cmds == [] and s.owner == "none" and s.deadline is None
    # Knob bei Licht ohne Besitzer: bleibt „niemand“
    s, cmds = run(o, mo.MotionState(), "knob", at(20), light_on=True)
    assert s.owner == "none" and cmds == []


def test_manual_never_switched_off_and_no_scene():
    o, s, _ = _on()
    s, _ = run(o, s, "knob", at(20, 0, 30), light_on=True)
    s, cmds = run(o, s, "motion", at(20, 5), light_on=True)
    assert cmds == []
    s, _ = run(o, s, "clear", at(20, 6), light_on=True)
    s, cmds = run(o, s, "timer", at(23), light_on=True)
    assert cmds == [] and s.owner == "manual"


def test_manual_delay_optional():
    o = opts(manual_delay=600)
    o, s, _ = _on(o)
    s, _ = run(o, s, "clear", at(20, 0, 5))
    s, _ = run(o, s, "knob", at(20, 0, 30), light_on=True)
    assert s.owner == "manual" and s.deadline == pytest.approx(at(20, 0, 30).timestamp() + 600)
    s, cmds = run(o, s, "timer", at(20, 11), light_on=True)
    assert kinds(cmds) == ["off", "motion_off"] and s.owner == "none"


def test_manual_returns_to_none_when_light_off():
    o, s, _ = _on()
    s, _ = run(o, s, "knob", at(20, 0, 30), light_on=True)
    s, _ = run(o, s, "light", at(20, 5), light_on=False)
    assert s.owner == "none"
    # und der nächste Melder darf wieder schalten
    s, cmds = run(o, s, "motion", at(20, 6))
    assert kinds(cmds) == ["on", "motion_on"] and s.owner == "motion"


def test_user_switches_motion_light_off():
    o, s, _ = _on()
    s, cmds = run(o, s, "light", at(20, 5), light_on=False)
    assert s.owner == "none" and s.deadline is None and cmds == []


def test_own_off_is_not_a_takeover():
    o, s, _ = _on()
    s, _ = run(o, s, "clear", at(20, 1))
    s, _ = run(o, s, "timer", at(20, 3), light_on=True)
    s, cmds = run(o, s, "light", at(20, 3, 1), light_on=False)
    assert s.owner == "none" and cmds == []


def test_warning_dims_and_motion_restores():
    o = opts(warn=True, warn_seconds=20, light={"mode": "scene", "scene": 2})
    o, s, _ = _on(o)
    s, _ = run(o, s, "clear", at(20, 1))
    assert s.warn_at == pytest.approx(s.deadline - 20)
    assert mo.next_wake(s) == s.warn_at
    s, cmds = run(o, s, "timer", at(20, 2, 41), light_on=True, brightness=200)
    assert kinds(cmds) == ["dim", "warn"] and cmds[0].brightness == 100 and s.warned
    assert mo.next_wake(s) == s.deadline
    # eigene Dimmung ist keine Übernahme
    s, _ = run(o, s, "light", at(20, 2, 42), light_on=True, brightness=100)
    assert s.owner == "motion"
    # Bewegung: zurück in den vorherigen Zustand (Szene), Nachlauf neu
    s, cmds = run(o, s, "motion", at(20, 2, 50), light_on=True, brightness=100)
    assert kinds(cmds) == ["on", "motion_extend"] and cmds[0].light["scene"] == 2
    assert not s.warned
    # ohne Bewegung: Dimmen, dann Aus
    s, _ = run(o, s, "clear", at(20, 3))
    s, cmds = run(o, s, "timer", at(20, 4, 41), light_on=True, brightness=200)
    assert kinds(cmds) == ["dim", "warn"]
    s, cmds = run(o, s, "timer", at(20, 5, 1), light_on=True)
    assert kinds(cmds) == ["off", "motion_off"]


def test_no_warning_by_default():
    o, s, _ = _on()
    s, _ = run(o, s, "clear", at(20, 1))
    assert s.warn_at is None


def test_relight_within_30s_ignores_conditions():
    o = opts(lux={"below": 30})
    o, s, _ = _on(o, lux=5.0)
    s, _ = run(o, s, "clear", at(20, 1))
    s, _ = run(o, s, "timer", at(20, 3), light_on=True, lux=5.0)
    # 10 s später: Sensor sieht das eigene Restlicht (lux 100), Nachschalten trotzdem
    s, cmds = run(o, s, "motion", at(20, 3, 10), lux=100.0)
    assert kinds(cmds) == ["on", "motion_on"] and s.owner == "motion"
    # nach 30 s gelten die Bedingungen wieder
    s, _ = run(o, s, "clear", at(20, 4))
    s, _ = run(o, s, "timer", at(20, 6), light_on=True, lux=5.0)
    s, cmds = run(o, s, "motion", at(20, 6, 45), lux=100.0)
    assert cmds == [] and s.owner == "none"
    # abgeschaltet
    off = opts(relight=False)
    off, s, _ = _on(off)
    s, _ = run(off, s, "clear", at(20, 1))
    s, _ = run(off, s, "timer", at(20, 3), light_on=True)
    s, cmds = run(off, s, "motion", at(20, 3, 5), lux=None)
    assert kinds(cmds) == ["on", "motion_on"]  # ohne Bedingungen im Weg wie sonst auch
    lux = opts(relight=False, lux={"below": 30})
    lux, s, _ = _on(lux, lux=5.0)
    s, _ = run(lux, s, "clear", at(20, 1))
    s, _ = run(lux, s, "timer", at(20, 3), light_on=True, lux=5.0)
    s, cmds = run(lux, s, "motion", at(20, 3, 5), lux=100.0)
    assert cmds == []


def test_night_light_applied():
    night = {"from": "23:00", "to": "06:00", "light": {"mode": "brightness", "brightness": 25}}
    o = opts(night=night)
    s, cmds = run(o, mo.MotionState(), "motion", at(23, 30))
    assert cmds[0].light["brightness"] == 25 and s.night
    s, cmds = run(o, mo.MotionState(), "motion", at(20))
    assert cmds[0].light["brightness"] == o["light"]["brightness"] and not s.night


def test_missed_off_event_restarts():
    o, s, _ = _on()
    s, cmds = run(o, s, "motion", at(20, 10), light_on=False)
    assert kinds(cmds) == ["on", "motion_on"]


def test_stale_manual_with_light_off_switches_on_again():
    # „manuell“ gespeichert, das Aus-Ereignis ging verloren (z. B. HA-Neustart)
    o, s, _ = _on()
    s, _ = run(o, s, "knob", at(20, 0, 30), light_on=True)
    assert s.owner == "manual"
    assert mo.status(o, s, env(at(21)))["owner"] == "none"  # Anzeige: Licht ist aus
    s, cmds = run(o, s, "motion", at(21), light_on=False)
    assert kinds(cmds) == ["on", "motion_on"] and s.owner == "motion"
    # Licht an: „manuell“ bleibt und der Melder schaltet nichts
    s, _ = run(o, s, "knob", at(21, 0, 30), light_on=True)
    s, cmds = run(o, s, "motion", at(21, 5), light_on=True)
    assert cmds == [] and s.owner == "manual"
    assert mo.status(o, s, env(at(21, 5), light_on=True))["owner"] == "manual"


def test_status():
    o, s, _ = _on()
    st = mo.status(o, s, env(at(20), light_on=True))
    assert st["phase"] == "motion" and st["off_at"] is None and st["occupied"]
    s, _ = run(o, s, "clear", at(20, 1))
    assert mo.status(o, s, env(at(20, 1)))["off_at"] == pytest.approx(s.deadline)
    s, _ = run(o, s, "knob", at(20, 1, 30), light_on=True)
    assert mo.status(o, s, env(at(20, 2), light_on=True))["phase"] == "manual"


def test_state_roundtrip_and_garbage():
    _, s, _ = _on(opts(light={"mode": "brightness", "brightness": 100}))
    s, _ = run(opts(), s, "light", at(20, 0, 1), light_on=True, brightness=100, color=("ct", 300))
    data = json.loads(json.dumps(s.to_dict()))
    assert mo.MotionState.from_dict(data) == s
    assert mo.MotionState.from_dict("x") == mo.MotionState()
    assert mo.MotionState.from_dict({"owner": "fremd"}).owner == "none"


# --- Befehle ---------------------------------------------------------------------------------------


def test_plan_scene_and_brightness_group(targets):
    _, rooms = targets
    room = rooms["salon"]
    scene = room.scenes[0]["id"]
    p = mo.plan_command(mo.Command("on", light={"mode": "scene", "scene": scene}), room, auto.RoomState(False, 0))
    assert p.mqtt == [(f"zigbee2mqtt/{room.group}/set", {"scene_recall": scene})] and p.scene == scene
    light = {"mode": "brightness", "brightness": 80, "color_temp": 2500, "transition": 2}
    p = mo.plan_command(mo.Command("on", light=light), room, auto.RoomState(False, 0))
    assert p.mqtt[0][1] == {"state": "ON", "brightness": 80, "transition": 2.0, "color_temp": 400}
    p = mo.plan_command(mo.Command("dim", brightness=40, transition=1.0), room, auto.RoomState(True, 80))
    assert p.mqtt[0][1] == {"brightness": 40, "transition": 1.0}
    p = mo.plan_command(mo.Command("off"), room, auto.RoomState(True, 80))
    assert p.mqtt[0][1] == {"state": "OFF"}


def test_plan_ha_scene_and_fallback_services():
    room = auto.RoomTarget("bad", "Bad", lights=["light.bad_1", "light.bad_2"])
    p = mo.plan_command(mo.Command("on", light={"mode": "scene", "scene_entity": "scene.bad_nacht", "transition": 3}), room, auto.RoomState(False, 0))
    assert p.services == [("scene", "turn_on", {"entity_id": "scene.bad_nacht", "transition": 3.0})]
    p = mo.plan_command(mo.Command("on", light={"mode": "brightness", "brightness": 50, "color_temp": 2700}), room, auto.RoomState(False, 0))
    assert p.services[0][:2] == ("light", "turn_on") and p.services[0][2]["color_temp_kelvin"] == 2700
    # Szene fehlt: trotzdem Licht an (Standardhelligkeit) statt nichts
    p = mo.plan_command(mo.Command("on", light={"mode": "scene", "scene": 1}), room, auto.RoomState(False, 0))
    assert p.action == "on" and p.services[0][2]["entity_id"] == ["light.bad_1", "light.bad_2"]
    # Raum ohne Gruppe mit Haven-Szene: Geräte über HA
    room = auto.RoomTarget(
        "bad", "Bad", lights=["light.bad_1"], scenes=[{"id": 0, "name": "Nacht"}],
        scene_devices={0: [{"entity_id": "light.bad_1", "on": True, "brightness": 10}]},
    )
    p = mo.plan_command(mo.Command("on", light={"mode": "scene", "scene": 0}), room, auto.RoomState(False, 0))
    assert p.action == "on" and p.scene == 0
    assert p.services == [("light", "turn_on", {"entity_id": ["light.bad_1"], "brightness_pct": 10, "transition": 0.0})]
    assert mo.plan_command(mo.Command("off"), room, auto.RoomState(True, 10)).services[0][1] == "turn_off"


def test_pick_entities():
    states = {
        "binary_sensor.m_occ": {"attributes": {"device_class": "occupancy"}},
        "binary_sensor.m_pres": {"attributes": {"device_class": "presence"}},
        "binary_sensor.m_tamper": {"attributes": {"device_class": "tamper"}},
        "sensor.m_lux": {"attributes": {"device_class": "illuminance"}},
    }
    ids = sorted(states)
    assert mo.pick_detector_entity(states, ids) == "binary_sensor.m_pres"
    assert mo.pick_detector_entity(states, ["binary_sensor.m_tamper"]) is None
    assert mo.pick_lux_entity(states, ids) == "sensor.m_lux"
    assert mo.parse_lux("12.5") == 12.5 and mo.parse_lux("unavailable") is None
    assert mo.detector_active("on") is True and mo.detector_active("unknown") is None


# --- Validierung und Migration ----------------------------------------------------------------------


def _motion(**kw) -> dict:
    return {"id": "m1", "type": "room_motion", "name": "Bad", "room_area_id": "salon", "devices": [DETECTOR], **kw}


def test_normalize_motion_defaults():
    n = auto.normalize_automation(_motion())
    o = n["options"]
    assert o["delay"] == 120 and o["warn"] is False and o["relight"] is True and o["manual_delay"] is None
    assert o["light"]["mode"] == "brightness" and "gestures" not in o
    assert auto.normalize_automation(_motion(options={"zukunft": 1}))["options"]["zukunft"] == 1


def test_validate_shape_motion():
    v = auto.validate_shape
    assert v(_motion()) == []
    assert v(_motion(devices=[]))
    assert v(_motion(options={"windows": [{"from": "25:00", "to": "07:00"}]}))
    assert v(_motion(options={"windows": [{"from": "18:00", "to": "07:00"}], "delay": 2}))
    assert v(_motion(options={"light": {"mode": "scene"}}))
    assert v(_motion(options={"light": {"mode": "scene", "scene_entity": "light.x"}}))
    assert v(_motion(options={"lux": {"below": -1}}))
    assert v(_motion(options={"sun": {"from": {"event": "mittag"}}}))
    assert v(_motion(options={"night": {"from": "23:00", "to": "06:00"}}))
    assert v(_motion(options={"manual_delay": 3}))
    ok = {
        "windows": [{"from": "18:00", "to": "07:00"}],
        "sun": {"from": {"event": "sunset", "offset": -20}, "until": {"event": "sunrise", "offset": 60}},
        "lux": {"below": 30, "entity": "sensor.flur_lux"},
        "light": {"mode": "scene", "scene_entity": "scene.bad_nacht"},
        "night": {"from": "23:00", "to": "06:00", "light": {"mode": "brightness", "brightness": 25}},
        "delay": 90, "warn": True, "relight": False, "manual_delay": 600,
    }
    assert v(_motion(options=ok)) == []


def test_detector_only_in_one_active_motion_automation(targets):
    _, rooms = targets
    existing = [auto.normalize_automation(_motion())]
    other = {"type": "room_motion", "name": "Zweit", "room_area_id": "kochbereich", "devices": [DETECTOR]}
    with pytest.raises(Z2MError) as err:
        auto.validate_automation(other, existing, rooms, {DETECTOR: "Melder Bad"})
    assert err.value.code == "device_taken" and "Melder Bad meldet schon für Bad" in err.value.message
    assert auto.validate_automation({**other, "enabled": False}, existing, rooms)["enabled"] is False
    assert auto.validate_automation({**existing[0], "name": "Neu"}, existing, rooms)["name"] == "Neu"
    # Knob und Motion im selben Raum sind erlaubt
    knob = {"type": "room_knob", "room_area_id": "salon", "devices": [DETECTOR]}
    assert auto.validate_automation(knob, existing, rooms)["type"] == "room_knob"
    assert auto.device_owner(existing, DETECTOR, type="room_knob") is None
    assert auto.device_owner(existing, DETECTOR, type="room_motion")["id"] == "m1"


def test_motion_room_without_lights(targets):
    _, rooms = targets
    rooms = {**rooms, "leer": auto.RoomTarget("leer", "Abstellraum")}
    with pytest.raises(Z2MError) as err:
        auto.validate_automation(_motion(room_area_id="leer"), [], rooms)
    assert err.value.code == "no_lights"


def test_default_name():
    assert auto.default_name(auto.RoomTarget("bad", "Bad"), "bad", "room_motion") == "Bewegungslicht Bad"
    assert auto.default_name(auto.RoomTarget("bad", "Bad"), "bad") == "Drehregler Bad"


def test_store_migration_1_2_to_1_3():
    old = {
        "automations": [{"id": "k", "type": "room_knob", "room_area_id": "salon", "devices": ["0x1"], "options": {}}],
        "runtime": {"last_scene": {"salon": 1}, "last_triggered": {}},
    }
    data = auto.migrate_store(1, 2, old)
    assert data["runtime"]["motion"] == {} and data["runtime"]["last_scene"] == {"salon": 1}
    assert data["automations"][0]["options"]["step"] == 35
    assert auto.STORE_MINOR_VERSION == 3
    new = auto.read_store({"automations": [_motion()], "runtime": {"motion": {"m1": {"owner": "manual"}, "x": 5}}})
    assert new["automations"][0]["type"] == "room_motion"
    assert new["runtime"]["motion"] == {"m1": {"owner": "manual"}}
    json.dumps(new)
    assert auto.empty_store()["runtime"]["motion"] == {}


def test_detector_value_from_z2m():
    assert mo.detector_value({"occupancy": True, "illuminance": 12}) is True
    assert mo.detector_value({"presence": False, "occupancy": True}) is False
    assert mo.detector_value({"illuminance": 12}) is None
    assert mo.detector_value({"occupancy": "on"}) is None
    assert mo.detector_value(None) is None

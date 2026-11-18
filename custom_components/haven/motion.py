"""Bewegungslicht (``room_motion``): Optionen, Bedingungen und Besitz-Automat ohne Home Assistant.

Ein Melder (Bewegung oder Anwesenheit) schaltet das Licht eines Raums, aber nur unter
Bedingungen (Zeitfenster, Sonne, Helligkeit). Die Engine merkt sich je Raum, wem das
Licht gerade gehört:

``none``    Niemand. Licht aus oder von Hand eingeschaltet, bevor der Melder etwas tat.
``motion``  Der Melder hat es eingeschaltet. Nur dann schaltet er auch wieder aus.
``manual``  Jemand hat das Licht verstellt. Der Melder setzt nichts mehr und schaltet
            nicht aus (außer optional nach dem manuellen Nachlauf).

Der Automat ist eine reine Funktion::

    step(options, state, event, env) -> (new_state, commands)

``state`` ist ein :class:`MotionState` (wird im Store gesichert), ``event`` ein
:class:`MotionEvent`, ``env`` die Umgebung (:class:`MotionEnv`: Zeit, Lichtzustand,
Lux, Sonne). Die Befehle (:class:`Command`) setzt :func:`plan_command` in MQTT bzw.
HA-Dienste um. Zeiten im Zustand sind Unix-Sekunden. Alles hier ist in
``tests/test_motion.py`` geprüft.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import re
from typing import Any

from .automations import Plan, RoomState, RoomTarget, _group_topic, _is_int, _is_num, _plan_off

# --- Konstanten ---------------------------------------------------------------------

OWNERS = ("none", "motion", "manual")
LIGHT_MODES = ("scene", "brightness")
SUN_EVENTS = ("sunrise", "sunset")

DEFAULT_DELAY = 120  # Sekunden ohne Bewegung bis Aus
DEFAULT_WARN_SECONDS = 20
RELIGHT_SECONDS = 30  # Nachschalten nach dem automatischen Aus
EXPECT_WINDOW = 1.5  # Sekunden, in denen Zustandsänderungen nach eigenem Befehl als eigene gelten
BRIGHTNESS_TOLERANCE = 0.10  # Abweichung (Anteil von 254), ab der eine Änderung eine Übernahme ist
HOLD_SAFETY = 6 * 3600  # Melder hängt auf „an“: spätestens dann aus
WARN_FACTOR = 0.5
MAX_WINDOWS = 6

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def default_light() -> dict[str, Any]:
    return {"mode": "brightness", "brightness": 204, "color_temp": None, "transition": 1.0}


def default_motion_options() -> dict[str, Any]:
    return {
        "windows": [],  # [{"from": "18:00", "to": "07:00"}], leer = immer
        "sun": None,  # {"from": {"event": "sunset", "offset": 0}, "until": {"event": "sunrise", "offset": 60}}
        "lux": None,  # {"below": 30, "entity": None}, entity None = Helligkeit des Melders
        "light": default_light(),
        "night": None,  # {"from": "23:00", "to": "06:00", "light": {...}}
        "delay": DEFAULT_DELAY,
        "warn": False,
        "warn_seconds": DEFAULT_WARN_SECONDS,
        "relight": True,
        "manual_delay": None,  # Sekunden, None = bei manuellem Licht nie automatisch aus
    }


def normalize_light(light: Any) -> dict[str, Any]:
    out = {**default_light(), **(light if isinstance(light, dict) else {})}
    return out


def normalize_motion_options(options: Any) -> dict[str, Any]:
    """Standardwerte ergänzen. Unbekannte Schlüssel bleiben erhalten."""
    opts = dict(options) if isinstance(options, dict) else {}
    out = {**default_motion_options(), **opts}
    out["light"] = normalize_light(opts.get("light"))
    if isinstance(out.get("night"), dict):
        out["night"] = {**out["night"], "light": normalize_light(out["night"].get("light"))}
    if not isinstance(out.get("windows"), list):
        out["windows"] = []
    return out


# --- Validierung ----------------------------------------------------------------------


def _valid_hhmm(v: Any) -> bool:
    return isinstance(v, str) and bool(_HHMM.match(v))


def _validate_light(light: Any, label: str) -> list[str]:
    if not isinstance(light, dict):
        return [f"{label}: Licht muss ein Objekt sein"]
    errors: list[str] = []
    mode = light.get("mode", "brightness")
    if mode not in LIGHT_MODES:
        return [f"{label}: mode muss scene oder brightness sein"]
    if mode == "scene":
        entity = light.get("scene_entity")
        if entity is not None and not (isinstance(entity, str) and entity.startswith("scene.")):
            errors.append(f"{label}: scene_entity muss eine scene.-Entity sein")
        elif entity is None and not _is_int(light.get("scene")):
            errors.append(f"{label}: Szene fehlt")
    else:
        b = light.get("brightness", 204)
        if not _is_int(b) or not 1 <= b <= 254:
            errors.append(f"{label}: brightness muss zwischen 1 und 254 liegen")
        ct = light.get("color_temp")
        if ct is not None and (not _is_int(ct) or not 1500 <= ct <= 9000):
            errors.append(f"{label}: color_temp muss in Kelvin zwischen 1500 und 9000 liegen")
    tr = light.get("transition", 1.0)
    if not _is_num(tr) or not 0 <= tr <= 30:
        errors.append(f"{label}: transition muss zwischen 0 und 30 Sekunden liegen")
    return errors


def _validate_sun_edge(edge: Any, label: str) -> list[str]:
    if edge is None:
        return []
    if not isinstance(edge, dict) or edge.get("event") not in SUN_EVENTS:
        return [f"sun.{label}: event muss sunrise oder sunset sein"]
    off = edge.get("offset", 0)
    if not _is_int(off) or not -360 <= off <= 360:
        return [f"sun.{label}: offset in Minuten zwischen -360 und 360"]
    return []


def validate_motion_shape(doc: dict[str, Any]) -> list[str]:
    """Optionen eines Bewegungslichts (Form, ohne Kontext)."""
    opts = doc.get("options") or {}
    if not isinstance(opts, dict):
        return ["options muss ein Objekt sein"]
    errors: list[str] = []
    windows = opts.get("windows", [])
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        errors.append("windows muss eine Liste mit höchstens 6 Zeitfenstern sein")
    else:
        for w in windows:
            if not (isinstance(w, dict) and _valid_hhmm(w.get("from")) and _valid_hhmm(w.get("to"))):
                errors.append("Zeitfenster brauchen from und to als HH:MM")
                break
    sun = opts.get("sun")
    if sun is not None:
        if not isinstance(sun, dict) or not (sun.get("from") or sun.get("until")):
            errors.append("sun braucht from oder until")
        else:
            errors += _validate_sun_edge(sun.get("from"), "from") + _validate_sun_edge(sun.get("until"), "until")
    lux = opts.get("lux")
    if lux is not None:
        if not isinstance(lux, dict) or not _is_num(lux.get("below")) or lux["below"] < 0:
            errors.append("lux.below muss eine Zahl >= 0 sein")
        elif lux.get("entity") is not None and not (isinstance(lux["entity"], str) and lux["entity"].startswith("sensor.")):
            errors.append("lux.entity muss ein sensor. sein")
    if "light" in opts:
        errors += _validate_light(opts["light"], "light")
    night = opts.get("night")
    if night is not None:
        if not (isinstance(night, dict) and _valid_hhmm(night.get("from")) and _valid_hhmm(night.get("to"))):
            errors.append("night braucht from und to als HH:MM")
        else:
            errors += _validate_light(night.get("light"), "night.light")
    delay = opts.get("delay", DEFAULT_DELAY)
    if not _is_int(delay) or not 5 <= delay <= 6 * 3600:
        errors.append("delay muss zwischen 5 und 21600 Sekunden liegen")
    if "warn" in opts and not isinstance(opts["warn"], bool):
        errors.append("warn muss wahr oder falsch sein")
    ws = opts.get("warn_seconds", DEFAULT_WARN_SECONDS)
    if not _is_int(ws) or not 5 <= ws <= 120:
        errors.append("warn_seconds muss zwischen 5 und 120 liegen")
    if "relight" in opts and not isinstance(opts["relight"], bool):
        errors.append("relight muss wahr oder falsch sein")
    md = opts.get("manual_delay")
    if md is not None and (not _is_int(md) or not 10 <= md <= 24 * 3600):
        errors.append("manual_delay muss leer oder zwischen 10 und 86400 Sekunden sein")
    return errors


# --- Bedingungen -----------------------------------------------------------------------


@dataclass
class MotionEnv:
    """Umgebung eines Schritts. ``now`` mit Zeitzone (lokal). ``None`` = unbekannt."""

    now: datetime
    light_on: bool | None = None
    brightness: int | None = None
    lux: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, start: str, end: str) -> bool:
    """``start`` bis ``end`` (Ende offen), über Mitternacht möglich. Gleich = ganzer Tag."""
    cur = now.hour * 60 + now.minute
    a, b = _minutes(start), _minutes(end)
    if a == b:
        return True
    return a <= cur < b if a < b else (cur >= a or cur < b)


def _edge_time(edge: dict[str, Any] | None, env: MotionEnv) -> datetime | None:
    if not edge:
        return None
    base = env.sunset if edge.get("event") == "sunset" else env.sunrise
    return base + timedelta(minutes=int(edge.get("offset") or 0)) if base else None


def sun_active(sun: dict[str, Any], env: MotionEnv) -> bool | None:
    """``from`` bis ``until`` an Sonnenereignissen mit Versatz. ``None`` = Sonnenzeiten unbekannt."""
    start = _edge_time(sun.get("from"), env)
    end = _edge_time(sun.get("until"), env)
    if (sun.get("from") and start is None) or (sun.get("until") and end is None):
        return None
    now = env.now
    if start and end:
        return start <= now < end if start <= end else (now >= start or now < end)
    if start:
        return now >= start
    return now < end if end else True


def windows_active(options: dict[str, Any], env: MotionEnv) -> bool:
    windows = options.get("windows") or []
    return not windows or any(in_window(env.now, w["from"], w["to"]) for w in windows)


def check_conditions(options: dict[str, Any], env: MotionEnv, *, light_off: bool = True) -> tuple[bool, str | None]:
    """Alle Bedingungen müssen gelten. Liefert ``(ok, Grund)`` mit Grund ``time``, ``sun`` oder ``lux``.

    Die Helligkeit zählt nur bei ausgeschaltetem Licht, sonst misst der Sensor das eigene Licht.
    Unbekannte Sonnenzeiten oder ein fehlender Lux-Wert blockieren nicht.
    """
    if not windows_active(options, env):
        return False, "time"
    sun = options.get("sun")
    if sun and sun_active(sun, env) is False:
        return False, "sun"
    lux = options.get("lux")
    if lux and light_off and env.lux is not None and not env.lux < float(lux["below"]):
        return False, "lux"
    return True, None


def select_light(options: dict[str, Any], env: MotionEnv) -> tuple[dict[str, Any], bool]:
    """Licht für jetzt: Nachtlicht im zweiten Fenster, sonst das Hauptlicht. ``(light, is_night)``."""
    night = options.get("night")
    if night and in_window(env.now, night["from"], night["to"]):
        return night["light"], True
    return options["light"], False


# --- Zustand und Ereignisse ------------------------------------------------------------------


@dataclass
class MotionState:
    owner: str = "none"
    occupied: bool = False
    deadline: float | None = None  # Unix-Zeit: Aus (owner motion) bzw. manueller Nachlauf (manual)
    warn_at: float | None = None
    warned: bool = False
    applied: dict[str, Any] | None = None  # Licht, das der Melder gesetzt hat (für „wiederherstellen“)
    night: bool = False
    expect_until: float = 0.0
    expect_on: bool | None = None
    baseline: int | None = None  # zuletzt bekannte eigene Helligkeit
    baseline_color: tuple[Any, ...] | None = None
    last_off: float | None = None  # Zeit des letzten automatischen Aus

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["baseline_color"] = list(self.baseline_color) if self.baseline_color else None
        return d

    @classmethod
    def from_dict(cls, data: Any) -> MotionState:
        if not isinstance(data, dict):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if known.get("owner") not in OWNERS:
            known["owner"] = "none"
        if isinstance(known.get("baseline_color"), list):
            known["baseline_color"] = tuple(known["baseline_color"])
        try:
            return cls(**known)
        except TypeError:
            return cls()


@dataclass(frozen=True)
class MotionEvent:
    """``kind``: ``motion`` (Melder an), ``clear`` (Melder aus), ``light`` (Licht des Raums geändert),
    ``timer`` (Zeitgeber), ``knob`` (Haven-Regler im Raum, ``light_on`` = erwarteter Zustand danach),
    ``takeover`` (Test: Übernahme erzwingen).

    Bei ``light``: ``own`` = HA-Kontext stammt von der Engine, ``user`` = fremder Benutzer im Kontext.
    """

    kind: str
    light_on: bool | None = None
    brightness: int | None = None
    color: tuple[Any, ...] | None = None
    own: bool = False
    user: bool = False


@dataclass(frozen=True)
class Command:
    """``kind``: ``on`` (``light``), ``off``, ``dim`` (``brightness``), ``notify`` (``action``)."""

    kind: str
    light: dict[str, Any] | None = None
    brightness: int | None = None
    transition: float | None = None
    action: str | None = None


def _notify(action: str) -> Command:
    return Command("notify", action=action)


def color_differs(a: tuple[Any, ...] | None, b: tuple[Any, ...] | None) -> bool:
    """Farbe als ``("ct", mired)``, ``("hs", h, s)`` oder ``("xy", x, y)``. Unbekannt = gleich."""
    if not a or not b:
        return False
    if a[0] != b[0]:
        return True
    try:
        if a[0] == "ct":
            return abs(a[1] - b[1]) >= 20
        if a[0] == "hs":
            dh = abs(a[1] - b[1])
            return min(dh, 360 - dh) >= 15 or abs(a[2] - b[2]) >= 15
        if a[0] == "xy":
            return abs(a[1] - b[1]) >= 0.03 or abs(a[2] - b[2]) >= 0.03
    except (TypeError, IndexError):
        return False
    return False


def brightness_differs(base: int | None, value: int | None) -> bool:
    if base is None or value is None:
        return False
    return abs(value - base) > BRIGHTNESS_TOLERANCE * 254


def _expect(s: MotionState, now: float, on: bool, brightness: int | None, transition: float = 0.0) -> None:
    s.expect_until = now + EXPECT_WINDOW + max(0.0, transition or 0.0)
    s.expect_on = on
    s.baseline = brightness
    s.baseline_color = None


def _arm(s: MotionState, options: dict[str, Any], now: float) -> None:
    """Zeitgeber für owner ``motion``: Melder aktiv = nur Sicherung, sonst Nachlauf (mit Vorwarnung)."""
    s.warned = False
    if s.occupied:
        s.deadline, s.warn_at = now + HOLD_SAFETY, None
        return
    delay = float(options["delay"])
    s.deadline = now + delay
    s.warn_at = now + delay - min(float(options["warn_seconds"]), delay / 2) if options["warn"] else None


def _hand_over(s: MotionState, options: dict[str, Any], now: float) -> list[Command]:
    s.owner, s.warned, s.warn_at = "manual", False, None
    md = options.get("manual_delay")
    s.deadline = None if (md is None or s.occupied) else now + float(md)
    return [_notify("takeover")]


def _release(s: MotionState) -> None:
    s.owner, s.deadline, s.warn_at, s.warned, s.applied = "none", None, None, False, None


def step(options: dict[str, Any], state: MotionState, event: MotionEvent, env: MotionEnv) -> tuple[MotionState, list[Command]]:
    """Ein Schritt des Besitz-Automaten. Rein: ``state`` bleibt unverändert, es gibt eine Kopie zurück."""
    s = replace(state)
    now = env.now.timestamp()
    cmds: list[Command] = []
    kind = event.kind

    if kind == "motion":
        s.occupied = True
        if s.owner != "none" and env.light_on is False and now > s.expect_until:
            # Licht ist aus, ohne dass wir es mitbekamen (z. B. Neustart, verlorenes Ereignis).
            # „manuell“ gilt nur, solange das Licht an ist.
            _release(s)
        if s.owner == "motion":
            if s.warned and s.applied:
                cmds.append(Command("on", light=s.applied, transition=1.0))
                _expect(s, now, True, None, 1.0)
            _arm(s, options, now)
            cmds.append(_notify("motion_extend"))
        elif s.owner == "manual":
            if options.get("manual_delay") is not None:
                s.deadline = None
        elif env.light_on is True:
            pass  # Licht war schon an: nicht anfassen, nicht ausschalten
        else:
            light, night = select_light(options, env)
            relight = options["relight"] and s.last_off is not None and now - s.last_off <= RELIGHT_SECONDS
            ok = relight or check_conditions(options, env, light_off=True)[0]
            if ok:
                s.owner, s.applied, s.night, s.last_off = "motion", light, night, None
                cmds.append(Command("on", light=light, transition=light.get("transition")))
                _expect(s, now, True, light.get("brightness") if light.get("mode") == "brightness" else None, light.get("transition") or 0)
                _arm(s, options, now)
                cmds.append(_notify("motion_on"))

    elif kind == "clear":
        s.occupied = False
        if s.owner == "motion":
            _arm(s, options, now)
        elif s.owner == "manual" and options.get("manual_delay") is not None:
            s.deadline = now + float(options["manual_delay"])

    elif kind == "timer":
        due = s.deadline is not None and now >= s.deadline - 1e-6
        if s.owner == "motion" and due:
            cmds.append(Command("off"))
            _expect(s, now, False, None)
            _release(s)
            s.last_off = now
            cmds.append(_notify("motion_off"))
        elif s.owner == "manual" and due:
            cmds.append(Command("off"))
            _expect(s, now, False, None)
            _release(s)
            cmds.append(_notify("motion_off"))
        elif s.owner == "motion" and s.warn_at is not None and not s.warned and now >= s.warn_at - 1e-6:
            current = env.brightness if env.brightness else (s.baseline or 128)
            target = max(1, round(current * WARN_FACTOR))
            cmds.append(Command("dim", brightness=target, transition=1.0))
            _expect(s, now, True, target, 1.0)
            s.warned = True
            cmds.append(_notify("warn"))

    elif kind == "light":
        cmds = _on_light(options, s, event, now)

    elif kind == "knob":
        # Haven-Regler im selben Raum: Mensch am Werk
        if event.light_on is False:
            if s.owner != "none":
                _release(s)
        elif s.owner == "motion":
            cmds = _hand_over(s, options, now)

    elif kind == "takeover":
        if s.owner == "motion":
            cmds = _hand_over(s, options, now)

    return s, cmds


def _on_light(options: dict[str, Any], s: MotionState, e: MotionEvent, now: float) -> list[Command]:
    if e.own:
        if e.light_on:
            s.baseline = e.brightness if e.brightness is not None else s.baseline
        return []
    if s.owner == "none":
        return []
    in_window = now <= s.expect_until
    if in_window and not e.user:
        if e.light_on is True and (s.expect_on is not False):
            s.baseline = e.brightness if e.brightness is not None else s.baseline
            s.baseline_color = e.color or s.baseline_color
        return []  # passt zum erwarteten Zustand (oder Zwischenstand): eigene Änderung
    if e.light_on is False:
        _release(s)  # Licht aus: zurück zu „niemand“, kein Aus mehr nötig
        return []
    if e.light_on is not True or s.owner == "manual":
        return []
    first = s.baseline is None and s.baseline_color is None
    if first and not e.user:
        s.baseline, s.baseline_color = e.brightness, e.color
        return []
    if e.user or brightness_differs(s.baseline, e.brightness) or color_differs(s.baseline_color, e.color):
        return _hand_over(s, options, now)
    return []


# --- Anzeige und Zeitgeber ------------------------------------------------------------------


def next_wake(s: MotionState) -> float | None:
    """Nächster Zeitpunkt, an dem ein ``timer``-Ereignis nötig ist (Unix-Zeit)."""
    times = [t for t in (s.deadline, None if s.warned else s.warn_at) if t is not None]
    return min(times) if times else None


def status(options: dict[str, Any], s: MotionState, env: MotionEnv) -> dict[str, Any]:
    """Für die App: ``owner``, ``off_at`` (Unix-Zeit), ``inactive`` (Grund) und ``phase``."""
    inactive = None
    if s.owner == "manual" and env.light_on is False and env.now.timestamp() > s.expect_until:
        s = replace(s, owner="none", deadline=None)  # Anzeige: aus = niemand (siehe ``step``)
    if s.owner == "none":
        ok, reason = check_conditions(options, env, light_off=env.light_on is not True)
        inactive = None if ok else reason
    phase = {"motion": "motion", "manual": "manual"}.get(s.owner) or ("inactive" if inactive else "idle")
    if s.owner == "motion" and s.warned:
        phase = "warn"
    return {
        "owner": s.owner,
        "phase": phase,
        "occupied": s.occupied,
        "off_at": s.deadline if s.owner != "none" and not (s.occupied and s.owner == "motion") else None,
        "inactive": inactive,
        "night": s.night if s.owner == "motion" else None,
        "lux": env.lux,
    }


# --- Befehle -> MQTT / HA ------------------------------------------------------------------------


def _mired(kelvin: int) -> int:
    return round(1_000_000 / kelvin)


def plan_command(cmd: Command, room: RoomTarget, state: RoomState, base_topic: str = "zigbee2mqtt") -> Plan:
    """Befehl als :class:`~.automations.Plan`: MQTT auf die Raumgruppe, sonst HA-Dienste."""
    if cmd.kind == "off":
        return _plan_off(room, state, base_topic)
    if cmd.kind == "dim":
        b = int(cmd.brightness or 1)
        exp = RoomState(True, b, state.lights_on)
        if room.group:
            return Plan("dim", mqtt=[(_group_topic(base_topic, room.group), {"brightness": b, "transition": cmd.transition or 0})], expected=exp)
        targets = state.lights_on or room.lights
        if not targets:
            return Plan(note="keine Lichter")
        return Plan("dim", services=[("light", "turn_on", {"entity_id": list(targets), "brightness": b, "transition": cmd.transition or 0})], expected=exp)
    if cmd.kind != "on" or not cmd.light:
        return Plan()
    light = cmd.light
    tr = float(light.get("transition") or 0)
    if light.get("mode") == "scene":
        entity = light.get("scene_entity")
        if entity:
            return Plan("on", services=[("scene", "turn_on", {"entity_id": entity, "transition": tr})], expected=RoomState(True, None, []))
        scene = light.get("scene")
        if room.group and scene in [s["id"] for s in room.scenes]:
            return Plan("on", mqtt=[(_group_topic(base_topic, room.group), {"scene_recall": scene})], scene=scene, expected=RoomState(True, None, []))
        return Plan(note="Szene fehlt")
    b = int(light.get("brightness") or 204)
    ct = light.get("color_temp")
    exp = RoomState(True, b, [])
    if room.group:
        payload: dict[str, Any] = {"state": "ON", "brightness": b, "transition": tr}
        if ct:
            payload["color_temp"] = _mired(int(ct))
        return Plan("on", mqtt=[(_group_topic(base_topic, room.group), payload)], expected=exp)
    if not room.lights:
        return Plan(note="keine Lichter")
    data: dict[str, Any] = {"entity_id": list(room.lights), "brightness": b, "transition": tr}
    if ct:
        data["color_temp_kelvin"] = int(ct)
    return Plan("on", services=[("light", "turn_on", data)], expected=exp)


# --- Entities des Melders -------------------------------------------------------------------------

DETECTOR_CLASSES = ("occupancy", "motion", "presence")


def pick_detector_entity(states: dict[str, dict[str, Any]], entity_ids: list[str]) -> str | None:
    """``binary_sensor`` eines Melders (HA-Entities je IEEE). Bevorzugt ``presence`` vor ``occupancy`` vor ``motion``."""
    found: dict[str, str] = {}
    for eid in entity_ids:
        if not eid.startswith("binary_sensor."):
            continue
        cls = ((states.get(eid) or {}).get("attributes") or {}).get("device_class")
        if cls in DETECTOR_CLASSES:
            found.setdefault(cls, eid)
    for cls in ("presence", "occupancy", "motion"):
        if cls in found:
            return found[cls]
    return None


def pick_lux_entity(states: dict[str, dict[str, Any]], entity_ids: list[str]) -> str | None:
    """``sensor`` mit ``device_class: illuminance`` unter den Entities eines Geräts."""
    for eid in entity_ids:
        if eid.startswith("sensor.") and ((states.get(eid) or {}).get("attributes") or {}).get("device_class") == "illuminance":
            return eid
    return None


def parse_lux(state: str | None) -> float | None:
    try:
        return float(state) if state not in (None, "", "unknown", "unavailable") else None
    except (TypeError, ValueError):
        return None


def detector_value(values: Any) -> bool | None:
    """Melderzustand aus einer Z2M-Meldung: ``presence`` vor ``occupancy``, sonst unbekannt."""
    if not isinstance(values, dict):
        return None
    for key in ("presence", "occupancy"):
        if isinstance(values.get(key), bool):
            return values[key]
    return None


def detector_active(state: str | None) -> bool | None:
    """HA-Zustand eines Melders: ``on`` = aktiv, ``off`` = nicht, sonst unbekannt."""
    return True if state == "on" else False if state == "off" else None

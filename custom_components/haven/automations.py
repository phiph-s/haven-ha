"""Haven-Automationen: Modell, Validierung, Normalisierung und Planung (ohne Home Assistant).

Eine Automation ist ein Dokument ``{id, type, name, enabled, room_area_id, devices, options}``.
Typen: ``room_knob`` (Drehregler und Taster steuern das Licht eines Raums) und
``room_motion`` (Bewegungslicht, siehe ``motion.py``).

Ablauf zur Laufzeit (``automations_runtime``):

1. Z2M meldet auf ``<base>/<friendly_name>`` ein JSON mit ``action``.
   :func:`normalize_action` macht daraus eine :class:`Gesture` (Drehen, Drücken,
   Doppelt, Halten), egal ob das Gerät im ``event``- oder ``command``-Modus läuft.
2. Drehungen laufen durch :class:`StepCoalescer` (erste sofort, weitere im Fenster
   zusammengefasst), damit die Gruppe nicht geflutet wird.
3. :func:`plan_gesture` liefert die Befehle als reine Daten: MQTT ``(topic, payload)``
   auf die Z2M-Gruppe des Raums oder, ohne Gruppe, HA-Dienstaufrufe auf die Lichter.

Alles hier ist reines Python und wird in ``tests/test_automations.py`` geprüft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any
import uuid

from .commands import Z2MError

# --- Konstanten -----------------------------------------------------------------

AUTOMATION_TYPES = ("room_knob", "room_motion")
GESTURES = ("rotate", "single", "double", "hold")
# Aktionen für Druck-Gesten (Drücken, Doppelt, Halten)
PRESS_ACTIONS = ("none", "toggle", "next_scene", "scene", "off")
# Aktionen für Drehen
ROTATE_ACTIONS = ("none", "brightness")
ON_ACTIONS = ("last", "scene")

DEFAULT_STEP = 35  # von 255, wie bisher in den HA-Automationen
DEFAULT_TRANSITION = 0.2
DEFAULT_MIN_BRIGHTNESS = 3
COALESCE_WINDOW = 0.12  # Sekunden, Drehereignisse in diesem Fenster werden zusammengefasst
MAX_AUTOMATIONS = 200
MAX_DEVICES = 32

# Store ``.storage/haven.automations``
STORE_KEY = "haven.automations"
STORE_VERSION = 1
STORE_MINOR_VERSION = 3

DEFAULT_GESTURES: dict[str, dict[str, Any]] = {
    "rotate": {"action": "brightness"},
    "single": {"action": "toggle"},
    "double": {"action": "next_scene"},
    "hold": {"action": "none"},
}


def default_options() -> dict[str, Any]:
    return {
        "gestures": {k: dict(v) for k, v in DEFAULT_GESTURES.items()},
        "scenes": None,  # None = alle Szenen der Gruppe in ihrer Reihenfolge
        "on_action": "last",
        "on_scene": None,
        "step": DEFAULT_STEP,
        "transition": DEFAULT_TRANSITION,
        "min_brightness": DEFAULT_MIN_BRIGHTNESS,
        "off_at_min": False,
    }


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- Normalisierung der Z2M-Aktionen ------------------------------------------------

# Rohwert von ``action`` -> (Geste, Richtung). Event-Modus und Command-Modus.
_ACTION_MAP: dict[str, tuple[str, int]] = {
    # event-Modus (ZG-101ZD, ZG-101ZL)
    "rotate_right": ("rotate", 1),
    "rotate_left": ("rotate", -1),
    "single": ("single", 0),
    "double": ("double", 0),
    "hold": ("hold", 0),
    # command-Modus: Drehen = Helligkeit, Drücken = Ein/Aus
    "brightness_step_up": ("rotate", 1),
    "brightness_step_down": ("rotate", -1),
    "toggle": ("single", 0),
    "on": ("single", 0),
    "off": ("single", 0),
    # verbreitete Schreibweisen anderer Taster
    "single_click": ("single", 0),
    "click": ("single", 0),
    "press": ("single", 0),
    "double_click": ("double", 0),
    "long": ("hold", 0),
    "long_press": ("hold", 0),
}

# command-Modus: Drücken und Drehen bzw. Halten. Werden bewusst ignoriert, damit
# nichts Unerwartetes passiert (Farbtemperatur/Farbton steuert Haven noch nicht).
IGNORED_ACTIONS = frozenset(
    {
        "color_temperature_step_up",
        "color_temperature_step_down",
        "color_temperature_move",
        "brightness_move_to_level",
        "hue_move",
        "hue_stop",
        "saturation_move",
    }
)


@dataclass(frozen=True)
class Gesture:
    """Normalisierte Geste. ``direction`` +1/-1 nur bei ``rotate``, ``steps`` >= 1."""

    kind: str
    direction: int = 0
    steps: int = 1
    raw: str = ""

    @property
    def name(self) -> str:
        """Name für Ereignisse und die App: ``rotate_left``, ``rotate_right``, ``single`` ..."""
        if self.kind == "rotate":
            return "rotate_right" if self.direction > 0 else "rotate_left"
        return self.kind


def normalize_action(values: Any) -> Gesture | None:
    """Gerätemeldung (``{"action": ..., ...}``) zu einer Geste, sonst ``None``.

    Leere Aktionen (``""``, ``None``) und unbekannte Werte ergeben ``None``.
    """
    if not isinstance(values, dict):
        return None
    action = values.get("action")
    if not isinstance(action, str):
        return None
    action = action.strip().lower()
    if not action or action in IGNORED_ACTIONS:
        return None
    mapped = _ACTION_MAP.get(action)
    if mapped is None:
        return None
    kind, direction = mapped
    return Gesture(kind, direction, 1, action)


def gesture_from_name(name: str) -> Gesture | None:
    """``rotate_left`` / ``rotate_right`` / ``single`` / ``double`` / ``hold`` (für den Test-Befehl)."""
    return normalize_action({"action": name}) if name in ("rotate_left", "rotate_right", "single", "double", "hold") else None


# --- Entprellen der Drehungen ----------------------------------------------------------


class StepCoalescer:
    """Fasst Drehschritte je Schlüssel zusammen.

    Der erste Schritt nach einer Pause geht sofort raus (schnelle Reaktion). Weitere
    Schritte innerhalb von ``window`` Sekunden werden addiert und beim nächsten
    :meth:`flush` gemeinsam geliefert, höchstens einmal pro Fenster. Gegenläufige
    Schritte heben sich auf. ``now`` ist monotone Zeit in Sekunden.
    """

    def __init__(self, window: float = COALESCE_WINDOW) -> None:
        self.window = window
        self._last: dict[str, float] = {}
        self._pending: dict[str, int] = {}

    def push(self, key: str, steps: int, now: float) -> int | None:
        """Sofort auszuführende Schritte oder ``None`` (zurückgestellt)."""
        last = self._last.get(key)
        if key not in self._pending and (last is None or now - last >= self.window):
            self._last[key] = now
            return steps
        self._pending[key] = self._pending.get(key, 0) + steps
        return None

    def delay(self, key: str, now: float) -> float | None:
        if key not in self._pending:
            return None
        return max(0.0, self._last[key] + self.window - now)

    def flush(self, key: str, now: float) -> int | None:
        """Gesammelte Schritte, wenn das Fenster abgelaufen ist. 0 wird zu ``None``."""
        if key not in self._pending or now - self._last[key] < self.window - 1e-9:
            return None
        self._last[key] = now
        steps = self._pending.pop(key)
        return steps or None

    def clear(self) -> None:
        self._last.clear()
        self._pending.clear()


# --- Raum ----------------------------------------------------------------------------


@dataclass
class RoomTarget:
    """Was eine Raum-Automation steuert.

    ``group``: Friendly-Name der Z2M-Raumgruppe (``None`` = Fallback über HA-Dienste).
    ``lights``: gesteuerte Lichter des Raums (ohne ausgenommene), für den Fallback.
    ``scenes``: Szenen der Gruppe ``[{id, name}]`` in Z2M-Reihenfolge.
    """

    area_id: str
    name: str
    group: str | None = None
    group_id: int | None = None
    group_entity: str | None = None
    lights: list[str] = field(default_factory=list)
    scenes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_lights(self) -> bool:
        return bool(self.group or self.lights)

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "group": self.group,
            "group_id": self.group_id,
            "group_entity": self.group_entity,
            "lights": list(self.lights),
            "scenes": [dict(s) for s in self.scenes],
        }


def build_room_targets(
    rooms: list[dict[str, Any]], groups: list[dict[str, Any]], excluded: list[str] | set[str] = ()
) -> dict[str, RoomTarget]:
    """Aus ``haven/rooms`` (``rooms``) und ``bridge.groups`` (roh) die Ziele je Bereich."""
    excluded_set = set(excluded)
    by_id = {g.get("id"): g for g in groups if isinstance(g, dict)}
    out: dict[str, RoomTarget] = {}
    for room in rooms:
        group = room.get("group") or None
        raw = by_id.get(group.get("id")) if group else None
        scenes = sorted(
            (
                {"id": s.get("id"), "name": s.get("name")}
                for s in (raw or {}).get("scenes", [])
                if isinstance(s, dict) and isinstance(s.get("id"), int)
            ),
            key=lambda s: s["id"],
        )
        out[room["area_id"]] = RoomTarget(
            area_id=room["area_id"],
            name=room.get("name") or room["area_id"],
            group=(group or {}).get("friendly_name"),
            group_id=(group or {}).get("id"),
            group_entity=room.get("group_entity"),
            lights=[l for l in room.get("lights", []) if l not in excluded_set],
            scenes=scenes,
        )
    return out


@dataclass
class RoomState:
    """Zustand des Raums zum Planen. ``None`` = unbekannt."""

    on: bool | None = None
    brightness: int | None = None  # 0..255
    lights_on: list[str] = field(default_factory=list)


def room_state_from(states: dict[str, dict[str, Any]], target: RoomTarget) -> RoomState:
    """``states``: entity_id -> ``{"state": "on", "attributes": {...}}`` (HA-Zustände).

    Mit Gruppen-Entity zählt sie, sonst das hellste Licht.
    """
    def _bri(s: dict[str, Any]) -> int | None:
        b = (s.get("attributes") or {}).get("brightness")
        return int(b) if isinstance(b, (int, float)) and not isinstance(b, bool) else None

    lights_on = [l for l in target.lights if (states.get(l) or {}).get("state") == "on"]
    if target.group_entity and target.group_entity in states:
        s = states[target.group_entity]
        if s.get("state") in ("on", "off"):
            return RoomState(s["state"] == "on", _bri(s) if s["state"] == "on" else 0, lights_on)
    known = [states[l] for l in target.lights if (states.get(l) or {}).get("state") in ("on", "off")]
    if not known:
        return RoomState(None, None, lights_on)
    bris = [b for s in known if s.get("state") == "on" and (b := _bri(s)) is not None]
    on = bool(lights_on)
    return RoomState(on, (max(bris) if bris else None) if on else 0, lights_on)


# --- Modell und Validierung -------------------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def normalize_options(options: Any) -> dict[str, Any]:
    """Füllt fehlende Optionen mit Standardwerten. Unbekannte Schlüssel bleiben erhalten."""
    opts = dict(options) if isinstance(options, dict) else {}
    base = default_options()
    gestures = opts.get("gestures") if isinstance(opts.get("gestures"), dict) else {}
    merged = {}
    for g in GESTURES:
        value = gestures.get(g)
        if isinstance(value, str):  # Kurzform {"hold": "toggle"}
            value = {"action": value}
        merged[g] = dict(value) if isinstance(value, dict) else dict(DEFAULT_GESTURES[g])
    out = {**base, **opts, "gestures": merged}
    return out


def normalize_automation(doc: dict[str, Any]) -> dict[str, Any]:
    """Gespeicherte Form: Standardwerte ergänzt, Geräte ohne Duplikate."""
    devices = doc.get("devices") if isinstance(doc.get("devices"), list) else []
    if doc.get("type") == "room_motion":
        from .motion import normalize_motion_options  # noqa: PLC0415 - motion importiert dieses Modul

        options = normalize_motion_options(doc.get("options"))
    else:
        options = normalize_options(doc.get("options"))
    return {
        **doc,
        "id": doc.get("id") or new_id(),
        "type": doc.get("type") or "room_knob",
        "name": doc.get("name") if isinstance(doc.get("name"), str) else "",
        "enabled": bool(doc.get("enabled", True)),
        "room_area_id": doc.get("room_area_id"),
        "devices": list(dict.fromkeys(d for d in devices if isinstance(d, str))),
        "options": options,
    }


def validate_shape(doc: Any) -> list[str]:
    """Form einer einzelnen Automation (ohne Kontext)."""
    if not isinstance(doc, dict):
        return ["Die Automation muss ein JSON-Objekt sein"]
    errors: list[str] = []
    if doc.get("type", "room_knob") not in AUTOMATION_TYPES:
        errors.append(f"Unbekannter Typ {doc.get('type')}")
        return errors
    if "id" in doc and doc["id"] is not None and not isinstance(doc["id"], str):
        errors.append("id muss ein Text sein")
    if "name" in doc and not isinstance(doc["name"], str):
        errors.append("name muss ein Text sein")
    if "enabled" in doc and not isinstance(doc["enabled"], bool):
        errors.append("enabled muss wahr oder falsch sein")
    if not isinstance(doc.get("room_area_id"), str) or not doc["room_area_id"]:
        errors.append("room_area_id fehlt")
    devices = doc.get("devices")
    motion = doc.get("type") == "room_motion"
    if not isinstance(devices, list) or not devices:
        errors.append("Wähl mindestens einen Melder" if motion else "Wähl mindestens einen Regler oder Taster")
    elif len(devices) > MAX_DEVICES:
        errors.append("Zu viele Geräte")
    elif not all(isinstance(d, str) and d for d in devices):
        errors.append("devices: jeder Eintrag muss eine IEEE-Adresse sein")
    opts = doc.get("options", {})
    if opts is None:
        opts = {}
    if not isinstance(opts, dict):
        errors.append("options muss ein Objekt sein")
        return errors
    if motion:
        from .motion import validate_motion_shape  # noqa: PLC0415

        return errors + validate_motion_shape(doc)
    gestures = opts.get("gestures", {})
    if not isinstance(gestures, dict):
        errors.append("gestures muss ein Objekt sein")
    else:
        for g, value in gestures.items():
            if g not in GESTURES:
                errors.append(f"Unbekannte Geste {g}")
                continue
            value = {"action": value} if isinstance(value, str) else value
            if not isinstance(value, dict):
                errors.append(f"{g}: Belegung muss ein Objekt sein")
                continue
            allowed = ROTATE_ACTIONS if g == "rotate" else PRESS_ACTIONS
            if value.get("action") not in allowed:
                errors.append(f"{g}: unbekannte Aktion {value.get('action')}")
            elif value.get("action") == "scene" and not _is_int(value.get("scene")):
                errors.append(f"{g}: Szene fehlt")
    scenes = opts.get("scenes")
    if scenes is not None and (not isinstance(scenes, list) or not all(_is_int(s) for s in scenes)):
        errors.append("scenes muss eine Liste von Szenen-IDs sein")
    if opts.get("on_action", "last") not in ON_ACTIONS:
        errors.append("on_action muss last oder scene sein")
    elif opts.get("on_action") == "scene" and not _is_int(opts.get("on_scene")):
        errors.append("Beim Einschalten: Szene fehlt")
    for key, lo, hi in (("step", 1, 255), ("min_brightness", 1, 254)):
        if key in opts and (not _is_int(opts[key]) or not lo <= opts[key] <= hi):
            errors.append(f"{key} muss zwischen {lo} und {hi} liegen")
    if "transition" in opts and (not _is_num(opts["transition"]) or not 0 <= opts["transition"] <= 10):
        errors.append("transition muss zwischen 0 und 10 Sekunden liegen")
    if "off_at_min" in opts and not isinstance(opts["off_at_min"], bool):
        errors.append("off_at_min muss wahr oder falsch sein")
    return errors


def device_owner(
    automations: list[dict[str, Any]], ieee: str, *, exclude_id: str | None = None, type: str | None = None
) -> dict[str, Any] | None:
    """Aktive Automation, die das Gerät schon steuert (optional nur eines Typs)."""
    for a in automations:
        if type is not None and a.get("type", "room_knob") != type:
            continue
        if a.get("id") != exclude_id and a.get("enabled", True) and ieee in a.get("devices", []):
            return a
    return None


def validate_automation(
    doc: Any,
    existing: list[dict[str, Any]],
    rooms: dict[str, RoomTarget] | None = None,
    known_devices: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Prüft eine Automation im Zusammenhang und liefert die gespeicherte Form.

    Fehler als :class:`Z2MError`: ``invalid_format`` (Form), ``not_found`` (Raum oder
    Gerät unbekannt), ``no_lights`` (Raum ohne Lichter), ``device_taken`` (Gerät steckt
    schon in einer anderen aktiven Knob-Automation). ``known_devices``: IEEE -> Name.
    """
    errors = validate_shape(doc)
    if errors:
        raise Z2MError("invalid_format", "Die Automation ist ungültig: " + "; ".join(errors[:3]))
    automation = normalize_automation(doc)
    if rooms is not None:
        room = rooms.get(automation["room_area_id"])
        if room is None:
            raise Z2MError("not_found", f"Unbekannter Raum {automation['room_area_id']}")
        if not room.has_lights:
            raise Z2MError("no_lights", f"{room.name} hat keine Lichter, die Haven steuern kann")
    if known_devices is not None:
        unknown = [d for d in automation["devices"] if d not in known_devices]
        if unknown:
            raise Z2MError("not_found", f"Unbekanntes Zigbee-Gerät {unknown[0]}")
    if automation["enabled"]:
        for ieee in automation["devices"]:
            owner = device_owner(existing, ieee, exclude_id=automation["id"], type=automation["type"])
            if owner is not None:
                name = (known_devices or {}).get(ieee) or ieee
                raise Z2MError(
                    "device_taken", f"{name} {'meldet schon für' if automation['type'] == 'room_motion' else 'steuert schon'} {owner.get('name') or 'eine andere Automation'}"
                )
    return automation


def default_name(room: RoomTarget | None, area_id: str, type: str = "room_knob") -> str:
    if type == "room_motion":
        return f"Bewegungslicht {room.name if room else area_id}"
    return f"Drehregler {room.name if room else area_id}"


# --- Szenen --------------------------------------------------------------------------


def scene_cycle(options: dict[str, Any], room: RoomTarget) -> list[int]:
    """Szenen-IDs zum Weiterschalten: gewählte Reihenfolge, nur vorhandene. Standard: alle."""
    available = [s["id"] for s in room.scenes]
    chosen = options.get("scenes")
    if not chosen:
        return available
    return [s for s in dict.fromkeys(chosen) if s in available]


def next_scene(cycle: list[int], last_scene: int | None) -> int | None:
    """Szene nach ``last_scene`` mit Umlauf. Unbekannt oder ``None``: die erste."""
    if not cycle:
        return None
    if last_scene in cycle:
        return cycle[(cycle.index(last_scene) + 1) % len(cycle)]
    return cycle[0]


# --- Planung ------------------------------------------------------------------------------


@dataclass
class Plan:
    """Ergebnis von :func:`plan_gesture`.

    ``mqtt``: ``[(topic, payload_dict)]``, ``services``: ``[(domain, service, data)]``.
    ``scene``: gerufene Szene (für den Zähler), ``expected``: erwarteter Zustand danach.
    ``action``: was passiert ist (``brightness``, ``on``, ``off``, ``scene``, ``none``).
    """

    action: str = "none"
    mqtt: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    services: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    scene: int | None = None
    expected: RoomState | None = None
    note: str | None = None

    @property
    def empty(self) -> bool:
        return not self.mqtt and not self.services


def _group_topic(base: str, group: str) -> str:
    return f"{base}/{group}/set"


def gesture_action(options: dict[str, Any], gesture: Gesture) -> dict[str, Any]:
    gestures = normalize_options(options)["gestures"]
    return gestures.get(gesture.kind) or {"action": "none"}


def plan_gesture(
    automation: dict[str, Any],
    gesture: Gesture,
    room: RoomTarget,
    state: RoomState,
    *,
    base_topic: str = "zigbee2mqtt",
    last_scene: int | None = None,
) -> Plan:
    """Befehle für eine Geste. Rein, ohne Nebenwirkungen.

    Bei ``rotate`` zählt ``gesture.steps`` (zusammengefasste Schritte, Vorzeichen in ``direction``).
    """
    options = normalize_options(automation.get("options"))
    binding = gesture_action(options, gesture)
    action = binding.get("action", "none")
    if gesture.kind == "rotate":
        if action != "brightness":
            return Plan(note="nicht belegt")
        return _plan_brightness(options, gesture.direction * gesture.steps, room, state, base_topic)
    if action == "toggle":
        if state.on:
            return _plan_off(room, state, base_topic)
        return _plan_on(options, room, state, base_topic, last_scene)
    if action == "off":
        return _plan_off(room, state, base_topic)
    if action == "next_scene":
        target = next_scene(scene_cycle(options, room), last_scene)
        if target is None:
            return Plan(note="keine Szenen")
        return _plan_scene(room, target, base_topic)
    if action == "scene":
        return _plan_scene(room, binding.get("scene"), base_topic)
    return Plan(note="nicht belegt")


def _plan_brightness(
    options: dict[str, Any], steps: int, room: RoomTarget, state: RoomState, base: str
) -> Plan:
    if steps == 0:
        return Plan()
    step = int(options["step"])
    delta = steps * step
    transition = float(options["transition"])
    minimum = int(options["min_brightness"])
    current = state.brightness if state.on else (0 if state.on is False else None)

    if delta < 0:
        if state.on is False:
            return Plan(note="schon aus")
        if current is not None:
            if current <= minimum:
                if options.get("off_at_min"):
                    return _plan_off(room, state, base)
                return Plan(note="Minimum")
            new = max(minimum, current + delta)
            delta = new - current
    else:
        if current is not None and current >= 254:
            return Plan(note="Maximum")
        if current is not None:
            delta = min(254, current + delta) - current
    expected = RoomState(True, None if current is None else max(0, min(254, current + delta)), state.lights_on)
    if room.group:
        payload: dict[str, Any] = {"brightness_step": delta, "transition": transition}
        if delta > 0 and state.on is False:
            # Aus und hochdrehen: mit kleiner Helligkeit einschalten statt der letzten
            payload = {"state": "ON", "brightness": max(minimum, min(254, delta)), "transition": transition}
            expected = RoomState(True, payload["brightness"], state.lights_on)
        return Plan("brightness", mqtt=[(_group_topic(base, room.group), payload)], expected=expected)
    targets = state.lights_on or (room.lights if delta > 0 else [])
    if not targets:
        return Plan(note="keine Lichter")
    data = {"entity_id": list(targets), "brightness_step": delta, "transition": transition}
    return Plan("brightness", services=[("light", "turn_on", data)], expected=expected)


def _plan_off(room: RoomTarget, state: RoomState, base: str) -> Plan:
    expected = RoomState(False, 0, [])
    if room.group:
        return Plan("off", mqtt=[(_group_topic(base, room.group), {"state": "OFF"})], expected=expected)
    if not room.lights:
        return Plan(note="keine Lichter")
    return Plan("off", services=[("light", "turn_off", {"entity_id": list(room.lights)})], expected=expected)


def _plan_on(
    options: dict[str, Any], room: RoomTarget, state: RoomState, base: str, last_scene: int | None
) -> Plan:
    if options.get("on_action") == "scene" and room.group:
        scene = options.get("on_scene")
        if scene in [s["id"] for s in room.scenes]:
            plan = _plan_scene(room, scene, base)
            plan.action = "on"
            return plan
    if room.group:
        # Zustand unbekannt: umschalten wie bisher in HA, sonst eindeutig einschalten
        payload = {"state": "TOGGLE"} if state.on is None else {"state": "ON"}
        return Plan("on", mqtt=[(_group_topic(base, room.group), payload)], expected=RoomState(True, None, []))
    if not room.lights:
        return Plan(note="keine Lichter")
    service = "toggle" if state.on is None else "turn_on"
    return Plan("on", services=[("light", service, {"entity_id": list(room.lights)})], expected=RoomState(True, None, []))


def _plan_scene(room: RoomTarget, scene: Any, base: str) -> Plan:
    if not room.group or not _is_int(scene) or scene not in [s["id"] for s in room.scenes]:
        return Plan(note="Szene fehlt")
    return Plan(
        "scene",
        mqtt=[(_group_topic(base, room.group), {"scene_recall": scene})],
        scene=scene,
        expected=RoomState(True, None, []),
    )


def plan_to_json(plan: Plan) -> dict[str, Any]:
    """Für Antworten von ``haven/automations/test``."""
    return {
        "action": plan.action,
        "mqtt": [{"topic": t, "payload": p} for t, p in plan.mqtt],
        "services": [{"domain": d, "service": s, "data": data} for d, s, data in plan.services],
        "scene": plan.scene,
        "note": plan.note,
    }


def mqtt_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


# --- Store ----------------------------------------------------------------------------------


def empty_store() -> dict[str, Any]:
    return {"automations": [], "runtime": {"last_scene": {}, "last_triggered": {}, "motion": {}}}


def migrate_store(old_major: int, old_minor: int, data: Any) -> dict[str, Any]:
    """Store-Migration (``haven.automations``).

    - 1.1: ``{"automations": [...]}`` ohne ``runtime``, Optionen flach
      (``hold_action``, ``hold_scene``, ``step``), Szenen-Zähler unter ``scene_index``.
    - 1.3 (aktuell): wie 1.2, dazu ``runtime.motion`` (``{id: MotionState}``, Besitzer und Zeitgeber der
      Bewegungslichter) und Automationen vom Typ ``room_motion``. Die Migration von 1.2 ergänzt nur ``motion``.
    - 1.2: ``{"automations": [...], "runtime": {"last_scene": {room: id},
      "last_triggered": {id: {at, action}}}}``, Gesten unter ``options.gestures``.
    Eine bloße Liste gilt als 1.1 ohne Hülle.
    """
    if isinstance(data, list):
        data = {"automations": data}
    if not isinstance(data, dict):
        return empty_store()
    automations = [a for a in data.get("automations", []) if isinstance(a, dict)]
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    if old_major == 1 and old_minor < 2:
        migrated = []
        for a in automations:
            opts = dict(a.get("options") or {})
            gestures = dict(opts.get("gestures") or {})
            if "hold_action" in opts:
                hold = opts.pop("hold_action")
                gestures.setdefault("hold", {"action": hold, **({"scene": opts.get("hold_scene")} if hold == "scene" else {})})
                opts.pop("hold_scene", None)
            opts["gestures"] = gestures
            migrated.append({**a, "options": opts})
        automations = migrated
        legacy = data.get("scene_index")
        if isinstance(legacy, dict):
            runtime = {**runtime, "last_scene": {k: v for k, v in legacy.items() if _is_int(v)}}
    return {
        "automations": [normalize_automation(a) for a in automations],
        "runtime": {
            "last_scene": dict(runtime.get("last_scene") or {}),
            "last_triggered": dict(runtime.get("last_triggered") or {}),
            "motion": {k: v for k, v in (runtime.get("motion") or {}).items() if isinstance(v, dict)},
        },
    }


def read_store(data: Any) -> dict[str, Any]:
    """Geladener Store (aktuelle Version) in die Arbeitsform."""
    return migrate_store(1, STORE_MINOR_VERSION, data) if data is not None else empty_store()


def public_automation(automation: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Listeneintrag für ``haven/automations/list`` mit ``last_triggered``."""
    return {**automation, "last_triggered": (runtime.get("last_triggered") or {}).get(automation["id"])}


def event_data(automation: dict[str, Any], gesture: Gesture, room: RoomTarget | None, **extra: Any) -> dict[str, Any]:
    """Payload von ``haven_automation_triggered``."""
    return {
        "id": automation["id"],
        "action": gesture.name,
        "room": automation.get("room_area_id"),
        "room_name": room.name if room else None,
        **extra,
    }

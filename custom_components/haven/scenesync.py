"""Szenen-Abgleich: Soll, Messung, Vergleich und Status (ohne Home Assistant).

Z2M speichert Szenen auf den Lampen und kann sie nicht zurücklesen. Haven merkt sich deshalb
je Szene ein **Soll** (Zustand jeder Mitgliedslampe beim Speichern) und prüft eine Szene,
indem es sie aufruft und die Lampen echt lesen lässt (``<friendly_name>/get``).

Lampenzustand (``LampState``): ``{on, brightness: 0–100|None, kelvin: int|None, rgb: "#rrggbb"|None}``,
bei ``on=False`` sind die übrigen Felder ``None``. Weiß: ``kelvin``, Farbe: ``rgb``, nur dimmbar: beides ``None``.

Soll im Store (``.storage/haven.scenes`` unter ``targets``, Schlüssel ``"<group_id>:<scene_id>"``):
``{members: {entity_id: LampState}, ieees: [...], stored_at, source: "store"|"adopt"}``.
``ieees`` sind die Licht-Mitglieder der Z2M-Gruppe beim Speichern (für ``stale``).

Die Abläufe mit Home Assistant (Aufrufen, Lesen, Wiederherstellen) stehen in ``scenesync_runtime.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
import math
from typing import Any

# Schwellen des Vergleichs je Lampe
BRIGHTNESS_TOLERANCE = 8  # Prozentpunkte
RGB_TOLERANCE = 40  # je Kanal (0–255)
KELVIN_TOLERANCE = 300

# Farbe einer nur dimmbaren Lampe im gemessenen Look (warmweiß)
_DIMMABLE_KELVIN = 2700

_COLOR_MODES = ("hs", "xy", "rgb", "rgbw", "rgbww")
_UNREACHABLE = ("unavailable", "unknown")

MEMBER_STATUSES = ("ok", "missing", "differs", "unreachable")
SYNC_STATUSES = ("ok", "stale", "differs", "unknown")

OFF: dict[str, Any] = {"on": False, "brightness": None, "kelvin": None, "rgb": None}


# --- Farben ---------------------------------------------------------------------------


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb[:3])


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def kelvin_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """Näherung nach Tanner Helland (wie ``color_temperature_to_rgb`` in HA)."""
    t = max(1000.0, min(40000.0, float(kelvin))) / 100
    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        red = 329.698727446 * (t - 60) ** -0.1332047592
        green = 288.1221695283 * (t - 60) ** -0.0755148492
    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0, min(255, round(c))) for c in (red, green, blue))  # type: ignore[return-value]


def _hs_to_rgb(hs: Sequence[float]) -> tuple[int, int, int]:
    import colorsys  # noqa: PLC0415

    r, g, b = colorsys.hsv_to_rgb((float(hs[0]) % 360) / 360, max(0.0, min(100.0, float(hs[1]))) / 100, 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


# --- Lampenzustand aus Home Assistant -------------------------------------------------------


def lamp_state_from(state: str | None, attributes: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``LampState`` aus einem HA-Zustand, ``None`` bei ``unavailable``/``unknown``/fehlend."""
    if state not in ("on", "off"):
        return None
    if state == "off":
        return dict(OFF)
    attrs = attributes or {}
    raw = attrs.get("brightness")
    brightness = round(float(raw) * 100 / 255) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
    mode = attrs.get("color_mode")
    kelvin = rgb = None
    if mode == "color_temp" or (mode is None and attrs.get("color_temp_kelvin") is not None):
        kelvin = _kelvin_attr(attrs)
    if kelvin is None and (mode in _COLOR_MODES or mode is None):
        rgb = _rgb_attr(attrs)
    return {"on": True, "brightness": brightness, "kelvin": kelvin, "rgb": rgb}


def _kelvin_attr(attrs: Mapping[str, Any]) -> int | None:
    value = attrs.get("color_temp_kelvin")
    if isinstance(value, (int, float)) and value > 0:
        return round(value)
    mired = attrs.get("color_temp")
    if isinstance(mired, (int, float)) and mired > 0:
        return round(1_000_000 / mired)
    return None


def _rgb_attr(attrs: Mapping[str, Any]) -> str | None:
    value = attrs.get("rgb_color")
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return _hex(value)
    hs = attrs.get("hs_color")
    if isinstance(hs, (list, tuple)) and len(hs) >= 2:
        return _hex(_hs_to_rgb(hs))
    return None


# --- Vergleich -------------------------------------------------------------------------------


def _color_rgb(state: Mapping[str, Any]) -> tuple[int, int, int] | None:
    if state.get("rgb"):
        return _rgb(state["rgb"])
    if state.get("kelvin") is not None:
        return kelvin_to_rgb(state["kelvin"])
    return None


def color_differs(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Δkelvin > 300 (beide weiß), sonst ΔRGB > 40 in einem Kanal. Ohne Farbe auf einer Seite: gleich."""
    ka, kb = a.get("kelvin"), b.get("kelvin")
    if ka is not None and kb is not None:
        return abs(ka - kb) > KELVIN_TOLERANCE
    ra, rb = _color_rgb(a), _color_rgb(b)
    if ra is None or rb is None:
        return False
    return any(abs(x - y) > RGB_TOLERANCE for x, y in zip(ra, rb))


def lamp_differs(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Merklich anders: an/aus, Helligkeit > 8 Prozentpunkte oder Farbe (siehe :func:`color_differs`)."""
    if bool(a.get("on")) != bool(b.get("on")):
        return True
    if not a.get("on"):
        return False
    ba, bb = a.get("brightness"), b.get("brightness")
    if ba is not None and bb is not None and abs(ba - bb) > BRIGHTNESS_TOLERANCE:
        return True
    return color_differs(a, b)


def compare_member(
    expected: Mapping[str, Any] | None,
    before: Mapping[str, Any] | None,
    measured: Mapping[str, Any] | None,
    others_reacted: bool,
) -> str:
    """``ok`` | ``missing`` | ``differs`` | ``unreachable`` für eine Lampe.

    - ``unreachable``: kein Messwert (``unavailable`` oder keine Antwort auf ``get``).
    - Mit Soll: passt → ``ok``; unverändert gegenüber vorher → ``missing``; sonst ``differs``.
    - Ohne Soll: unverändert, obwohl andere Lampen reagierten → ``missing``, sonst ``ok``.
    """
    if measured is None:
        return "unreachable"
    changed = before is None or lamp_differs(before, measured)
    if expected is not None:
        if not lamp_differs(expected, measured):
            return "ok"
        return "differs" if changed else "missing"
    return "missing" if not changed and others_reacted else "ok"


def compare_members(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Status je Zeile ``{expected, before, measured}`` (gleiche Reihenfolge)."""
    reacted = [
        r.get("measured") is not None and r.get("before") is not None and lamp_differs(r["before"], r["measured"])
        for r in rows
    ]
    return [
        compare_member(r.get("expected"), r.get("before"), r.get("measured"), any(reacted[:i] + reacted[i + 1 :]))
        for i, r in enumerate(rows)
    ]


def scene_status(statuses: Iterable[str], has_target: bool) -> str:
    """``unknown`` ohne Soll, ``differs`` bei ``missing``/``differs``, sonst ``ok``.

    ``unreachable`` zählt nicht: eine ausgeschaltete Lampe macht die Szene nicht falsch,
    und Abgleichen hilft dort nicht.
    """
    if not has_target:
        return "unknown"
    return "differs" if any(s in ("missing", "differs") for s in statuses) else "ok"


def measured_look(states: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Look aus den gemessenen Lampen: ``{colors (1–3), brightness 0–100, kelvin|None}``.

    Farben nach Häufigkeit (ähnliche zusammengefasst), Helligkeit als Mittel der eingeschalteten
    Lampen (ohne Helligkeit = 100), ``kelvin`` nur, wenn alle farbigen Lampen weiß sind.
    Alles aus: schwarz mit Helligkeit 0.
    """
    lit = [s for s in states if s is not None and s.get("on")]
    if not lit:
        return {"colors": ["#000000"], "brightness": 0, "kelvin": None}
    groups: list[list[Any]] = []  # [rgb, Anzahl]
    for s in lit:
        rgb = _color_rgb(s) or kelvin_to_rgb(_DIMMABLE_KELVIN)
        for group in groups:
            if all(abs(x - y) <= RGB_TOLERANCE for x, y in zip(group[0], rgb)):
                group[1] += 1
                break
        else:
            groups.append([rgb, 1])
    groups.sort(key=lambda g: -g[1])  # stabil: bei Gleichstand die Reihenfolge der Lampen
    brightness = round(sum(100 if s.get("brightness") is None else s["brightness"] for s in lit) / len(lit))
    kelvins = [s["kelvin"] for s in lit if s.get("kelvin") is not None]
    any_rgb = any(s.get("rgb") for s in lit)
    kelvin = None if any_rgb or not kelvins else max(1000, min(10000, round(sum(kelvins) / len(kelvins))))
    return {"colors": [_hex(g[0]) for g in groups[:3]], "brightness": max(0, min(100, brightness)), "kelvin": kelvin}


def build_check(
    group_id: int,
    scene_id: int,
    name: str | None,
    rows: Sequence[Mapping[str, Any]],
    has_target: bool,
    checked_at: str,
) -> dict[str, Any]:
    """Ergebnis von ``scene_check`` aus ``rows`` (``entity_id, name, expected, before, measured``)."""
    statuses = compare_members(rows)
    return {
        "group_id": group_id,
        "scene_id": scene_id,
        "name": name,
        "has_target": has_target,
        "status": scene_status(statuses, has_target),
        "members": [
            {
                "entity_id": r["entity_id"],
                "name": r.get("name"),
                "expected": r.get("expected"),
                "measured": r.get("measured"),
                "status": status,
            }
            for r, status in zip(rows, statuses)
        ],
        "measured_look": measured_look(r.get("measured") for r in rows),
        "checked_at": checked_at,
    }


# --- Soll ------------------------------------------------------------------------------------


def build_target(
    states: Mapping[str, Mapping[str, Any] | None], ieees: Iterable[str], source: str, stored_at: str
) -> dict[str, Any]:
    """Soll aus ``entity_id -> LampState|None`` (Lampen ohne Zustand fehlen im Soll)."""
    return {
        "members": {eid: dict(s) for eid, s in sorted(states.items()) if s is not None},
        "ieees": sorted(set(ieees)),
        "stored_at": stored_at,
        "source": source,
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sync_status(target: Mapping[str, Any] | None, check: Mapping[str, Any] | None, light_ieees: Iterable[str]) -> str:
    """``unknown`` (kein Soll), ``stale`` (Lampen der Gruppe seit dem Soll geändert),
    ``differs`` (letzte Messung nach dem Soll wich ab) oder ``ok``.

    Messungen vor dem Soll zählen nicht (nach Speichern, Übernehmen oder Abgleichen gilt das neue Soll).
    """
    if not target:
        return "unknown"
    ieees = target.get("ieees")
    if isinstance(ieees, list) and set(ieees) != set(light_ieees):
        return "stale"
    if check and check.get("status") == "differs":
        checked, stored = _parse_time(check.get("checked_at")), _parse_time(target.get("stored_at"))
        if checked is None or stored is None or checked >= stored:
            return "differs"
    return "ok"


def attach_sync(
    summary: list[dict[str, Any]], targets: Mapping[str, Any], checks: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Ergänzt ``sync`` und ``checked_at`` an jeder Szene von ``groups_summary``."""
    out = []
    for group in summary:
        gid = group.get("id")
        lights = {m.get("ieee") for m in group.get("members", []) if m.get("device_type") == "light"}
        scenes = []
        for s in group.get("scenes", []):
            key = f"{gid}:{s.get('id')}"
            target = targets.get(key) if isinstance(gid, int) else None
            check = checks.get(key) if isinstance(gid, int) else None
            scenes.append({**s, "sync": sync_status(target, check, lights), "checked_at": (check or {}).get("checked_at")})
        out.append({**group, "scenes": scenes})
    return out


# --- Lampen ansteuern -----------------------------------------------------------------------


def target_call(state: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """``light``-Dienst und Daten, um eine Lampe auf ``LampState`` zu setzen."""
    if not state.get("on"):
        return "turn_off", {}
    data: dict[str, Any] = {}
    if state.get("brightness") is not None:
        data["brightness_pct"] = max(1, min(100, int(state["brightness"])))
    if state.get("kelvin") is not None:
        data["color_temp_kelvin"] = int(state["kelvin"])
    elif state.get("rgb"):
        data["rgb_color"] = list(_rgb(state["rgb"]))
    return "turn_on", data


_RESTORE_COLOR = {
    "hs": "hs_color",
    "xy": "xy_color",
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
}


def restore_call(state: str | None, attributes: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """Dienst, der den gemerkten HA-Zustand zurückbringt (genaue Werte statt ``LampState``).

    ``None`` bei ``unavailable``/``unknown``: da gibt es nichts wiederherzustellen.
    """
    if state not in ("on", "off"):
        return None
    if state == "off":
        return "turn_off", {}
    attrs = attributes or {}
    data: dict[str, Any] = {}
    if isinstance(attrs.get("brightness"), (int, float)):
        data["brightness"] = int(attrs["brightness"])
    mode = attrs.get("color_mode")
    if mode == "color_temp" and (kelvin := _kelvin_attr(attrs)) is not None:
        data["color_temp_kelvin"] = kelvin
    elif (key := _RESTORE_COLOR.get(str(mode))) and isinstance(attrs.get(key), (list, tuple)):
        data[key] = list(attrs[key])
    return "turn_on", data


def get_payload(capabilities: Iterable[str]) -> dict[str, str]:
    """``<friendly_name>/get``: nur was die Lampe kann (``state`` immer)."""
    caps = set(capabilities)
    payload = {"state": ""}
    if "brightness" in caps:
        payload["brightness"] = ""
    if caps & {"color_xy", "color_hs"}:
        payload["color"] = ""
    if "color_temp" in caps:
        payload["color_temp"] = ""
    return payload


def map_members(light_ieees: Iterable[str], ha_lights: Mapping[str, Sequence[str]]) -> list[tuple[str, str]]:
    """``(ieee, entity_id)`` je Licht-Mitglied mit HA-Licht, sortiert nach ``entity_id``.

    Hat ein Gerät mehrere ``light.*`` (mehrere Endpunkte), zählt die erste nach Namen.
    Mitglieder ohne HA-Licht fehlen (sie lassen sich weder messen noch setzen).
    """
    out = []
    for ieee in set(light_ieees):
        lights = sorted(e for e in ha_lights.get(ieee, ()) if e.startswith("light."))
        if lights:
            out.append((ieee, lights[0]))
    return sorted(out, key=lambda m: m[1])

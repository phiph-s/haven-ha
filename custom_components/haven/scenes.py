"""Szenen der Z2M-Gruppen anlegen, überschreiben, umbenennen, löschen (ohne Home Assistant).

Z2M speichert Szenen auf den Geräten einer Gruppe und führt sie in ``bridge/groups``
als ``scenes: [{id, name}]``. Befehle gehen an ``<base>/<gruppe>/set``:
``scene_store`` (aktueller Zustand der Mitglieder), ``scene_rename``, ``scene_remove``,
``scene_recall``. Haven vergibt die IDs selbst, die App sieht nur Namen.

Dazu merkt sich Haven je Szene ein ``look`` (Farben, Helligkeit, Farbtemperatur zum
Zeitpunkt des Speicherns), damit die Szenenkarten echte Farben zeigen. Schlüssel
``"<group_id>:<scene_id>"``.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .commands import Z2MError

MAX_SCENE_ID = 255  # ZCL: Szenen-ID ist ein uint8
MAX_NAME_LENGTH = 40
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise Z2MError("invalid_format", "Der Name darf nicht leer sein")
    if len(text) > MAX_NAME_LENGTH:
        raise Z2MError("invalid_format", f"Der Name darf höchstens {MAX_NAME_LENGTH} Zeichen haben")
    return text


def find_group(groups: list[dict[str, Any]], group_id: int) -> dict[str, Any]:
    for group in groups:
        if group.get("id") == group_id:
            return group
    raise Z2MError("not_found", f"Unbekannte Zigbee-Gruppe {group_id}")


def group_scenes(group: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": s.get("id"), "name": s.get("name")}
        for s in group.get("scenes", [])
        if isinstance(s, dict) and isinstance(s.get("id"), int)
    ]


def _scene(group: dict[str, Any], scene_id: int) -> dict[str, Any]:
    for s in group_scenes(group):
        if s["id"] == scene_id:
            return s
    raise Z2MError("not_found", f"Die Szene {scene_id} gibt es in dieser Gruppe nicht")


def _name_owner(group: dict[str, Any], name: str) -> dict[str, Any] | None:
    key = name.casefold()
    for s in group_scenes(group):
        if str(s.get("name") or "").strip().casefold() == key:
            return s
    return None


def next_free_id(scenes: list[dict[str, Any]]) -> int:
    used = {s["id"] for s in scenes}
    for candidate in range(MAX_SCENE_ID + 1):
        if candidate not in used:
            return candidate
    raise Z2MError("full", "Diese Gruppe hat keinen Platz für weitere Szenen")


def plan_store(group: dict[str, Any], name: Any = None, scene_id: int | None = None) -> tuple[int, str, dict[str, Any]]:
    """Neue Szene (ohne ``scene_id``) oder Überschreiben einer vorhandenen.

    Neu: der Name muss frei sein (``name_taken`` mit der ID der vorhandenen Szene, damit die
    App das Überschreiben anbieten kann). Überschreiben: ohne Namen bleibt der alte.
    Liefert ``(scene_id, name, payload)``.
    """
    if scene_id is None:
        clean = normalize_name(name)
        if (owner := _name_owner(group, clean)) is not None:
            raise Z2MError("name_taken", f"Die Szene '{owner['name']}' gibt es schon")
        scene_id = next_free_id(group_scenes(group))
    else:
        existing = _scene(group, scene_id)
        clean = normalize_name(name if name is not None else existing.get("name") or f"Szene {scene_id}")
        owner = _name_owner(group, clean)
        if owner is not None and owner["id"] != scene_id:
            raise Z2MError("name_taken", f"Die Szene '{owner['name']}' gibt es schon")
    return scene_id, clean, {"scene_store": {"ID": scene_id, "name": clean}}


def plan_rename(group: dict[str, Any], scene_id: int, name: Any) -> tuple[str, dict[str, Any]]:
    _scene(group, scene_id)
    clean = normalize_name(name)
    owner = _name_owner(group, clean)
    if owner is not None and owner["id"] != scene_id:
        raise Z2MError("name_taken", f"Die Szene '{owner['name']}' gibt es schon")
    return clean, {"scene_rename": {"ID": scene_id, "name": clean}}


def plan_remove(group: dict[str, Any], scene_id: int) -> dict[str, Any]:
    _scene(group, scene_id)
    return {"scene_remove": scene_id}


def plan_recall(group: dict[str, Any], scene_id: int) -> dict[str, Any]:
    _scene(group, scene_id)
    return {"scene_recall": scene_id}


# --- Bestätigung über bridge/groups -------------------------------------------------


def stored(group_id: int, scene_id: int, name: str) -> Callable[[list[dict[str, Any]]], bool]:
    """Prüft, ob ``bridge/groups`` die Szene mit diesem Namen führt (nach Speichern/Umbenennen)."""

    def check(groups: list[dict[str, Any]]) -> bool:
        try:
            return _scene(find_group(groups, group_id), scene_id).get("name") == name
        except Z2MError:
            return False

    return check


def removed(group_id: int, scene_id: int) -> Callable[[list[dict[str, Any]]], bool]:
    def check(groups: list[dict[str, Any]]) -> bool:
        try:
            group = find_group(groups, group_id)
        except Z2MError:
            return True
        return all(s["id"] != scene_id for s in group_scenes(group))

    return check


# --- Looks ---------------------------------------------------------------------------


def scene_key(group_id: int, scene_id: int) -> str:
    return f"{group_id}:{scene_id}"


def validate_look(look: Any) -> dict[str, Any]:
    """``{colors: ["#rrggbb", ...] (1–3), brightness: 0–100, kelvin?: int|None}``."""
    if not isinstance(look, dict):
        raise Z2MError("invalid_format", "look muss ein Objekt sein")
    colors = look.get("colors")
    if not isinstance(colors, list) or not 1 <= len(colors) <= 3 or not all(
        isinstance(c, str) and _HEX.match(c) for c in colors
    ):
        raise Z2MError("invalid_format", "look.colors muss 1 bis 3 Farben wie #ffaa00 enthalten")
    brightness = look.get("brightness")
    if not isinstance(brightness, int) or isinstance(brightness, bool) or not 0 <= brightness <= 100:
        raise Z2MError("invalid_format", "look.brightness muss zwischen 0 und 100 liegen")
    kelvin = look.get("kelvin")
    if kelvin is not None and (not isinstance(kelvin, int) or isinstance(kelvin, bool) or not 1000 <= kelvin <= 10000):
        raise Z2MError("invalid_format", "look.kelvin muss zwischen 1000 und 10000 liegen")
    return {"colors": [c.lower() for c in colors], "brightness": brightness, "kelvin": kelvin}


def prune_looks(looks: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Entfernt Looks von Szenen, die es nicht mehr gibt (auch in Z2M gelöscht)."""
    present = {
        scene_key(group["id"], s["id"])
        for group in groups
        if isinstance(group.get("id"), int)
        for s in group_scenes(group)
    }
    return {key: look for key, look in looks.items() if key in present}


def attach_looks(summary: list[dict[str, Any]], looks: dict[str, Any]) -> list[dict[str, Any]]:
    """Ergänzt ``look`` (oder ``None``) an jeder Szene von ``groups_summary``."""
    out = []
    for group in summary:
        gid = group.get("id")
        scenes = []
        for s in group.get("scenes", []):
            look = looks.get(scene_key(gid, s.get("id"))) if isinstance(gid, int) else None
            scenes.append(
                {**s, "look": None if look is None else {k: look.get(k) for k in ("colors", "brightness", "kelvin", "stored_at")}}
            )
        out.append({**group, "scenes": scenes})
    return out

"""Grobe Validierung der App-Dokumente (Grundriss, Widgets, Prefs), ohne Home Assistant."""

from __future__ import annotations

from typing import Any

from .commands import Z2MError

MAX_DOC_ITEMS = 2000


def validate_floorplan(plan: Any) -> list[str]:
    """Fehlerliste (leer = brauchbar). Geometrie prüft die App selbst."""
    if not isinstance(plan, dict):
        return ["Der Grundriss muss ein JSON-Objekt sein"]
    errors: list[str] = []
    if "version" not in plan:
        errors.append("version fehlt")
    elif not isinstance(plan["version"], int) or isinstance(plan["version"], bool):
        errors.append("version muss eine Zahl sein")
    for key, id_key in (("rooms", "id"), ("placements", "entityId")):
        items = plan.get(key)
        if items is None:
            errors.append(f"{key} fehlt")
        elif not isinstance(items, list):
            errors.append(f"{key} muss eine Liste sein")
        elif len(items) > MAX_DOC_ITEMS:
            errors.append(f"{key} ist zu groß")
        elif not all(isinstance(i, dict) and isinstance(i.get(id_key), str) for i in items):
            errors.append(f"{key}: jeder Eintrag braucht ein Feld {id_key}")
    return errors


def validate_widgets(widgets: Any) -> list[str]:
    """Liste von ``{id, type, entities, options}`` wie ``HavenWidgetConfig.toJson``."""
    if not isinstance(widgets, list):
        return ["widgets muss eine Liste sein"]
    if len(widgets) > MAX_DOC_ITEMS:
        return ["widgets ist zu groß"]
    errors: list[str] = []
    ids: set[str] = set()
    for w in widgets:
        if not isinstance(w, dict) or not isinstance(w.get("id"), str) or not isinstance(w.get("type"), str):
            errors.append("Jedes Widget braucht id und type als Text")
            continue
        if w["id"] in ids:
            errors.append(f"Widget-ID doppelt: {w['id']}")
        ids.add(w["id"])
        if not isinstance(w.get("entities", []), list):
            errors.append(f"{w['id']}: entities muss eine Liste sein")
        if not isinstance(w.get("options", {}), dict):
            errors.append(f"{w['id']}: options muss ein Objekt sein")
    return errors


def validate_prefs(prefs: Any) -> list[str]:
    """Objekt; ``excluded_lights`` optional, Liste von ``light.*``-IDs. Unbekannte Schlüssel sind erlaubt."""
    if not isinstance(prefs, dict):
        return ["prefs muss ein JSON-Objekt sein"]
    excluded = prefs.get("excluded_lights", [])
    if not isinstance(excluded, list):
        return ["excluded_lights muss eine Liste sein"]
    if len(excluded) > MAX_DOC_ITEMS:
        return ["excluded_lights ist zu groß"]
    errors: list[str] = []
    for item in excluded:
        if not isinstance(item, str):
            errors.append("excluded_lights: jeder Eintrag muss ein Text sein")
        elif not item.startswith("light.") or item == "light.":
            errors.append(f"excluded_lights: {item} ist keine Licht-Entity")
    return errors


def normalize_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    """Gültige Prefs mit ``excluded_lights`` (Duplikate entfernt, Reihenfolge bleibt)."""
    excluded = prefs.get("excluded_lights")
    items = [i for i in excluded if isinstance(i, str)] if isinstance(excluded, list) else []
    return {**prefs, "excluded_lights": list(dict.fromkeys(items))}


def require_valid(errors: list[str], what: str) -> None:
    if errors:
        raise Z2MError("invalid_format", f"{what} ist ungültig: " + "; ".join(errors[:3]))

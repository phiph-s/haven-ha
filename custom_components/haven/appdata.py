"""Grobe Validierung der App-Dokumente (Grundriss, Widgets, Prefs), ohne Home Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
    # Möbel (ab App 0.6) sind optional und nur Deko: Liste von Objekten mit id.
    if "furniture" in plan:
        items = plan["furniture"]
        if not isinstance(items, list):
            errors.append("furniture muss eine Liste sein")
        elif len(items) > MAX_DOC_ITEMS:
            errors.append("furniture ist zu groß")
        elif not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in items):
            errors.append("furniture: jeder Eintrag braucht ein Feld id")
    return errors


def merge_plan_furniture(plan: dict[str, Any], previous: Any = None) -> dict[str, Any]:
    """Möbel erhalten, wenn ein älterer Client speichert.

    Apps bis 0.5 kennen ``furniture`` nicht und schicken das Feld nicht mit. Fehlt der
    Schlüssel, übernimmt Haven die Möbel aus dem gespeicherten Stand ``previous``
    (Speicherformat ``{"plan": ..., "updated_at": ...}``). Neuere Apps senden das Feld
    immer, auch leer: Dann gilt es, ``[]`` löscht also alle Möbel.
    """
    if not isinstance(plan, dict) or "furniture" in plan:
        return plan
    old = read_plan_doc(previous)["plan"]
    furniture = old.get("furniture") if isinstance(old, dict) else None
    if isinstance(furniture, list) and furniture:
        return {**plan, "furniture": furniture}
    return plan


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
    """Objekt; ``excluded_lights`` optional, Liste von ``light.*``-IDs; ``locked`` optional, Liste von Entity-IDs
    und IEEE-Adressen gesperrter Geräte („immer an“). Unbekannte Schlüssel sind erlaubt."""
    if not isinstance(prefs, dict):
        return ["prefs muss ein JSON-Objekt sein"]
    weather = prefs.get("weather")
    if weather is not None and (
        not isinstance(weather, dict)
        or any(k not in ("entity", "temperature", "snow_depth") for k in weather)
        or not all(isinstance(v, str) and "." in v for v in weather.values())
    ):
        return ["weather: entity, temperature und snow_depth müssen Entity-IDs sein"]
    locked = prefs.get("locked", [])
    if not isinstance(locked, list) or len(locked) > MAX_DOC_ITEMS:
        return ["locked muss eine Liste sein"]
    if not all(isinstance(i, str) and i for i in locked):
        return ["locked: jeder Eintrag muss ein Text sein"]
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
    locked = prefs.get("locked")
    locks = [i for i in locked if isinstance(i, str) and i] if isinstance(locked, list) else []
    return {**prefs, "excluded_lights": list(dict.fromkeys(items)), "locked": list(dict.fromkeys(locks))}


def read_prefs_doc(data: Any) -> dict[str, Any]:
    """Antwort von ``haven/prefs/get``: ``{"prefs": {...}, "revision": n}``. Nie gespeichert:
    ``excluded_lights`` und ``locked`` leer, Revision 0."""
    prefs = data.get("prefs") if isinstance(data, dict) else None
    return {"prefs": normalize_prefs(prefs if isinstance(prefs, dict) else {}), "revision": read_revision(data)}


# Z2M-Werte, die ein Gerät schalten (bei gesperrten Geräten abgelehnt). Einstellungen bleiben erlaubt.
_CONTROL_KEYS = ("state", "brightness", "color", "color_temp", "effect")


def controls(values: dict[str, Any]) -> bool:
    """Schaltet ``values`` das Gerät (Zustand, Helligkeit, Farbe), statt nur etwas einzustellen?"""
    return any(k == c or k.startswith(c + "_") for k in values for c in _CONTROL_KEYS)


def is_locked(prefs: dict[str, Any], ieee: str | None = None, entities: list[str] | None = None) -> bool:
    """Gerät gesperrt (IEEE oder eine seiner Entities in ``locked``)?"""
    locked = set(prefs.get("locked") or [])
    return (ieee is not None and ieee in locked) or any(e in locked for e in entities or [])


def validate_updated_at(value: Any) -> list[str]:
    """``updated_at`` ist optional und wird nur durchgereicht: Text, Zahl (ms) oder ``None``."""
    if value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool)):
        return []
    return ["updated_at muss ein Text oder eine Zahl sein"]


def validate_layout(layout: Any) -> list[str]:
    """``layout`` ist optional und beliebig, nur ein Objekt (oder ``None``)."""
    if layout is None or isinstance(layout, dict):
        return []
    return ["layout muss ein Objekt sein"]


def resolve_updated_at(explicit: Any, doc: Any = None) -> Any:
    """Explizites ``updated_at`` gewinnt, sonst ``updatedAt`` aus dem Dokument, sonst ``None``."""
    if explicit is not None:
        return explicit
    if isinstance(doc, dict):
        value = doc.get("updatedAt")
        if value is not None and validate_updated_at(value) == []:
            return value
    return None


def build_plan_doc(plan: dict[str, Any], updated_at: Any = None, *, previous: Any = None) -> dict[str, Any]:
    """Speicherformat ``haven.floorplan``. Das Dokument ``plan`` bleibt unverändert,
    nur fehlende Möbel kommen aus ``previous`` dazu (siehe :func:`merge_plan_furniture`)."""
    plan = merge_plan_furniture(plan, previous)
    return {"plan": plan, "updated_at": resolve_updated_at(updated_at, plan)}


def read_plan_doc(data: Any) -> dict[str, Any]:
    """Antwort von ``haven/floorplan/get``: ``{"plan": ..., "updated_at": ...}``."""
    data = data if isinstance(data, dict) else {}
    plan = data.get("plan")
    updated_at = data.get("updated_at")
    if updated_at is None or validate_updated_at(updated_at):
        updated_at = resolve_updated_at(None, plan)
    return {
        "plan": plan if isinstance(plan, dict) else None,
        "updated_at": updated_at,
        "revision": read_revision(data),
    }


def build_widgets_doc(
    widgets: list[dict[str, Any]],
    layout: Any = None,
    updated_at: Any = None,
    *,
    previous: Any = None,
    layout_given: bool = False,
) -> dict[str, Any]:
    """Speicherformat ``haven.widgets``.

    Fehlt ``layout`` im Befehl (``layout_given=False``), bleibt das bisherige Layout aus
    ``previous`` erhalten, damit alte Clients es nicht löschen. ``layout: null`` löscht es.
    """
    if not layout_given:
        layout = read_widgets_doc(previous)["layout"]
    return {"widgets": widgets, "layout": layout, "updated_at": updated_at}


def read_widgets_doc(data: Any) -> dict[str, Any]:
    """Antwort von ``haven/widgets/get``: ``{"widgets": [...]|None, "layout": {...}|None, "updated_at": ...|None}``."""
    data = data if isinstance(data, dict) else {}
    widgets = data.get("widgets")
    layout = data.get("layout")
    updated_at = data.get("updated_at")
    return {
        "widgets": widgets if isinstance(widgets, list) else None,
        "layout": layout if isinstance(layout, dict) else None,
        "updated_at": None if validate_updated_at(updated_at) else updated_at,
        "revision": read_revision(data),
    }


def build_event_data(kind: str, updated_at: Any = None, revision: int | None = None) -> dict[str, Any]:
    """Payload von ``haven_data_updated``: ``kind`` und, falls bekannt, ``updated_at`` und ``revision``."""
    data: dict[str, Any] = {"kind": kind}
    if updated_at is not None:
        data["updated_at"] = updated_at
    if revision is not None:
        data["revision"] = revision
    return data


# --- Revisionen (Feature ``revisions``) -------------------------------------------
#
# Jedes App-Dokument (Grundriss, Widgets, Prefs) trägt eine Revision, die nur der Server
# vergibt: 0 = nie mit Revision gespeichert, danach +1 je ``set``. Ein Client schickt beim
# Speichern die Revision mit, auf der seine Fassung beruht (``base_revision``). Passt sie
# nicht mehr, hat inzwischen ein anderes Gerät gespeichert: Haven lehnt mit ``conflict``
# ab, der Client lädt neu und führt zusammen. Uhrzeiten der Geräte zählen dabei nicht.
# Ohne ``base_revision`` (ältere Apps) wird wie bisher ohne Prüfung gespeichert, die
# Revision steigt trotzdem, damit neuere Geräte die Änderung erkennen.


def read_revision(data: Any) -> int:
    """Revision eines gespeicherten Dokuments, 0 wenn es keine trägt."""
    value = data.get("revision") if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def validate_base_revision(value: Any) -> list[str]:
    """``base_revision`` ist optional: ``None`` oder eine ganze Zahl ab 0."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        return []
    return ["base_revision muss eine ganze Zahl ab 0 sein"]


def check_base_revision(current: int, base: Any, what: str = "Das Dokument") -> None:
    """Wirft ``conflict``, wenn ``base`` angegeben ist und nicht mehr der gespeicherte Stand ist."""
    if base is None or base == current:
        return
    raise Z2MError(
        "conflict",
        f"{what} wurde inzwischen auf einem anderen Gerät geändert "
        f"(Stand {current}, gesendet für {base}). Bitte neu laden.",
    )


class VersionedDoc:
    """Ein Store-Dokument mit Revision, Lesen und Schreiben ohne Home Assistant.

    ``load`` und ``save`` sind die Funktionen des HA-``Store``. Alle Schreibvorgänge laufen
    unter einer Sperre: Prüfen der ``base_revision``, Bauen und Speichern sind ein Schritt,
    zwei gleichzeitige ``set`` können sich nicht gegenseitig überschreiben.
    """

    def __init__(
        self,
        load: Callable[[], Awaitable[Any]],
        save: Callable[[dict[str, Any]], Awaitable[None]],
        what: str = "Das Dokument",
    ) -> None:
        self._load = load
        self._save = save
        self._what = what
        self._lock = asyncio.Lock()
        self._data: Any = None
        self._loaded = False

    async def async_load(self) -> Any:
        """Gespeicherter Inhalt (roh, ``None`` wenn nie gespeichert)."""
        if not self._loaded:
            self._data = await self._load()
            self._loaded = True
        return self._data

    async def async_update(
        self, build: Callable[[Any], dict[str, Any]], base_revision: Any = None
    ) -> dict[str, Any]:
        """Baut aus dem bisherigen Inhalt den neuen (``build(previous)``) und speichert ihn
        mit der nächsten Revision. Gibt das gespeicherte Dokument zurück."""
        async with self._lock:
            previous = await self.async_load()
            current = read_revision(previous)
            check_base_revision(current, base_revision, self._what)
            doc = {**build(previous), "revision": current + 1}
            await self._save(doc)
            self._data = doc
            return doc


def normalize_networkmap_cache(data: Any) -> dict[str, Any] | None:
    """Gespeicherten Cache prüfen: ``{nodes, links, timestamp}`` oder ``None`` bei Unbrauchbarem."""
    if not isinstance(data, dict):
        return None
    nodes, links, timestamp = data.get("nodes"), data.get("links"), data.get("timestamp")
    if not isinstance(nodes, list) or not isinstance(links, list) or not isinstance(timestamp, str):
        return None
    return {"nodes": nodes, "links": links, "timestamp": timestamp}


def serialize_networkmap_cache(networkmap: Any) -> dict[str, Any]:
    """Speicherformat ``haven.networkmap``: ``{"networkmap": {...}|None}``."""
    return {"networkmap": normalize_networkmap_cache(networkmap)}


def deserialize_networkmap_cache(data: Any) -> dict[str, Any] | None:
    """Gegenstück zu :func:`serialize_networkmap_cache`."""
    return normalize_networkmap_cache(data.get("networkmap")) if isinstance(data, dict) else None


def require_valid(errors: list[str], what: str) -> None:
    if errors:
        raise Z2MError("invalid_format", f"{what} ist ungültig: " + "; ".join(errors[:3]))

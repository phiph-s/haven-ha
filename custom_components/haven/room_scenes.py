"""Szenen über Zigbee hinaus (Feature ``room_scenes``, ohne Home Assistant).

Eine Haven-Szene hat zwei Teile:

* den Zigbee-Teil: eine Z2M-Szene der Raumgruppe (siehe ``scenes``), falls der Raum
  eine Gruppe hat;
* Zusatzgeräte: gespeicherte Zustände anderer Geräte des Raums (Lampen außerhalb der
  Gruppe, auf Wunsch Steckdosen und Schalter). Haven setzt sie beim Aufrufen über HA.

Räume ohne Gruppe haben nur Zusatzgeräte. Ihre Szenen führt Haven selbst
(``room_scenes``: ``{area_id: [{id, name, look, devices, stored_at}]}``), die
Zusatzgeräte der Gruppenszenen liegen unter ``extras``:
``{"<group_id>:<scene_id>": [device, ...]}``.

Ein Gerät: ``{"entity_id", "on"}``, bei Lampen, die an sind, dazu ``brightness``
(1 bis 100 %) und ``kelvin`` oder ``rgb``. Dasselbe Format wie die Drehregler-Aktion
``lights``.
"""

from __future__ import annotations

from typing import Any

from .commands import Z2MError
from .scenes import MAX_SCENE_ID, normalize_name

# Höchstens so viele Zusatzgeräte je Szene und Szenen je Raum ohne Gruppe
MAX_SCENE_DEVICES = 50
MAX_ROOM_SCENES = 64
# Diese Bereiche kann eine Szene schalten
DEVICE_DOMAINS = ("light", "switch")


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def capture_device(entity_id: str, state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Aktueller Zustand als Szenen-Eintrag. ``None``, wenn unbekannt oder nicht schaltbar.

    ``state``: ``{"state": "on", "attributes": {...}}`` wie in ``hass.states``.
    """
    if _domain(entity_id) not in DEVICE_DOMAINS or not state or state.get("state") not in ("on", "off"):
        return None
    on = state["state"] == "on"
    entry: dict[str, Any] = {"entity_id": entity_id, "on": on}
    if not on or _domain(entity_id) != "light":
        return entry
    a = state.get("attributes") or {}
    b = a.get("brightness")
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        entry["brightness"] = max(1, min(100, round(b / 255 * 100)))
    mode = a.get("color_mode")
    kelvin = a.get("color_temp_kelvin")
    rgb = a.get("rgb_color")
    if mode == "color_temp" and isinstance(kelvin, (int, float)):
        entry["kelvin"] = max(1000, min(10000, int(kelvin)))
    elif mode in ("hs", "xy", "rgb", "rgbw", "rgbww") and isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        entry["rgb"] = [max(0, min(255, int(c))) for c in rgb]
    return entry


def validate_device_ids(entity_ids: Any) -> list[str]:
    """Liste der Geräte, die in die Szene sollen (aus der App)."""
    if not isinstance(entity_ids, list):
        raise Z2MError("invalid_format", "devices muss eine Liste sein")
    if len(entity_ids) > MAX_SCENE_DEVICES:
        raise Z2MError("invalid_format", f"Höchstens {MAX_SCENE_DEVICES} Geräte je Szene")
    out: list[str] = []
    for e in entity_ids:
        if not isinstance(e, str) or _domain(e) not in DEVICE_DOMAINS or "." not in e:
            raise Z2MError("invalid_format", f"{e} kann keine Szene schalten")
        if e not in out:
            out.append(e)
    return out


def plan_devices(devices: list[dict[str, Any]], transition: float = 0.0, skip: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str, dict[str, Any]]]:
    """HA-Dienste, die die Zusatzgeräte in ihren Zustand bringen. ``skip``: gesperrte Geräte."""
    from .automations import plan_light_targets  # noqa: PLC0415 - automations importiert dieses Modul

    lights = [d for d in devices if isinstance(d, dict) and _domain(str(d.get("entity_id"))) == "light" and d.get("entity_id") not in skip]
    services: list[tuple[str, str, dict[str, Any]]] = []
    if lights:
        services += plan_light_targets(lights, transition).services
    switches = [d for d in devices if isinstance(d, dict) and _domain(str(d.get("entity_id"))) == "switch" and d.get("entity_id") not in skip]
    on = [d["entity_id"] for d in switches if d.get("on")]
    off = [d["entity_id"] for d in switches if not d.get("on")]
    if on:
        services.append(("switch", "turn_on", {"entity_id": on}))
    if off:
        services.append(("switch", "turn_off", {"entity_id": off}))
    return services


# --- Szenen von Räumen ohne Gruppe ----------------------------------------------------------


def room_scene_list(room_scenes: dict[str, Any], area_id: str) -> list[dict[str, Any]]:
    scenes = room_scenes.get(area_id)
    return [s for s in scenes if isinstance(s, dict) and isinstance(s.get("id"), int)] if isinstance(scenes, list) else []


def store_room_scene(
    scenes: list[dict[str, Any]], name: Any, scene_id: int | None, devices: list[dict[str, Any]], look: dict[str, Any] | None, now: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Neue Szene (ohne ``scene_id``, Name muss frei sein) oder überschreiben. Neue Liste und Szene."""
    if not devices:
        raise Z2MError("no_lights", "Wähl mindestens ein Gerät für die Szene")
    if scene_id is None:
        text = normalize_name(name)
        if any(s.get("name", "").casefold() == text.casefold() for s in scenes):
            raise Z2MError("name_taken", f"Es gibt schon eine Szene {text}")
        if len(scenes) >= MAX_ROOM_SCENES:
            raise Z2MError("invalid_format", f"Höchstens {MAX_ROOM_SCENES} Szenen je Raum")
        used = {s["id"] for s in scenes}
        new_id = next(i for i in range(MAX_SCENE_ID + 1) if i not in used)
        scene = {"id": new_id, "name": text, "look": look, "devices": devices, "stored_at": now}
        return [*scenes, scene], scene
    old = next((s for s in scenes if s["id"] == scene_id), None)
    if old is None:
        raise Z2MError("not_found", f"Unbekannte Szene {scene_id}")
    text = old["name"] if name is None else normalize_name(name)
    if any(s["id"] != scene_id and s.get("name", "").casefold() == text.casefold() for s in scenes):
        raise Z2MError("name_taken", f"Es gibt schon eine Szene {text}")
    scene = {**old, "name": text, "look": look, "devices": devices, "stored_at": now}
    return [scene if s["id"] == scene_id else s for s in scenes], scene


def rename_room_scene(scenes: list[dict[str, Any]], scene_id: int, name: Any) -> tuple[list[dict[str, Any]], str]:
    text = normalize_name(name)
    if not any(s["id"] == scene_id for s in scenes):
        raise Z2MError("not_found", f"Unbekannte Szene {scene_id}")
    if any(s["id"] != scene_id and s.get("name", "").casefold() == text.casefold() for s in scenes):
        raise Z2MError("name_taken", f"Es gibt schon eine Szene {text}")
    return [{**s, "name": text} if s["id"] == scene_id else s for s in scenes], text


def remove_room_scene(scenes: list[dict[str, Any]], scene_id: int) -> list[dict[str, Any]]:
    if not any(s["id"] == scene_id for s in scenes):
        raise Z2MError("not_found", f"Unbekannte Szene {scene_id}")
    return [s for s in scenes if s["id"] != scene_id]


def keep_devices(devices: list[dict[str, Any]], entity_ids: list[str]) -> list[dict[str, Any]]:
    """Nur diese Geräte behalten (Zusatzgeräte aus einer Szene nehmen)."""
    keep = set(entity_ids)
    return [d for d in devices if isinstance(d, dict) and d.get("entity_id") in keep]


def public_room_scene(scene: dict[str, Any]) -> dict[str, Any]:
    look = scene.get("look")
    return {
        "id": scene["id"],
        "name": scene.get("name", ""),
        "look": None if not isinstance(look, dict) else {k: look.get(k) for k in ("colors", "brightness", "kelvin")} | {"stored_at": scene.get("stored_at")},
        "devices": list(scene.get("devices") or []),
    }

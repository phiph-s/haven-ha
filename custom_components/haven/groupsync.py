"""Planung der Z2M-Gruppenänderungen bei einem Raumwechsel (ohne Home Assistant)."""

from __future__ import annotations

import re
from typing import Any

from .commands import build_group_member


def _member_ieee(member: Any) -> str | None:
    ref = member.get("ieee_address") if isinstance(member, dict) else member
    return str(ref) if ref else None


def _members(group: dict[str, Any]) -> dict[str, Any]:
    """ieee -> endpoint (oder None)."""
    out: dict[str, Any] = {}
    for member in group.get("members", []):
        ieee = _member_ieee(member)
        if ieee:
            out[ieee] = member.get("endpoint") if isinstance(member, dict) else None
    return out


def find_room_group(
    groups: list[dict[str, Any]], room_lights: set[str], light_ieees: set[str]
) -> dict[str, Any] | None:
    """Raumgruppe: größte Überschneidung der Lichter, bei Gleichstand wenigste Fremdlichter.

    Nur Lichter zählen (``light_ieees``), Knöpfe und Sensoren als Mitglieder nicht.
    """
    best: tuple[int, int, dict[str, Any]] | None = None
    for group in groups:
        lights = set(_members(group)) & light_ieees
        overlap = len(lights & room_lights)
        if not overlap:
            continue
        key = (overlap, -len(lights - room_lights))
        if best is None or key > best[:2]:
            best = (key[0], key[1], group)
    return best[2] if best else None


def plan_group_sync(
    ieee: str,
    is_light: bool,
    groups: list[dict[str, Any]],
    light_ieees: set[str],
    old_area_lights: set[str] | None,
    new_area_lights: set[str] | None,
) -> dict[str, Any]:
    """Welche Gruppen-Requests braucht ein Raumwechsel?

    ``old_area_lights``/``new_area_lights``: Zigbee-Lichter der Bereiche (IEEE),
    ``None`` wenn es keinen Bereich gibt. Das Gerät selbst wird herausgerechnet.
    Ergebnis: ``actions`` (Reihenfolge: erst entfernen, dann aufnehmen, jeweils
    ``{action, topic, payload, group, group_id}``), ``old_group``, ``new_group``
    und ``skipped`` (Grund, warum etwas nicht passiert, sonst ``None``).
    """
    plan: dict[str, Any] = {"actions": [], "old_group": None, "new_group": None, "skipped": None}
    if not is_light:
        plan["skipped"] = "not_a_light"
        return plan

    old_group = find_room_group(groups, (old_area_lights or set()) - {ieee}, light_ieees) if old_area_lights else None
    new_group = find_room_group(groups, (new_area_lights or set()) - {ieee}, light_ieees) if new_area_lights else None
    plan["old_group"] = old_group.get("friendly_name") if old_group else None
    plan["new_group"] = new_group.get("friendly_name") if new_group else None

    if old_group and new_group and old_group.get("id") == new_group.get("id"):
        plan["skipped"] = "same_group"
        return plan

    endpoint: Any = None
    if old_group:
        old_members = _members(old_group)
        if ieee in old_members:
            endpoint = old_members[ieee]
            plan["actions"].append(_action("remove", old_group, ieee, endpoint))
    if endpoint is None:
        for group in groups:
            if (ep := _members(group).get(ieee)) is not None:
                endpoint = ep
                break

    if new_group:
        if ieee not in _members(new_group):
            plan["actions"].append(_action("add", new_group, ieee, endpoint))
    else:
        plan["skipped"] = "no_new_area" if new_area_lights is None else "no_group_for_area"
    return plan


def _action(kind: str, group: dict[str, Any], ieee: str, endpoint: Any) -> dict[str, Any]:
    return {
        "action": kind,
        "topic": f"group/members/{kind}",
        "payload": build_group_member(str(group.get("friendly_name")), ieee, endpoint),
        "group": group.get("friendly_name"),
        "group_id": group.get("id"),
    }


# --- Gruppe für einen Raum anlegen ------------------------------------------------------

# Zeichen, die Zigbee2MQTT in Namen nicht mag (MQTT-Platzhalter, Pfade).
_BAD_NAME = re.compile(r"[#+/\\]")


def room_group_name(area_name: str, taken: set[str]) -> str:
    """Freier Name für die Gruppe eines Raums: der Raumname, bei Kollision
    mit einem Gerät oder einer Gruppe „<Raum> Gruppe“, dann mit Nummer."""
    base = _BAD_NAME.sub(" ", area_name).strip()
    base = re.sub(r"\s+", " ", base) or "Raum"
    lower = {t.casefold() for t in taken}
    for candidate in (base, f"{base} Gruppe", *(f"{base} Gruppe {i}" for i in range(2, 100))):
        if candidate.casefold() not in lower:
            return candidate
    raise ValueError("kein freier Name")


def plan_room_group(
    area_name: str,
    room_lights: set[str],
    groups: list[dict[str, Any]],
    taken_names: set[str],
) -> dict[str, Any]:
    """Anfragen, um für einen Raum ohne Gruppe eine Z2M-Gruppe mit seinen
    Zigbee-Lichtern anzulegen.

    Ergebnis ``{name, requests: [(topic, payload)]}`` oder ``{error, message}``:
    ``no_lights`` (keine Zigbee-Lichter im Raum), ``group_exists`` (eine Gruppe
    enthält schon Lichter des Raums, Haven nimmt dann diese).
    """
    if not room_lights:
        return {"error": "no_lights", "message": f"In {area_name} gibt es keine Zigbee-Lampen"}
    existing = find_room_group(groups, room_lights, room_lights)
    if existing is not None:
        return {
            "error": "group_exists",
            "message": f"{area_name} hat schon die Gruppe {existing.get('friendly_name')}",
        }
    name = room_group_name(area_name, taken_names)
    requests: list[tuple[str, dict[str, Any]]] = [("group/add", {"friendly_name": name})]
    requests += [("group/members/add", build_group_member(name, ieee)) for ieee in sorted(room_lights)]
    return {"name": name, "requests": requests}

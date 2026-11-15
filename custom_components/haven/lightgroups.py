"""Erkennung von Lichtgruppen (HA-Gruppen und Z2M-Gruppen), ohne Home Assistant.

Lichtgruppen sind nie Lampen. Erkannt wird an Registry-Plattform, Zustandsattributen
und Namen, eines davon reicht.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

GroupType = Literal["ha_group", "z2m_group"]


def _fold(value: Any) -> str | None:
    return str(value).casefold() if value else None


def classify_light(
    entity_id: str,
    platform: str | None,
    attributes: Mapping[str, Any] | None,
    device_name: str | None,
    z2m_group_names: Iterable[str],
    *,
    ieee: str | None = None,
) -> GroupType | None:
    """``ha_group``, ``z2m_group`` oder ``None`` (echte Lampe).

    - Registry-Plattform ``group`` oder Attribut ``entity_id`` als Liste: HA-Lichtgruppe.
    - Attribut ``group_entities`` (MQTT-Gruppe aus Z2M): Z2M-Gruppe.
    - Geräte- oder Anzeigename gleich einer Z2M-Gruppe (ohne Groß/klein): Z2M-Gruppe.
      Gilt nicht für Geräte mit IEEE, das sind echte Zigbee-Geräte.
    """
    if not entity_id.startswith("light."):
        return None
    attrs = attributes or {}
    if platform == "group" or isinstance(attrs.get("entity_id"), list):
        return "ha_group"
    if "group_entities" in attrs:
        return "z2m_group"
    if ieee is None:
        names = {n for n in (_fold(g) for g in z2m_group_names) if n}
        if any(_fold(c) in names for c in (device_name, attrs.get("friendly_name")) if c):
            return "z2m_group"
    return None


def group_members(attributes: Mapping[str, Any] | None) -> list[str]:
    """Mitglieder aus ``entity_id`` (HA-Gruppe) bzw. ``group_entities`` (Z2M), sortiert."""
    attrs = attributes or {}
    for key in ("entity_id", "group_entities"):
        value = attrs.get(key)
        if isinstance(value, list):
            return sorted({str(v) for v in value})
    return []


def z2m_group_id(names: Iterable[str | None], groups: Iterable[Mapping[str, Any]]) -> int | None:
    """ID der Z2M-Gruppe aus ``bridge/groups``, deren friendly_name zu einem der Namen passt."""
    wanted = {n for n in (_fold(x) for x in names) if n}
    for group in groups:
        if _fold(group.get("friendly_name")) in wanted and isinstance(group.get("id"), int):
            return group["id"]
    return None

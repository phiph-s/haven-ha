"""Zusammenstellung von ``haven/rooms`` aus vorab gesammelten Registry-Daten, ohne HA.

``websocket_api.build_rooms`` sammelt Bereiche, Geräte, Entities und Zustände aus
Home Assistant und ruft :func:`assemble_rooms` auf.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .lightgroups import classify_light, group_members, z2m_group_id

if TYPE_CHECKING:
    from .z2m import Z2MBridge

CLIMATE_CLASSES = ("temperature", "humidity")
MOTION_CLASSES = ("motion", "occupancy", "presence")


@dataclass(frozen=True)
class AreaData:
    area_id: str
    name: str
    floor_id: str | None = None
    icon: str | None = None


@dataclass(frozen=True)
class DeviceData:
    device_id: str
    name: str | None  # name_by_user or name
    raw_name: str | None = None  # Name ohne Nutzeränderung (für den Z2M-Gruppenabgleich)
    manufacturer: str | None = None
    model: str | None = None
    ieee: str | None = None
    area_id: str | None = None
    disabled: bool = False
    service: bool = False
    protocol: str | None = None  # Funkprotokoll außer Zigbee: ``matter``, ``zwave`` (Zigbee steckt in ``ieee``)
    sw_version: str | None = None


@dataclass(frozen=True)
class EntityData:
    """Registry-Eintrag (nicht deaktiviert) plus Zustandsattribute bei Lichtern."""

    entity_id: str
    device_id: str | None = None
    area_id: str | None = None  # eigener Bereich der Entity
    platform: str | None = None
    has_category: bool = False  # entity_category gesetzt (Diagnose/Konfig)
    hidden: bool = False
    device_class: str | None = None
    original_name: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


# Domänen der Geräte-Kennungen (``identifiers``) je Funkprotokoll.
_PROTOCOL_DOMAINS = {"matter": "matter", "zwave_js": "zwave"}


def protocol_of_identifiers(identifiers: Any) -> str | None:
    """Funkprotokoll außer Zigbee aus den Geräte-Kennungen (``matter``, ``zwave``), sonst ``None``."""
    for item in identifiers or ():
        if isinstance(item, (tuple, list)) and item and item[0] in _PROTOCOL_DOMAINS:
            return _PROTOCOL_DOMAINS[item[0]]
    return None


def _device_item(device: DeviceData) -> dict[str, Any]:
    return {
        "device_id": device.device_id,
        "name": device.name,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "ieee": device.ieee,
        **({"protocol": device.protocol} if device.protocol else {}),
        **({"sw_version": device.sw_version} if device.protocol and device.sw_version else {}),
        "entities": [],
    }


def _sorted_devices(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        if item["entities"]:
            item["entities"] = sorted(set(item["entities"]))
            out.append(item)
    return sorted(out, key=lambda d: ((d["name"] or "").casefold(), d["device_id"]))


def assemble_rooms(
    areas: Iterable[AreaData],
    devices: Mapping[str, DeviceData],
    entities: Iterable[EntityData],
    bridge: Z2MBridge,
) -> dict[str, Any]:
    """Räume, Lichtgruppen, Geräte und ``unassigned``.

    Bereich einer Entity: eigener Bereich vor Gerätebereich. ``entities`` darf
    Entities ohne Bereich enthalten (Z2M-Gruppen, Geräte ohne Bereich).
    """
    group_names = [str(g.get("friendly_name")) for g in bridge.groups if g.get("friendly_name")]

    rooms: dict[str, dict[str, Any]] = {}
    room_devices: dict[str, dict[str, dict[str, Any]]] = {}
    for area in areas:
        rooms[area.area_id] = {
            "area_id": area.area_id,
            "name": area.name,
            "floor_id": area.floor_id,
            "icon": area.icon,
            "lights": [],
            "light_groups": [],
            "zigbee_lights": [],
            "climate": {"temperature": [], "humidity": []},
            "motion": [],
            "group": None,
            "group_entity": None,
            "devices": [],
        }
        room_devices[area.area_id] = {}

    unassigned: dict[str, dict[str, Any]] = {}
    # Z2M-Gruppen-Entities nach Namen (friendly_name, Gerätename), ohne Groß/klein
    z2m_group_entities: dict[str, str] = {}
    seen: set[str] = set()

    for ent in entities:
        if ent.entity_id in seen:
            continue
        seen.add(ent.entity_id)
        device = devices.get(ent.device_id) if ent.device_id else None

        group_type = None
        if ent.domain == "light":
            name = (device.raw_name if device else None) or ent.original_name
            group_type = classify_light(
                ent.entity_id,
                ent.platform,
                ent.attributes,
                name,
                group_names,
                ieee=device.ieee if device else None,
            )
            if group_type == "z2m_group":
                for n in (ent.attributes.get("friendly_name"), name):
                    if n:
                        key = str(n).casefold()
                        if key not in z2m_group_entities or ent.entity_id < z2m_group_entities[key]:
                            z2m_group_entities[key] = ent.entity_id

        if ent.hidden:
            continue
        area_id = ent.area_id or (device.area_id if device else None)
        main = not ent.has_category

        if area_id is None:
            # Gerät ohne Bereich: nur Haupt-Entities, keine Diagnose/Konfig, keine Gruppen.
            if device is not None and main and group_type is None:
                if device.disabled or device.service:
                    continue
                unassigned.setdefault(device.device_id, _device_item(device))["entities"].append(ent.entity_id)
            continue

        room = rooms.get(area_id)
        if room is None or not main:
            continue

        if device is not None and not device.service and group_type is None:
            room_devices[area_id].setdefault(device.device_id, _device_item(device))["entities"].append(
                ent.entity_id
            )

        device_class = ent.device_class
        if ent.domain == "light":
            if group_type is not None:
                friendly = ent.attributes.get("friendly_name")
                room["light_groups"].append(
                    {
                        "entity_id": ent.entity_id,
                        "type": group_type,
                        "name": str(friendly) if friendly else None,
                        "members": group_members(ent.attributes),
                        "z2m_group_id": (
                            z2m_group_id((friendly, device.raw_name if device else None), bridge.groups)
                            if group_type == "z2m_group"
                            else None
                        ),
                    }
                )
                continue
            room["lights"].append(ent.entity_id)
            if device and device.ieee and device.ieee in bridge.devices:
                room["zigbee_lights"].append(device.ieee)
        elif ent.domain == "sensor" and device_class in CLIMATE_CLASSES:
            room["climate"][device_class].append(ent.entity_id)
        elif ent.domain == "binary_sensor" and device_class in MOTION_CLASSES:
            room["motion"].append(ent.entity_id)

    for area_id, room in rooms.items():
        room["group"] = bridge.match_group(set(room["zigbee_lights"]))
        if room["group"] and room["group"].get("friendly_name"):
            room["group_entity"] = z2m_group_entities.get(str(room["group"]["friendly_name"]).casefold())
        for key in ("lights", "zigbee_lights", "motion"):
            room[key] = sorted(set(room[key]))
        for key in CLIMATE_CLASSES:
            room["climate"][key] = sorted(set(room["climate"][key]))
        room["light_groups"].sort(key=lambda g: g["entity_id"])
        room["devices"] = _sorted_devices(room_devices[area_id].values())

    return {
        "rooms": sorted(rooms.values(), key=lambda r: r["name"].casefold()),
        "unassigned": _sorted_devices(unassigned.values()),
    }

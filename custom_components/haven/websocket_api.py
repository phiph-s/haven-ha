"""WebSocket-Befehle der Haven-Integration (lesen und schreiben)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .appdata import (
    build_event_data,
    controls,
    is_locked,
    normalize_prefs,
    require_valid,
    resolve_updated_at,
    validate_base_revision,
    validate_floorplan,
    validate_layout,
    validate_prefs,
    validate_updated_at,
    validate_widgets,
)
from .commands import (
    Z2MError,
    build_interview,
    build_permit_join,
    build_remove,
    build_rename,
    build_set,
    supports_identify,
)
from .const import DOMAIN, EVENT_DATA_UPDATED, FEATURES, STATE_MAX_PER_SECOND
from .groupsync import plan_group_sync, plan_room_group
from .scenes import (
    attach_looks,
    find_group,
    plan_recall,
    plan_remove,
    plan_rename,
    plan_store,
    prune_looks,
    removed,
    scene_key,
    stored,
    validate_look,
)
from .room_scenes import public_room_scene, room_scene_list
from .rooms import AreaData, DeviceData, EntityData, assemble_rooms, protocol_of_identifiers
from .scenesync import attach_sync
from .scenesync_runtime import SceneSync, group_lock
from .storage import HavenStorage, get_storage
from .throttle import Throttle

if TYPE_CHECKING:
    from . import HavenConfigEntry
    from .z2m import Z2MBridge

@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Registriert alle Befehle."""
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_rooms)
    websocket_api.async_register_command(hass, ws_zigbee_devices)
    websocket_api.async_register_command(hass, ws_zigbee_device)
    websocket_api.async_register_command(hass, ws_zigbee_groups)
    websocket_api.async_register_command(hass, ws_subscribe_zigbee)
    for command in (
        ws_zigbee_set,
        ws_zigbee_rename,
        ws_zigbee_set_area,
        ws_zigbee_create_room_group,
        ws_zigbee_identify,
        ws_zigbee_interview,
        ws_zigbee_remove,
        ws_zigbee_enable_last_seen,
        ws_zigbee_permit_join,
        ws_zigbee_state,
        ws_zigbee_networkmap,
        ws_floorplan_get,
        ws_floorplan_set,
        ws_widgets_get,
        ws_widgets_set,
        ws_prefs_get,
        ws_prefs_set,
        ws_zigbee_scene_store,
        ws_zigbee_scene_rename,
        ws_zigbee_scene_remove,
        ws_zigbee_scene_recall,
        ws_zigbee_scene_check,
        ws_zigbee_scene_adopt,
        ws_zigbee_scene_repair,
    ):
        websocket_api.async_register_command(hass, command)

    # Szenen je Raum mit Zusatzgeräten (haven/scenes/*)
    from .room_scenes_ws import async_register_room_scene_commands  # noqa: PLC0415

    async_register_room_scene_commands(hass)

    # Haven-Automationen (haven/automations/*), eigenes Modul
    from .automations_ws import async_register_automation_commands  # noqa: PLC0415

    async_register_automation_commands(hass)

    # Heizungsregelung je Raum (haven/heating/*)
    from .heating_ws import async_register_heating_commands  # noqa: PLC0415

    async_register_heating_commands(hass)

    # Bilder der Wohnung für Widgets und Displays (haven/render/*)
    from .render_ws import async_register_render_commands  # noqa: PLC0415

    async_register_render_commands(hass)


def _get_entry(hass: HomeAssistant) -> HavenConfigEntry | None:
    """Der (einzige) geladene Haven-Entry oder None."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is not None:
            return entry
    return None


def _bridge_or_error(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg_id: int
) -> Z2MBridge | None:
    entry = _get_entry(hass)
    if entry is None:
        connection.send_error(msg_id, websocket_api.ERR_NOT_FOUND, "Haven ist nicht eingerichtet oder nicht geladen")
        return None
    return entry.runtime_data.z2m


# --- haven/info ------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "haven/info"})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    entry = _get_entry(hass)
    result: dict[str, Any] = {
        "domain": DOMAIN,
        "version": str(integration.version) if integration.version else None,
        "api_version": 1,
        "features": list(FEATURES),
        "loaded": entry is not None,
        "z2m": entry.runtime_data.z2m.info_summary() if entry else None,
    }
    connection.send_result(msg["id"], result)


# --- haven/zigbee/* ----------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/devices"})
@websocket_api.async_response
async def ws_zigbee_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    connection.send_result(msg["id"], {"devices": _devices_with_ha(hass, bridge)})


@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/device", vol.Required("ieee"): str})
@websocket_api.async_response
async def ws_zigbee_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    detail = bridge.device_detail(msg["ieee"])
    if detail is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Unbekanntes Zigbee-Gerät {msg['ieee']}")
        return
    detail.update(_ha_info(hass).get(msg["ieee"], _NO_HA))
    connection.send_result(msg["id"], detail)


@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/groups"})
@websocket_api.async_response
async def ws_zigbee_groups(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    storage = get_storage(hass)
    await storage.async_get_scene_looks()  # lädt Looks, Soll und Messungen
    connection.send_result(msg["id"], {"groups": _groups_with_scenes(bridge, storage)})


def _groups_with_scenes(bridge: Z2MBridge, storage: HavenStorage) -> list[dict[str, Any]]:
    """``groups_summary`` mit ``look``, ``sync`` und ``checked_at`` an jeder Szene (aus dem Speicher)."""
    targets, checks = storage.scene_sync_cached()
    extras, _ = storage.scene_extras_cached()
    groups = attach_sync(attach_looks(bridge.groups_summary(), storage.scene_looks_cached()), targets, checks)
    # Zusatzgeräte je Szene (Feature room_scenes)
    return [
        {
            **g,
            "scenes": [
                {**s, "devices": list(extras.get(scene_key(g.get("id"), s.get("id"))) or [])} for s in g.get("scenes", [])
            ],
        }
        for g in groups
    ]


@websocket_api.websocket_command({vol.Required("type"): "haven/subscribe_zigbee"})
@callback
def ws_subscribe_zigbee(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Pusht Änderungen (devices, groups, info, state, event, availability) bis zum Abmelden.

    ``kind: "state"`` gibt es in zwei Formen: Bridge-Zustand (wie ``info``) und
    Gerätezustand ``{ieee, values}`` (höchstens 4/s je Gerät, Werte zusammengeführt).
    """
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    msg_id = msg["id"]
    throttle = Throttle(STATE_MAX_PER_SECOND)
    timers: dict[str, Any] = {}

    def _send(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, data))

    @callback
    def _flush(ieee: str) -> None:
        timers.pop(ieee, None)
        values = throttle.flush(ieee, hass.loop.time())
        if values is not None:
            _send({"kind": "state", "ieee": ieee, "values": values})
        elif (delay := throttle.delay(ieee, hass.loop.time())) is not None:
            timers[ieee] = hass.loop.call_later(delay, _flush, ieee)

    @callback
    def _forward(kind: str, payload: Any) -> None:
        if kind == "device_state":
            ieee = payload["ieee"]
            now = hass.loop.time()
            values = throttle.push(ieee, payload["values"], now)
            if values is not None:
                _send({"kind": "state", "ieee": ieee, "values": values})
            elif ieee not in timers and (delay := throttle.delay(ieee, now)) is not None:
                timers[ieee] = hass.loop.call_later(delay, _flush, ieee)
            return
        if kind == "devices":
            data: Any = {"devices": _devices_with_ha(hass, bridge)}
        elif kind == "groups":
            data = {"groups": _groups_with_scenes(bridge, get_storage(hass))}
        elif kind == "event":
            data = {"event": payload}
        elif kind == "availability":
            data = payload
        else:  # info, state
            data = bridge.info_summary()
        _send({"kind": kind, **data})

    remove_listener = bridge.add_listener(_forward)

    @callback
    def _unsub() -> None:
        remove_listener()
        for timer in timers.values():
            timer.cancel()
        timers.clear()
        throttle.clear()

    connection.subscriptions[msg_id] = _unsub
    connection.send_result(msg_id)


# --- haven/rooms -------------------------------------------------------------------


def _ieee_of_device(device: dr.DeviceEntry) -> str | None:
    """IEEE eines MQTT-Geräts aus Z2M (Kennung ``zigbee2mqtt_<ieee>``)."""
    for domain, ident in device.identifiers:
        if domain == "mqtt" and "0x" in ident:
            return "0x" + ident.rsplit("0x", 1)[1]
    for conn_type, value in device.connections:
        if conn_type == "zigbee":
            return value
    return None


def build_rooms(hass: HomeAssistant, bridge: Z2MBridge) -> dict[str, Any]:
    """Räume aus Area-, Device- und Entity-Registry plus Z2M-Gruppen.

    Ohne Iteration über ``dev_reg.devices`` / ``ent_reg.entities`` (ab HA 2027.9
    veraltet): Bereiche, dann Geräte und Entities je Bereich, Geräte ohne Bereich
    über alle Config-Entries, Lichter ohne Bereich über ``hass.states``.
    Die Zusammenstellung selbst ist HA-frei (``rooms.assemble_rooms``).
    """
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    devices: dict[str, DeviceData] = {}
    entities: dict[str, EntityData] = {}
    scanned: set[str] = set()  # Geräte, deren Entities schon gelesen sind

    def add_device(device: dr.DeviceEntry | None) -> DeviceData | None:
        if device is None:
            return None
        if device.id not in devices:
            devices[device.id] = DeviceData(
                device_id=device.id,
                name=device.name_by_user or device.name,
                raw_name=device.name,
                manufacturer=device.manufacturer,
                model=device.model,
                ieee=_ieee_of_device(device),
                area_id=device.area_id,
                disabled=device.disabled_by is not None,
                service=device.entry_type is dr.DeviceEntryType.SERVICE,
                protocol=protocol_of_identifiers(device.identifiers),
                sw_version=device.sw_version,
            )
        return devices[device.id]

    def add_entity(entry: er.RegistryEntry | None) -> None:
        if entry is None or entry.disabled_by is not None or entry.entity_id in entities:
            return
        if entry.device_id:
            add_device(dev_reg.async_get(entry.device_id))
        attributes: dict[str, Any] = {}
        if entry.domain == "light" and (state := hass.states.get(entry.entity_id)) is not None:
            attributes = dict(state.attributes)
        entities[entry.entity_id] = EntityData(
            entity_id=entry.entity_id,
            device_id=entry.device_id,
            area_id=entry.area_id,
            platform=entry.platform,
            has_category=entry.entity_category is not None,
            hidden=entry.hidden_by is not None,
            device_class=entry.device_class or entry.original_device_class,
            original_name=entry.original_name,
            attributes=attributes,
        )

    areas = area_reg.async_list_areas()
    for area in areas:
        # Eigener Bereich der Entity, auch wenn ihr Gerät woanders steht
        for entry in er.async_entries_for_area(ent_reg, area.id):
            add_entity(entry)
        # Entities der Geräte im Bereich (eigener Bereich entscheidet assemble_rooms)
        for device in dr.async_entries_for_area(dev_reg, area.id):
            add_device(device)
            scanned.add(device.id)
            for entry in er.async_entries_for_device(ent_reg, device.id):
                add_entity(entry)

    # Geräte ohne Bereich (für "unassigned")
    for config_entry in hass.config_entries.async_entries():
        for device in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id):
            if device.area_id is None and device.id not in scanned:
                add_device(device)
                scanned.add(device.id)
                for entry in er.async_entries_for_device(ent_reg, device.id):
                    add_entity(entry)

    # Lichter ohne Bereich (Z2M-Gruppen haben meist keinen), für "group_entity"
    for state in hass.states.async_all("light"):
        add_entity(ent_reg.async_get(state.entity_id))

    return assemble_rooms(
        [
            AreaData(area.id, area.name, getattr(area, "floor_id", None), area.icon)
            for area in areas
        ],
        devices,
        entities.values(),
        bridge,
    )


@websocket_api.websocket_command({vol.Required("type"): "haven/rooms"})
@websocket_api.async_response
async def ws_rooms(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    storage = get_storage(hass)
    await storage.async_get_scene_looks()  # Szenen-Speicher laden
    result = build_rooms(hass, bridge)
    _, room_scenes = storage.scene_extras_cached()
    for room in result["rooms"]:
        # Räume ohne Gruppe: Szenen, die Haven selbst führt (Feature room_scenes)
        room["scenes"] = (
            [] if room.get("group") else [public_room_scene(s) for s in room_scene_list(room_scenes, room["area_id"])]
        )
    connection.send_result(msg["id"], result)


# --- HA-Zuordnung (Bereich, Entities) ---------------------------------------------------

_NO_HA: dict[str, Any] = {"area_id": None, "area_name": None, "ha_entities": []}


def _ha_info(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """ieee -> ``{device_id, area_id, area_name, ha_entities}`` aus den Registries."""
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    out: dict[str, dict[str, Any]] = {}
    # Z2M-Geräte sind MQTT-Geräte: über die MQTT-Config-Entries statt dev_reg.devices
    mqtt_devices = {
        device.id: device
        for config_entry in hass.config_entries.async_entries("mqtt")
        for device in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    }
    for device in mqtt_devices.values():
        ieee = _ieee_of_device(device)
        if ieee is None:
            continue
        area = area_reg.async_get_area(device.area_id) if device.area_id else None
        out[ieee] = {
            "device_id": device.id,
            "area_id": device.area_id,
            "area_name": area.name if area else None,
            "ha_entities": sorted(e.entity_id for e in er.async_entries_for_device(ent_reg, device.id)),
        }
    return out


def _devices_with_ha(hass: HomeAssistant, bridge: Z2MBridge) -> list[dict[str, Any]]:
    ha = _ha_info(hass)
    devices = bridge.devices_summary()
    for device in devices:
        info = ha.get(device["ieee"], _NO_HA)
        device.update({k: info[k] for k in ("area_id", "area_name", "ha_entities")})
    return devices


def _area_lights(hass: HomeAssistant, bridge: Z2MBridge, area_id: str | None) -> set[str] | None:
    """Zigbee-Lichter (IEEE) mit HA-Gerätebereich ``area_id``; ``None`` ohne Bereich."""
    if area_id is None:
        return None
    return {
        ieee
        for ieee, info in _ha_info(hass).items()
        if info["area_id"] == area_id and bridge.device_type(ieee) == "light"
    }


# --- Schreibende Befehle -----------------------------------------------------------------


def _fail(connection: websocket_api.ActiveConnection, msg_id: int, err: Z2MError) -> None:
    connection.send_error(msg_id, err.code, err.message)


def _target(bridge: Z2MBridge, ieee: str) -> dict[str, Any]:
    return bridge.device_ref(ieee)


async def _write(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any], run: Any
) -> None:
    """Wie ``_z2m_write`` für Befehle anderer Module: ``run(bridge)`` liefert das Ergebnis."""
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    try:
        result = await run(bridge)
    except Z2MError as err:
        _fail(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


def _z2m_write(handler):
    """Gemeinsamer Rahmen: Bridge holen, Z2MError in ``send_error`` umsetzen."""

    async def wrapper(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
            return
        try:
            result = await handler(hass, bridge, msg)
        except Z2MError as err:
            _fail(connection, msg["id"], err)
            return
        connection.send_result(msg["id"], result)

    wrapper.__name__ = handler.__name__
    return wrapper


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "haven/zigbee/set", vol.Required("ieee"): str, vol.Required("values"): dict}
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_set(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    device = _target(bridge, msg["ieee"])
    if controls(msg["values"]) and is_locked(await get_storage(hass).async_get_prefs(), msg["ieee"]):
        raise Z2MError("locked", f"{device['friendly_name']} ist gesperrt und bleibt an")
    await bridge.async_publish_set(device["friendly_name"], build_set(msg["values"]))
    return {"ok": True}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/rename",
        vol.Required("ieee"): str,
        vol.Required("name"): str,
        vol.Optional("rename_ha_entities", default=True): bool,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_rename(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    device = _target(bridge, msg["ieee"])
    payload = build_rename(device["friendly_name"], msg["name"], msg["rename_ha_entities"])
    for other_ieee, other in bridge.devices.items():
        if other_ieee != msg["ieee"] and other.get("friendly_name") == payload["to"]:
            raise Z2MError("name_taken", f"Der Name '{payload['to']}' ist schon vergeben")
    await bridge.async_request("device/rename", payload)
    return {"ok": True, "name": payload["to"]}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/set_area",
        vol.Required("ieee"): str,
        vol.Required("area_id"): vol.Any(str, None),
        vol.Optional("sync_group", default=False): bool,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_set_area(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    ieee, area_id = msg["ieee"], msg["area_id"]
    _target(bridge, ieee)
    info = _ha_info(hass).get(ieee)
    if info is None:
        raise Z2MError("not_found", "Das Gerät ist in Home Assistant nicht bekannt (MQTT-Erkennung prüfen)")
    if area_id is not None and ar.async_get(hass).async_get_area(area_id) is None:
        raise Z2MError("not_found", f"Unbekannter Bereich {area_id}")

    old_area = info["area_id"]
    old_lights = _area_lights(hass, bridge, old_area)
    dr.async_get(hass).async_update_device(info["device_id"], area_id=area_id)
    result: dict[str, Any] = {"ok": True, "area_id": area_id, "previous_area_id": old_area}
    if not msg["sync_group"] or old_area == area_id:
        return result

    plan = plan_group_sync(
        ieee,
        bridge.device_type(ieee) == "light",
        bridge.groups,
        {i for i in bridge.devices if bridge.device_type(i) == "light"},
        old_lights,
        _area_lights(hass, bridge, area_id),
    )
    done: list[dict[str, Any]] = []
    for action in plan["actions"]:
        entry = {"action": action["action"], "group": action["group"], "group_id": action["group_id"]}
        try:
            await bridge.async_request(action["topic"], action["payload"])
            entry["status"] = "ok"
        except Z2MError as err:
            entry.update(status="error", error=err.message)
        done.append(entry)
    result["group_sync"] = {
        "old_group": plan["old_group"],
        "new_group": plan["new_group"],
        "skipped": plan["skipped"],
        "actions": done,
    }
    return result


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/create_room_group", vol.Required("area_id"): str})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_create_room_group(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Legt für einen Bereich ohne Gruppe eine Z2M-Gruppe mit seinen
    Zigbee-Lichtern an (Szenen brauchen eine Gruppe)."""
    area = ar.async_get(hass).async_get_area(msg["area_id"])
    if area is None:
        raise Z2MError("not_found", f"Unbekannter Bereich {msg['area_id']}")
    taken = {str(d.get("friendly_name")) for d in bridge.devices.values() if d.get("friendly_name")}
    taken |= {str(g.get("friendly_name")) for g in bridge.groups if g.get("friendly_name")}
    plan = plan_room_group(area.name, _area_lights(hass, bridge, area.id) or set(), bridge.groups, taken)
    if "error" in plan:
        raise Z2MError(plan["error"], plan["message"])
    (add_topic, add_payload), *members = plan["requests"]
    created = await bridge.async_request(add_topic, add_payload)
    added: list[str] = []
    failed: list[dict[str, str]] = []
    for topic, payload in members:
        try:
            await bridge.async_request(topic, payload)
            added.append(payload["device"])
        except Z2MError as err:
            failed.append({"ieee": payload["device"], "error": err.message})
    return {
        "ok": True,
        "group": {"id": created.get("id"), "friendly_name": plan["name"]},
        "added": added,
        "failed": failed,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/identify", vol.Required("ieee"): str})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_identify(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    device = _target(bridge, msg["ieee"])
    if not supports_identify(device):
        raise Z2MError("not_supported", "Dieses Gerät kann sich nicht melden (kein Identify)")
    await bridge.async_publish_set(device["friendly_name"], {"identify": "identify"})
    return {"ok": True}


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/interview", vol.Required("ieee"): str})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_interview(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    _target(bridge, msg["ieee"])
    await bridge.async_request("device/interview", build_interview(msg["ieee"]))
    return {"ok": True}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/remove",
        vol.Required("ieee"): str,
        vol.Optional("force", default=False): bool,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_remove(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    _target(bridge, msg["ieee"])
    await bridge.async_request("device/remove", build_remove(msg["ieee"], msg["force"]))
    return {"ok": True}


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/enable_last_seen"})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_enable_last_seen(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Schaltet die Z2M-Option ``advanced.last_seen`` auf ``ISO_8601``."""
    return await bridge.async_enable_last_seen()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/permit_join",
        vol.Optional("time", default=240): vol.All(int, vol.Range(min=0, max=254)),
        vol.Optional("device"): vol.Any(str, None),
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_permit_join(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    device = msg.get("device")
    if device:
        _target(bridge, device)
    await bridge.async_request("permit_join", build_permit_join(msg["time"], device))
    return {"ok": True}


@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/state"})
@websocket_api.async_response
async def ws_zigbee_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    connection.send_result(msg["id"], {"states": bridge.state_cache.states})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/networkmap",
        vol.Optional("refresh", default=False): bool,
        vol.Optional("background", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_zigbee_networkmap(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Cache lesen darf jeder Benutzer, eine neue Messung nur ein Admin.

    ``refresh`` + ``background``: Antwort sofort ``{status, started_at}``, Ergebnis per Ereignis
    ``haven_networkmap_updated``. ``refresh`` allein blockiert wie bis 0.5.0 (bis 10 min).
    Ohne ``refresh``: Cache plus ``running``, ``started_at``, ``last_error``.
    """
    if (bridge := _bridge_or_error(hass, connection, msg["id"])) is None:
        return
    if msg["refresh"] and not connection.user.is_admin:
        connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "Nur Administratoren dürfen messen")
        return
    try:
        if msg["refresh"] and msg["background"]:
            result: Any = bridge.start_networkmap()
        elif msg["refresh"]:
            result = await bridge.async_networkmap(True)
        else:
            result = bridge.networkmap_view()
    except Z2MError as err:
        _fail(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


# --- App-Daten (.storage) -------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "haven/floorplan/get"})
@websocket_api.async_response
async def ws_floorplan_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(msg["id"], await get_storage(hass).async_get_plan_doc())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/floorplan/set",
        vol.Required("plan"): dict,
        vol.Optional("updated_at"): vol.Any(str, int, float, None),
        vol.Optional("base_revision"): vol.Any(int, None),
    }
)
@websocket_api.async_response
async def ws_floorplan_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        require_valid(validate_floorplan(msg["plan"]), "Der Grundriss")
        require_valid(validate_updated_at(msg.get("updated_at")), "updated_at")
        require_valid(validate_base_revision(msg.get("base_revision")), "base_revision")
        doc = await get_storage(hass).async_set_plan(msg["plan"], msg.get("updated_at"), msg.get("base_revision"))
    except Z2MError as err:
        _fail(connection, msg["id"], err)
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("floorplan", doc["updated_at"], doc["revision"]))
    connection.send_result(msg["id"], {"ok": True, "updated_at": doc["updated_at"], "revision": doc["revision"]})


@websocket_api.websocket_command({vol.Required("type"): "haven/widgets/get"})
@websocket_api.async_response
async def ws_widgets_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(msg["id"], await get_storage(hass).async_get_widgets_doc())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/widgets/set",
        vol.Required("widgets"): list,
        vol.Optional("layout"): vol.Any(dict, None),
        vol.Optional("updated_at"): vol.Any(str, int, float, None),
        vol.Optional("base_revision"): vol.Any(int, None),
    }
)
@websocket_api.async_response
async def ws_widgets_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        require_valid(validate_widgets(msg["widgets"]), "Die Widget-Konfiguration")
        require_valid(validate_layout(msg.get("layout")), "layout")
        require_valid(validate_updated_at(msg.get("updated_at")), "updated_at")
        require_valid(validate_base_revision(msg.get("base_revision")), "base_revision")
        doc = await get_storage(hass).async_set_widgets(
            msg["widgets"],
            msg.get("layout"),
            msg.get("updated_at"),
            layout_given="layout" in msg,
            base_revision=msg.get("base_revision"),
        )
    except Z2MError as err:
        _fail(connection, msg["id"], err)
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("widgets", doc["updated_at"], doc["revision"]))
    connection.send_result(msg["id"], {"ok": True, "updated_at": doc["updated_at"], "revision": doc["revision"]})


@websocket_api.websocket_command({vol.Required("type"): "haven/prefs/get"})
@websocket_api.async_response
async def ws_prefs_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(msg["id"], await get_storage(hass).async_get_prefs_doc())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/prefs/set",
        vol.Required("prefs"): dict,
        vol.Optional("base_revision"): vol.Any(int, None),
    }
)
@websocket_api.async_response
async def ws_prefs_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        require_valid(validate_prefs(msg["prefs"]), "Die Einstellungen")
        require_valid(validate_base_revision(msg.get("base_revision")), "base_revision")
        prefs = normalize_prefs(msg["prefs"])
        doc = await get_storage(hass).async_set_prefs(prefs, msg.get("base_revision"))
    except Z2MError as err:
        _fail(connection, msg["id"], err)
        return
    hass.bus.async_fire(
        EVENT_DATA_UPDATED, build_event_data("prefs", resolve_updated_at(None, prefs), doc["revision"])
    )
    connection.send_result(msg["id"], {"ok": True, "prefs": doc["prefs"], "revision": doc["revision"]})


# --- Szenen der Z2M-Gruppen (Feature ``scenes``) ------------------------------------

# So lange wartet ein Befehl, bis ``bridge/groups`` die Änderung zeigt
SCENE_CONFIRM_TIMEOUT = 5.0
# Zwei schnelle „Neu“-Befehle dürfen nicht dieselbe freie ID bekommen
_SCENE_LOCK = asyncio.Lock()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/scene_store",
        vol.Required("group_id"): int,
        vol.Optional("scene_id"): vol.Any(int, None),
        vol.Optional("name"): vol.Any(str, None),
        vol.Optional("look"): vol.Any(dict, None),
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_store(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Speichert den aktuellen Zustand der Gruppe als neue Szene oder überschreibt eine."""
    look = validate_look(msg["look"]) if msg.get("look") is not None else None
    return await async_group_scene_store(hass, bridge, msg["group_id"], msg.get("name"), msg.get("scene_id"), look)


async def async_group_scene_store(
    hass: HomeAssistant, bridge: Z2MBridge, group_id: int, name: Any, scene_id: int | None, look: dict[str, Any] | None
) -> dict[str, Any]:
    """Zigbee-Teil einer Szene speichern (auch für ``haven/scenes/store``)."""
    msg = {"scene_id": scene_id}
    storage = get_storage(hass)
    async with group_lock(group_id):
        async with _SCENE_LOCK:
            group = find_group(bridge.groups, group_id)
            scene_id, name, payload = plan_store(group, name, scene_id)
            await bridge.async_publish_set(group["friendly_name"], payload)
            confirmed = await bridge.async_wait_groups(stored(group_id, scene_id, name), SCENE_CONFIRM_TIMEOUT)
        # Soll (Feature scene_sync): die Zustände, die die Lampen gerade gespeichert haben
        await _scene_sync(hass, bridge).async_capture_target(group_id, scene_id, "store")
    looks = dict(await storage.async_get_scene_looks())
    key = scene_key(group_id, scene_id)
    if look is None:
        looks.pop(key, None)  # der alte Look passt nicht mehr zum neuen Zustand
    else:
        looks[key] = {**look, "stored_at": datetime.now(timezone.utc).isoformat()}
    if confirmed:
        looks = prune_looks(looks, bridge.groups)
        await storage.async_set_scene_targets(prune_looks(await storage.async_get_scene_targets(), bridge.groups))
        await storage.async_set_scene_checks(prune_looks(await storage.async_get_scene_checks(), bridge.groups))
    await storage.async_set_scene_looks(looks)
    return {
        "ok": True,
        "group_id": group_id,
        "scene_id": scene_id,
        "name": name,
        "created": msg.get("scene_id") is None,
        "confirmed": confirmed,
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/scene_rename",
        vol.Required("group_id"): int,
        vol.Required("scene_id"): int,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_rename(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    return await async_group_scene_rename(bridge, msg["group_id"], msg["scene_id"], msg["name"])


async def async_group_scene_rename(bridge: Z2MBridge, group_id: int, scene_id: int, new_name: Any) -> dict[str, Any]:
    async with group_lock(group_id), _SCENE_LOCK:
        group = find_group(bridge.groups, group_id)
        name, payload = plan_rename(group, scene_id, new_name)
        await bridge.async_publish_set(group["friendly_name"], payload)
        confirmed = await bridge.async_wait_groups(stored(group_id, scene_id, name), SCENE_CONFIRM_TIMEOUT)
    return {"ok": True, "group_id": group_id, "scene_id": scene_id, "name": name, "confirmed": confirmed}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/scene_remove",
        vol.Required("group_id"): int,
        vol.Required("scene_id"): int,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_remove(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    return await async_group_scene_remove(hass, bridge, msg["group_id"], msg["scene_id"])


async def async_group_scene_remove(hass: HomeAssistant, bridge: Z2MBridge, group_id: int, scene_id: int) -> dict[str, Any]:
    async with group_lock(group_id), _SCENE_LOCK:
        group = find_group(bridge.groups, group_id)
        payload = plan_remove(group, scene_id)
        await bridge.async_publish_set(group["friendly_name"], payload)
        confirmed = await bridge.async_wait_groups(removed(group_id, scene_id), SCENE_CONFIRM_TIMEOUT)
    storage = get_storage(hass)
    key = scene_key(group_id, scene_id)
    # Look, Soll, letzte Messung und Zusatzgeräte gehören zur Szene
    for get, set_ in (
        (storage.async_get_scene_extras, storage.async_set_scene_extras),
        (storage.async_get_scene_looks, storage.async_set_scene_looks),
        (storage.async_get_scene_targets, storage.async_set_scene_targets),
        (storage.async_get_scene_checks, storage.async_set_scene_checks),
    ):
        values = dict(await get())
        if values.pop(key, None) is not None:
            await set_(values)
    return {"ok": True, "group_id": group_id, "scene_id": scene_id, "confirmed": confirmed}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/zigbee/scene_recall",
        vol.Required("group_id"): int,
        vol.Required("scene_id"): int,
    }
)
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_recall(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Ruft eine Szene auf. Ohne Admin, wie ``scene.turn_on``. Wartet auf eine laufende Messung der Gruppe."""
    async with group_lock(msg["group_id"]):
        group = find_group(bridge.groups, msg["group_id"])
        await bridge.async_publish_set(group["friendly_name"], plan_recall(group, msg["scene_id"]))
    return {"ok": True}


# --- Szenen-Abgleich (Feature ``scene_sync``) ------------------------------------------


def _scene_sync(hass: HomeAssistant, bridge: Z2MBridge) -> SceneSync:
    """Abgleich mit der HA-Zuordnung ``ieee -> [light.*]`` aus den Registries."""

    def ha_lights() -> dict[str, list[str]]:
        return {
            ieee: [e for e in info["ha_entities"] if e.startswith("light.")]
            for ieee, info in _ha_info(hass).items()
        }

    return SceneSync(hass, bridge, get_storage(hass), ha_lights)


_SCENE_REF = {vol.Required("group_id"): int, vol.Required("scene_id"): int}


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/scene_check", **_SCENE_REF})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_check(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Ruft die Szene auf, lässt jede Lampe lesen, vergleicht mit dem Soll, stellt den Zustand wieder her."""
    return await _scene_sync(hass, bridge).async_check(msg["group_id"], msg["scene_id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/scene_adopt", **_SCENE_REF})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_adopt(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Letzte Messung als Soll und Look übernehmen."""
    return await _scene_sync(hass, bridge).async_adopt(msg["group_id"], msg["scene_id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/zigbee/scene_repair", **_SCENE_REF})
@websocket_api.async_response
@_z2m_write
async def ws_zigbee_scene_repair(hass: HomeAssistant, bridge: Z2MBridge, msg: dict[str, Any]) -> dict[str, Any]:
    """Soll neu auf alle Lampen speichern, dann messen (Antwort wie ``scene_check``)."""
    return await _scene_sync(hass, bridge).async_repair(msg["group_id"], msg["scene_id"])

"""WebSocket-Befehle für Szenen je Raum (``haven/scenes/*``, Feature ``room_scenes``).

Anders als ``haven/zigbee/scene_*`` arbeiten sie mit dem Bereich: Hat der Raum eine
Z2M-Gruppe, gehen sie an deren Szene und speichern Zusatzgeräte dazu, sonst führt
Haven die Szene selbst (siehe ``room_scenes``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .commands import Z2MError
from .const import EVENT_DATA_UPDATED
from .room_scenes import (
    capture_device,
    keep_devices,
    remove_room_scene,
    rename_room_scene,
    room_scene_list,
    store_room_scene,
    validate_device_ids,
)
from .scenes import scene_key, validate_look
from .storage import get_storage


def _room(hass: HomeAssistant, bridge: Any, area_id: str) -> dict[str, Any]:
    from .websocket_api import build_rooms  # noqa: PLC0415 - Importzyklus

    for room in build_rooms(hass, bridge)["rooms"]:
        if room["area_id"] == area_id:
            return room
    raise Z2MError("not_found", f"Unbekannter Raum {area_id}")


def _group_id(room: dict[str, Any]) -> int | None:
    group = room.get("group")
    return group.get("id") if isinstance(group, dict) and isinstance(group.get("id"), int) else None


def _capture(hass: HomeAssistant, entity_ids: list[str]) -> list[dict[str, Any]]:
    devices = []
    for entity_id in entity_ids:
        s = hass.states.get(entity_id)
        entry = capture_device(entity_id, None if s is None else {"state": s.state, "attributes": dict(s.attributes)})
        if entry is not None:
            devices.append(entry)
    return devices


def _changed(hass: HomeAssistant) -> None:
    from .automations_runtime import get_engine  # noqa: PLC0415

    get_engine(hass).invalidate_rooms()
    hass.bus.async_fire(EVENT_DATA_UPDATED, {"kind": "scenes"})


_REF = {vol.Required("area_id"): str, vol.Required("scene_id"): int}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/scenes/store",
        vol.Required("area_id"): str,
        vol.Optional("scene_id"): vol.Any(int, None),
        vol.Optional("name"): vol.Any(str, None),
        vol.Optional("look"): vol.Any(dict, None),
        vol.Optional("devices", default=list): list,
    }
)
@websocket_api.async_response
async def ws_scenes_store(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Aktuellen Zustand als Szene: Zigbee-Teil (falls Gruppe) und die gewählten Zusatzgeräte."""
    from .websocket_api import _write  # noqa: PLC0415

    async def run(bridge: Any) -> dict[str, Any]:
        from .websocket_api import async_group_scene_store  # noqa: PLC0415

        look = validate_look(msg["look"]) if msg.get("look") is not None else None
        devices = _capture(hass, validate_device_ids(msg["devices"]))
        room = _room(hass, bridge, msg["area_id"])
        storage = get_storage(hass)
        group_id = _group_id(room)
        if group_id is not None:
            result = await async_group_scene_store(hass, bridge, group_id, msg.get("name"), msg.get("scene_id"), look)
            extras = dict(await storage.async_get_scene_extras())
            key = scene_key(group_id, result["scene_id"])
            if devices:
                extras[key] = devices
            else:
                extras.pop(key, None)
            await storage.async_set_scene_extras(extras)
            _changed(hass)
            return {**result, "area_id": msg["area_id"], "devices": len(devices)}
        all_scenes = dict(await storage.async_get_room_scenes())
        scenes, scene = store_room_scene(
            room_scene_list(all_scenes, msg["area_id"]),
            msg.get("name"),
            msg.get("scene_id"),
            devices,
            look,
            datetime.now(timezone.utc).isoformat(),
        )
        all_scenes[msg["area_id"]] = scenes
        await storage.async_set_room_scenes(all_scenes)
        _changed(hass)
        return {
            "ok": True,
            "area_id": msg["area_id"],
            "group_id": None,
            "scene_id": scene["id"],
            "name": scene["name"],
            "created": msg.get("scene_id") is None,
            "confirmed": True,
            "devices": len(devices),
        }

    await _write(hass, connection, msg, run)


@websocket_api.websocket_command({vol.Required("type"): "haven/scenes/recall", **_REF})
@websocket_api.async_response
async def ws_scenes_recall(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Szene aufrufen (ohne Admin, wie ``scene.turn_on``): Gruppe plus Zusatzgeräte."""
    from .automations import _plan_scene  # noqa: PLC0415
    from .automations_runtime import get_engine  # noqa: PLC0415
    from .websocket_api import _write  # noqa: PLC0415

    async def run(bridge: Any) -> dict[str, Any]:
        engine = get_engine(hass)
        await get_storage(hass).async_get_scene_looks()
        room = engine.room_targets(max_age=0).get(msg["area_id"])
        if room is None:
            raise Z2MError("not_found", f"Unbekannter Raum {msg['area_id']}")
        plan = _plan_scene(room, msg["scene_id"], bridge.base_topic)
        if plan.note is not None:
            raise Z2MError("not_found", f"Unbekannte Szene {msg['scene_id']}")
        await engine._async_send(plan)  # noqa: SLF001
        if plan.expected is not None:
            engine.motion.handle_knob(room.area_id, True)
        return {"ok": True}

    await _write(hass, connection, msg, run)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/scenes/rename", **_REF, vol.Required("name"): str})
@websocket_api.async_response
async def ws_scenes_rename(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    from .websocket_api import _write, async_group_scene_rename  # noqa: PLC0415

    async def run(bridge: Any) -> dict[str, Any]:
        room = _room(hass, bridge, msg["area_id"])
        group_id = _group_id(room)
        if group_id is not None:
            result = await async_group_scene_rename(bridge, group_id, msg["scene_id"], msg["name"])
        else:
            storage = get_storage(hass)
            all_scenes = dict(await storage.async_get_room_scenes())
            scenes, name = rename_room_scene(room_scene_list(all_scenes, msg["area_id"]), msg["scene_id"], msg["name"])
            all_scenes[msg["area_id"]] = scenes
            await storage.async_set_room_scenes(all_scenes)
            result = {"ok": True, "group_id": None, "scene_id": msg["scene_id"], "name": name, "confirmed": True}
        _changed(hass)
        return result

    await _write(hass, connection, msg, run)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/scenes/remove", **_REF})
@websocket_api.async_response
async def ws_scenes_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    from .websocket_api import _write, async_group_scene_remove  # noqa: PLC0415

    async def run(bridge: Any) -> dict[str, Any]:
        room = _room(hass, bridge, msg["area_id"])
        group_id = _group_id(room)
        if group_id is not None:
            result = await async_group_scene_remove(hass, bridge, group_id, msg["scene_id"])
        else:
            storage = get_storage(hass)
            all_scenes = dict(await storage.async_get_room_scenes())
            all_scenes[msg["area_id"]] = remove_room_scene(room_scene_list(all_scenes, msg["area_id"]), msg["scene_id"])
            await storage.async_set_room_scenes(all_scenes)
            result = {"ok": True, "group_id": None, "scene_id": msg["scene_id"], "confirmed": True}
        _changed(hass)
        return result

    await _write(hass, connection, msg, run)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "haven/scenes/set_devices", **_REF, vol.Required("devices"): list}
)
@websocket_api.async_response
async def ws_scenes_set_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Zusatzgeräte aus einer Szene nehmen (``devices``: die, die bleiben)."""
    from .websocket_api import _write  # noqa: PLC0415

    async def run(bridge: Any) -> dict[str, Any]:
        keep = validate_device_ids(msg["devices"])
        room = _room(hass, bridge, msg["area_id"])
        storage = get_storage(hass)
        group_id = _group_id(room)
        if group_id is not None:
            extras = dict(await storage.async_get_scene_extras())
            key = scene_key(group_id, msg["scene_id"])
            remaining = keep_devices(extras.get(key) or [], keep)
            if remaining:
                extras[key] = remaining
            else:
                extras.pop(key, None)
            await storage.async_set_scene_extras(extras)
        else:
            all_scenes = dict(await storage.async_get_room_scenes())
            scenes = room_scene_list(all_scenes, msg["area_id"])
            scene = next((s for s in scenes if s["id"] == msg["scene_id"]), None)
            if scene is None:
                raise Z2MError("not_found", f"Unbekannte Szene {msg['scene_id']}")
            remaining = keep_devices(scene.get("devices") or [], keep)
            if not remaining:
                raise Z2MError("no_lights", "Eine Szene ohne Zigbee-Gruppe braucht mindestens ein Gerät")
            all_scenes[msg["area_id"]] = [{**s, "devices": remaining} if s is scene else s for s in scenes]
            await storage.async_set_room_scenes(all_scenes)
        _changed(hass)
        return {"ok": True, "devices": len(remaining)}

    await _write(hass, connection, msg, run)


def async_register_room_scene_commands(hass: HomeAssistant) -> None:
    for command in (ws_scenes_store, ws_scenes_recall, ws_scenes_rename, ws_scenes_remove, ws_scenes_set_devices):
        websocket_api.async_register_command(hass, command)

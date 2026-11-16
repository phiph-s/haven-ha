"""WebSocket-Befehle der Haven-Automationen (``haven/automations/*``)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .appdata import build_event_data
from .automations import gesture_from_name, plan_to_json
from .automations_runtime import get_engine
from .commands import Z2MError
from .const import EVENT_DATA_UPDATED

# Gerätetypen mit Aktionen (Drehregler, Taster), für die Konflikt-Suche
_ACTION_TYPES = ("knob", "remote")


@callback
def async_register_automation_commands(hass: HomeAssistant) -> None:
    for command in (ws_automations_list, ws_automations_set, ws_automations_delete, ws_automations_test):
        websocket_api.async_register_command(hass, command)


def _device_automations(hass: HomeAssistant) -> dict[str, list[str]]:
    """IEEE -> HA-Automationen, die das Gerät nennen (Device-Trigger, Bedingungen, Aktionen).

    Nur für Drehregler und Taster. Ohne geladene ``automation``-Integration leer.
    """
    from .websocket_api import _ha_info  # noqa: PLC0415

    bridge = get_engine(hass).bridge
    if bridge is None:
        return {}
    try:
        from homeassistant.components.automation import automations_with_device  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return {}
    out: dict[str, list[str]] = {}
    for ieee, info in _ha_info(hass).items():
        if bridge.device_type(ieee) not in _ACTION_TYPES or not info.get("device_id"):
            continue
        try:
            found = automations_with_device(hass, info["device_id"])
        except Exception:  # noqa: BLE001 - automation nicht geladen
            found = []
        if found:
            out[ieee] = sorted(found)
    return out


def _rooms(hass: HomeAssistant) -> dict[str, Any] | None:
    engine = get_engine(hass)
    if engine.bridge is None:
        return None
    engine.invalidate_rooms()
    return engine.room_targets()


@websocket_api.websocket_command({vol.Required("type"): "haven/automations/list"})
@websocket_api.async_response
async def ws_automations_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    engine = get_engine(hass)
    await engine.async_load()
    rooms = _rooms(hass) or {}
    connection.send_result(
        msg["id"],
        {
            "automations": engine.list_public(),
            "rooms": {k: {**v.as_dict(), "has_lights": v.has_lights} for k, v in rooms.items()},
            "device_automations": _device_automations(hass),
            "running": engine.bridge is not None,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "haven/automations/set", vol.Required("automation"): dict}
)
@websocket_api.async_response
async def ws_automations_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    engine = get_engine(hass)
    await engine.async_load()
    try:
        automation = await engine.async_set(msg["automation"], _rooms(hass))
    except Z2MError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("automations"))
    connection.send_result(msg["id"], {"ok": True, "automation": automation})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "haven/automations/delete", vol.Required("id"): str}
)
@websocket_api.async_response
async def ws_automations_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    engine = get_engine(hass)
    await engine.async_load()
    if not await engine.async_delete(msg["id"]):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Unbekannte Automation {msg['id']}")
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("automations"))
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/automations/test",
        vol.Required("id"): str,
        vol.Required("action"): vol.In(["rotate_left", "rotate_right", "single", "double", "hold"]),
    }
)
@websocket_api.async_response
async def ws_automations_test(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    engine = get_engine(hass)
    await engine.async_load()
    gesture = gesture_from_name(msg["action"])
    if engine.bridge is None or gesture is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Haven ist nicht eingerichtet oder nicht geladen")
        return
    try:
        plan = engine.test(msg["id"], gesture)
    except Z2MError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], {"ok": True, "plan": plan_to_json(plan)})

"""WebSocket-Befehle der Heizungsregelung (``haven/heating/*``)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar

from .appdata import build_event_data
from .commands import Z2MError
from .const import EVENT_DATA_UPDATED
from .heating import MODES
from .heating_runtime import get_heating


@callback
def async_register_heating_commands(hass: HomeAssistant) -> None:
    for command in (ws_heating_list, ws_heating_set, ws_heating_delete, ws_heating_control, ws_heating_display):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command({vol.Required("type"): "haven/heating/list"})
@websocket_api.async_response
async def ws_heating_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    runner = get_heating(hass)
    await runner.async_load()
    connection.send_result(
        msg["id"],
        {"rooms": runner.list_public(), "running": runner.bridge is not None, "displays": list(runner.displays)},
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/heating/set", vol.Required("config"): dict})
@websocket_api.async_response
async def ws_heating_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    runner = get_heating(hass)
    await runner.async_load()
    areas = {a.id for a in ar.async_get(hass).async_list_areas()}
    try:
        result = await runner.async_set(msg["config"], areas)
    except Z2MError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("heating"))
    connection.send_result(msg["id"], {"ok": True, **result})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/heating/delete", vol.Required("area_id"): str})
@websocket_api.async_response
async def ws_heating_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    runner = get_heating(hass)
    await runner.async_load()
    if not await runner.async_delete(msg["area_id"]):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Für {msg['area_id']} ist keine Heizungsregelung eingerichtet")
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("heating"))
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/heating/control",
        vol.Required("area_id"): str,
        vol.Optional("target"): vol.Coerce(float),
        vol.Optional("mode"): vol.In(MODES),
        vol.Optional("resume"): bool,
    }
)
@websocket_api.async_response
async def ws_heating_control(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    runner = get_heating(hass)
    await runner.async_load()
    try:
        status = await runner.async_control(
            msg["area_id"], target=msg.get("target"), mode=msg.get("mode"), resume=bool(msg.get("resume"))
        )
    except Z2MError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], {"ok": True, "status": status})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "haven/heating/display", vol.Required("ieee"): str, vol.Required("display"): bool}
)
@websocket_api.async_response
async def ws_heating_display(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Thermostat als reines Display markieren (``display: true``) oder die Markierung aufheben."""
    runner = get_heating(hass)
    await runner.async_load()
    try:
        result = await runner.async_display(msg["ieee"], msg["display"])
    except Z2MError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("heating"))
    connection.send_result(msg["id"], {"ok": True, **result})

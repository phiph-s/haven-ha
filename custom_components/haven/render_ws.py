"""WebSocket-Befehle für die Bilder der Wohnung (``haven/render/*``).

Die App lädt nach Kartenänderungen ihre Lichtebenen hoch (siehe ``render_store.py``):
``status`` → ``begin`` → viele ``chunk`` (Base64, je höchstens einige 100 KB) → ``commit``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .appdata import build_event_data
from .const import EVENT_DATA_UPDATED
from .render_runtime import get_render, render_available
from .render_store import RenderStoreError

# Höchstens so viele Bytes je Stück (nach Base64-Dekodierung)
MAX_CHUNK = 1024 * 1024


@callback
def async_register_render_commands(hass: HomeAssistant) -> None:
    for command in (
        ws_render_status,
        ws_render_file,
        ws_render_begin,
        ws_render_chunk,
        ws_render_commit,
        ws_render_abort,
    ):
        websocket_api.async_register_command(hass, command)


def _fail(connection: websocket_api.ActiveConnection, msg_id: int, err: Exception) -> None:
    connection.send_error(msg_id, "invalid_format", str(err))


@websocket_api.websocket_command({vol.Required("type"): "haven/render/status"})
@websocket_api.async_response
async def ws_render_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    runtime = get_render(hass)
    status = await hass.async_add_executor_job(runtime.store.status)
    connection.send_result(msg["id"], {"available": render_available(), **status})


# Höchstens so viele Bytes je gelesenem Stück (vor Base64)
MAX_READ = 512 * 1024


@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/render/file",
        vol.Required("set"): str,
        vol.Required("name"): str,
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    }
)
@websocket_api.async_response
async def ws_render_file(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Datei eines Bildsatzes lesen (Stück für Stück): für Bilder in der App, z. B. Raumkacheln."""
    runtime = get_render(hass)
    try:
        data, size = await hass.async_add_executor_job(
            runtime.store.read, msg["set"], msg["name"], msg["offset"], MAX_READ
        )
    except (RenderStoreError, OSError) as err:
        _fail(connection, msg["id"], err)
        return
    connection.send_result(
        msg["id"],
        {"size": size, "offset": msg["offset"], "data": base64.b64encode(data).decode("ascii")},
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/render/begin", vol.Required("key"): str})
@websocket_api.async_response
async def ws_render_begin(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if not render_available():
        connection.send_error(msg["id"], "not_supported", "Home Assistant fehlen numpy oder Pillow")
        return
    runtime = get_render(hass)
    async with runtime.upload_lock:
        try:
            upload = await hass.async_add_executor_job(runtime.store.begin, msg["key"])
        except (RenderStoreError, OSError) as err:
            _fail(connection, msg["id"], err)
            return
    connection.send_result(msg["id"], {"upload": upload, "max_chunk": MAX_CHUNK})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "haven/render/chunk",
        vol.Required("upload"): str,
        vol.Required("path"): str,
        vol.Required("offset"): vol.All(int, vol.Range(min=0)),
        vol.Required("data"): str,
    }
)
@websocket_api.async_response
async def ws_render_chunk(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    try:
        data = base64.b64decode(msg["data"], validate=True)
    except (binascii.Error, ValueError):
        connection.send_error(msg["id"], "invalid_format", "data ist kein Base64")
        return
    if len(data) > MAX_CHUNK:
        connection.send_error(msg["id"], "invalid_format", "Stück zu groß")
        return
    runtime = get_render(hass)
    async with runtime.upload_lock:
        try:
            size = await hass.async_add_executor_job(
                runtime.store.chunk, msg["upload"], msg["path"], msg["offset"], data
            )
        except (RenderStoreError, OSError) as err:
            _fail(connection, msg["id"], err)
            return
    connection.send_result(msg["id"], {"size": size})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/render/commit", vol.Required("upload"): str})
@websocket_api.async_response
async def ws_render_commit(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    runtime = get_render(hass)
    async with runtime.upload_lock:
        try:
            sets = await hass.async_add_executor_job(runtime.store.commit, msg["upload"])
        except (RenderStoreError, OSError) as err:
            _fail(connection, msg["id"], err)
            return
    await runtime.async_reload()
    hass.bus.async_fire(EVENT_DATA_UPDATED, build_event_data("render"))
    connection.send_result(msg["id"], {"ok": True, "sets": sorted(sets)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "haven/render/abort"})
@websocket_api.async_response
async def ws_render_abort(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    runtime = get_render(hass)
    async with runtime.upload_lock:
        await hass.async_add_executor_job(runtime.store.abort)
    connection.send_result(msg["id"], {"ok": True})

"""Haven: Home-Assistant-Integration für die Haven-App (nur lesend in Etappe 1)."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC, DOMAIN
from .storage import get_storage
from .websocket_api import async_register_websocket_commands
from .z2m import Z2MBridge

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class HavenData:
    """Laufzeitdaten eines Config Entries."""

    z2m: Z2MBridge


HavenConfigEntry = ConfigEntry[HavenData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registriert die WebSocket-Befehle einmalig."""
    async_register_websocket_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HavenConfigEntry) -> bool:
    """Startet die Z2M-Anbindung über die MQTT-Integration."""
    base_topic = entry.options.get(
        CONF_BASE_TOPIC, entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
    )
    bridge = Z2MBridge(hass, base_topic)
    storage = get_storage(hass)
    bridge.networkmap = await storage.async_load_networkmap()
    bridge.on_networkmap = storage.schedule_save_networkmap
    try:
        await bridge.async_start()
    except Exception as err:  # noqa: BLE001 - MQTT nicht bereit
        raise ConfigEntryNotReady(f"MQTT nicht verfügbar: {err}") from err

    entry.runtime_data = HavenData(z2m=bridge)
    entry.async_on_unload(bridge.stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HavenConfigEntry) -> bool:
    """Entlädt den Entry. Abos werden über async_on_unload beendet."""
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HavenConfigEntry) -> None:
    """Basis-Topic geändert: Entry neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)

"""Haven: Home-Assistant-Integration für die Haven-App (nur lesend in Etappe 1)."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC, DOMAIN, EVENT_NETWORKMAP_UPDATED
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
    bridge.on_networkmap_done = lambda payload: hass.bus.async_fire(EVENT_NETWORKMAP_UPDATED, payload)
    try:
        await bridge.async_start()
    except Exception as err:  # noqa: BLE001 - MQTT nicht bereit
        raise ConfigEntryNotReady(f"MQTT nicht verfügbar: {err}") from err

    entry.runtime_data = HavenData(z2m=bridge)
    entry.async_on_unload(bridge.stop)
    # Haven-Automationen (Drehregler je Raum) hören auf die Bridge
    from .automations_runtime import async_setup_automations  # noqa: PLC0415

    entry.async_on_unload(await async_setup_automations(hass, bridge))
    # Heizungsregelung je Raum (Raumthermometer, Fenster, Sommer) hört ebenfalls auf die Bridge
    from .heating_runtime import async_setup_heating  # noqa: PLC0415

    entry.async_on_unload(await async_setup_heating(hass, bridge))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # Seitenleisten-Eintrag „Haven“ (Web-Build der App), nur wenn mitgeliefert
    await _async_setup_panel(hass, entry)
    return True


async def _async_setup_panel(hass: HomeAssistant, entry: HavenConfigEntry) -> None:
    """Panel anmelden. Fehler kosten nur den Eintrag, nicht die Integration."""
    from homeassistant.loader import async_get_integration  # noqa: PLC0415

    from .panel_setup import async_remove_panel, async_setup_panel  # noqa: PLC0415

    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = str(integration.version) if integration.version else None
        if await async_setup_panel(hass, version):
            entry.async_on_unload(lambda: async_remove_panel(hass))
    except Exception:  # noqa: BLE001 - Panel ist optional
        _LOGGER.exception("Haven: Seitenleisten-Eintrag konnte nicht angelegt werden")


async def async_unload_entry(hass: HomeAssistant, entry: HavenConfigEntry) -> bool:
    """Entlädt den Entry. Abos werden über async_on_unload beendet."""
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HavenConfigEntry) -> None:
    """Basis-Topic geändert: Entry neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)

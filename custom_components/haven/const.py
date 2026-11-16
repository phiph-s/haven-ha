"""Konstanten der Haven-Integration."""

from __future__ import annotations

DOMAIN = "haven"

# Fähigkeiten für haven/info (die App prüft darauf statt auf die Version)
FEATURES = ("light_groups", "room_devices", "prefs", "data_events", "networkmap_cache", "automations", "networkmap_background")

# HA-Event nach erfolgreichem floorplan/set, widgets/set und prefs/set
EVENT_DATA_UPDATED = "haven_data_updated"

# Ende einer Hintergrund-Netzmessung: {ok: true, timestamp} oder {ok: false, error}
EVENT_NETWORKMAP_UPDATED = "haven_networkmap_updated"

CONF_BASE_TOPIC = "base_topic"
DEFAULT_BASE_TOPIC = "zigbee2mqtt"

# Anzahl gemerkter Z2M-Bridge-Events (device_joined, device_leave ...)
MAX_EVENTS = 50

# Version der Store-Dateien (.storage/haven.*)
STORAGE_VERSION = 1

# Höchstens so viele Zustandsmeldungen je Sekunde und Gerät im Abo
STATE_MAX_PER_SECOND = 4

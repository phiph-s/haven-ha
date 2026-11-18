"""„Zuletzt gesehen“ und Verfügbarkeit je Gerät (ohne Home Assistant).

Quellen in dieser Reihenfolge:

- ``last_seen`` im Gerätezustand (Z2M-Option ``advanced.last_seen``), ISO-Text oder Millisekunden,
- der Empfang einer echten Gerätemeldung (Quelle ``message``),
- ``lastSeen`` (ms) aus der zuletzt gemessenen Netzkarte (nur ein Schnappschuss der Messung).

``last_seen`` von Z2M gilt, wenn vorhanden. Sonst der jüngere aus Empfang und Netzkarte. Beim Empfang zählen keine gespeicherten (retained) Meldungen und
nichts in der ersten Minute nach dem Start von Z2M: Z2M veröffentlicht dann die gespeicherten
Zustände aller Geräte, das ließe Geister wieder frisch aussehen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SOURCE_Z2M = "z2m"
SOURCE_NETWORKMAP = "networkmap"
SOURCE_MESSAGE = "message"

# So lange nach dem Start von Z2M zählen Gerätemeldungen nicht als Lebenszeichen
STARTUP_QUIET_SECONDS = 60.0

# Werte unter dieser Grenze (Jahr 2001 in ms) gelten als Sekunden seit 1970
_MS_THRESHOLD = 1_000_000_000_000


def _from_number(value: float) -> datetime | None:
    if value <= 0:
        return None
    seconds = value / 1000 if value >= _MS_THRESHOLD else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_last_seen(value: Any) -> datetime | None:
    """ISO-8601-Text, Zahl oder Zahltext (ms, zur Not s) -> UTC-Zeit, sonst ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_number(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _from_number(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # Z2M-Format ISO_8601 ohne Zone ist UTC
    return parsed.astimezone(timezone.utc)


def networkmap_last_seen(nodes: Any) -> dict[str, datetime]:
    """IEEE -> Zeit aus ``nodes[].lastSeen`` der Netzkarte."""
    out: dict[str, datetime] = {}
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ieee = node.get("ieeeAddr")
        seen = parse_last_seen(node.get("lastSeen"))
        if isinstance(ieee, str) and seen is not None:
            out[ieee] = seen
    return out


def resolve_last_seen(
    state_value: Any, networkmap_value: datetime | None, received: datetime | None = None
) -> tuple[str | None, str | None]:
    """``(last_seen als ISO, Quelle)``, sonst ``(None, None)``.

    ``last_seen`` aus Z2M gilt, wenn vorhanden (die Zeit von Z2M selbst). Sonst der jüngere Zeitpunkt
    aus Empfang und Netzkarte, bei Gleichstand der Empfang.
    """
    seen = parse_last_seen(state_value)
    if seen is not None:
        return seen.isoformat(timespec="seconds"), SOURCE_Z2M
    candidates = [(t, s) for t, s in ((received, SOURCE_MESSAGE), (networkmap_value, SOURCE_NETWORKMAP)) if t is not None]
    if not candidates:
        return None, None
    seen, source = max(candidates, key=lambda c: c[0])  # max behält bei Gleichstand den ersten
    return seen.isoformat(timespec="seconds"), source


def parse_availability(payload: str | bytes) -> str | None:
    """``{"state":"online"}`` oder Text ``online``/``offline`` -> ``"online"``/``"offline"``/``None``."""
    import json  # noqa: PLC0415

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    state: Any = payload
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        state = data.get("state")
    elif isinstance(data, str):
        state = data
    if not isinstance(state, str):
        return None
    state = state.strip().lower()
    return state if state in ("online", "offline") else None


def last_seen_enabled(info: dict[str, Any]) -> bool | None:
    """``config.advanced.last_seen`` aus ``bridge/info``; ``None`` solange unbekannt."""
    config = info.get("config") if isinstance(info, dict) else None
    advanced = config.get("advanced") if isinstance(config, dict) else None
    if not isinstance(advanced, dict):
        return None
    value = advanced.get("last_seen", "disable")
    return value not in ("disable", None, False, "")


def availability_enabled(info: dict[str, Any]) -> bool | None:
    """Z2M-Verfügbarkeit an? ``config.availability``: ``true`` oder Objekt (Standard in Z2M: aus)."""
    config = info.get("config") if isinstance(info, dict) else None
    if not isinstance(config, dict):
        return None
    value = config.get("availability")
    if isinstance(value, dict):
        # Z2M 2.x: {"enabled": false, "active": {...}, "passive": {...}}; 1.x: Objekt ohne enabled = an
        return bool(value.get("enabled", True))
    return bool(value) if value is not None else False

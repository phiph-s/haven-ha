"""Z2M-Requests: Topics, Payload-Bauer und Transaktions-Abgleich (ohne Home Assistant).

Z2M beantwortet ``<base>/bridge/request/<x>`` auf ``<base>/bridge/response/<x>``
mit ``{"data": {...}, "status": "ok"|"error", "error": "...", "transaction": "..."}``.
Die ``transaction`` aus der Anfrage wird zurückgegeben und dient dem Abgleich.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from typing import Any

DEFAULT_TIMEOUT = 10.0
# Z2M wartet bei der Netzmessung auf jedes Gerät: 78 s im Normalfall, mit unerreichbaren Geräten deutlich länger
NETWORKMAP_TIMEOUT = 600.0
MAX_PERMIT_JOIN = 254  # Z2M akzeptiert 0..254 Sekunden


class Z2MError(Exception):
    """Fehler mit Code (für ``connection.send_error``) und deutschem Text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Topics ---------------------------------------------------------------------


def request_topic(base: str, endpoint: str) -> str:
    return f"{base}/bridge/request/{endpoint}"


def set_topic(base: str, friendly_name: str) -> str:
    return f"{base}/{friendly_name}/set"


# --- Payload-Bauer -----------------------------------------------------------------


def build_set(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict) or not values:
        raise Z2MError("invalid_format", "Keine Werte zum Setzen übergeben")
    return dict(values)


def build_rename(current: str, new: str, rename_ha_entities: bool = True) -> dict[str, Any]:
    new = (new or "").strip()
    if not new:
        raise Z2MError("invalid_format", "Der neue Name darf nicht leer sein")
    return {"from": current, "to": new, "homeassistant_rename": bool(rename_ha_entities)}


def build_interview(device_id: str) -> dict[str, Any]:
    return {"id": device_id}


def build_remove(device_id: str, force: bool = False) -> dict[str, Any]:
    return {"id": device_id, "force": bool(force)}


def build_permit_join(time: int, device: str | None = None) -> dict[str, Any]:
    """Z2M 2.x: ``{"time": s, "device"?: ieee}``, ``time: 0`` schließt das Netz."""
    if not isinstance(time, int) or isinstance(time, bool) or not 0 <= time <= MAX_PERMIT_JOIN:
        raise Z2MError("invalid_format", f"Die Dauer muss zwischen 0 und {MAX_PERMIT_JOIN} Sekunden liegen")
    payload: dict[str, Any] = {"time": time}
    if device:
        payload["device"] = device
    return payload


def build_networkmap() -> dict[str, Any]:
    return {"type": "raw", "routes": False}


def build_group_member(group: str, device: str, endpoint: int | str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"group": group, "device": device}
    if endpoint is not None:
        payload["endpoint"] = endpoint
    return payload


# --- Fähigkeiten ---------------------------------------------------------------------


def _walk(exposes: list[dict[str, Any]]):
    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        yield expose
        yield from _walk(expose.get("features") or [])


def supports_identify(device: dict[str, Any]) -> bool:
    """Hat das Gerät ein beschreibbares ``identify``-Expose?"""
    exposes = (device.get("definition") or {}).get("exposes") or []
    for expose in _walk(exposes):
        if expose.get("property") == "identify" or expose.get("name") == "identify":
            access = expose.get("access")
            return access is None or bool(int(access) & 0b010)
    return False


# --- Antworten -----------------------------------------------------------------------


def parse_response(response: dict[str, Any]) -> dict[str, Any]:
    """Liefert ``data`` einer ok-Antwort, sonst :class:`Z2MError`."""
    if response.get("status") == "ok":
        data = response.get("data")
        return data if isinstance(data, dict) else {}
    error = response.get("error") or "unbekannter Fehler"
    raise Z2MError("z2m_error", f"Zigbee2MQTT hat die Anfrage abgelehnt: {error}")


class TransactionTracker:
    """Ordnet Z2M-Responses ihren Requests über das Feld ``transaction`` zu."""

    def __init__(self, prefix: str = "haven") -> None:
        self._prefix = prefix
        self._counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def new(self) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        self._counter += 1
        tid = f"{self._prefix}-{self._counter}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[tid] = fut
        return tid, fut

    def resolve(self, response: Any) -> bool:
        """Ordnet eine Response zu. ``False`` bei fremder oder unbekannter Transaktion."""
        if not isinstance(response, dict):
            return False
        fut = self._pending.pop(str(response.get("transaction")), None)
        if fut is None or fut.done():
            return False
        fut.set_result(response)
        return True

    def discard(self, tid: str) -> None:
        self._pending.pop(tid, None)

    def cancel_all(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def wait(self, tid: str, fut: asyncio.Future[dict[str, Any]], timeout: float, what: str) -> dict[str, Any]:
        """Wartet auf die Response und liefert deren ``data``."""
        try:
            response = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.discard(tid)
            raise Z2MError(
                "timeout",
                f"Keine Antwort von Zigbee2MQTT auf '{what}' innerhalb von {timeout:g} Sekunden. "
                "Läuft Zigbee2MQTT?",
            ) from None
        except asyncio.CancelledError:
            self.discard(tid)
            raise
        return parse_response(response)


# --- Netzmessung im Hintergrund ------------------------------------------------------


def failed_nodes(nodes: Any) -> list[str]:
    """Namen der Knoten, bei denen Z2M ``failed``-Einträge meldet (Teilergebnis)."""
    out: list[str] = []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if isinstance(node, dict) and node.get("failed"):
            out.append(str(node.get("friendlyName") or node.get("ieeeAddr") or "?"))
    return out


_UNREACHABLE = re.compile(r"(timeout|timed out|not reachable|unreachable|no response|did not respond)", re.I)
_QUOTED = re.compile(r"'([^']+)'")


def describe_networkmap_error(text: str) -> str:
    """Z2M-Fehlertext verständlich: nennt das Gerät, wenn Z2M eins nennt und es nicht antwortet."""
    text = (text or "").strip() or "unbekannter Fehler"
    quoted = _QUOTED.search(text)
    if quoted and _UNREACHABLE.search(text):
        return f"Gerät {quoted.group(1)} nicht erreichbar (Zigbee2MQTT: {text})"
    return f"Zigbee2MQTT meldet: {text}"


def build_networkmap_cache(value: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Cache-Eintrag aus ``data.value`` der Z2M-Response, ``None`` ohne Netzkarte."""
    if not isinstance(value, dict):
        return None
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {"nodes": value.get("nodes", []), "links": value.get("links", []), "timestamp": stamp}


class NetworkmapJob:
    """Zustand der (einen) Netzmessung: läuft, Beginn, letzter Fehler. Reines Python."""

    def __init__(self) -> None:
        self.running = False
        self.started_at: str | None = None
        self.last_error: str | None = None

    def start(self, now: datetime | None = None) -> str:
        """``"started"`` oder ``"running"`` (dann bleibt ``started_at`` des laufenden Laufs)."""
        if self.running:
            return "running"
        self.running = True
        self.started_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        return "started"

    def finish_ok(self) -> None:
        self.running = False
        self.last_error = None

    def finish_error(self, error: str) -> None:
        self.running = False
        self.last_error = error

    def info(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at if self.running else None,
            "last_error": self.last_error,
        }

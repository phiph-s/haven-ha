"""Zigbee2MQTT-Zustand im Speicher, gespeist über die MQTT-Verbindung von HA.

Abonniert ``<base>/bridge/{devices,groups,info,event,state,response/#}`` und
``<base>/+`` (Gerätezustände). Schreibt über ``bridge/request/...`` und wartet auf
die Response. Die Parsing- und Zuordnungslogik ist reines Python,
HA-Importe passieren erst in :meth:`Z2MBridge.async_start`, damit das Modul auch
ohne Home Assistant testbar ist.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
from typing import TYPE_CHECKING, Any

from .commands import (
    DEFAULT_TIMEOUT,
    NETWORKMAP_TIMEOUT,
    NetworkmapJob,
    TransactionTracker,
    Z2MError,
    build_networkmap,
    build_last_seen_option,
    build_networkmap_cache,
    describe_networkmap_error,
    request_topic,
)
from .const import MAX_EVENTS
from .exposes import classify
from .lastseen import (
    availability_enabled,
    last_seen_enabled,
    networkmap_last_seen,
    parse_availability,
    resolve_last_seen,
)
from .statecache import StateCache

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[str, Any], None]


def image_url(model: str | None) -> str | None:
    """Bild-URL aus der Z2M-Gerätedatenbank (gleiche Regel wie Z2M-Frontend)."""
    if not model:
        return None
    safe = model.replace("/", "-").replace(":", "-").replace(" ", "-")
    return f"https://www.zigbee2mqtt.io/images/devices/{safe}.png"


class Z2MBridge:
    """Hält Geräte, Gruppen, Bridge-Info und letzte Events."""

    def __init__(self, hass: HomeAssistant | None, base_topic: str) -> None:
        self.hass = hass
        self.base_topic = base_topic.strip("/")
        self.devices: dict[str, dict[str, Any]] = {}  # ieee -> Rohdaten
        self.groups: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.state: str | None = None  # "online" / "offline"
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.devices_received = False
        self.groups_received = False
        self._classified: dict[str, dict[str, Any]] = {}
        self._listeners: list[Listener] = []
        self._unsubs: list[Callable[[], None]] = []
        self._slash_unsubs: dict[str, Callable[[], None]] = {}
        self._slash_av_unsubs: dict[str, Callable[[], None]] = {}
        self._slash_lock: asyncio.Lock | None = None
        self.tracker = TransactionTracker()
        self.state_cache = StateCache(self.base_topic)
        self.availability: dict[str, str] = {}  # ieee -> "online" | "offline"
        self.networkmap: dict[str, Any] | None = None  # {nodes, links, timestamp}
        self.networkmap_job = NetworkmapJob()
        self._networkmap_task: asyncio.Task[None] | None = None
        # Wird nach jeder Messung (Erfolg oder Fehler) mit dem Ereignis-Payload aufgerufen
        self.on_networkmap_done: Callable[[dict[str, Any]], None] | None = None
        # Wird nach jeder erfolgreichen Messung mit dem neuen Cache aufgerufen (Persistenz)
        self.on_networkmap: Callable[[dict[str, Any]], None] | None = None

    # --- Lebenszyklus --------------------------------------------------------

    async def async_start(self) -> None:
        """Abonniert die Bridge-Topics über die MQTT-Integration von HA."""
        from homeassistant.components import mqtt  # noqa: PLC0415
        from homeassistant.core import callback  # noqa: PLC0415

        assert self.hass is not None
        await mqtt.async_wait_for_mqtt_client(self.hass)

        handlers = {
            "bridge/devices": self.handle_devices,
            "bridge/groups": self.handle_groups,
            "bridge/info": self.handle_info,
            "bridge/event": self.handle_event,
            "bridge/state": self.handle_state,
            "bridge/response/#": self.handle_response,
        }
        for suffix, handler in handlers.items():

            @callback
            def _on_message(msg: Any, _handler: Callable[[str], None] = handler) -> None:
                _handler(msg.payload)

            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass, f"{self.base_topic}/{suffix}", _on_message, qos=0, encoding="utf-8"
                )
            )

        # Gerätezustände: ``<base>/+``; Namen mit ``/`` kommen in _async_sync_slash_subs dazu.
        @callback
        def _on_device_message(msg: Any) -> None:
            self.handle_device_message(msg.topic, msg.payload)

        @callback
        def _on_availability(msg: Any) -> None:
            self.handle_availability_message(msg.topic, msg.payload)

        self._availability_cb = _on_availability
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, self.state_cache.availability_wildcard, _on_availability, qos=0, encoding="utf-8"
            )
        )

        self._device_msg_cb = _on_device_message
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, self.state_cache.wildcard_topic, _on_device_message, qos=0, encoding="utf-8"
            )
        )

    async def _async_sync_slash_subs(self) -> None:
        """Abos für Friendly-Names mit ``/`` an die aktuelle Geräteliste angleichen."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        if self.hass is None or not hasattr(self, "_device_msg_cb"):
            return
        if self._slash_lock is None:
            self._slash_lock = asyncio.Lock()
        async with self._slash_lock:
            wanted = self.state_cache.slash_topics()
            for name in [n for n in self._slash_unsubs if n not in wanted]:
                self._slash_unsubs.pop(name)()
            for name, topic in wanted.items():
                if name not in self._slash_unsubs:
                    self._slash_unsubs[name] = await mqtt.async_subscribe(
                        self.hass, topic, self._device_msg_cb, qos=0, encoding="utf-8"
                    )
            wanted_av = self.state_cache.slash_availability_topics()
            for name in [n for n in self._slash_av_unsubs if n not in wanted_av]:
                self._slash_av_unsubs.pop(name)()
            for name, topic in wanted_av.items():
                if name not in self._slash_av_unsubs:
                    self._slash_av_unsubs[name] = await mqtt.async_subscribe(
                        self.hass, topic, self._availability_cb, qos=0, encoding="utf-8"
                    )

    def stop(self) -> None:
        for unsub in [*self._unsubs, *self._slash_unsubs.values(), *self._slash_av_unsubs.values()]:
            unsub()
        self._unsubs.clear()
        self._slash_unsubs.clear()
        self._slash_av_unsubs.clear()
        self.stop_networkmap()
        self.tracker.cancel_all()
        self._listeners.clear()

    # --- Listener --------------------------------------------------------------

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Registriert einen Listener ``(kind, payload)``. Liefert die Abmeldung."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self, kind: str, payload: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Haven-Listener fehlgeschlagen")

    # --- MQTT-Handler ----------------------------------------------------------

    @staticmethod
    def _parse(payload: str | bytes, topic: str) -> Any | None:
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            _LOGGER.warning("Ungültiges JSON auf %s", topic)
            return None

    def handle_devices(self, payload: str | bytes) -> None:
        data = self._parse(payload, "bridge/devices")
        if not isinstance(data, list):
            return
        self.devices = {
            d["ieee_address"]: d
            for d in data
            if isinstance(d, dict) and d.get("ieee_address") and d.get("type") != "Coordinator"
        }
        self._classified.clear()
        self.devices_received = True
        self.state_cache.set_names({d.get("friendly_name"): i for i, d in self.devices.items() if d.get("friendly_name")})
        for ieee in [i for i in self.availability if i not in self.devices]:
            del self.availability[ieee]
        if self.hass is not None:
            self.hass.async_create_task(self._async_sync_slash_subs())
        self._notify("devices", None)

    def handle_groups(self, payload: str | bytes) -> None:
        data = self._parse(payload, "bridge/groups")
        if not isinstance(data, list):
            return
        self.groups = [g for g in data if isinstance(g, dict)]
        self.groups_received = True
        self._notify("groups", None)

    def handle_info(self, payload: str | bytes) -> None:
        data = self._parse(payload, "bridge/info")
        if isinstance(data, dict):
            self.info = data
            self._notify("info", None)

    def handle_state(self, payload: str | bytes) -> None:
        # Neuere Z2M-Versionen senden {"state": "online"}, ältere den Rohtext.
        data = self._parse(payload, "bridge/state")
        state = data.get("state") if isinstance(data, dict) else (data if isinstance(data, str) else None)
        if state is None and isinstance(payload, str):
            state = payload.strip()
        self.state = state
        self._notify("state", None)

    def handle_event(self, payload: str | bytes) -> None:
        data = self._parse(payload, "bridge/event")
        if isinstance(data, dict):
            self.events.append(data)
            self._notify("event", data)

    def handle_device_message(self, topic: str, payload: str | bytes) -> None:
        """``<base>/<friendly_name>``: merkt den Zustand und meldet ``device_state``."""
        result = self.state_cache.ingest(topic, payload)
        if result is not None:
            ieee, values = result
            self._notify("device_state", {"ieee": ieee, "values": values})

    def handle_availability_message(self, topic: str, payload: str | bytes) -> None:
        """``<base>/<friendly_name>/availability``: merkt ``online``/``offline`` je Gerät."""
        ieee = self.state_cache.availability_ieee(topic)
        state = parse_availability(payload)
        if ieee is None or state is None:
            return
        if self.availability.get(ieee) != state:
            self.availability[ieee] = state
            self._notify("availability", {"ieee": ieee, "availability": state})

    def handle_response(self, payload: str | bytes, topic: str | None = None) -> None:
        """``<base>/bridge/response/...``: ordnet die Antwort ihrer Anfrage zu."""
        data = self._parse(payload, "bridge/response")
        self.tracker.resolve(data)

    # --- Schreiben --------------------------------------------------------------

    async def _async_publish(self, topic: str, payload: str) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        assert self.hass is not None
        await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=False)

    def _check_online(self) -> None:
        if self.state == "offline":
            raise Z2MError("bridge_offline", "Zigbee2MQTT ist offline")

    async def async_publish_set(self, friendly_name: str, values: dict[str, Any]) -> None:
        """``<base>/<friendly_name>/set``, ohne Bestätigung (Z2M antwortet nicht)."""
        self._check_online()
        try:
            await self._async_publish(f"{self.base_topic}/{friendly_name}/set", json.dumps(values))
        except Exception as err:  # noqa: BLE001
            raise Z2MError("mqtt_error", f"MQTT-Senden fehlgeschlagen: {err}") from err

    async def async_request(
        self, endpoint: str, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """Sendet ``bridge/request/<endpoint>`` und liefert ``data`` der Response."""
        self._check_online()
        tid, fut = self.tracker.new()
        try:
            await self._async_publish(
                request_topic(self.base_topic, endpoint), json.dumps({**payload, "transaction": tid})
            )
        except Exception as err:  # noqa: BLE001
            self.tracker.discard(tid)
            raise Z2MError("mqtt_error", f"MQTT-Senden fehlgeschlagen: {err}") from err
        return await self.tracker.wait(tid, fut, timeout, endpoint)

    async def _async_measure_networkmap(self) -> dict[str, Any]:
        """Eine Messung (Job muss gestartet sein). Wirft :class:`Z2MError`."""
        try:
            data = await self.async_request("networkmap", build_networkmap(), NETWORKMAP_TIMEOUT)
            cache = build_networkmap_cache(data.get("value"))
            if cache is None:
                raise Z2MError("z2m_error", "Zigbee2MQTT hat keine Netzwerkkarte geliefert")
        except Z2MError as err:
            if err.code == "z2m_error":
                reason = err.message.split(": ", 1)[-1]
                err = Z2MError("z2m_error", describe_networkmap_error(reason))
            self.networkmap_job.finish_error(err.message)
            raise err
        except BaseException:
            self.networkmap_job.finish_error("Messung abgebrochen")
            raise
        # Teilergebnis (``failed`` bei einzelnen Knoten) zählt als Erfolg
        self.networkmap = cache
        self.networkmap_job.finish_ok()
        if self.on_networkmap is not None:
            self.on_networkmap(cache)
        return cache

    def _fire_networkmap_done(self, payload: dict[str, Any]) -> None:
        if self.on_networkmap_done is not None:
            try:
                self.on_networkmap_done(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Haven-Ereignis der Netzmessung fehlgeschlagen")

    async def _async_networkmap_task(self) -> None:
        try:
            cache = await self._async_measure_networkmap()
        except Z2MError as err:
            self._fire_networkmap_done({"ok": False, "error": err.message})
        except asyncio.CancelledError:
            self._fire_networkmap_done({"ok": False, "error": "Messung abgebrochen"})
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Netzmessung fehlgeschlagen")
            self.networkmap_job.finish_error(f"Interner Fehler: {err}")
            self._fire_networkmap_done({"ok": False, "error": self.networkmap_job.last_error})
        else:
            self._fire_networkmap_done({"ok": True, "timestamp": cache["timestamp"]})

    def start_networkmap(self) -> dict[str, Any]:
        """Startet die Messung als Task und kehrt sofort zurück: ``{status, started_at}``."""
        job = self.networkmap_job
        if job.running:
            return {"status": "running", "started_at": job.started_at}
        self._check_online()
        status = job.start()
        self._networkmap_task = asyncio.get_running_loop().create_task(self._async_networkmap_task())
        return {"status": status, "started_at": job.started_at}

    def networkmap_view(self) -> dict[str, Any]:
        """Cache (oder leere Karte) plus ``running``, ``started_at``, ``last_error``."""
        base = self.networkmap or {"nodes": [], "links": [], "timestamp": None}
        return {**base, **self.networkmap_job.info()}

    async def async_networkmap(self, refresh: bool) -> dict[str, Any] | None:
        """Cache oder (bei ``refresh``) blockierende Messung. Nur eine Messung zur Zeit."""
        if not refresh:
            return self.networkmap
        if self.networkmap_job.running:
            raise Z2MError("busy", "Die Netzwerkkarte wird gerade schon gemessen")
        self.networkmap_job.start()
        return await self._async_measure_networkmap()

    async def async_enable_last_seen(self) -> dict[str, Any]:
        """Schaltet ``advanced.last_seen`` in Z2M ein (``bridge/request/options``)."""
        data = await self.async_request("options", build_last_seen_option())
        return {"ok": True, "restart_required": bool(data.get("restart_required", False))}

    def stop_networkmap(self) -> None:
        if self._networkmap_task is not None and not self._networkmap_task.done():
            self._networkmap_task.cancel()

    @property
    def networkmap_busy(self) -> bool:
        return self.networkmap_job.running

    def device_ref(self, ieee: str) -> dict[str, Any]:
        """Rohdaten eines Geräts oder :class:`Z2MError` ``not_found``."""
        device = self.devices.get(ieee)
        if device is None:
            raise Z2MError("not_found", f"Unbekanntes Zigbee-Gerät {ieee}")
        return device

    # --- Abfragen --------------------------------------------------------------

    def classified(self, ieee: str) -> dict[str, Any] | None:
        """Klassifizierte Exposes eines Geräts (gecacht bis zum nächsten bridge/devices)."""
        device = self.devices.get(ieee)
        if device is None:
            return None
        if ieee not in self._classified:
            definition = device.get("definition") or {}
            self._classified[ieee] = classify(definition.get("exposes") or [], definition.get("model"))
        return self._classified[ieee]

    def device_type(self, ieee: str) -> str:
        classified = self.classified(ieee)
        return classified["device_type"] if classified else "unknown"

    def last_seen_for(self, ieee: str) -> tuple[str | None, str | None]:
        """``(last_seen ISO, Quelle)`` je IEEE, siehe :mod:`lastseen`."""
        state_value = self.state_cache.states.get(ieee, {}).get("last_seen")
        nodes = (self.networkmap or {}).get("nodes")
        return resolve_last_seen(state_value, networkmap_last_seen(nodes).get(ieee))

    def device_summary(self, ieee: str) -> dict[str, Any] | None:
        device = self.devices.get(ieee)
        if device is None:
            return None
        last_seen, last_seen_source = self.last_seen_for(ieee)
        definition = device.get("definition") or {}
        classified = self.classified(ieee) or {}
        return {
            "ieee": ieee,
            "friendly_name": device.get("friendly_name"),
            "type": device.get("type"),
            "vendor": definition.get("vendor"),
            "model": definition.get("model"),
            "description": definition.get("description"),
            "power_source": device.get("power_source"),
            "supported": bool(device.get("supported", bool(definition))),
            "disabled": bool(device.get("disabled", False)),
            "interview_completed": device.get("interview_completed"),
            "image": image_url(definition.get("model")),
            "device_type": classified.get("device_type", "unknown"),
            "capabilities": classified.get("capabilities", []),
            "last_seen": last_seen,
            "last_seen_source": last_seen_source,
            "availability": self.availability.get(ieee),
        }

    def device_detail(self, ieee: str) -> dict[str, Any] | None:
        summary = self.device_summary(ieee)
        if summary is None:
            return None
        classified = self.classified(ieee) or {}
        definition = self.devices[ieee].get("definition") or {}
        summary["classified"] = {
            key: classified.get(key, [])
            for key in ("primary", "settings", "diagnostic", "hidden", "setting_groups")
        }
        summary["options"] = definition.get("options", [])
        return summary

    def devices_summary(self) -> list[dict[str, Any]]:
        out = [s for ieee in self.devices if (s := self.device_summary(ieee))]
        out.sort(key=lambda d: (d["friendly_name"] or "").casefold())
        return out

    def ieee_for(self, ref: str) -> str | None:
        """Löst Friendly-Name oder IEEE zu einer IEEE auf."""
        if ref in self.devices:
            return ref
        for ieee, device in self.devices.items():
            if device.get("friendly_name") == ref:
                return ieee
        return None

    def groups_summary(self) -> list[dict[str, Any]]:
        out = []
        for group in self.groups:
            members = []
            for member in group.get("members", []):
                ref = member.get("ieee_address") if isinstance(member, dict) else member
                ieee = self.ieee_for(str(ref)) if ref else None
                device = self.devices.get(ieee) if ieee else None
                members.append(
                    {
                        "ieee": ieee or ref,
                        "endpoint": member.get("endpoint") if isinstance(member, dict) else None,
                        "friendly_name": device.get("friendly_name") if device else None,
                        "device_type": self.device_type(ieee) if ieee else "unknown",
                    }
                )
            out.append(
                {
                    "id": group.get("id"),
                    "friendly_name": group.get("friendly_name"),
                    "members": members,
                    "scenes": [
                        {"id": s.get("id"), "name": s.get("name")}
                        for s in group.get("scenes", [])
                        if isinstance(s, dict)
                    ],
                }
            )
        out.sort(key=lambda g: (g["friendly_name"] or "").casefold())
        return out

    def group_light_members(self, group: dict[str, Any]) -> set[str]:
        """IEEEs der Mitglieder einer Gruppe, die selbst Lichter sind."""
        result: set[str] = set()
        for member in group.get("members", []):
            ref = member.get("ieee_address") if isinstance(member, dict) else member
            ieee = self.ieee_for(str(ref)) if ref else None
            if ieee and self.device_type(ieee) == "light":
                result.add(ieee)
        return result

    def match_group(self, light_ieees: set[str]) -> dict[str, Any] | None:
        """Findet die Z2M-Gruppe, die zu den Zigbee-Lichtern eines Raums passt.

        ``match``: ``exact`` (Lichter der Gruppe == Lichter des Raums) oder
        ``partial`` (größte Überschneidung, mit ``missing``/``extra``).
        Knöpfe und Sensoren in der Gruppe zählen nicht (Bindings).
        """
        if not light_ieees:
            return None
        best: tuple[int, int, dict[str, Any], set[str]] | None = None
        for group in self.groups:
            members = self.group_light_members(group)
            overlap = len(members & light_ieees)
            if not overlap:
                continue
            extra = len(members - light_ieees)
            key = (overlap, -extra)
            if best is None or key > (best[0], best[1]):
                best = (overlap, -extra, group, members)
        if best is None:
            return None
        _, _, group, members = best
        missing = sorted(light_ieees - members)
        extra_members = sorted(members - light_ieees)
        return {
            "id": group.get("id"),
            "friendly_name": group.get("friendly_name"),
            "match": "exact" if not missing and not extra_members else "partial",
            "missing": missing,
            "extra": extra_members,
        }

    def info_summary(self) -> dict[str, Any]:
        coordinator = self.info.get("coordinator") or {}
        meta = coordinator.get("meta") or {}
        network = self.info.get("network") or {}
        return {
            "base_topic": self.base_topic,
            "bridge_state": self.state,
            "info_received": bool(self.info),
            "version": self.info.get("version"),
            "coordinator_type": coordinator.get("type"),
            "coordinator_revision": meta.get("revision") if isinstance(meta, dict) else None,
            "permit_join": self.info.get("permit_join"),
            "channel": network.get("channel") if isinstance(network, dict) else None,
            "pan_id": network.get("pan_id") if isinstance(network, dict) else None,
            "device_count": len(self.devices),
            "group_count": len(self.groups),
            "devices_received": self.devices_received,
            "groups_received": self.groups_received,
            "last_seen_enabled": last_seen_enabled(self.info),
            "availability_enabled": availability_enabled(self.info),
        }


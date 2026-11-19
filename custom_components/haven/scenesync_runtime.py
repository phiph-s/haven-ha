"""Szenen-Abgleich in Home Assistant: Soll merken, Szene messen, übernehmen, abgleichen.

Die Logik (Vergleich, Status, Dienstdaten) liegt HA-frei in ``scenesync.py``. Hier passiert:

- **Messen** (``async_check``): Zustand aller Mitglieder merken, ``scene_recall`` an die Gruppe,
  kurz warten, dann jede Lampe von Z2M echt lesen lassen (``<friendly_name>/get``). Als Antwort zählt
  eine neue Gerätemeldung von Z2M nach dem ``get``; danach gilt der HA-Zustand der Lampe
  (nicht der Wert, den Z2M nach dem Aufruf aus seinem Gedächtnis meldet). Zum Schluss die gemerkten
  Zustände wiederherstellen und warten, bis HA sie zeigt.
- **Übernehmen** (``async_adopt``): letzte Messung als Soll und Look.
- **Abgleichen** (``async_repair``): alle Mitglieder auf ihr Soll setzen, ``scene_store``,
  vorherigen Zustand zurück, dann messen.

Je Gruppe läuft höchstens ein Befehl zur Zeit (:func:`group_lock`), die übrigen Szenenbefehle
der Gruppe warten darauf.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from . import scenesync as ss
from .commands import Z2MError
from .scenes import find_group, find_scene, plan_recall, plan_store, scene_key, stored

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .z2m import Z2MBridge

_LOGGER = logging.getLogger(__name__)

# Nach scene_recall: Übergänge der Lampen und die Meldungen von Z2M abwarten
RECALL_SETTLE = 2.0
# So lange wartet die Messung auf die erste Antwort jeder Lampe auf ``get``
GET_TIMEOUT = 5.0
# Danach: weitere Lesungen (Helligkeit, Farbe) kommen als eigene Meldungen, bis so lange Ruhe ist …
GET_QUIET = 0.5
# … höchstens aber so lange
GET_QUIET_MAX = 2.0
# Bis HA einen gesetzten Zustand zeigt (je Lampe, alle parallel)
CONFIRM_TIMEOUT = 5.0
# Bis Z2M bridge/groups nach scene_store zeigt
STORE_CONFIRM_TIMEOUT = 5.0
# Nach scene_store, bevor der alte Zustand zurückkommt (die Lampen speichern selbst)
STORE_SETTLE = 1.0

_LOCKS: dict[int, asyncio.Lock] = {}


def group_lock(group_id: int) -> asyncio.Lock:
    """Ein Abgleich, eine Messung oder ein Szenenbefehl je Gruppe zur Zeit."""
    if group_id not in _LOCKS:
        _LOCKS[group_id] = asyncio.Lock()
    return _LOCKS[group_id]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SceneStorage(Protocol):
    """Was der Abgleich von ``HavenStorage`` braucht."""

    async def async_get_scene_looks(self) -> dict[str, Any]: ...
    async def async_set_scene_looks(self, looks: dict[str, Any]) -> None: ...
    async def async_get_scene_targets(self) -> dict[str, Any]: ...
    async def async_set_scene_targets(self, targets: dict[str, Any]) -> None: ...
    async def async_get_scene_checks(self) -> dict[str, Any]: ...
    async def async_set_scene_checks(self, checks: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Member:
    """Licht-Mitglied einer Z2M-Gruppe mit seinem HA-Licht."""

    entity_id: str
    ieee: str
    friendly_name: str  # Z2M (für ``<name>/get``)
    name: str  # Anzeige (HA-Name)
    get_payload: dict[str, str]


class SceneSync:
    """Abläufe des Szenen-Abgleichs für eine Bridge.

    ``ha_lights`` liefert ``ieee -> [entity_id, ...]`` der HA-Entities je Zigbee-Gerät
    (in ``websocket_api`` aus den Registries).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: Z2MBridge,
        storage: SceneStorage,
        ha_lights: Callable[[], Mapping[str, list[str]]],
    ) -> None:
        self.hass = hass
        self.bridge = bridge
        self.storage = storage
        self._ha_lights = ha_lights

    # --- Mitglieder und Zustände ------------------------------------------------------------

    def members(self, group: dict[str, Any]) -> list[Member]:
        out = []
        for ieee, entity_id in ss.map_members(self.bridge.group_light_members(group), self._ha_lights()):
            device = self.bridge.devices.get(ieee) or {}
            state = self.hass.states.get(entity_id)
            name = (state.attributes.get("friendly_name") if state else None) or device.get("friendly_name") or entity_id
            caps = (self.bridge.classified(ieee) or {}).get("capabilities", [])
            out.append(Member(entity_id, ieee, str(device.get("friendly_name") or ieee), str(name), ss.get_payload(caps)))
        return out

    def _raw(self, entity_id: str) -> tuple[str | None, dict[str, Any]]:
        state = self.hass.states.get(entity_id)
        return (state.state, dict(state.attributes)) if state is not None else (None, {})

    def _lamp(self, entity_id: str) -> dict[str, Any] | None:
        return ss.lamp_state_from(*self._raw(entity_id))

    async def _call(self, entity_id: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call("light", service, {"entity_id": entity_id, **data}, blocking=True)
        except Exception:  # noqa: BLE001 - eine Lampe darf den Ablauf nicht abbrechen
            _LOGGER.warning("Haven: %s für %s fehlgeschlagen", service, entity_id, exc_info=True)

    async def _wait_states(self, expected: Mapping[str, Mapping[str, Any]], timeout: float) -> set[str]:
        """Wartet, bis HA für jede Lampe den erwarteten Zustand zeigt. Liefert die bestätigten."""

        def _matches(entity_id: str) -> bool:
            lamp = self._lamp(entity_id)
            return lamp is not None and not ss.lamp_differs(expected[entity_id], lamp)

        pending = {e for e in expected if not _matches(e)}
        if pending:
            done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            @callback
            def _changed(event: Any) -> None:
                entity_id = event.data.get("entity_id")
                if entity_id in pending and _matches(entity_id):
                    pending.discard(entity_id)
                    if not pending and not done.done():
                        done.set_result(None)

            unsub = async_track_state_change_event(self.hass, list(pending), _changed)
            try:
                await asyncio.wait_for(done, timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                unsub()
        return set(expected) - pending

    async def _restore(self, snapshot: Mapping[str, tuple[str | None, dict[str, Any]]]) -> None:
        """Gemerkte HA-Zustände zurück und warten, bis HA sie zeigt."""
        calls = {e: c for e, raw in snapshot.items() if (c := ss.restore_call(*raw)) is not None}
        await asyncio.gather(*(self._call(e, service, data) for e, (service, data) in calls.items()))
        expected = {e: lamp for e in calls if (lamp := ss.lamp_state_from(*snapshot[e])) is not None}
        missing = set(expected) - await self._wait_states(expected, CONFIRM_TIMEOUT)
        if missing:
            _LOGGER.warning("Haven: Zustand nicht wiederhergestellt bei %s", ", ".join(sorted(missing)))

    async def _read_lamps(self, members: list[Member]) -> set[str]:
        """``get`` an jede erreichbare Lampe, liefert die IEEEs, die geantwortet haben.

        ``members``: nur Lampen, die vor dem Aufruf erreichbar waren (``unavailable`` fragt Haven nicht).
        """
        wanted = {m.ieee for m in members if self._lamp(m.entity_id) is not None}
        if not wanted:
            return set()
        loop = asyncio.get_running_loop()
        seen: dict[str, float] = {}  # ieee -> Zeit der letzten Meldung
        all_answered = asyncio.Event()

        def _listener(kind: str, payload: Any) -> None:
            if kind == "device_state" and payload.get("ieee") in wanted:
                seen[payload["ieee"]] = loop.time()
                if len(seen) == len(wanted):
                    all_answered.set()

        remove = self.bridge.add_listener(_listener)
        try:
            await asyncio.gather(
                *(self.bridge.async_publish_get(m.friendly_name, m.get_payload) for m in members if m.ieee in wanted)
            )
            try:
                await asyncio.wait_for(all_answered.wait(), GET_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            deadline = loop.time() + GET_QUIET_MAX
            while seen and (now := loop.time()) < deadline:
                quiet = now - max(seen.values())
                if quiet >= GET_QUIET:
                    break
                await asyncio.sleep(min(GET_QUIET - quiet, deadline - now))
        finally:
            remove()
        return set(seen)

    # --- Soll --------------------------------------------------------------------------------

    async def _save_target(self, key: str, target: dict[str, Any]) -> None:
        targets = dict(await self.storage.async_get_scene_targets())
        targets[key] = target
        await self.storage.async_set_scene_targets(targets)

    async def async_capture_target(self, group_id: int, scene_id: int, source: str = "store") -> dict[str, Any]:
        """Soll aus den aktuellen HA-Zuständen der Mitglieder (nach ``scene_store``)."""
        group = find_group(self.bridge.groups, group_id)
        states = {m.entity_id: self._lamp(m.entity_id) for m in self.members(group)}
        target = ss.build_target(states, self.bridge.group_light_members(group), source, _now())
        await self._save_target(scene_key(group_id, scene_id), target)
        return target

    # --- Messen ------------------------------------------------------------------------------

    async def async_check(self, group_id: int, scene_id: int) -> dict[str, Any]:
        async with group_lock(group_id):
            return await self._check(group_id, scene_id)

    async def _check(self, group_id: int, scene_id: int) -> dict[str, Any]:
        group = find_group(self.bridge.groups, group_id)
        scene = find_scene(group, scene_id)
        key = scene_key(group_id, scene_id)
        target = (await self.storage.async_get_scene_targets()).get(key)
        members = self.members(group)
        snapshot = {m.entity_id: self._raw(m.entity_id) for m in members}
        before = {e: ss.lamp_state_from(*raw) for e, raw in snapshot.items()}
        try:
            await self.bridge.async_publish_set(group["friendly_name"], plan_recall(group, scene_id))
            await asyncio.sleep(RECALL_SETTLE)
            answered = await self._read_lamps([m for m in members if before[m.entity_id] is not None])
            measured = {m.entity_id: self._lamp(m.entity_id) if m.ieee in answered else None for m in members}
        finally:
            await self._restore(snapshot)
        expected = (target or {}).get("members", {})
        rows = [
            {
                "entity_id": m.entity_id,
                "name": m.name,
                "expected": expected.get(m.entity_id) if target else None,
                "before": before[m.entity_id],
                "measured": measured[m.entity_id],
            }
            for m in members
        ]
        result = ss.build_check(group_id, scene_id, scene.get("name"), rows, target is not None, _now())
        checks = dict(await self.storage.async_get_scene_checks())
        checks[key] = result
        await self.storage.async_set_scene_checks(checks)
        return result

    # --- Übernehmen --------------------------------------------------------------------------

    async def async_adopt(self, group_id: int, scene_id: int) -> dict[str, Any]:
        """Letzte Messung (ohne: jetzt messen) wird Soll (``source: "adopt"``) und Look."""
        async with group_lock(group_id):
            key = scene_key(group_id, scene_id)
            find_scene(find_group(self.bridge.groups, group_id), scene_id)
            check = (await self.storage.async_get_scene_checks()).get(key)
            if check is None:
                check = await self._check(group_id, scene_id)
            group = find_group(self.bridge.groups, group_id)
            now = _now()
            states = {row["entity_id"]: row.get("measured") for row in check.get("members", [])}
            await self._save_target(key, ss.build_target(states, self.bridge.group_light_members(group), "adopt", now))
            look = {**check["measured_look"], "stored_at": now}
            looks = dict(await self.storage.async_get_scene_looks())
            looks[key] = look
            await self.storage.async_set_scene_looks(looks)
            return {"ok": True, "look": look}

    # --- Abgleichen --------------------------------------------------------------------------

    async def async_repair(self, group_id: int, scene_id: int) -> dict[str, Any]:
        """Alle Mitglieder aufs Soll (ohne Eintrag: aus), ``scene_store``, zurück, messen."""
        async with group_lock(group_id):
            group = find_group(self.bridge.groups, group_id)
            find_scene(group, scene_id)
            key = scene_key(group_id, scene_id)
            target = (await self.storage.async_get_scene_targets()).get(key)
            if not target:
                raise Z2MError("no_target", "Für diese Szene ist kein Soll gespeichert, erst prüfen und übernehmen")
            members = self.members(group)
            snapshot = {m.entity_id: self._raw(m.entity_id) for m in members}
            desired = {m.entity_id: dict(target.get("members", {}).get(m.entity_id) or ss.OFF) for m in members}
            try:
                reachable = [e for e in desired if ss.lamp_state_from(*snapshot[e]) is not None]
                await asyncio.gather(*(self._call(e, *ss.target_call(desired[e])) for e in reachable))
                confirmed = await self._wait_states({e: desired[e] for e in reachable}, CONFIRM_TIMEOUT)
                if missing := set(reachable) - confirmed:
                    _LOGGER.warning("Haven: Soll nicht bestätigt bei %s", ", ".join(sorted(missing)))
                scene_id, name, payload = plan_store(group, None, scene_id)
                await self.bridge.async_publish_set(group["friendly_name"], payload)
                await self.bridge.async_wait_groups(stored(group_id, scene_id, name), STORE_CONFIRM_TIMEOUT)
                await asyncio.sleep(STORE_SETTLE)
                # Neue Lampen sind jetzt mit „aus“ in der Szene: das Soll umfasst sie
                await self._save_target(
                    key,
                    ss.build_target(desired, self.bridge.group_light_members(group), target.get("source", "store"), _now()),
                )
            finally:
                await self._restore(snapshot)
            return await self._check(group_id, scene_id)

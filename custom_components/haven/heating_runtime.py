"""Laufzeit der Heizungsregelung (``haven.heating``) in Home Assistant.

Die Logik liegt HA-frei in ``heating.py``. Hier passiert nur:

- Store ``.storage/haven.heating`` (Einstellungen je Raum und der Zustand: Ziel, Betriebsart, Gesendetes),
- Eingänge sammeln: TRV-Zustände aus den Z2M-Meldungen (über die Bridge), Raumthermometer, Fensterkontakte
  und Außentemperatur aus HA-Zuständen (State-Abos),
- Auswerten bei jeder Änderung, alle 2 Minuten und zu den Fenster-Verzögerungen (``async_call_later``),
- Befehle an Z2M (``<base>/<name>/set``) und das Ereignis ``haven_heating_updated`` mit dem Status.

Alles, was HA zurückruft (Zeitgeber, State-Abos, Bridge-Listener), ist ``@callback``: Sonst liefe es im
Thread-Pool, und dort lehnt HA ``async_create_task`` und ``bus.async_fire`` ab (siehe Bewegungslicht).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from . import heating as ht
from .commands import Z2MError

if TYPE_CHECKING:
    from .z2m import Z2MBridge

_LOGGER = logging.getLogger(__name__)

EVENT_HEATING_UPDATED = "haven_heating_updated"
TICK_SECONDS = 120
_KEY = "haven_heating"


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class HeatingRunner:
    """Hält die Raumregelungen, hört auf Bridge und HA-Zustände und schickt Befehle an die TRVs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.bridge: Z2MBridge | None = None
        self._store: Store[dict[str, Any]] = Store(hass, ht.STORE_VERSION, ht.STORE_KEY)
        self._data: dict[str, Any] = ht.empty_store()
        self._loaded = False
        self.states: dict[str, ht.HeatingState] = {}
        self._caps: dict[str, ht.TrvCaps] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._timers: dict[str, Callable[[], None]] = {}
        self._tick: Callable[[], None] | None = None
        self._remove_listener: Callable[[], None] | None = None
        self._last_status: dict[str, dict[str, Any]] = {}

    # --- Lebenszyklus -------------------------------------------------------------------

    async def async_load(self) -> None:
        if self._loaded:
            return
        self._data = ht.read_store(await self._store.async_load())
        self.states = {k: ht.HeatingState.from_dict(v) for k, v in self._data["state"].items()}
        self._loaded = True

    @property
    def configs(self) -> dict[str, dict[str, Any]]:
        return self._data["rooms"]

    @callback
    def attach(self, bridge: Z2MBridge) -> None:
        self.detach()
        self.bridge = bridge
        self._remove_listener = bridge.add_listener(self._on_bridge)
        self.sync()
        self._schedule_tick()

    @callback
    def detach(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        self._stop_tracking()
        if self._tick is not None:
            self._tick()
            self._tick = None
        self.bridge = None

    @callback
    def _stop_tracking(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    @callback
    def sync(self) -> None:
        """State-Abos neu aufbauen und alle Räume auswerten (nach Laden, Speichern, Löschen)."""
        self._stop_tracking()
        for gone in [k for k in self.states if k not in self.configs]:
            self.states.pop(gone, None)
            self._last_status.pop(gone, None)
        if self.bridge is None:
            return
        for area, config in self.configs.items():
            entities = [*config["sensors"], *config["windows"]]
            if config.get("outdoor"):
                entities.append(config["outdoor"]["entity"])
            if entities:
                self._unsubs.append(
                    async_track_state_change_event(
                        self.hass, entities, callback(lambda _e, area=area: self.evaluate(area, "sensor"))
                    )
                )
        for area in list(self.configs):
            self.evaluate(area, "sync")

    @callback
    def _schedule_tick(self) -> None:
        self._tick = async_call_later(self.hass, TICK_SECONDS, self._on_tick)  # schon @callback

    @callback
    def _on_tick(self, _now: Any) -> None:
        self._tick = None
        for area in list(self.configs):
            self.evaluate(area, "tick")
        if self.bridge is not None:
            self._schedule_tick()

    # --- Bridge ---------------------------------------------------------------------------

    @callback
    def _on_bridge(self, kind: str, payload: Any) -> None:
        if kind == "devices":
            self._caps.clear()
            for area in list(self.configs):
                self.evaluate(area, "devices")
            return
        if kind == "availability" or kind == "device_state":
            ieee = payload.get("ieee")
            for area, config in self.configs.items():
                if ieee in config["trvs"]:
                    self.evaluate(area, "device")

    def caps(self, ieee: str) -> ht.TrvCaps:
        if ieee not in self._caps:
            device = (self.bridge.devices if self.bridge is not None else {}).get(ieee) or {}
            self._caps[ieee] = ht.trv_caps((device.get("definition") or {}).get("exposes"))
        return self._caps[ieee]

    # --- Eingänge ---------------------------------------------------------------------------

    def _inputs(self, config: dict[str, Any], now: float) -> ht.HeatingInputs:
        bridge = self.bridge
        trvs: dict[str, ht.TrvInput] = {}
        for ieee in config["trvs"]:
            caps = self.caps(ieee)
            device = (bridge.devices if bridge is not None else {}).get(ieee)
            values = bridge.state_cache.states.get(ieee) if bridge is not None else None
            available = device is not None and (bridge.availability.get(ieee) != "offline" if bridge is not None else False)
            trvs[ieee] = ht.TrvInput(
                caps,
                ht.trv_reading(values, caps, available=available),
                (device or {}).get("friendly_name") or ieee,
            )
        sensors: dict[str, float | None] = {}
        for entity in config["sensors"]:
            s = self.hass.states.get(entity)
            if s is None:
                sensors[entity] = None
                continue
            seen = getattr(s, "last_reported", None) or getattr(s, "last_updated", None)
            age = now - seen.timestamp() if isinstance(seen, datetime) else None
            sensors[entity] = ht.sensor_value(s.state, age)
        windows = {e: ht.contact_open((s := self.hass.states.get(e)) and s.state) for e in config["windows"]}
        outdoor = None
        if config.get("outdoor"):
            entity = config["outdoor"]["entity"]
            if (s := self.hass.states.get(entity)) is not None:
                outdoor = ht.outdoor_value(entity.split(".", 1)[0], s.state, dict(s.attributes))
        return ht.HeatingInputs(now=now, trvs=trvs, sensors=sensors, windows=windows, outdoor=outdoor)

    # --- Auswerten --------------------------------------------------------------------------

    @callback
    def evaluate(self, area: str, source: str = "") -> dict[str, dict[str, Any]]:
        """Eine Auswertung: beobachten, planen, senden, Status melden. Liefert die Befehle (für Tests)."""
        config = self.configs.get(area)
        if config is None or self.bridge is None:
            return {}
        now = time.time()
        inputs = self._inputs(config, now)
        before = self.states.get(area) or ht.HeatingState()
        state, events = ht.observe(config, before, inputs)
        state, commands = ht.plan(config, state, inputs)
        self.states[area] = state
        if state != before:
            self._data["state"][area] = state.to_dict()
            self._store.async_delay_save(lambda: self._data, 5)
        for event in events:
            _LOGGER.debug("Haven-Heizung %s: %s (%s)", area, event, source)
        for ieee, values in commands.items():
            name = inputs.trvs[ieee].name
            self.hass.async_create_task(self._async_send(name, values))
        self._schedule_wake(area, config, state)
        status = self.status(area, inputs=inputs)
        if status != self._last_status.get(area):
            self._last_status[area] = status
            self.hass.bus.async_fire(EVENT_HEATING_UPDATED, {"area_id": area, "status": status})
        return commands

    async def _async_send(self, friendly_name: str, values: dict[str, Any]) -> None:
        if self.bridge is None:
            return
        try:
            await self.bridge.async_publish_set(friendly_name, values)
        except Z2MError as err:
            _LOGGER.warning("Haven-Heizung: %s nicht gesendet: %s", friendly_name, err.message)

    @callback
    def _schedule_wake(self, area: str, config: dict[str, Any], state: ht.HeatingState) -> None:
        if cancel := self._timers.pop(area, None):
            cancel()
        wake = ht.next_wake(config, state)
        if wake is None:
            return
        delay = max(0.0, wake - time.time()) + 0.05
        self._timers[area] = async_call_later(
            self.hass, delay, callback(lambda _now, area=area: self.evaluate(area, "timer"))
        )

    # --- Abfragen und Ändern -------------------------------------------------------------

    def status(self, area: str, *, inputs: ht.HeatingInputs | None = None) -> dict[str, Any] | None:
        config = self.configs.get(area)
        if config is None:
            return None
        state = self.states.get(area) or ht.HeatingState()
        if inputs is None:
            inputs = self._inputs(config, time.time())
        out = ht.status(config, state, inputs)
        out["running"] = self.bridge is not None
        window = out["window"]
        window["pause_at"], window["resume_at"] = _iso(window["pause_at"]), _iso(window["resume_at"])
        for trv in out["trvs"]:
            trv["release_until"] = _iso(trv["release_until"])
        return out

    def list_public(self) -> list[dict[str, Any]]:
        return [{"config": dict(c), "status": self.status(area)} for area, c in sorted(self.configs.items())]

    def _thermostats(self) -> dict[str, str] | None:
        bridge = self.bridge
        if bridge is None or not bridge.devices_received:
            return None
        return {
            ieee: d.get("friendly_name") or ieee
            for ieee, d in bridge.devices.items()
            if bridge.device_type(ieee) == "thermostat"
        }

    async def async_set(self, doc: dict[str, Any], known_areas: set[str] | None = None) -> dict[str, Any]:
        config = ht.validate_config(doc, self.configs, thermostats=self._thermostats(), known_areas=known_areas)
        area = config["area_id"]
        previous = self.configs.get(area)
        self.configs[area] = config
        await self._store.async_save(self._data)
        if previous is not None:
            self._reset_calibration(area, [i for i in previous["trvs"] if i not in config["trvs"]])
        self.sync()
        return {"config": dict(config), "status": self.status(area)}

    async def async_delete(self, area: str) -> bool:
        config = self.configs.pop(area, None)
        if config is None:
            return False
        self._reset_calibration(area, config["trvs"])
        self._data["state"].pop(area, None)
        self.states.pop(area, None)
        await self._store.async_save(self._data)
        self.sync()
        return True

    @callback
    def _reset_calibration(self, area: str, trvs: list[str]) -> None:
        """TRVs, die Haven nicht mehr regelt: eigene Kalibrierung auf 0 zurücknehmen."""
        state = self.states.get(area)
        if state is None or self.bridge is None:
            return
        for ieee in trvs:
            memo = state.trvs.get(ieee)
            caps = self.caps(ieee)
            device = self.bridge.devices.get(ieee)
            if memo is None or device is None or not caps.calibration or memo.sent_calibration in (None, 0.0):
                continue
            self.hass.async_create_task(self._async_send(device.get("friendly_name") or ieee, {caps.calibration: 0}))

    async def async_control(
        self, area: str, *, target: Any = None, mode: Any = None, resume: bool = False
    ) -> dict[str, Any]:
        """Ziel/Betriebsart aus der App; ``resume`` übernimmt nur freigegebene TRVs wieder."""
        if area not in self.configs:
            raise Z2MError("not_found", f"Für {area} ist keine Heizungsregelung eingerichtet")
        if target is None and mode is None and not resume:
            raise Z2MError("invalid_format", "target, mode oder resume fehlt")
        state = self.states.get(area) or ht.HeatingState()
        self.states[area] = ht.control(state, target=target, mode=mode, now=time.time())
        self._data["state"][area] = self.states[area].to_dict()
        self._store.async_delay_save(lambda: self._data, 2)
        self.evaluate(area, "app")
        return self.status(area) or {}


def get_heating(hass: HomeAssistant) -> HeatingRunner:
    if _KEY not in hass.data:
        hass.data[_KEY] = HeatingRunner(hass)
    return hass.data[_KEY]


async def async_setup_heating(hass: HomeAssistant, bridge: Z2MBridge) -> Callable[[], None]:
    """Beim Laden des Entries: Store lesen, an die Bridge hängen. Liefert die Abmeldung."""
    runner = get_heating(hass)
    await runner.async_load()
    runner.attach(bridge)
    return runner.detach


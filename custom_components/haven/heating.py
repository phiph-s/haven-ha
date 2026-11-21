"""Heizungsregelung je Raum (``haven.heating``): Optionen, Fähigkeiten und Regler ohne Home Assistant.

Haven übernimmt hier, was sonst die Integration Better Thermostat macht: Die Heizkörperthermostate (TRV)
eines Raums laufen als Gruppe mit einem gemeinsamen Ziel und regeln nach einem Raumthermometer statt nach
ihrem eigenen Fühler am Heizkörper. Dazu kommen Fensterkontakte (Pause), die Außentemperatur
(Sommerpause) und Betriebsarten des Raums (Komfort, Eco, Abwesend, Aus).

Korrektur (``calibration``):

``offset``  Das TRV bekommt die Abweichung als ``local_temperature_calibration``. Es misst dann so, als hinge
            es im Raum, und regelt mit seinem eigenen Regler auf das Ziel. Leise und batterieschonend.
``target``  Der Sollwert am TRV wird um die Abweichung verschoben: Ziel + (TRV-Messung − Raum).
            Für Geräte ohne Kalibrierung.
``none``    Keine Korrektur, nur Gruppe, Fenster, Sommer und Betriebsarten.

Die Korrektur folgt gedämpft: Raum und TRV-Messung werden geglättet (``CAL_TAU``), je Schritt ändert sich die
Kalibrierung höchstens um ``CAL_MAX_STEP``, und kurz nachdem das Ventil umgeschaltet hat (der Fühler am
Heizkörper wird warm oder kühlt ab) wird nicht nachgeführt.

Regelmodus am Gerät: ``manual`` (Preset), sonst ``system_mode: heat``, sonst ``comfort`` (Preset). Nie ``on``:
das heißt bei Tuya-Ventilen „Ventil dauerhaft offen“.

Bedienung am Gerät: Eine neue Temperatur gilt für den ganzen Raum, ein Preset Komfort/Eco/Frostschutz/Aus wird
zur Betriebsart des Raums. Stellt jemand das Gerät auf ``on`` (Ventil offen) oder ``auto`` (eigener Wochenplan),
gibt Haven dieses TRV frei und schickt ihm weder Modus noch Sollwert, bis jemand in der App wieder übernimmt
(``on`` holt Haven nach ``BOOST_RETURN`` von selbst zurück). Die Kalibrierung läuft auf Wunsch weiter.

Display (``display``): Ein Wandthermostat mit Anzeige (z. B. Beok BOT-R9V-ZB), das als reines Display markiert
ist (``displays`` im Store), regelt Haven nie als Heizkörperthermostat und kalibriert es nie. Es ist die
Bedienung des Raums: Sein Sollwert wird das Ziel des Raums (auch mit ``lock_manual`` und in Fenster- oder
Sommerpause), Aus/Ein am Display schaltet die Betriebsart des Raums. Haven spiegelt das wirksame Ziel (auch
Eco/Abwesend) auf das Display und schaltet es bei Betriebsart Aus und in der Fensterpause ab, nicht im Sommer.
Wer das Display in der Fensterpause einschaltet, heizt trotz offenem Fenster (``window_override``): Die Pause
endet, Betriebsart und Ziel bleiben, und erst wenn alle Fenster zu sind, pausiert die nächste Öffnung wieder.
Hat das Display neben ``system_mode`` ein eigenes ``preset`` (Beok: manual/eco/auto), ist Eco am Display die
Betriebsart Eco des Raums und umgekehrt; das Gerät regelt dann nach seiner Eco-Temperatur, die Haven gleich der
Eco-Temperatur des Raums hält. Zurück auf Manuell am Display heißt ``heat`` mit dem gemeldeten Sollwert. Den
eigenen Wochenplan des Geräts (``auto``) stellt Haven auf Manuell bzw. Eco zurück, Haven hat den Plan. Bei
Displays mit Modus über ``preset`` wird ``auto``/Wochenplan ignoriert. Seine Messung zählt wie ein
Raumthermometer (Quelle ``display``, wenn sonst kein Thermometer gilt).

Ablauf je Auswertung (Zeitgeber, TRV-Meldung, Sensor-Änderung)::

    state, events = observe(options, state, inputs)   # Fenster, Sommer, Änderungen am Gerät übernehmen
    state, commands = plan(options, state, inputs)    # was an welches TRV muss (Z2M ``set``)
    status(options, state, inputs)                    # für die App

Alles ist eine reine Funktion. Zeiten sind Unix-Sekunden. Geprüft in ``tests/test_heating.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import math
import re
from typing import Any

from .automations import _is_int, _is_num
from .commands import Z2MError

# --- Konstanten ---------------------------------------------------------------------

MODES = ("heat", "comfort", "eco", "away", "off")
PRESET_MODES = ("comfort", "eco", "away")
CALIBRATIONS = ("offset", "target", "none")

DEFAULT_TARGET = 21.0
DEFAULT_PRESETS = {"comfort": 21.0, "eco": 18.0, "away": 16.0}
DEFAULT_WINDOW_OPEN_DELAY = 60  # Sekunden offen bis zur Pause
DEFAULT_WINDOW_CLOSE_DELAY = 120  # Sekunden zu, bis wieder geheizt wird
DEFAULT_OUTDOOR_THRESHOLD = 16.0

TARGET_MIN, TARGET_MAX = 5.0, 30.0
MAX_TRVS = 8
MAX_SENSORS = 8
MAX_WINDOWS = 16

SENSOR_MAX_AGE = 4 * 3600  # ältere Raumwerte gelten nicht mehr
CAL_DEADBAND = 0.3  # erst ab dieser Abweichung neu kalibrieren
CAL_INTERVAL = 600  # höchstens alle 10 min neu kalibrieren
CAL_SETTLE = 1800  # so lange nach einer Kalibrierung auf eine neue TRV-Messung warten
CAL_DETECT = 0.8  # ab dieser Änderung der Kalibrierung prüfen, ob die Messung mitspringt
SHIFT_INTERVAL = 600  # verschobener Sollwert (``target``) höchstens alle 10 min
RESEND = 300  # unbestätigte Befehle nach 5 min erneut senden
MODE_SETTLE = 60  # nach einem eigenen Moduswechsel Sollwert-Meldungen nicht als Handarbeit werten
OUTDOOR_TAU = 4 * 3600  # Glättung der Außentemperatur
OUTDOOR_HYSTERESIS = 1.0
OUTDOOR_RESET = 12 * 3600  # nach so langer Lücke neu anfangen
EPS = 0.05
CAL_TAU = 900  # Glättung der Abweichung Raum − TRV (s)
CAL_MAX_STEP = 0.5  # höchstens so viel Kalibrierung je Schritt
VALVE_JUMP = 25  # Ventil ändert sich um so viele Prozentpunkte: umgeschaltet
VALVE_SETTLE = 600  # danach so lange nicht nachkalibrieren
BOOST_RETURN = 45 * 60  # Ventil offen am Gerät: danach übernimmt Haven wieder

# Preset-Werte, mit denen das Gerät selbst steuert (Haven gibt es frei) bzw. die nur kurz gelten
_SELF_CONTROL = ("on", "boost", "auto", "schedule", "program", "programming")
_BOOST = ("on", "boost")

_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# Z2M-Werte des ``preset`` bzw. ``system_mode`` -> Betriebsart des Raums (Änderung am Gerät)
_PRESET_TO_MODE = {
    "off": "off",
    "manual": "heat",
    "heat": "heat",
    "comfort": "comfort",
    "eco": "eco",
    "antifrost": "away",
    "away": "away",
    "holiday": "away",
}


# --- Optionen ---------------------------------------------------------------------------


def default_options() -> dict[str, Any]:
    return {
        "enabled": True,
        "trvs": [],
        "sensors": [],
        "windows": [],
        "window_open_delay": DEFAULT_WINDOW_OPEN_DELAY,
        "window_close_delay": DEFAULT_WINDOW_CLOSE_DELAY,
        "calibration": "offset",
        "outdoor": None,  # {"entity": "sensor.x" | "weather.x", "threshold": 16.0}
        "presets": dict(DEFAULT_PRESETS),
        "lock_manual": False,
        "calibrate_released": True,  # freigegebene TRVs weiter kalibrieren
        "display": None,  # IEEE eines als Display markierten Wandthermostats (Bedienung des Raums)
    }


def normalize_options(doc: Any) -> dict[str, Any]:
    """Standardwerte ergänzen. Unbekannte Schlüssel fallen weg."""
    src = doc if isinstance(doc, dict) else {}
    out = default_options()
    for key in out:
        if key in src:
            out[key] = src[key]
    out["presets"] = {**DEFAULT_PRESETS, **(src.get("presets") if isinstance(src.get("presets"), dict) else {})}
    for key in ("trvs", "sensors", "windows"):
        if not isinstance(out[key], list):
            out[key] = []
    if isinstance(out["outdoor"], dict):
        out["outdoor"] = {"threshold": DEFAULT_OUTDOOR_THRESHOLD, **out["outdoor"]}
    if not (isinstance(out["display"], str) and out["display"]):
        out["display"] = None
    out["area_id"] = src.get("area_id")
    return out


def _entity_list(value: Any, domains: tuple[str, ...], label: str, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        return [f"{label} muss eine Liste mit höchstens {limit} Einträgen sein"]
    if len(set(value)) != len(value):
        return [f"{label} enthält doppelte Einträge"]
    for v in value:
        if not (isinstance(v, str) and _ENTITY.match(v) and v.split(".", 1)[0] in domains):
            return [f"{label}: {v!r} ist keine {' oder '.join(d + '.' for d in domains)}-Entity"]
    return []


def validate_shape(doc: Any) -> list[str]:
    """Form einer Raumregelung (ohne Kontext)."""
    if not isinstance(doc, dict):
        return ["Die Regelung muss ein Objekt sein"]
    errors: list[str] = []
    if not isinstance(doc.get("area_id"), str) or not doc["area_id"]:
        errors.append("area_id fehlt")
    if "enabled" in doc and not isinstance(doc["enabled"], bool):
        errors.append("enabled muss wahr oder falsch sein")
    trvs = doc.get("trvs", [])
    if not isinstance(trvs, list) or not 1 <= len(trvs) <= MAX_TRVS or not all(isinstance(t, str) and t for t in trvs):
        errors.append(f"trvs braucht 1 bis {MAX_TRVS} Thermostate")
    elif len(set(trvs)) != len(trvs):
        errors.append("trvs enthält doppelte Einträge")
    errors += _entity_list(doc.get("sensors", []), ("sensor",), "sensors", MAX_SENSORS)
    errors += _entity_list(doc.get("windows", []), ("binary_sensor",), "windows", MAX_WINDOWS)
    for key, hi in (("window_open_delay", 1800), ("window_close_delay", 3600)):
        v = doc.get(key, 0)
        if not _is_int(v) or not 0 <= v <= hi:
            errors.append(f"{key} muss zwischen 0 und {hi} Sekunden liegen")
    if doc.get("calibration", "offset") not in CALIBRATIONS:
        errors.append("calibration muss offset, target oder none sein")
    outdoor = doc.get("outdoor")
    if outdoor is not None:
        if not isinstance(outdoor, dict):
            errors.append("outdoor muss ein Objekt oder leer sein")
        else:
            entity = outdoor.get("entity")
            if not (isinstance(entity, str) and _ENTITY.match(entity) and entity.split(".", 1)[0] in ("sensor", "weather")):
                errors.append("outdoor.entity muss ein sensor. oder weather. sein")
            th = outdoor.get("threshold", DEFAULT_OUTDOOR_THRESHOLD)
            if not _is_num(th) or not 5 <= th <= 30:
                errors.append("outdoor.threshold muss zwischen 5 und 30 °C liegen")
    presets = doc.get("presets")
    if presets is not None:
        if not isinstance(presets, dict):
            errors.append("presets muss ein Objekt sein")
        else:
            for key, v in presets.items():
                if key not in PRESET_MODES:
                    errors.append(f"presets: unbekannte Betriebsart {key}")
                elif not _is_num(v) or not TARGET_MIN <= v <= TARGET_MAX:
                    errors.append(f"presets.{key} muss zwischen 5 und 30 °C liegen")
    for key in ("lock_manual", "calibrate_released"):
        if key in doc and not isinstance(doc[key], bool):
            errors.append(f"{key} muss wahr oder falsch sein")
    display = doc.get("display")
    if display is not None:
        if not (isinstance(display, str) and display):
            errors.append("display muss ein Thermostat (IEEE) oder leer sein")
        elif isinstance(trvs, list) and display in trvs:
            errors.append("display darf nicht zugleich unter trvs stehen")
    return errors


def validate_config(
    doc: Any,
    configs: dict[str, dict[str, Any]],
    *,
    thermostats: dict[str, str] | None = None,
    known_areas: set[str] | None = None,
    displays: set[str] | None = None,
) -> dict[str, Any]:
    """Prüft und normalisiert eine Raumregelung. Fehler als :class:`Z2MError` ``invalid_format``/``device_taken``.

    ``thermostats``: IEEE -> Name aller Zigbee-Thermostate (``None`` = Geräteliste noch nicht da, nicht prüfen).
    ``displays``: als Display markierte Thermostate (``None`` = nicht prüfen). Ein TRV gehört höchstens zu einem
    Raum, ein Display ebenso; ein markiertes Display ist nie ein TRV.
    """
    errors = validate_shape(doc)
    if errors:
        raise Z2MError("invalid_format", "; ".join(errors))
    config = normalize_options(doc)
    area = config["area_id"]
    if known_areas is not None and area not in known_areas:
        raise Z2MError("not_found", f"Unbekannter Raum {area}")
    if thermostats is not None:
        for ieee in config["trvs"]:
            if ieee not in thermostats:
                raise Z2MError("not_found", f"{ieee} ist kein bekanntes Thermostat")

    def name(ieee: str) -> str:
        return (thermostats or {}).get(ieee) or ieee

    display = config["display"]
    if displays is not None:
        for ieee in config["trvs"]:
            if ieee in displays:
                raise Z2MError("device_taken", f"{name(ieee)} ist als Display markiert und kein Heizkörperthermostat")
        if display is not None and display not in displays:
            raise Z2MError("invalid_format", f"{name(display)} ist nicht als Display markiert")
    for other_area, other in configs.items():
        if other_area == area:
            continue
        taken = set(other.get("trvs", [])) & set(config["trvs"])
        if taken:
            raise Z2MError("device_taken", f"{name(next(iter(taken)))} wird schon in einem anderen Raum geregelt")
        if display is not None and other.get("display") == display:
            raise Z2MError("device_taken", f"{name(display)} ist schon das Display eines anderen Raums")
    return config


# --- Fähigkeiten eines TRV (aus den Z2M-Exposes) -----------------------------------------


@dataclass
class TrvCaps:
    """Was Haven an einem TRV schreiben kann. ``mode_prop`` ``None``: nur Sollwert."""

    setpoint: str = "current_heating_setpoint"
    min: float = 5.0
    max: float = 30.0
    step: float = 0.5
    calibration: str | None = None
    cal_min: float = -10.0
    cal_max: float = 10.0
    cal_step: float = 0.1
    mode_prop: str | None = None
    on_value: str | None = None
    off_value: str | None = None
    release_values: tuple[str, ...] = ()  # Gerät steuert selbst (Haven gibt frei)
    boost_values: tuple[str, ...] = ()  # davon nur kurz (Ventil offen)
    comfort_temp: str | None = None  # Komfort-Temperatur am Gerät (Regelmodus ``comfort``)
    hysteresis: str | None = None  # Ventilverhalten, ``comfort`` = stufenlos
    # Nur Displays mit Ein/Aus über ``system_mode`` und eigenem ``preset`` (siehe :func:`display_caps`)
    preset_prop: str | None = None
    preset_manual: str | None = None
    preset_eco: str | None = None
    preset_self: tuple[str, ...] = ()  # eigener Wochenplan des Geräts (Haven stellt zurück)
    eco_temp_prop: str | None = None  # Eco-Temperatur am Gerät (gilt im Preset Eco statt des Sollwerts)
    eco_min: float = 5.0
    eco_max: float = 30.0
    eco_step: float = 0.5


def _flatten(exposes: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in exposes if isinstance(exposes, list) else []:
        if not isinstance(e, dict):
            continue
        out.append(e)
        out += _flatten(e.get("features"))
    return out


def _writable(e: dict[str, Any]) -> bool:
    return bool(int(e.get("access", 0) or 0) & 2)


def trv_caps(exposes: Any) -> TrvCaps:
    """Fähigkeiten aus den Exposes (``definition.exposes`` von ``bridge/devices``)."""
    by = {e.get("property"): e for e in _flatten(exposes) if e.get("property")}
    caps = TrvCaps()
    for prop in ("current_heating_setpoint", "occupied_heating_setpoint"):
        e = by.get(prop)
        if e is not None and _writable(e):
            caps.setpoint = prop
            caps.min = float(e.get("value_min", caps.min))
            caps.max = float(e.get("value_max", caps.max))
            caps.step = float(e.get("value_step", caps.step) or caps.step)
            break
    cal = by.get("local_temperature_calibration")
    if cal is not None and _writable(cal):
        caps.calibration = "local_temperature_calibration"
        caps.cal_min = float(cal.get("value_min", caps.cal_min))
        caps.cal_max = float(cal.get("value_max", caps.cal_max))
        caps.cal_step = float(cal.get("value_step", caps.cal_step) or caps.cal_step)
    preset = by.get("preset")
    preset_values = [str(v) for v in (preset or {}).get("values") or []]
    system = by.get("system_mode")
    system_values = [str(v) for v in (system or {}).get("values") or []]
    preset_ok = preset is not None and _writable(preset)
    system_ok = system is not None and _writable(system) and "heat" in system_values
    # Regelmodus: nie ``on`` (bei Tuya „Ventil dauerhaft offen“)
    if preset_ok and "manual" in preset_values:
        caps.mode_prop, caps.on_value = "preset", "manual"
    elif system_ok:
        caps.mode_prop, caps.on_value = "system_mode", "heat"
    elif preset_ok and "comfort" in preset_values:
        caps.mode_prop, caps.on_value = "preset", "comfort"
    elif preset_ok and "heat" in preset_values:
        caps.mode_prop, caps.on_value = "preset", "heat"
    if caps.mode_prop == "preset":
        caps.off_value = "off" if "off" in preset_values else None
        values = preset_values
    elif caps.mode_prop == "system_mode":
        caps.off_value = "off" if "off" in system_values else None
        values = system_values
    else:
        values = []
    caps.release_values = tuple(v for v in values if v in _SELF_CONTROL and v != caps.on_value)
    caps.boost_values = tuple(v for v in caps.release_values if v in _BOOST)
    comfort = by.get("comfort_temperature")
    if caps.on_value == "comfort" and comfort is not None and _writable(comfort):
        caps.comfort_temp = "comfort_temperature"
    hyst = by.get("hysteresis")
    if hyst is not None and _writable(hyst) and "comfort" in [str(v) for v in hyst.get("values") or []]:
        caps.hysteresis = "hysteresis"
    return caps


def display_caps(exposes: Any) -> TrvCaps:
    """Fähigkeiten eines Displays: wie :func:`trv_caps`, aber Ein/Aus über ``system_mode``, wenn der
    Regelmodus sonst kein Aus kennt (Tuya-Wandthermostate: ``preset`` manual/auto, ``system_mode`` heat/off)."""
    caps = trv_caps(exposes)
    if caps.off_value is not None:
        return caps
    system = {e.get("property"): e for e in _flatten(exposes) if e.get("property")}.get("system_mode")
    values = [str(v) for v in (system or {}).get("values") or []]
    if system is not None and _writable(system) and "heat" in values and "off" in values:
        caps.mode_prop, caps.on_value, caps.off_value = "system_mode", "heat", "off"
        caps.release_values = tuple(v for v in values if v in _SELF_CONTROL)
        caps.boost_values = tuple(v for v in caps.release_values if v in _BOOST)
        caps.comfort_temp = None
    if caps.mode_prop == "system_mode":
        _display_presets(caps, exposes)
    return caps


# Werte des ``preset`` eines Displays, mit denen es nach eigenem Wochenplan regelt
_DISPLAY_SELF = ("auto", "schedule", "program")


def _display_presets(caps: TrvCaps, exposes: Any) -> None:
    """Preset eines Displays neben ``system_mode`` (Beok: manual/eco/auto) und dessen Eco-Temperatur."""
    by = {e.get("property"): e for e in _flatten(exposes) if e.get("property")}
    preset = by.get("preset")
    values = [str(v) for v in (preset or {}).get("values") or []]
    if preset is None or not _writable(preset) or "manual" not in values:
        return
    caps.preset_prop, caps.preset_manual = "preset", "manual"
    caps.preset_eco = "eco" if "eco" in values else None
    caps.preset_self = tuple(v for v in values if v in _DISPLAY_SELF)
    eco = by.get("eco_temperature")
    if caps.preset_eco and eco is not None and _writable(eco) and eco.get("type", "numeric") == "numeric":
        caps.eco_temp_prop = "eco_temperature"
        caps.eco_min = float(eco.get("value_min", caps.eco_min))
        caps.eco_max = float(eco.get("value_max", caps.eco_max))
        caps.eco_step = float(eco.get("value_step", caps.eco_step) or caps.eco_step)


@dataclass
class TrvReading:
    """Letzter Zustand eines TRV (aus der Z2M-Meldung)."""

    local: float | None = None
    setpoint: float | None = None
    calibration: float | None = None
    mode: str | None = None
    heating: bool | None = None
    valve: int | None = None
    available: bool = True
    comfort_temp: float | None = None
    hysteresis: str | None = None
    preset: str | None = None  # nur Displays mit eigenem Preset (``caps.preset_prop``)
    eco_temp: float | None = None
    battery: int | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def trv_reading(values: dict[str, Any] | None, caps: TrvCaps, *, available: bool = True) -> TrvReading:
    v = values or {}
    running = v.get("running_state")
    valve = _num(v.get("position", v.get("valve_position", v.get("pi_heating_demand"))))
    battery = _num(v.get("battery"))
    return TrvReading(
        local=_num(v.get("local_temperature")),
        setpoint=_num(v.get(caps.setpoint)),
        calibration=_num(v.get(caps.calibration)) if caps.calibration else None,
        mode=str(v[caps.mode_prop]) if caps.mode_prop and v.get(caps.mode_prop) is not None else None,
        heating=None if running is None else str(running).lower() in ("heat", "heating"),
        valve=None if valve is None else round(valve),
        available=available,
        comfort_temp=_num(v.get(caps.comfort_temp)) if caps.comfort_temp else None,
        hysteresis=str(v[caps.hysteresis]) if caps.hysteresis and v.get(caps.hysteresis) is not None else None,
        preset=str(v[caps.preset_prop]) if caps.preset_prop and v.get(caps.preset_prop) is not None else None,
        eco_temp=_num(v.get(caps.eco_temp_prop)) if caps.eco_temp_prop else None,
        battery=None if battery is None else round(battery),
    )


@dataclass
class TrvInput:
    caps: TrvCaps
    reading: TrvReading
    name: str = ""


@dataclass
class HeatingInputs:
    """Umgebung einer Auswertung. ``None`` bei Sensoren = ungültig, bei Fenstern = unbekannt (zählt als zu)."""

    now: float
    trvs: dict[str, TrvInput] = field(default_factory=dict)
    sensors: dict[str, float | None] = field(default_factory=dict)
    windows: dict[str, bool | None] = field(default_factory=dict)
    outdoor: float | None = None
    display: TrvInput | None = None  # Display des Raums (``options["display"]``)


# --- Zustand -------------------------------------------------------------------------------


@dataclass
class TrvMemo:
    """Was Haven über ein TRV weiß: zuletzt gesehen und zuletzt gesendet."""

    seen_setpoint: float | None = None
    seen_mode: str | None = None
    seen_calibration: float | None = None
    seen_local: float | None = None
    sent_setpoint: float | None = None
    sent_setpoint_at: float | None = None
    prev_sent_setpoint: float | None = None  # davor gesendet (späte Bestätigung zählt nicht als Handarbeit)
    sent_mode: str | None = None
    sent_mode_at: float | None = None
    sent_calibration: float | None = None
    sent_calibration_at: float | None = None
    cal_changed_at: float | None = None  # Kalibrierung geändert, neue TRV-Messung steht aus
    shift: float | None = None  # zuletzt gesendete Verschiebung (``target``)
    cal_included: bool | None = None  # gemeldete Temperatur enthält die Kalibrierung (None = noch nicht gesehen, gilt als ja)
    cal_ref_local: float | None = None
    cal_ref_delta: float | None = None
    corr_ema: float | None = None  # geglättete Abweichung Raum − TRV-Rohmessung
    corr_at: float | None = None
    seen_valve: int | None = None
    valve_changed_at: float | None = None  # Ventil hat umgeschaltet (Fühler am Heizkörper ändert sich)
    released_at: float | None = None  # am Gerät auf eigene Steuerung gestellt, Haven hält still
    released_mode: str | None = None
    release_until: float | None = None  # Ventil offen: so lange, dann übernimmt Haven wieder
    sent_comfort_at: float | None = None
    sent_hysteresis_at: float | None = None
    # nur Displays: Preset und Eco-Temperatur
    seen_preset: str | None = None
    sent_preset: str | None = None
    sent_preset_at: float | None = None
    sent_eco_temp: float | None = None
    sent_eco_temp_at: float | None = None


@dataclass
class HeatingState:
    target: float = DEFAULT_TARGET
    mode: str = "heat"
    changed_at: float | None = None  # letzte Änderung von Ziel/Betriebsart (App oder Gerät)
    window_open_since: float | None = None
    window_closed_since: float | None = None
    paused: bool = False
    window_override: bool = False  # am Display trotz offenem Fenster eingeschaltet: keine Pause, bis alle zu sind
    summer: bool = False
    outdoor_ema: float | None = None
    outdoor_at: float | None = None
    trvs: dict[str, TrvMemo] = field(default_factory=dict)
    display: TrvMemo | None = None  # was Haven über das Display weiß (gilt für ``display_ieee``)
    display_ieee: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> HeatingState:
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f not in ("trvs", "display")}
        state = cls(**{k: v for k, v in data.items() if k in known})
        if state.mode not in MODES:
            state.mode = "heat"
        if not _is_num(state.target):
            state.target = DEFAULT_TARGET
        state.window_override = state.window_override is True
        memo_fields = set(TrvMemo.__dataclass_fields__)
        trvs = data.get("trvs") if isinstance(data.get("trvs"), dict) else {}
        state.trvs = {
            ieee: TrvMemo(**{k: v for k, v in m.items() if k in memo_fields}) for ieee, m in trvs.items() if isinstance(m, dict)
        }
        display = data.get("display")
        if isinstance(display, dict) and isinstance(state.display_ieee, str):
            state.display = TrvMemo(**{k: v for k, v in display.items() if k in memo_fields})
        else:
            state.display, state.display_ieee = None, None
        return state


def _copy(state: HeatingState) -> HeatingState:
    return replace(
        state,
        trvs={k: replace(v) for k, v in state.trvs.items()},
        display=replace(state.display) if state.display is not None else None,
    )


def clamp_target(v: float) -> float:
    return round(min(TARGET_MAX, max(TARGET_MIN, round(v * 2) / 2)), 1)


def _round_step(v: float, step: float) -> float:
    s = step if step and step > 0 else 0.5
    return round(round(v / s) * s, 2)


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < EPS


# --- Abgeleitete Werte -------------------------------------------------------------------------


# Quellen der Raumtemperatur, nach denen die TRVs korrigiert werden (nicht deren eigene Messung)
ROOM_SOURCES = ("sensors", "display")


def display_temperature(inputs: HeatingInputs) -> float | None:
    """Messung des Displays, wenn es erreichbar ist und sie plausibel ist."""
    d = inputs.display
    if d is None or not d.reading.available or d.reading.local is None:
        return None
    return d.reading.local if -30 <= d.reading.local <= 60 else None


def room_temperature(inputs: HeatingInputs) -> tuple[float | None, str | None]:
    """Mittel der gültigen Raumthermometer (das Display zählt mit), sonst Mittel der TRV-Messungen.
    ``(Wert, Quelle)``: ``sensors`` mit mindestens einem Thermometer, ``display`` nur mit dem Display, ``trv``."""
    values = [v for v in inputs.sensors.values() if v is not None]
    display = display_temperature(inputs)
    if values:
        if display is not None:
            values.append(display)
        return round(sum(values) / len(values), 2), "sensors"
    if display is not None:
        return round(display, 2), "display"
    locals_ = [t.reading.local for t in inputs.trvs.values() if t.reading.available and t.reading.local is not None]
    if locals_:
        return round(sum(locals_) / len(locals_), 2), "trv"
    return None, None


def effective_target(options: dict[str, Any], state: HeatingState) -> float | None:
    """Ziel nach Betriebsart, ``None`` bei Aus."""
    if state.mode == "off":
        return None
    if state.mode in PRESET_MODES:
        return float((options.get("presets") or DEFAULT_PRESETS).get(state.mode, DEFAULT_PRESETS[state.mode]))
    return float(state.target)


def pause_reason(options: dict[str, Any], state: HeatingState) -> str | None:
    """Warum gerade nicht geheizt wird: ``off``, ``window``, ``summer`` oder ``None``."""
    if not options.get("enabled", True):
        return None
    if state.mode == "off":
        return "off"
    if state.paused:
        return "window"
    if state.summer:
        return "summer"
    return None


def raw_temperature(r: TrvReading, memo: TrvMemo | None) -> float | None:
    """Messung am Heizkörper ohne Kalibrierung. Die meisten TRVs melden die kalibrierte Temperatur;
    hat Haven gesehen, dass ein Gerät das nicht tut (``cal_included`` falsch), gilt die Meldung selbst."""
    if r.local is None:
        return None
    if memo is not None and memo.cal_included is False:
        return r.local
    return r.local - (r.calibration or 0.0)


def method_for(options: dict[str, Any], caps: TrvCaps, room_source: str | None) -> str:
    """Korrektur, die bei diesem TRV wirklich greift."""
    if room_source not in ROOM_SOURCES:
        return "none"
    method = options.get("calibration", "offset")
    if method == "offset" and caps.calibration is None:
        return "target"
    return method


# --- Beobachten -------------------------------------------------------------------------------


def observe(options: dict[str, Any], state: HeatingState, inputs: HeatingInputs) -> tuple[HeatingState, list[str]]:
    """Fenster, Sommer und Änderungen am Gerät in den Zustand übernehmen. Liefert Ereignisse für das Log."""
    new = _copy(state)
    now = inputs.now
    events: list[str] = []
    room, source = room_temperature(inputs)

    # Fenster: Pause nach Verzögerung, Weiter nach Verzögerung (unbekannt zählt als zu). Am Display trotz
    # offenem Fenster eingeschaltet (``window_override``): keine Pause, bis alle Fenster zu sind.
    open_any = any(v is True for v in inputs.windows.values())
    if open_any:
        new.window_closed_since = None
        if new.window_open_since is None:
            new.window_open_since = now
        if (
            not new.paused
            and not new.window_override
            and now - new.window_open_since >= options.get("window_open_delay", 0)
        ):
            new.paused = True
            events.append("window_pause")
    else:
        new.window_open_since = None
        if new.window_override:
            new.window_override = False
            events.append("window_override_end")
        if new.paused:
            if new.window_closed_since is None:
                new.window_closed_since = now
            if now - new.window_closed_since >= options.get("window_close_delay", 0):
                new.paused = False
                new.window_closed_since = None
                events.append("window_resume")
        else:
            new.window_closed_since = None

    # Außentemperatur: geglättet, Sommerpause mit Hysterese
    outdoor = options.get("outdoor")
    if not outdoor:
        new.outdoor_ema = new.outdoor_at = None
        if new.summer:
            new.summer = False
            events.append("summer_end")
    elif inputs.outdoor is not None:
        x = inputs.outdoor
        if new.outdoor_ema is None or new.outdoor_at is None or now - new.outdoor_at > OUTDOOR_RESET:
            new.outdoor_ema = x
        else:
            dt = max(0.0, now - new.outdoor_at)
            new.outdoor_ema += (x - new.outdoor_ema) * (1 - math.exp(-dt / OUTDOOR_TAU))
        new.outdoor_ema = round(new.outdoor_ema, 3)
        new.outdoor_at = now
        threshold = float(outdoor.get("threshold", DEFAULT_OUTDOOR_THRESHOLD))
        if not new.summer and new.outdoor_ema >= threshold:
            new.summer = True
            events.append("summer_start")
        elif new.summer and new.outdoor_ema <= threshold - OUTDOOR_HYSTERESIS:
            new.summer = False
            events.append("summer_end")

    # Änderungen am Gerät (Drehrad, Taste): für den ganzen Raum übernehmen
    for ieee, trv in inputs.trvs.items():
        memo = new.trvs.setdefault(ieee, TrvMemo())
        r, caps = trv.reading, trv.caps
        first = memo.seen_setpoint is None and memo.seen_mode is None
        mode_changed = r.mode is not None and memo.seen_mode is not None and r.mode != memo.seen_mode
        sp_changed = r.setpoint is not None and memo.seen_setpoint is not None and not _same(r.setpoint, memo.seen_setpoint)
        cal_changed = r.calibration is not None and memo.seen_calibration is not None and not _same(r.calibration, memo.seen_calibration)
        local_changed = r.local is not None and memo.seen_local is not None and not _same(r.local, memo.seen_local)
        if cal_changed:
            # Neue Kalibrierung: bis zur nächsten Messung nicht nachlegen, und merken, wie sich die Messung verschiebt
            memo.cal_changed_at = None if local_changed else now
            delta = r.calibration - memo.seen_calibration
            if abs(delta) >= CAL_DETECT and memo.seen_local is not None:
                memo.cal_ref_local, memo.cal_ref_delta = memo.seen_local, delta
        elif local_changed:
            memo.cal_changed_at = None
        if local_changed and memo.cal_ref_delta is not None and memo.cal_ref_local is not None:
            # Enthält die gemeldete Temperatur die Kalibrierung? Dann springt sie um etwa die Änderung mit.
            jump = r.local - memo.cal_ref_local
            memo.cal_included = abs(jump - memo.cal_ref_delta) < abs(jump)
            memo.cal_ref_local = memo.cal_ref_delta = None
        if r.local is not None:
            memo.seen_local = r.local
        if r.calibration is not None:
            memo.seen_calibration = r.calibration
        # Ventil umgeschaltet: der Fühler am Heizkörper ändert sich gleich, Kalibrierung kurz halten
        if r.valve is not None:
            if memo.seen_valve is not None and abs(r.valve - memo.seen_valve) >= VALVE_JUMP:
                memo.valve_changed_at = now
            memo.seen_valve = r.valve
        # Abweichung Raum − TRV geglättet (nicht, solange eine neue Kalibrierung noch nicht gemessen ist)
        raw = raw_temperature(r, memo)
        if source in ROOM_SOURCES and room is not None and raw is not None and r.available and memo.cal_changed_at is None:
            corr = room - raw
            if memo.corr_ema is None or memo.corr_at is None or now - memo.corr_at > 8 * CAL_TAU:
                memo.corr_ema = corr
            else:
                dt = max(0.0, now - memo.corr_at)
                memo.corr_ema += (corr - memo.corr_ema) * (1 - math.exp(-dt / CAL_TAU))
            memo.corr_ema = round(memo.corr_ema, 3)
            memo.corr_at = now
        # Ventil offen am Gerät: nach einer Weile übernimmt Haven wieder
        if memo.released_at is not None and memo.release_until is not None and now >= memo.release_until:
            _resume(memo)
            events.append("resume_boost")
        if not first and options.get("enabled", True) and not options.get("lock_manual"):
            if mode_changed and r.mode in caps.release_values and r.mode != memo.sent_mode:
                # Gerät steuert selbst (Ventil offen, eigener Wochenplan): freigeben statt zurückstellen
                memo.released_at = now
                memo.released_mode = r.mode
                memo.release_until = now + BOOST_RETURN if r.mode in caps.boost_values else None
                events.append(f"released:{r.mode}")
            elif mode_changed and (r.mode != memo.sent_mode or memo.released_at is not None):
                if memo.released_at is not None:
                    _resume(memo)
                    events.append("resume_device")
                adopted = _PRESET_TO_MODE.get(str(r.mode).lower())
                if adopted is not None and adopted != new.mode:
                    new.mode = adopted
                    new.changed_at = now
                    events.append(f"manual_mode:{adopted}")
            elif (
                memo.released_at is None
                and sp_changed
                and not mode_changed
                and not _same(r.setpoint, memo.sent_setpoint)
                and not _same(r.setpoint, memo.prev_sent_setpoint)
                and (caps.mode_prop is None or r.mode == caps.on_value)
                and not (memo.sent_mode_at is not None and now - memo.sent_mode_at < MODE_SETTLE)
                and pause_reason(options, new) is None
            ):
                shift = memo.shift if options.get("calibration") == "target" and memo.shift is not None else 0.0
                adopted_t = clamp_target(r.setpoint - shift)
                if new.mode != "heat" or not _same(adopted_t, new.target):
                    new.mode = "heat"
                    new.target = adopted_t
                    new.changed_at = now
                    events.append(f"manual_target:{adopted_t}")
        if r.setpoint is not None:
            memo.seen_setpoint = r.setpoint
        if r.mode is not None:
            memo.seen_mode = r.mode
    for gone in [k for k in new.trvs if k not in inputs.trvs]:
        new.trvs.pop(gone)
    _observe_display(options, new, inputs, events)
    if new.mode == "off" and new.window_override:
        new.window_override = False  # Raum aus: die nächste Öffnung pausiert wieder
        events.append("window_override_end")
    return new, events


def _display(options: dict[str, Any], inputs: HeatingInputs) -> tuple[str, TrvInput] | None:
    """``(IEEE, Eingang)`` des Displays, wenn der Raum eines hat und es gemeldet ist."""
    ieee = options.get("display")
    if not ieee or inputs.display is None:
        return None
    return ieee, inputs.display


def _observe_display(options: dict[str, Any], new: HeatingState, inputs: HeatingInputs, events: list[str]) -> None:
    """Bedienung am Display übernehmen: Sollwert -> Ziel des Raums, Aus/Ein/Preset -> Betriebsart.

    Gilt auch mit ``lock_manual`` und in Fenster- oder Sommerpause (das Display ist die Bedienung des Raums).
    Eigene Befehle erkennt Haven an ``sent_*``; eine Bestätigung verbraucht sie, damit ein späteres Zurückdrehen
    auf denselben Wert wieder als Bedienung zählt.

    Fensterpause: Haven schaltet das Display aus. Ein Aus in der Pause ist keine Bedienung. Schaltet es jemand
    ein, heißt das „trotzdem heizen“: Pause zu Ende, ``window_override`` bis alle Fenster zu sind (Betriebsart und
    Ziel bleiben, nur aus Aus wird ``heat``).

    Preset (``caps.preset_prop``): Eco am Display -> Betriebsart Eco, von Eco zurück auf Manuell -> ``heat`` mit
    dem gemeldeten Sollwert. Der eigene Wochenplan (``preset_self``) wird nicht übernommen, Haven stellt ihn zurück.
    In Eco und im Wochenplan zählt ein neuer Sollwert nicht (das Gerät zeigt dann seine Eco-Temperatur bzw. die
    des Plans), eine Eco-Temperatur am Gerät auch nicht (Haven stellt die des Raums wieder ein).
    """
    found = _display(options, inputs)
    if found is None:
        new.display, new.display_ieee = None, None
        return
    ieee, disp = found
    if new.display is None or new.display_ieee != ieee:
        new.display, new.display_ieee = TrvMemo(), ieee
    memo, r, caps, now = new.display, disp.reading, disp.caps, inputs.now
    enabled = options.get("enabled", True)
    first = memo.seen_setpoint is None and memo.seen_mode is None
    mode_changed = r.mode is not None and memo.seen_mode is not None and r.mode != memo.seen_mode
    sp_changed = r.setpoint is not None and memo.seen_setpoint is not None and not _same(r.setpoint, memo.seen_setpoint)
    preset_changed = r.preset is not None and memo.seen_preset is not None and r.preset != memo.seen_preset
    is_off = caps.mode_prop is not None and caps.off_value is not None and r.mode == caps.off_value
    if r.local is not None:
        memo.seen_local = r.local
    mode_adopted = False
    if mode_changed and r.mode == memo.sent_mode and _recent(memo.sent_mode_at, now):
        memo.sent_mode = None  # Bestätigung des eigenen Befehls
    elif mode_changed and not first and enabled and r.mode not in caps.release_values:
        adopted: str | None = None
        if r.mode == caps.off_value:
            # in der Fensterpause soll das Display aus sein: das ist kein Ausschalten des Raums
            adopted = None if new.paused else "off"
        elif r.mode == caps.on_value:
            adopted = "heat" if new.mode == "off" or new.mode in PRESET_MODES else None
            if new.paused and memo.seen_mode == caps.off_value:
                # trotz Fensterpause eingeschaltet: heizen, bis alle Fenster zu sind
                if new.mode != "off":
                    adopted = None
                new.paused = False
                new.window_closed_since = None
                new.window_override = any(v is True for v in inputs.windows.values())
                events.append("display_window_override")
                mode_adopted = True
        else:
            adopted = _PRESET_TO_MODE.get(str(r.mode).lower())
        if adopted is not None and adopted != new.mode:
            new.mode = adopted
            new.changed_at = now
            events.append(f"display_mode:{adopted}")
        mode_adopted = mode_adopted or adopted is not None
    preset_adopted = False
    if preset_changed and r.preset == memo.sent_preset and _recent(memo.sent_preset_at, now):
        memo.sent_preset = None  # Bestätigung des eigenen Befehls
    elif preset_changed and not first and enabled and not is_off and not mode_adopted:
        if r.preset == caps.preset_eco:
            if new.mode != "eco":
                new.mode = "eco"
                new.changed_at = now
                events.append("display_mode:eco")
            preset_adopted = True
        elif r.preset == caps.preset_manual and memo.seen_preset == caps.preset_eco:
            adopted_t = clamp_target(r.setpoint) if r.setpoint is not None else new.target
            if new.mode != "heat":
                new.mode = "heat"
                new.changed_at = now
                events.append("display_mode:heat")
            if not _same(adopted_t, new.target):
                new.target = adopted_t
                new.changed_at = now
                events.append(f"display_target:{adopted_t}")
            preset_adopted = True
    if r.eco_temp is not None and _same(r.eco_temp, memo.sent_eco_temp):
        memo.sent_eco_temp = None  # bestätigt; eine spätere Änderung am Gerät stellt Haven sofort zurück
    preset_holds = caps.preset_prop is not None and r.preset is not None and (
        r.preset == caps.preset_eco or r.preset in caps.preset_self
    )
    echo = _recent(memo.sent_setpoint_at, now) and (
        _same(r.setpoint, memo.sent_setpoint) or _same(r.setpoint, memo.prev_sent_setpoint)
    )
    if sp_changed and echo:
        # Bestätigung (auch eine späte des vorigen Werts)
        if _same(r.setpoint, memo.sent_setpoint):
            memo.sent_setpoint = None
        memo.prev_sent_setpoint = None
    elif (
        sp_changed
        and not first
        and not mode_adopted
        and not preset_adopted
        and not preset_holds
        and enabled
        and not is_off
        and not (memo.sent_mode_at is not None and now - memo.sent_mode_at < MODE_SETTLE)
        and not (memo.sent_preset_at is not None and now - memo.sent_preset_at < MODE_SETTLE)
    ):
        adopted_t = clamp_target(r.setpoint)
        if new.mode != "heat" or not _same(adopted_t, new.target):
            new.mode = "heat"
            new.target = adopted_t
            new.changed_at = now
            events.append(f"display_target:{adopted_t}")
    if r.setpoint is not None:
        memo.seen_setpoint = r.setpoint
    if r.mode is not None:
        memo.seen_mode = r.mode
    if r.preset is not None:
        memo.seen_preset = r.preset


def _recent(at: float | None, now: float) -> bool:
    """Eigener Befehl so frisch, dass eine passende Meldung seine Bestätigung ist."""
    return at is not None and now - at < 2 * RESEND


def _resume(memo: TrvMemo) -> None:
    memo.released_at = memo.released_mode = memo.release_until = None


def next_wake(options: dict[str, Any], state: HeatingState) -> float | None:
    """Nächster Zeitpunkt, zu dem sich ohne neue Meldung etwas ändert (Fenster-Verzögerungen, Ende einer
    Freigabe mit offenem Ventil)."""
    times = [m.release_until for m in state.trvs.values() if m.released_at is not None and m.release_until is not None]
    if state.window_open_since is not None and not state.paused and not state.window_override:
        times.append(state.window_open_since + options.get("window_open_delay", 0))
    if state.paused and state.window_closed_since is not None:
        times.append(state.window_closed_since + options.get("window_close_delay", 0))
    return min(times) if times else None


# --- Planen -----------------------------------------------------------------------------------


@dataclass
class TrvPlan:
    """Sollzustand eines TRV und warum."""

    mode: str | None = None
    setpoint: float | None = None
    calibration: float | None = None
    method: str = "none"
    correction: float | None = None  # Raum − TRV-Rohmessung
    released: bool = False  # am Gerät freigegeben: kein Modus, kein Sollwert
    comfort_temp: float | None = None
    hysteresis: str | None = None


def desired(options: dict[str, Any], state: HeatingState, inputs: HeatingInputs) -> dict[str, TrvPlan]:
    """Sollzustand je TRV (ohne Rücksicht auf Takt und Bestätigung)."""
    room, source = room_temperature(inputs)
    reason = pause_reason(options, state)
    target = effective_target(options, state)
    out: dict[str, TrvPlan] = {}
    for ieee, trv in inputs.trvs.items():
        caps, r = trv.caps, trv.reading
        memo = state.trvs.get(ieee) or TrvMemo()
        p = TrvPlan(method=method_for(options, caps, source))
        p.released = memo.released_at is not None
        if p.released and not options.get("calibrate_released", True) and p.method == "offset":
            p.method = "none"  # freigegeben ohne Kalibrierung: eigene zurücknehmen (unten)
        raw = raw_temperature(r, memo)
        if room is not None and raw is not None and source in ROOM_SOURCES:
            p.correction = round(room - raw, 2)
        if p.method == "offset" and p.correction is not None:
            # Gedämpft: geglättete Abweichung, je Schritt höchstens CAL_MAX_STEP (der erste Schritt voll)
            want = memo.corr_ema if memo.corr_ema is not None else p.correction
            base = r.calibration
            if memo.sent_calibration_at is not None and inputs.now - memo.sent_calibration_at < RESEND:
                base = memo.sent_calibration  # gerade gesendet, Bestätigung steht aus
            if memo.sent_calibration is not None and base is not None:
                want = min(base + CAL_MAX_STEP, max(base - CAL_MAX_STEP, want))
            cal = min(caps.cal_max, max(caps.cal_min, want))
            p.calibration = round(_round_step(cal, caps.cal_step), 1)
        elif (
            options.get("calibration") != "offset" or (p.released and not options.get("calibrate_released", True))
        ) and caps.calibration and memo.sent_calibration not in (None, 0.0):
            p.calibration = 0.0  # Korrektur abgewählt: eigene Kalibrierung zurücknehmen
        else:
            p.calibration = r.calibration if r.calibration is not None else memo.sent_calibration
        if p.released:
            out[ieee] = p  # Gerät steuert selbst: weder Modus noch Sollwert
            continue
        if caps.hysteresis:
            p.hysteresis = "comfort"  # Ventil stufenlos statt auf/zu
        if reason is not None or target is None:
            if caps.off_value is not None:
                p.mode = caps.off_value
            else:
                p.mode = caps.on_value
                p.setpoint = caps.min
            out[ieee] = p
            continue
        p.mode = caps.on_value
        sp = target
        if p.method == "target" and room is not None and r.local is not None:
            sp = target + (r.local - room)
        p.setpoint = min(caps.max, max(caps.min, _round_step(sp, caps.step)))
        if caps.comfort_temp:
            # Regelmodus ``comfort``: auch die Komfort-Temperatur des Geräts, falls es auf sie zurückfällt
            p.comfort_temp = p.setpoint
        out[ieee] = p
    return out


def plan(
    options: dict[str, Any], state: HeatingState, inputs: HeatingInputs
) -> tuple[HeatingState, dict[str, dict[str, Any]]]:
    """Befehle je TRV (Z2M-``set``-Werte) und der Zustand mit den gesendeten Werten."""
    new = _copy(state)
    if not options.get("enabled", True):
        return new, {}
    now = inputs.now
    commands: dict[str, dict[str, Any]] = {}
    target = effective_target(options, state)
    for ieee, p in desired(options, state, inputs).items():
        trv = inputs.trvs[ieee]
        caps, r = trv.caps, trv.reading
        if not r.available:
            continue
        memo = new.trvs.setdefault(ieee, TrvMemo())
        values: dict[str, Any] = {}
        # Betriebsart am Gerät
        if caps.mode_prop and p.mode is not None and r.mode != p.mode:
            waiting = p.mode == memo.sent_mode and memo.sent_mode_at is not None and now - memo.sent_mode_at < RESEND
            if not waiting:
                values[caps.mode_prop] = p.mode
                memo.sent_mode, memo.sent_mode_at = p.mode, now
        # Sollwert (nicht bei ausgeschaltetem Gerät)
        if p.setpoint is not None and not _same(r.setpoint, p.setpoint):
            waiting = _same(p.setpoint, memo.sent_setpoint) and memo.sent_setpoint_at is not None and now - memo.sent_setpoint_at < RESEND
            user_change = state.changed_at is not None and (memo.sent_setpoint_at is None or state.changed_at > memo.sent_setpoint_at)
            throttled = (
                p.method == "target"
                and not user_change
                and memo.sent_setpoint_at is not None
                and now - memo.sent_setpoint_at < SHIFT_INTERVAL
                and r.setpoint is not None
                and abs(r.setpoint - p.setpoint) < 1.5
            )
            if not waiting and not throttled:
                values[caps.setpoint] = p.setpoint
                memo.prev_sent_setpoint = memo.sent_setpoint
                memo.sent_setpoint, memo.sent_setpoint_at = p.setpoint, now
                memo.shift = round(p.setpoint - target, 2) if target is not None and p.method == "target" else 0.0
        # Kalibrierung (nur ``offset``)
        if p.method == "offset" and caps.calibration and p.calibration is not None:
            current = r.calibration
            settling = (memo.cal_changed_at is not None and now - memo.cal_changed_at < CAL_SETTLE) or (
                memo.sent_calibration is not None
                and memo.valve_changed_at is not None
                and now - memo.valve_changed_at < VALVE_SETTLE
            )
            due = memo.sent_calibration_at is None or now - memo.sent_calibration_at >= CAL_INTERVAL
            far = current is None or abs(p.calibration - current) >= CAL_DEADBAND
            waiting = _same(p.calibration, memo.sent_calibration) and memo.sent_calibration_at is not None and now - memo.sent_calibration_at < RESEND
            if far and due and not settling and not waiting:
                values[caps.calibration] = p.calibration
                memo.sent_calibration, memo.sent_calibration_at = p.calibration, now
        elif p.calibration == 0.0 and caps.calibration and memo.sent_calibration not in (None, 0.0) and not _same(r.calibration, 0.0):
            values[caps.calibration] = 0.0
            memo.sent_calibration, memo.sent_calibration_at = 0.0, now
        # Komfort-Temperatur und Ventilverhalten (selten, nur bei Abweichung)
        if p.comfort_temp is not None and caps.comfort_temp and not _same(r.comfort_temp, p.comfort_temp):
            if memo.sent_comfort_at is None or now - memo.sent_comfort_at >= RESEND:
                values[caps.comfort_temp] = p.comfort_temp
                memo.sent_comfort_at = now
        if p.hysteresis is not None and caps.hysteresis and r.hysteresis is not None and r.hysteresis != p.hysteresis:
            if memo.sent_hysteresis_at is None or now - memo.sent_hysteresis_at >= RESEND:
                values[caps.hysteresis] = p.hysteresis
                memo.sent_hysteresis_at = now
        if values:
            commands[ieee] = values
    found = _display(options, inputs)
    if found is not None:
        ieee, disp = found
        if new.display is None or new.display_ieee != ieee:
            new.display, new.display_ieee = TrvMemo(), ieee
        if values := _plan_display(options, new, disp, now):
            commands[ieee] = values
    return new, commands


@dataclass
class DisplayPlan:
    """Sollzustand des Displays, ``None`` = nicht stellen."""

    mode: str | None = None
    setpoint: float | None = None
    preset: str | None = None
    eco_temp: float | None = None


def display_desired(options: dict[str, Any], state: HeatingState, disp: TrvInput) -> DisplayPlan:
    """Sollzustand des Displays.

    Aus bei Betriebsart Aus und in der Fensterpause (nur, wenn das Gerät ein Aus hat), die Sommerpause lässt es an.
    Steht das Display auf eigener Steuerung (``release_values``), bleibt sein Modus. Sollwert = wirksames Ziel des
    Raums (Eco/Abwesend mit ihrer Temperatur), auf den Schritt des Geräts gerundet, in dessen Grenzen; keiner bei Aus.

    Mit eigenem Preset (``caps.preset_prop``): Eco, wenn der Raum auf Eco steht und das Gerät Eco kennt, sonst
    Manuell (auch gegen den eigenen Wochenplan); kein Preset, solange es aus ist. In Eco regelt das Gerät nach seiner
    Eco-Temperatur, dann kein Sollwert. Die Eco-Temperatur ist immer die Eco-Temperatur des Raums.
    """
    caps, r = disp.caps, disp.reading
    target = effective_target(options, state)
    off = target is None or (state.paused and caps.off_value is not None)
    out = DisplayPlan()
    if caps.mode_prop and r.mode not in caps.release_values:
        out.mode = caps.off_value if off else caps.on_value
    if caps.preset_prop and not off:
        out.preset = caps.preset_eco if state.mode == "eco" and caps.preset_eco else caps.preset_manual
    if caps.eco_temp_prop:
        eco = float((options.get("presets") or DEFAULT_PRESETS).get("eco", DEFAULT_PRESETS["eco"]))
        out.eco_temp = min(caps.eco_max, max(caps.eco_min, _round_step(eco, caps.eco_step)))
    if not off and target is not None and (out.preset is None or out.preset == caps.preset_manual):
        out.setpoint = min(caps.max, max(caps.min, _round_step(target, caps.step)))
    return out


def _plan_display(options: dict[str, Any], state: HeatingState, disp: TrvInput, now: float) -> dict[str, Any]:
    """Befehle an das Display (nie Kalibrierung, Komfort-Temperatur oder Ventilverhalten)."""
    caps, r, memo = disp.caps, disp.reading, state.display
    if not r.available or memo is None:
        return {}
    want = display_desired(options, state, disp)
    values: dict[str, Any] = {}
    if caps.mode_prop and want.mode is not None and r.mode != want.mode:
        waiting = want.mode == memo.sent_mode and memo.sent_mode_at is not None and now - memo.sent_mode_at < RESEND
        if not waiting:
            values[caps.mode_prop] = want.mode
            memo.sent_mode, memo.sent_mode_at = want.mode, now
    if caps.preset_prop and want.preset is not None and r.preset != want.preset:
        waiting = want.preset == memo.sent_preset and memo.sent_preset_at is not None and now - memo.sent_preset_at < RESEND
        if not waiting:
            values[caps.preset_prop] = want.preset
            memo.sent_preset, memo.sent_preset_at = want.preset, now
    if want.setpoint is not None and not _same(r.setpoint, want.setpoint):
        waiting = (
            _same(want.setpoint, memo.sent_setpoint)
            and memo.sent_setpoint_at is not None
            and now - memo.sent_setpoint_at < RESEND
        )
        if not waiting:
            values[caps.setpoint] = want.setpoint
            memo.prev_sent_setpoint = memo.sent_setpoint
            memo.sent_setpoint, memo.sent_setpoint_at = want.setpoint, now
    if caps.eco_temp_prop and want.eco_temp is not None and not _same(r.eco_temp, want.eco_temp):
        waiting = (
            _same(want.eco_temp, memo.sent_eco_temp)
            and memo.sent_eco_temp_at is not None
            and now - memo.sent_eco_temp_at < RESEND
        )
        if not waiting:
            values[caps.eco_temp_prop] = want.eco_temp
            memo.sent_eco_temp, memo.sent_eco_temp_at = want.eco_temp, now
    return values


# --- Status -------------------------------------------------------------------------------------


def status(options: dict[str, Any], state: HeatingState, inputs: HeatingInputs) -> dict[str, Any]:
    """Zustand für die App. Zeiten als Unix-Sekunden (die Laufzeit macht ISO daraus)."""
    room, source = room_temperature(inputs)
    plans = desired(options, state, inputs)
    reason = pause_reason(options, state)
    trvs = []
    for ieee, trv in inputs.trvs.items():
        r, p = trv.reading, plans[ieee]
        pending = False
        if r.available and options.get("enabled", True) and not p.released:
            pending = (p.mode is not None and trv.caps.mode_prop is not None and r.mode != p.mode) or (
                p.setpoint is not None and r.setpoint is not None and abs(r.setpoint - p.setpoint) >= trv.caps.step
            )
        trvs.append(
            {
                "ieee": ieee,
                "name": trv.name,
                "available": r.available,
                "local_temperature": r.local,
                "setpoint": r.setpoint,
                "calibration": r.calibration,
                "mode": r.mode,
                "heating": r.heating,
                "valve": r.valve,
                "method": p.method,
                "correction": p.correction,
                "desired_setpoint": p.setpoint,
                "desired_calibration": p.calibration if p.method == "offset" else None,
                "pending": pending,
                "released": p.released,
                "released_mode": (state.trvs.get(ieee) or TrvMemo()).released_mode if p.released else None,
                "release_until": (state.trvs.get(ieee) or TrvMemo()).release_until if p.released else None,
            }
        )
    display = None
    if (found := _display(options, inputs)) is not None:
        ieee, disp = found
        r, caps = disp.reading, disp.caps
        want = display_desired(options, state, disp)
        pending = False
        if r.available and options.get("enabled", True):
            pending = (
                (want.mode is not None and caps.mode_prop is not None and r.mode != want.mode)
                or (want.setpoint is not None and r.setpoint is not None and abs(r.setpoint - want.setpoint) >= caps.step)
                or (want.preset is not None and r.preset is not None and r.preset != want.preset)
                or (want.eco_temp is not None and r.eco_temp is not None and abs(r.eco_temp - want.eco_temp) >= caps.eco_step)
            )
        display = {
            "ieee": ieee,
            "name": disp.name,
            "available": r.available,
            "local_temperature": r.local,
            "setpoint": r.setpoint,
            "mode": r.mode,
            "preset": r.preset,
            "eco_temperature": r.eco_temp,
            "battery": r.battery,
            "pending": pending,
        }
    # nur die TRVs: das Display meldet zwar running_state, heizt aber nicht selbst
    heating_flags = [t["heating"] for t in trvs if t["heating"] is not None]
    outdoor = options.get("outdoor")
    window_open = any(v is True for v in inputs.windows.values())
    return {
        "area_id": options.get("area_id"),
        "enabled": bool(options.get("enabled", True)),
        "mode": state.mode,
        "target": state.target,
        "effective_target": effective_target(options, state),
        "room_temperature": None if room is None else round(room, 1),
        "room_source": source,
        "sensors": [{"entity_id": e, "value": v} for e, v in inputs.sensors.items()],
        "reason": reason,
        "released": any(t["released"] for t in trvs),
        "heating": (any(heating_flags) if heating_flags else None) if reason is None else False,
        "window": {
            "open": window_open,
            "paused": state.paused,
            "override": state.window_override,
            "pause_at": state.window_open_since + options.get("window_open_delay", 0)
            if state.window_open_since is not None and not state.paused and not state.window_override
            else None,
            "resume_at": state.window_closed_since + options.get("window_close_delay", 0)
            if state.paused and state.window_closed_since is not None
            else None,
            "contacts": [{"entity_id": e, "open": v} for e, v in inputs.windows.items()],
        },
        "outdoor": None
        if not outdoor
        else {
            "entity_id": outdoor.get("entity"),
            "value": None if state.outdoor_ema is None else round(state.outdoor_ema, 1),
            "current": inputs.outdoor,
            "threshold": float(outdoor.get("threshold", DEFAULT_OUTDOOR_THRESHOLD)),
            "summer": state.summer,
        },
        "trvs": trvs,
        "display": display,
    }


# --- Bedienung aus der App ------------------------------------------------------------------------


def control(state: HeatingState, *, target: Any = None, mode: Any = None, now: float) -> HeatingState:
    """Ziel und/oder Betriebsart setzen (App). Ein Ziel ohne Betriebsart schaltet auf Manuell. Jede Bedienung
    in der App übernimmt am Gerät freigegebene TRVs wieder; ohne Ziel und Betriebsart nur das."""
    new = _copy(state)
    for memo in new.trvs.values():
        _resume(memo)
    if target is None and mode is None:
        return new
    if mode is not None:
        if mode not in MODES:
            raise Z2MError("invalid_format", "mode muss heat, comfort, eco, away oder off sein")
        new.mode = mode
        if mode == "off":
            new.window_override = False
    if target is not None:
        if not _is_num(target) or not TARGET_MIN <= target <= TARGET_MAX:
            raise Z2MError("invalid_format", "target muss zwischen 5 und 30 °C liegen")
        new.target = clamp_target(float(target))
        if mode is None:
            new.mode = "heat"
    new.changed_at = now
    return new


# --- Eingänge aus HA-Zuständen ---------------------------------------------------------------------


def sensor_value(state: str | None, age: float | None) -> float | None:
    """Raumthermometer: Zahl oder ``None`` (unbekannt, nicht verfügbar, zu alt, unplausibel)."""
    v = _num(state)
    if v is None or not -30 <= v <= 60:
        return None
    if age is not None and age > SENSOR_MAX_AGE:
        return None
    return v


def contact_open(state: str | None) -> bool | None:
    if state == "on":
        return True
    if state == "off":
        return False
    return None


def outdoor_value(domain: str, state: str | None, attributes: dict[str, Any] | None) -> float | None:
    if domain == "weather":
        v = _num((attributes or {}).get("temperature"))
    else:
        v = _num(state)
    return v if v is not None and -60 <= v <= 60 else None


# --- Store ----------------------------------------------------------------------------------------

STORE_KEY = "haven.heating"
STORE_VERSION = 1


def empty_store() -> dict[str, Any]:
    return {"rooms": {}, "state": {}, "displays": []}


def read_displays(value: Any) -> list[str]:
    """Als Display markierte Thermostate: nur nicht leere Zeichenketten, ohne Doppelte, Reihenfolge bleibt."""
    out: list[str] = []
    for v in value if isinstance(value, list) else []:
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def read_store(data: Any) -> dict[str, Any]:
    out = empty_store()
    if not isinstance(data, dict):
        return out
    rooms = data.get("rooms")
    if isinstance(rooms, dict):
        out["rooms"] = {k: normalize_options({**v, "area_id": k}) for k, v in rooms.items() if isinstance(v, dict)}
    out["displays"] = read_displays(data.get("displays"))
    for room in out["rooms"].values():
        if room["display"] is not None and (room["display"] not in out["displays"] or room["display"] in room["trvs"]):
            room["display"] = None  # nicht (mehr) markiert
    st = data.get("state")
    if isinstance(st, dict):
        out["state"] = {k: v for k, v in st.items() if isinstance(v, dict) and k in out["rooms"]}
    return out

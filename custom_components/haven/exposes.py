"""Klassifizierung von Zigbee2MQTT-Exposes in verständliche Bereiche.

Reines Python, kein Home-Assistant-Import. Das Modul kann daher ohne HA
getestet werden (siehe integration/tests/test_exposes.py).

Eingang: ``definition["exposes"]`` eines Geräts aus ``<base>/bridge/devices``.
Ausgang von :func:`classify`::

    {
      "device_type": "light" | "plug_meter" | ... | "unknown",
      "capabilities": ["brightness", "color_temp", "color_xy", ...],
      "primary": [feature, ...],     # Hauptfunktion
      "settings": [feature, ...],    # Einstellungen (mit "group")
      "diagnostic": [feature, ...],  # Batterie, Spannung, LQI
      "hidden": [feature, ...],      # Details (hue/saturation/x/y, effect_speed)
      "setting_groups": ["Erkennung", ...],
      "generic": ["prop", ...],      # Properties, die nur über den Rückfall liefen
    }

Zuordnung über Eigenschaften (Property-Namen, Typen, access-Bits), nicht ueber
Modellnamen. Die gepflegte Tabelle ``LABELS`` ist maßgeblich, die Z2M-
``category`` zählt nur im generischen Rückfall.
"""

from __future__ import annotations

import re
from typing import Any

AREA_PRIMARY = "primary"
AREA_SETTINGS = "settings"
AREA_DIAGNOSTIC = "diagnostic"
AREA_HIDDEN = "hidden"
AREAS = (AREA_PRIMARY, AREA_SETTINGS, AREA_DIAGNOSTIC, AREA_HIDDEN)

# Sonderwert in LABELS: Messwert. primary, wenn das Gerät Leistung/Energie misst,
# sonst diagnostic (z. B. voltage bei Batteriegeräten).
AREA_MEASURE = "measure"

DEVICE_TYPES = (
    "light",
    "plug_meter",
    "switch",
    "relay",
    "climate_sensor",
    "motion",
    "presence",
    "contact",
    "remote",
    "knob",
    "thermostat",
    "unknown",
)

# Gruppennamen
G_DETECT = "Erkennung"
G_POWER = "Verhalten nach Stromausfall"
G_DISPLAY = "Anzeige"
G_CALIB = "Kalibrierung"
G_PROTECT = "Schutz"
G_MEASURE = "Messung"
G_BEHAVIOR = "Verhalten"
G_MAINT = "Wartung"
G_TIMER = "Zeitsteuerung"
G_BUTTON = "Tasten"


def _l(
    label: str,
    help_: str = "",
    area: str = AREA_SETTINGS,
    group: str | None = None,
    values: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {"label": label, "help": help_, "area": area, "group": group, "values": values}


_P = AREA_PRIMARY
_S = AREA_SETTINGS
_D = AREA_DIAGNOSTIC
_H = AREA_HIDDEN

# Gepflegte Tabelle je Property-Name (ohne Endpoint-Suffix wie _l1).
LABELS: dict[str, dict[str, Any]] = {
    # --- Licht / Schalter -------------------------------------------------
    "state": _l("Zustand", "Ein oder aus.", _P),  # Label je Eltern-Expose ueberschrieben
    "brightness": _l("Helligkeit", "Wie hell das Licht leuchtet.", _P),
    "color_temp": _l("Farbtemperatur", "Von warmweiß (gemütlich) bis kaltweiß (hell, wach).", _P),
    "color_xy": _l("Farbe", "Beliebige Lichtfarbe.", _P),
    "color_hs": _l("Farbe (Farbton/Sättigung)", "Alternative Farbdarstellung, wird nicht angezeigt.", _H),
    "color": _l("Farbe", "Beliebige Lichtfarbe.", _P),
    "hue": _l("Farbton", "Technisches Detail der Farbe.", _H),
    "saturation": _l("Sättigung", "Technisches Detail der Farbe.", _H),
    "x": _l("Farbe X", "Technisches Detail der Farbe.", _H),
    "y": _l("Farbe Y", "Technisches Detail der Farbe.", _H),
    "effect": _l(
        "Effekt",
        "Startet einen Lichteffekt, etwa Blinken oder Farbwechsel.",
        _P,
        values={
            "blink": "Blinken",
            "breathe": "Atmen",
            "okay": "OK-Signal",
            "channel_change": "Kanalwechsel",
            "finish_effect": "Effekt beenden",
            "stop_effect": "Effekt stoppen",
            "candle": "Kerze",
            "fireplace": "Kamin",
            "colorloop": "Farbwechsel",
            "stop_colorloop": "Farbwechsel stoppen",
            "sunset": "Sonnenuntergang",
        },
    ),
    "effect_color": _l("Effektfarbe", "Farbe für den Effekt, technisches Detail.", _H),
    "effect_speed": _l("Effektgeschwindigkeit", "Tempo des Effekts, technisches Detail.", _H),
    "color_temp_startup": _l(
        "Farbtemperatur beim Einschalten",
        "Mit welcher Farbtemperatur das Licht nach Stromzufuhr startet.",
        _S,
        G_POWER,
    ),
    "color_power_on_behavior": _l(
        "Farbe nach Stromausfall",
        "Welche Farbe das Licht nach einem Stromausfall zeigt.",
        _S,
        G_POWER,
        {"initial": "Standardfarbe", "previous": "Letzte Farbe", "customized": "Eigene Farbe"},
    ),
    "power_on_behavior": _l(
        "Zustand nach Stromausfall",
        "Ob das Gerät nach einem Stromausfall an, aus oder im letzten Zustand startet.",
        _S,
        G_POWER,
        {"off": "Aus", "on": "An", "toggle": "Umschalten", "previous": "Letzter Zustand"},
    ),
    "power_outage_memory": _l(
        "Zustand nach Stromausfall",
        "Ob die Steckdose nach einem Stromausfall an, aus oder im letzten Zustand startet.",
        _S,
        G_POWER,
        {"on": "Immer an", "off": "Immer aus", "restore": "Letzter Zustand"},
    ),
    "delayed_power_on_state": _l(
        "Verzögert einschalten",
        "Nach Stromzufuhr erst nach einer Wartezeit einschalten.",
        _S,
        G_POWER,
    ),
    "delayed_power_on_time": _l(
        "Wartezeit bis Einschalten",
        "Wie lange nach Stromzufuhr gewartet wird, bevor das Gerät einschaltet.",
        _S,
        G_POWER,
    ),
    "do_not_disturb": _l(
        "Nicht stören",
        "Schaltet den Nicht-stören-Modus des Geräts ein (Verhalten laut Hersteller, ungeprüft).",
        _S,
        G_BEHAVIOR,
    ),
    # --- Relais / Zeitsteuerung -------------------------------------------
    "countdown": _l("Countdown", "Schaltet das Gerät nach der eingestellten Zeit um.", _S, G_TIMER),
    "inching_control_set": _l(
        "Impulsbetrieb (Inching)",
        "Schaltet das Relais nach kurzer Zeit automatisch zurück, etwa für Türdrückerrelais.",
        _S,
        G_TIMER,
    ),
    "inching_control": _l("Impulsbetrieb aktiv", "Schaltet den Impulsbetrieb ein.", _S, G_TIMER),
    "inching_time": _l("Impulsdauer", "Nach dieser Zeit schaltet das Relais zurück.", _S, G_TIMER),
    "inching_mode": _l("Impuls schaltet ein", "Ob der Impuls ein- oder ausschaltet.", _S, G_TIMER),
    "detach_relay_mode": _l(
        "Relais vom Taster trennen",
        "Der Taster am Gerät schaltet das Relais nicht mehr direkt, sondern meldet nur Aktionen.",
        _S,
        G_BEHAVIOR,
    ),
    "external_trigger_mode": _l(
        "Schalteingang-Modus",
        "Wie der angeschlossene Schalter oder Taster das Relais steuert.",
        _S,
        G_BEHAVIOR,
        {
            "edge": "Flanke",
            "pulse": "Impuls",
            "following(off)": "Folgt dem Schalter (aus)",
            "following(on)": "Folgt dem Schalter (an)",
        },
    ),
    "turbo_mode": _l(
        "Turbo-Modus",
        "Höhere Funkleistung für bessere Reichweite.",
        _S,
        G_BEHAVIOR,
    ),
    "switch_type_button": _l(
        "Tasterauslösung",
        "Wann der Taster am Gerät auslöst (ungeprüft: Bedeutung laut Hersteller).",
        _S,
        G_BEHAVIOR,
        {"release": "Beim Loslassen", "press": "Beim Drücken"},
    ),
    # --- Anzeige ----------------------------------------------------------
    "indicator": _l("Status-LED", "Schaltet die kleine Anzeige-LED am Gerät ein oder aus.", _S, G_DISPLAY),
    "indicator_mode": _l(
        "LED-Modus",
        "Wann die LED am Gerät leuchtet. Bedeutung der Zwischenwerte laut Hersteller, ungeprüft.",
        _S,
        G_DISPLAY,
        {"off": "Aus", "off/on": "Aus/An", "on/off": "An/Aus", "on": "An"},
    ),
    "network_indicator": _l("Netzwerk-LED", "Zeigt mit einer LED den Funkstatus an.", _S, G_DISPLAY),
    "temperature_unit": _l(
        "Temperatureinheit",
        "Einheit auf dem Display des Sensors.",
        _S,
        G_DISPLAY,
        {"celsius": "Celsius", "fahrenheit": "Fahrenheit"},
    ),
    # --- Schutz -----------------------------------------------------------
    "child_lock": _l("Kindersicherung", "Sperrt die Tasten am Gerät.", _S, G_PROTECT),
    "outlet_control_protect": _l(
        "Schaltschutz",
        "Schützt die Steckdose vor ungewolltem Schalten (laut Hersteller, ungeprüft).",
        _S,
        G_PROTECT,
    ),
    "overload_protection": _l(
        "Überlastschutz",
        "Schaltet bei zu hoher oder zu niedriger Leistung, Spannung oder Strom automatisch ab.",
        _S,
        G_PROTECT,
    ),
    "max_power": _l("Maximale Leistung", "Darüber schaltet das Gerät ab.", _S, G_PROTECT),
    "min_power": _l("Minimale Leistung", "Darunter schaltet das Gerät ab.", _S, G_PROTECT),
    "enable_min_power": _l("Minimale Leistung prüfen", "Schutz bei zu geringer Leistung aktivieren.", _S, G_PROTECT),
    "max_voltage": _l("Maximale Spannung", "Darüber schaltet das Gerät ab.", _S, G_PROTECT),
    "min_voltage": _l("Minimale Spannung", "Darunter schaltet das Gerät ab.", _S, G_PROTECT),
    "enable_max_voltage": _l("Maximale Spannung prüfen", "Schutz bei zu hoher Spannung aktivieren.", _S, G_PROTECT),
    "enable_min_voltage": _l("Minimale Spannung prüfen", "Schutz bei zu niedriger Spannung aktivieren.", _S, G_PROTECT),
    "max_current": _l("Maximaler Strom", "Darüber schaltet das Gerät ab.", _S, G_PROTECT),
    "min_current": _l("Minimaler Strom", "Darunter schaltet das Gerät ab.", _S, G_PROTECT),
    "enable_min_current": _l("Minimalen Strom prüfen", "Schutz bei zu geringem Strom aktivieren.", _S, G_PROTECT),
    # --- Erkennung (Bewegung/Anwesenheit) -----------------------------------
    "sensitivity": _l(
        "Empfindlichkeit",
        "Wie schnell der Sensor Bewegung erkennt. Hoch reagiert auch auf kleine Bewegungen.",
        _S,
        G_DETECT,
        {"low": "Niedrig", "medium": "Mittel", "high": "Hoch"},
    ),
    "motion_detection_sensitivity": _l(
        "Empfindlichkeit",
        "Wie fein der Sensor Bewegung und Anwesenheit erkennt. Höher heisst empfindlicher.",
        _S,
        G_DETECT,
    ),
    "keep_time": _l(
        "Haltezeit",
        "Wie lange der Sensor nach der letzten Bewegung noch 'Bewegung' meldet.",
        _S,
        G_DETECT,
        {"10": "10 Sekunden", "30": "30 Sekunden", "60": "1 Minute", "120": "2 Minuten"},
    ),
    "fading_time": _l(
        "Nachlaufzeit",
        "Wie lange der Sensor nach dem Verlassen des Raums noch 'Anwesend' meldet.",
        _S,
        G_DETECT,
    ),
    # --- Messung / Kalibrierung --------------------------------------------
    "illuminance_interval": _l(
        "Messintervall Helligkeit",
        "Wie oft der Sensor die Umgebungshelligkeit meldet (in Minuten).",
        _S,
        G_MEASURE,
    ),
    "temperature_calibration": _l(
        "Temperatur abgleichen",
        "Wert, der zur gemessenen Temperatur addiert wird, falls der Sensor falsch liegt.",
        _S,
        G_CALIB,
    ),
    "humidity_calibration": _l(
        "Luftfeuchte abgleichen",
        "Wert, der zur gemessenen Luftfeuchte addiert wird, falls der Sensor falsch liegt.",
        _S,
        G_CALIB,
    ),
    # --- Taster ------------------------------------------------------------
    "operation_mode": _l(
        "Tastenmodus",
        "Befehle: Der Taster steuert verbundene Lampen direkt. Ereignisse: Er meldet nur, was gedrückt wurde.",
        _S,
        G_BUTTON,
        {"command": "Befehle (steuert direkt)", "event": "Ereignisse (meldet nur)"},
    ),
    "identify": _l(
        "Gerät finden",
        "Lässt das Gerät kurz blinken, damit man es erkennt.",
        _S,
        G_MAINT,
        {"identify": "Blinken lassen"},
    ),
    # --- Messwerte / Sensoren (primary) -------------------------------------
    "power": _l("Leistung", "Aktueller Stromverbrauch.", AREA_MEASURE),
    "energy": _l("Energie gesamt", "Insgesamt verbrauchte Energie.", AREA_MEASURE),
    "energy_today": _l("Energie heute", "Heute verbrauchte Energie.", AREA_MEASURE),
    "energy_yesterday": _l("Energie gestern", "Gestern verbrauchte Energie.", AREA_MEASURE),
    "energy_month": _l("Energie im Monat", "In diesem Monat verbrauchte Energie.", AREA_MEASURE),
    "current": _l("Strom", "Aktuelle Stromstärke.", AREA_MEASURE),
    "voltage": _l("Spannung", "Aktuelle Spannung.", AREA_MEASURE),
    "temperature": _l("Temperatur", "Gemessene Temperatur.", _P),
    "humidity": _l("Luftfeuchte", "Gemessene relative Luftfeuchtigkeit.", _P),
    "illuminance": _l("Umgebungshelligkeit", "Gemessene Helligkeit im Raum.", _P),
    "illuminance_lux": _l("Umgebungshelligkeit", "Gemessene Helligkeit im Raum.", _P),
    "pressure": _l("Luftdruck", "Gemessener Luftdruck.", _P),
    "co2": _l("CO2", "Gemessene CO2-Konzentration.", _P),
    "pm25": _l("Feinstaub PM2.5", "Gemessene Feinstaubbelastung.", _P),
    "soil_moisture": _l("Bodenfeuchte", "Gemessene Feuchte der Erde.", _P),
    "occupancy": _l("Bewegung", "Meldet, ob Bewegung erkannt wurde.", _P),
    "presence": _l("Anwesenheit", "Meldet, ob jemand im Raum ist, auch bei ruhigem Sitzen.", _P),
    "contact": _l("Kontakt", "Geschlossen oder offen (Tür/Fenster).", _P),
    "water_leak": _l("Wasseralarm", "Meldet Wasser am Sensor.", _P),
    "smoke": _l("Rauchalarm", "Meldet Rauch.", _P),
    "vibration": _l("Erschütterung", "Meldet Erschütterung.", _P),
    "action": _l(
        "Tastenaktion",
        "Meldet, was am Taster gemacht wurde (drücken, drehen ...).",
        _P,
        values={
            "single": "Einfach drücken",
            "double": "Doppelt drücken",
            "hold": "Halten",
            "toggle": "Umschalten",
            "double_click": "Doppelklick",
            "long_press": "Lang drücken",
            "on": "An",
            "off": "Aus",
            "rotate_left": "Nach links drehen",
            "rotate_right": "Nach rechts drehen",
            "brightness_step_up": "Heller",
            "brightness_step_down": "Dunkler",
            "brightness_move_to_level": "Helligkeit setzen",
            "color_temperature_move": "Farbtemperatur ändern",
            "color_temperature_step_up": "Farbtemperatur höher",
            "color_temperature_step_down": "Farbtemperatur niedriger",
            "saturation_move": "Sättigung ändern",
            "hue_move": "Farbton ändern",
            "hue_stop": "Farbton stoppen",
        },
    ),
    "action_brightness_delta": _l("Helligkeitsänderung", "Detail zur Tastenaktion.", _H),
    "action_color_temperature_delta": _l("Farbtemperaturänderung", "Detail zur Tastenaktion.", _H),
    "action_rate": _l("Änderungsrate", "Detail zur Tastenaktion.", _H),
    "action_step_size": _l("Schrittweite", "Detail zur Tastenaktion.", _H),
    "action_transition_time": _l("Übergangszeit", "Detail zur Tastenaktion.", _H),
    # --- Diagnose -----------------------------------------------------------
    "battery": _l("Batterie", "Ladezustand der Batterie in Prozent.", _D),
    "battery_low": _l("Batterie schwach", "Meldet, wenn die Batterie bald leer ist.", _D),
    "linkquality": _l("Funkqualität", "Qualität der Zigbee-Verbindung (LQI, 0 bis 255).", _D),
    "tamper": _l("Sabotagekontakt", "Meldet, wenn das Gerät geöffnet wurde.", _D),
    # --- Thermostat (Grundlagen, noch kein Gerät im Bestand) ------------------
    "local_temperature": _l("Raumtemperatur", "Aktuell gemessene Temperatur.", _P),
    "occupied_heating_setpoint": _l("Zieltemperatur", "Gewünschte Temperatur.", _P),
    "current_heating_setpoint": _l("Zieltemperatur", "Gewünschte Temperatur.", _P),
    "system_mode": _l("Betriebsart", "Heizen, aus oder automatisch.", _P),
    "running_state": _l("Heizstatus", "Ob das Thermostat gerade heizt.", _P),
    "local_temperature_calibration": _l("Temperatur abgleichen", "Korrekturwert für den Temperaturfühler.", _S, G_CALIB),
    "window_detection": _l("Fenster-offen-Erkennung", "Heizung aus, wenn ein Fenster offen scheint.", _S, G_BEHAVIOR),
    "valve_detection": _l("Ventilerkennung", "Erkennt automatisch den Ventiltyp.", _S, G_BEHAVIOR),
}

# Anzeigenamen für Container-Expose "state"
_STATE_LABELS = {"light": "Licht", "switch": "Schalter", "fan": "Ventilator", "lock": "Schloss"}

# Fallback-Werte-Labels, wenn die Tabelle keine eigenen kennt.
VALUE_LABELS: dict[str, str] = {
    "on": "An",
    "off": "Aus",
    "toggle": "Umschalten",
    "low": "Niedrig",
    "medium": "Mittel",
    "high": "Hoch",
    "auto": "Automatisch",
    "manual": "Manuell",
    "heat": "Heizen",
    "cool": "Kühlen",
    "previous": "Letzter Zustand",
    "true": "An",
    "false": "Aus",
}

# Modellspezifische Overrides: nur, wo unvermeidbar. Schluessel (model, property).
MODEL_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {}

_ENDPOINT_RE = re.compile(r"^(?P<base>.+?)_(?P<ep>l\d+|left|right|center|top|bottom)$")

_KNOB_ACTIONS = {"rotate_left", "rotate_right"}
_KNOB_PROPS = {"action_step_size", "action_rate", "action_brightness_delta"}
_MEASURE_PROPS = {"power", "energy", "energy_today", "energy_yesterday", "energy_month"}
_RELAY_PROPS = {
    "inching_control_set",
    "detach_relay_mode",
    "external_trigger_mode",
    "delayed_power_on_state",
}
_CONTAINERS = {"light", "switch", "fan", "cover", "lock", "climate"}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def humanize(name: str) -> str:
    """'motion_detection_sensitivity' -> 'Motion detection sensitivity'."""
    text = re.sub(r"[_\-]+", " ", name).strip()
    return text[:1].upper() + text[1:] if text else name


def _prop(expose: dict[str, Any]) -> str:
    return str(expose.get("property") or expose.get("name") or "")


def _lookup(prop: str) -> tuple[str, str | None]:
    """Liefert (Tabellenschlüssel, Endpoint-Suffix) für eine Property."""
    if prop in LABELS:
        return prop, None
    match = _ENDPOINT_RE.match(prop)
    if match and match.group("base") in LABELS:
        return match.group("base"), match.group("ep")
    return prop, None


def _access(expose: dict[str, Any]) -> dict[str, bool]:
    raw = int(expose.get("access", 0) or 0)
    return {"read": bool(raw & 1), "write": bool(raw & 2), "get": bool(raw & 4)}


def _walk_leaves(exposes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for expose in exposes:
        if "features" in expose:
            out.extend(_walk_leaves(expose["features"]))
        else:
            out.append(expose)
    return out


def _generic_area(expose: dict[str, Any]) -> str:
    """Rückfall: Bereich nach Z2M-category, access-Bits und Typ."""
    category = expose.get("category")
    access = _access(expose)
    if category == "diagnostic":
        return AREA_DIAGNOSTIC
    if category == "config":
        return AREA_SETTINGS
    if access["write"]:
        return AREA_SETTINGS
    # nur lesbar, ohne Kategorie: Messwert oder Zustand, also Hauptfunktion
    return AREA_PRIMARY


def _value_labels(expose: dict[str, Any], table: dict[str, Any] | None) -> list[dict[str, str]] | None:
    values = expose.get("values")
    if expose.get("type") != "enum" or not isinstance(values, list):
        return None
    mapping = (table or {}).get("values") or {}
    return [
        {"value": v, "label": mapping.get(str(v)) or VALUE_LABELS.get(str(v)) or humanize(str(v))}
        for v in values
    ]


class _Context:
    def __init__(self, exposes: list[dict[str, Any]], model: str | None) -> None:
        leaves = _walk_leaves(exposes)
        self.props = {_lookup(_prop(e))[0] for e in leaves}
        self.has_measure = bool(self.props & _MEASURE_PROPS)
        self.container_types = {e.get("type") for e in exposes if "features" in e}
        self.model = model


def _build_feature(
    expose: dict[str, Any],
    ctx: _Context,
    parent: str | None,
    generic: list[str],
    forced_area: str | None = None,
) -> dict[str, Any]:
    prop = _prop(expose)
    key, suffix = _lookup(prop)
    table = LABELS.get(key)
    override = ctx.model and MODEL_OVERRIDES.get((ctx.model, key))
    if override:
        table = {**(table or {}), **override}

    if table:
        area = table["area"]
        if area == AREA_MEASURE:
            area = AREA_PRIMARY if ctx.has_measure else AREA_DIAGNOSTIC
        label = table["label"]
        if key == "state" and parent in _STATE_LABELS:
            label = _STATE_LABELS[parent]
        help_ = table["help"]
        group = table["group"]
        source = "table"
    else:
        area = _generic_area(expose)
        label = humanize(str(expose.get("label") or key))
        help_ = str(expose.get("description") or "")
        group = None
        source = "generic"
        generic.append(prop)

    if suffix:
        label = f"{label} ({suffix.upper() if suffix[0] == 'l' else humanize(suffix)})"
    if forced_area:
        area = forced_area

    feature: dict[str, Any] = {
        "property": prop,
        "label": label,
        "help": help_,
        "area": area,
        "group": group,
        "type": expose.get("type"),
        "access": _access(expose),
        "source": source,
    }
    if parent:
        feature["parent"] = parent
    if expose.get("endpoint"):
        feature["endpoint"] = expose["endpoint"]
    if expose.get("category"):
        feature["z2m_category"] = expose["category"]
    for src, dst in (
        ("unit", "unit"),
        ("value_min", "min"),
        ("value_max", "max"),
        ("value_step", "step"),
        ("value_on", "value_on"),
        ("value_off", "value_off"),
        ("value_toggle", "value_toggle"),
        ("presets", "presets"),
    ):
        if src in expose:
            feature[dst] = expose[src]
    values = _value_labels(expose, table)
    if values is not None:
        feature["values"] = values
    return feature


def _build_composite(
    expose: dict[str, Any],
    ctx: _Context,
    parent: str | None,
    generic: list[str],
) -> dict[str, Any]:
    """Composite (z. B. overload_protection) als ein Eintrag mit Kindern."""
    prop = _prop(expose)
    feature = _build_feature({**expose, "access": expose.get("access", 2)}, ctx, parent, generic)
    if feature["source"] == "generic":
        # Bereich aus Kindern ableiten
        child_areas = [_generic_area(c) for c in expose["features"] if "features" not in c]
        feature["area"] = (
            AREA_SETTINGS if AREA_SETTINGS in child_areas else (child_areas[0] if child_areas else AREA_SETTINGS)
        )
    children = []
    for child in expose["features"]:
        built = _build_feature(child, ctx, None, generic, forced_area=feature["area"])
        if built["group"] is None:
            built["group"] = feature["group"]
        children.append(built)
    feature["features"] = children
    feature["property"] = prop
    return feature


def _light_color(
    composites: list[dict[str, Any]], ctx: _Context, generic: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Faltet die Farb-Composites einer Lampe: color_xy primary, color_hs hidden."""
    has_xy = any({_prop(c) for c in comp["features"]} >= {"x", "y"} for comp in composites)
    out: list[dict[str, Any]] = []
    caps: list[str] = []
    for comp in composites:
        names = {_prop(c) for c in comp["features"]}
        if names >= {"x", "y"}:
            kind, area = "color_xy", AREA_PRIMARY
        elif names >= {"hue", "saturation"}:
            kind, area = "color_hs", AREA_HIDDEN if has_xy else AREA_PRIMARY
        else:
            continue
        feat = _build_composite({**comp, "property": "color", "name": kind}, ctx, "light", generic)
        feat["property"] = kind
        feat.update(LABELS_COLOR[kind])
        feat["area"] = area
        feat["source"] = "table"
        for child in feat["features"]:
            child["area"] = area if kind == "color_xy" else AREA_HIDDEN
        out.append(feat)
        if area == AREA_PRIMARY:
            caps.append(kind)
    return out, caps


LABELS_COLOR = {
    "color_xy": {"label": LABELS["color_xy"]["label"], "help": LABELS["color_xy"]["help"], "group": None},
    "color_hs": {"label": LABELS["color_hs"]["label"], "help": LABELS["color_hs"]["help"], "group": None},
}


def _device_type(exposes: list[dict[str, Any]], ctx: _Context) -> str:
    types = ctx.container_types
    props = ctx.props
    if "climate" in types:
        return "thermostat"
    if "light" in types:
        return "light"
    if "switch" in types:
        if ctx.has_measure or "current" in props:
            return "plug_meter"
        if props & _RELAY_PROPS:
            return "relay"
        return "switch"
    if "presence" in props:
        return "presence"
    if "occupancy" in props:
        return "motion"
    if "contact" in props:
        return "contact"
    if "action" in props:
        action_values: set[str] = set()
        for leaf in _walk_leaves(exposes):
            if _prop(leaf) == "action":
                action_values.update(str(v) for v in leaf.get("values", []))
        if action_values & _KNOB_ACTIONS or props & _KNOB_PROPS:
            return "knob"
        return "remote"
    if "temperature" in props or "humidity" in props:
        return "climate_sensor"
    return "unknown"


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------


def classify(definition_exposes: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    """Ordnet alle Exposes eines Geräts Bereichen zu und leitet die Geräteart ab."""
    exposes = list(definition_exposes or [])
    ctx = _Context(exposes, model)
    generic: list[str] = []
    features: list[dict[str, Any]] = []
    capabilities: list[str] = []

    for expose in exposes:
        etype = expose.get("type")
        if etype in _CONTAINERS and "features" in expose:
            container = str(etype)
            subs = expose["features"]
            colors = [s for s in subs if s.get("type") == "composite" and _prop(s) == "color"]
            if container == "light" and colors:
                color_feats, caps = _light_color(colors, ctx, generic)
                features.extend(color_feats)
                capabilities.extend(caps)
            for sub in subs:
                if sub in colors:
                    continue
                if "features" in sub:
                    features.append(_build_composite(sub, ctx, container, generic))
                else:
                    features.append(_build_feature(sub, ctx, container, generic))
                    if container == "light" and _prop(sub) in ("brightness", "color_temp"):
                        capabilities.append(_prop(sub))
        elif "features" in expose:
            features.append(_build_composite(expose, ctx, None, generic))
        else:
            features.append(_build_feature(expose, ctx, None, generic))

    result: dict[str, Any] = {
        "device_type": _device_type(exposes, ctx),
        "capabilities": capabilities,
        AREA_PRIMARY: [],
        AREA_SETTINGS: [],
        AREA_DIAGNOSTIC: [],
        AREA_HIDDEN: [],
    }
    for feat in features:
        result[feat["area"]].append(feat)
    groups: list[str] = []
    for feat in result[AREA_SETTINGS]:
        grp = feat["group"] or "Weitere Einstellungen"
        feat["group"] = grp
        if grp not in groups:
            groups.append(grp)
    result["setting_groups"] = groups
    result["generic"] = generic
    return result


def iter_features(classified: dict[str, Any]):
    """Alle Features inklusive Kindern (für Tests und Auswertung)."""
    stack = [f for area in AREAS for f in classified[area]]
    while stack:
        feat = stack.pop(0)
        yield feat
        stack = list(feat.get("features", [])) + stack

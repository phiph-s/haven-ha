# Haven: Home-Assistant-Integration

*English: `haven` is a Home Assistant custom integration that is the backend for the Haven Android app ([phiph-s/haven](https://github.com/phiph-s/haven)). It exposes rooms, Zigbee2MQTT devices, groups, floor plan and app preferences through the Home Assistant WebSocket API. It needs the MQTT integration and Zigbee2MQTT 2.x. Install it via HACS as a custom repository (`https://github.com/phiph-s/haven-ha`, category Integration) or copy `custom_components/haven` manually.*

Die Integration ist die Gegenstelle der Flutter-App [Haven](https://github.com/phiph-s/haven). Die App spricht ausschließlich über die HA-WebSocket-API mit ihr. Zigbee2MQTT wird über die MQTT-Verbindung von HA gelesen und gesteuert (`<base>/bridge/request/...`).

## Was sie tut
- Liefert Räume (HA-Bereiche) mit Lampen, Lichtgruppen, Klima- und Bewegungssensoren und den Geräten je Raum.
- Liest die Zigbee2MQTT-Geräte und -Gruppen (`bridge/devices`, `bridge/groups`) und klassifiziert die Exposes für die App (Hauptbedienung, Einstellungen, Diagnose, deutsche Beschriftungen).
- Schreibt zurück: Geräte steuern, umbenennen, Bereich setzen (mit Gruppenabgleich), Anlernen, Interview, Entfernen, Netzwerkkarte.
- Speichert Grundriss, Widgets und App-Einstellungen (Lichter, die die App ausblendet) zentral in `.storage`, damit mehrere Geräte denselben Stand sehen.

## Voraussetzungen
- Home Assistant 2025.1 oder neuer mit eingerichteter **MQTT-Integration**.
- **Zigbee2MQTT 2.x**, das auf `<base>/bridge/...` sendet (Standard `zigbee2mqtt`).
- Ein HA-Administrator-Konto für die schreibenden Befehle (die App verbindet sich mit dessen Long-Lived-Token).

## Installation
### HACS (benutzerdefiniertes Repository)
1. HACS öffnen, oben rechts Menü, **Benutzerdefinierte Repositories**.
2. `https://github.com/phiph-s/haven-ha` eintragen, Kategorie **Integration**, hinzufügen.
3. „Haven“ in HACS suchen, herunterladen, HA neu starten.
4. Einstellungen, Geräte & Dienste, Integration hinzufügen, „Haven“. Es gibt nur eine Instanz. Option: Z2M-Basis-Topic (Standard `zigbee2mqtt`), auch nachträglich änderbar (lädt neu).

### Manuell
Den Ordner `custom_components/haven/` nach `/config/custom_components/haven/` kopieren (oder `haven.zip` aus dem neuesten Release dort entpacken), HA neu starten, Integration wie oben hinzufügen.

Abonnierte Topics: `<base>/bridge/devices`, `bridge/groups`, `bridge/info`, `bridge/event`, `bridge/state` (online/offline), `bridge/response/#` (Antworten auf Requests) und `<base>/+` (Gerätezustände, `bridge` wird gefiltert). Für Friendly-Names mit `/` greift `+` nicht: Haven abonniert dafür das exakte Topic `<base>/<name>` und gleicht die Abos bei jeder neuen `bridge/devices` an.

## WebSocket-Befehle
Alle Befehle: `{"id": N, "type": "...", ...}`, Antwort im HA-üblichen Format `{"id": N, "type": "result", "success": true, "result": ...}`. Fehler: `success: false`, `error.code` (`not_found` wenn Haven nicht geladen oder Gerät unbekannt). Texte sind deutsch. Die Beispiele sind gekürzt.

### `haven/info`
```json
{"domain": "haven", "version": "0.2.0", "api_version": 1,
 "features": ["light_groups", "room_devices", "prefs"], "loaded": true,
 "z2m": {"base_topic": "zigbee2mqtt", "bridge_state": "online", "info_received": true,
         "version": "2.x", "coordinator_type": "...", "permit_join": false,
         "channel": 15, "pan_id": 6754,
         "device_count": 42, "group_count": 4, "devices_received": true, "groups_received": true}}
```
(`z2m` ist `null`, wenn der Entry nicht geladen ist. `channel` und `pan_id` kommen aus `bridge/info` → `network`, sonst `null`.) `api_version` steigt nur bei inkompatiblen Änderungen. Neues kommt additiv und steht in `features` (ab 0.2.0, fehlt bei 0.1.0): `light_groups` (`light_groups`, `group_entity` in `haven/rooms`), `room_devices` (`devices` in `haven/rooms`), `prefs` (`haven/prefs/*`).

### `haven/rooms`
Raum = HA-Bereich. Bereich einer Entity: eigener Bereich vor Gerätebereich. Gezählt werden nur Haupt-Entities (ohne `entity_category`, nicht deaktiviert, nicht versteckt).
- `lights`: Lampen als Entity-IDs, ohne Lichtgruppen. `zigbee_lights`: IEEEs der Zigbee-Lampen darunter.
- `light_groups`: Lichtgruppen im Bereich, nach `entity_id` sortiert. `type`: `ha_group` (Registry-Plattform `group` oder Attribut `entity_id` als Liste) oder `z2m_group` (Attribut `group_entities` oder Geräte-/Anzeigename gleich einer Z2M-Gruppe, ohne Groß/klein, nur bei Geräten ohne IEEE). `name`: `friendly_name` oder `null`. `members`: Entity-IDs aus `entity_id` bzw. `group_entities`, sortiert. `z2m_group_id`: bei `z2m_group` die ID aus `bridge/groups` (Abgleich über den Namen), sonst `null`.
- `group`: passende Z2M-Gruppe, gemessen an den Zigbee-Lampen darin (Knöpfe und Sensoren als Mitglieder zählen nicht). `match`: `exact` oder `partial` (mit `missing`/`extra`, IEEE-Listen), sonst `null`.
- `group_entity`: HA-Entity dieser Z2M-Gruppe, gesucht unter allen Lichtern, auch ohne Bereich (Z2M-Gruppen haben meist keinen). Ohne Treffer `null`.
- `devices`: HA-Geräte mit mindestens einer Haupt-Entity im Raum, nach Name sortiert. `name` ist der Name vom Nutzer, sonst der vom Gerät. `entities`: nur die Haupt-Entities, die im Raum landen (ohne Lichtgruppen). Dienst-Geräte (`entry_type` `service`) fehlen.
- `unassigned`: HA-Geräte ohne Bereich mit ihren Haupt-Entities, gleiches Format wie `devices`. Z2M-Gruppen erscheinen hier nicht (ab 0.2.0).
```json
{"rooms": [{"area_id": "kochbereich", "name": "Kochbereich", "floor_id": null, "icon": null,
            "lights": ["light.kochbereich_arbeitslicht_1", "light.kochbereich_arbeitslicht_2"],
            "light_groups": [],
            "zigbee_lights": ["0x00124b0000000013", "0x00124b0000000012"],
            "climate": {"temperature": ["sensor.x_temperature"], "humidity": ["sensor.x_humidity"]},
            "motion": ["binary_sensor.bewegungsmelder_kochbereich_occupancy"],
            "group": {"id": 3, "friendly_name": "ZGKochbereich", "match": "exact", "missing": [], "extra": []},
            "group_entity": "light.zgkochbereich",
            "devices": [{"device_id": "abc123", "name": "Kochbereich Arbeitslicht 1", "manufacturer": "Tuya", "model": "TS0502B",
                         "ieee": "0x00124b0000000013", "entities": ["light.kochbereich_arbeitslicht_1"]}]},
           {"area_id": "salon", "name": "Salon", "...": "...",
            "light_groups": [{"entity_id": "light.salon", "type": "ha_group", "name": "Salon",
                              "members": ["light.lampe_salon", "light.tischlampe_salon"], "z2m_group_id": null}]}],
 "unassigned": [{"device_id": "def456", "name": "Sensor Waschraum", "manufacturer": "SONOFF", "model": "SNZB-02",
                 "ieee": "0x...", "entities": ["sensor.sensor_waschraum_temperature", "sensor.sensor_waschraum_humidity"]}]}
```

### `haven/zigbee/devices`
```json
{"devices": [{"ieee": "0x00124b000000000a", "friendly_name": "Bewegungsmelder Waschraum", "type": "EndDevice",
              "area_id": "waschraum", "area_name": "Waschraum", "ha_entities": ["binary_sensor.bewegungsmelder_waschraum_occupancy"],
              "vendor": "HOBEIAN", "model": "ZG-204ZL", "description": "Luminance motion sensor",
              "power_source": "Battery", "supported": true, "disabled": false, "interview_completed": true,
              "image": "https://www.zigbee2mqtt.io/images/devices/ZG-204ZL.png",
              "device_type": "motion", "capabilities": []}]}
```
`device_type`: `light, plug_meter, switch, relay, climate_sensor, motion, presence, contact, remote, knob, thermostat, unknown`. `capabilities` bei Lichtern: `brightness, color_temp, color_xy, color_hs`.

### `haven/zigbee/device` (`{"ieee": "0x..."}`)
Wie ein Listeneintrag (mit `area_id`, `area_name`, `ha_entities`; ohne HA-Gerät `null`, `null`, `[]`) plus `options` (Z2M-Geräteoptionen, roh) und `classified` mit den Bereichen `primary`, `settings`, `diagnostic`, `hidden` und `setting_groups` (Reihenfolge der Gruppen):
```json
{"classified": {
  "primary": [{"property": "occupancy", "label": "Zone", "help": "Meldet, ob Bewegung erkannt wurde.",
               "area": "primary", "group": null, "type": "binary",
               "access": {"read": true, "write": false, "get": false}, "source": "table"}],
  "settings": [{"property": "sensitivity", "label": "Empfindlichkeit",
                "help": "Wie schnell der Sensor Bewegung erkennt. ...", "area": "settings", "group": "Erkennung",
                "type": "enum", "access": {"read": true, "write": true, "get": false}, "source": "table",
                "values": [{"value": "low", "label": "Niedrig"}, {"value": "medium", "label": "Mittel"},
                           {"value": "high", "label": "Hoch"}]}],
  "diagnostic": [{"property": "battery", "label": "Batterie", "area": "diagnostic", "unit": "%", "min": 0, "max": 100}],
  "hidden": [],
  "setting_groups": ["Erkennung", "Messung"]}}
```
Feature-Felder: `property` (Z2M-Schlüssel), `label`, `help`, `area`, `group`, `type` (`binary|numeric|enum|text|composite`), `access` (`read/write/get`), `source` (`table` = gepflegt, `generic` = Rückfall, Label nur humanisiert, englisch), optional `unit`, `min`, `max`, `step`, `value_on/off/toggle`, `presets`, `values` (Enum mit deutschen Labels), `parent` (`light`/`switch`), `endpoint`, `z2m_category`, `features` (Kinder bei Composites). Farbe: `color_xy` (primär) mit Kindern `x`,`y`; `color_hs` (versteckt, bei Lampen ohne xy primär) mit `hue`,`saturation`.

### `haven/zigbee/groups`
```json
{"groups": [{"id": 3, "friendly_name": "ZGKochbereich",
             "members": [{"ieee": "0x00124b0000000012", "endpoint": 11, "friendly_name": "Kochbereich Arbeitslicht 2", "device_type": "light"}],
             "scenes": [{"id": 0, "name": "Full"}]}]}
```

### `haven/subscribe_zigbee`
Antwort sofort `result: null`, danach Events (`{"id": N, "type": "event", "event": {...}}`) bis `unsubscribe_events` mit der Abo-ID. `kind`:
- `devices`: `{"kind": "devices", "devices": [...]}` wie `haven/zigbee/devices`
- `groups`: `{"kind": "groups", "groups": [...]}`
- `info`, `state`: `{"kind": "info", ...}` mit dem Inhalt von `haven/info.z2m` (so auch der Bridge-Zustand `kind: "state"`)
- `state` mit `ieee`: Gerätezustand `{"kind": "state", "ieee": "0x...", "values": {"state": "ON", "brightness": 120}}`. Höchstens 4 Meldungen pro Sekunde und Gerät, übersprungene Werte werden zusammengeführt nachgeliefert. Unterscheidung vom Bridge-Zustand: nur dieser hat `ieee`.
- `event`: `{"kind": "event", "event": {"type": "device_joined", "data": {...}}}` (Z2M-Bridge-Event roh)

## Schreibende Befehle
Alle Befehle in diesem Abschnitt außer den Lesebefehlen (`state`, `floorplan/get`, `widgets/get`, `prefs/get`, `networkmap` ohne `refresh`) erfordern einen HA-Administrator. Befehle über `bridge/request/...` melden Erfolg erst, wenn Z2M auf `<base>/bridge/response/...` bestätigt hat (Abgleich über das Feld `transaction`, Präfix `haven-`). Timeout 10 s (Netzwerkkarte 90 s). Fehler: `success: false`, `error.code` und deutscher `error.message`. Codes: `timeout`, `z2m_error` (Z2M lehnt ab, Text von Z2M steht in der Meldung), `bridge_offline`, `mqtt_error`, `not_found`, `not_supported`, `name_taken`, `busy`, `invalid_format`, `unauthorized`.

```json
{"id": 9, "type": "result", "success": false,
 "error": {"code": "timeout", "message": "Keine Antwort von Zigbee2MQTT auf 'device/rename' innerhalb von 10 Sekunden. Läuft Zigbee2MQTT?"}}
```

### `haven/zigbee/set`
Topic: `<base>/<friendly_name>/set` (ohne Bestätigung, Z2M antwortet nicht). Endpoint-Suffixe wie `state_l1` stehen in `values` und werden unverändert durchgereicht.
```json
{"type": "haven/zigbee/set", "ieee": "0x00124b0000000012", "values": {"state": "ON", "brightness": 120}}
→ {"ok": true}
```

### `haven/zigbee/rename`
Topic: `bridge/request/device/rename`, Payload `{from, to, homeassistant_rename}`. Fehler `name_taken`, wenn ein anderes Gerät den Namen schon trägt.
```json
{"type": "haven/zigbee/rename", "ieee": "0x...", "name": "Lampe Flur", "rename_ha_entities": true}
→ {"ok": true, "name": "Lampe Flur"}
```

### `haven/zigbee/set_area`
Setzt den Bereich des HA-Geräts in der Device-Registry (Z2M bleibt unverändert). `area_id: null` entfernt den Bereich. Mit `sync_group: true` und einem Licht: Haven bestimmt die Raumgruppe des alten und neuen Bereichs (Gruppe mit den meisten Lichtern des Bereichs, ohne das Gerät selbst), entfernt das Gerät aus der alten und nimmt es in die neue auf. Topics: `bridge/request/group/members/remove` und `.../add`, Payload `{group, device, endpoint?}`. Der Endpoint kommt aus der bisherigen Mitgliedschaft. Fehler einer Gruppenaktion brechen nicht ab, der Bereich ist dann schon gesetzt und der Fehler steht in `actions`.
```json
{"type": "haven/zigbee/set_area", "ieee": "0x...", "area_id": "kochbereich", "sync_group": true}
→ {"ok": true, "area_id": "kochbereich", "previous_area_id": "waschraum",
   "group_sync": {"old_group": "ZGWaschraum", "new_group": "ZGKochbereich", "skipped": null,
                  "actions": [{"action": "remove", "group": "ZGWaschraum", "group_id": 4, "status": "ok"},
                              {"action": "add", "group": "ZGKochbereich", "group_id": 3, "status": "ok"}]}}
```
`group_sync` fehlt ohne `sync_group` oder bei gleichem Bereich. `skipped`: `null`, `not_a_light`, `same_group`, `no_new_area`, `no_group_for_area` (Aktionen für den anderen Teil können trotzdem laufen).

### `haven/zigbee/identify`
Topic: `<base>/<friendly_name>/set` mit `{"identify": "identify"}`. Nur wenn das Gerät ein beschreibbares `identify`-Expose hat, sonst Fehler `not_supported`.
```json
{"type": "haven/zigbee/identify", "ieee": "0x..."} → {"ok": true}
```

### `haven/zigbee/interview`
Topic: `bridge/request/device/interview`, Payload `{id}` (IEEE).
```json
{"type": "haven/zigbee/interview", "ieee": "0x..."} → {"ok": true}
```

### `haven/zigbee/remove`
Topic: `bridge/request/device/remove`, Payload `{id, force}`.
```json
{"type": "haven/zigbee/remove", "ieee": "0x...", "force": false} → {"ok": true}
```

### `haven/zigbee/permit_join`
Topic: `bridge/request/permit_join`, Payload `{time, device?}` (Z2M 2.x), `time` 0 bis 254, Standard 240. `time: 0` schließt das Netz. `device` (IEEE) lässt nur über diesen Router/Koordinator anlernen.
```json
{"type": "haven/zigbee/permit_join", "time": 240, "device": null} → {"ok": true}
```

### `haven/zigbee/state`
Letzter bekannter Zustand je Gerät aus `<base>/<friendly_name>` (zusammengeführt, nur seit Start von Haven gesehene Meldungen). Gruppen erscheinen nicht.
```json
{"type": "haven/zigbee/state"} → {"states": {"0x00124b0000000012": {"state": "ON", "brightness": 120, "linkquality": 88}}}
```

### `haven/zigbee/networkmap`
Mit `refresh: true` (nur Admin): Topic `bridge/request/networkmap`, Payload `{type: "raw", routes: false}`, Timeout 90 s, nur eine Messung zur Zeit (sonst `busy`). Aus `data.value` werden `nodes` und `links` mit Zeitstempel (UTC, ISO 8601) im Speicher gehalten (nicht über einen HA-Neustart). Ohne `refresh` kommt der Cache oder `null`.
```json
{"type": "haven/zigbee/networkmap", "refresh": true}
→ {"nodes": [{"ieeeAddr": "0x...", "friendlyName": "...", "type": "Router"}],
   "links": [{"source": {"ieeeAddr": "0x..."}, "target": {"ieeeAddr": "0x..."}, "lqi": 120}],
   "timestamp": "2026-11-15T12:00:00+00:00"}
```

### `haven/floorplan/get` und `haven/floorplan/set`
Speicher: `.storage/haven.floorplan` (Store-Version 1, Inhalt `{"plan": {...}}`). `plan` ist das Dokument von `HavenFloorPlan.toJson` der App. Geprüft wird nur: Objekt, `version` (Zahl), `rooms` (Liste, jeder Eintrag mit `id`), `placements` (Liste, jeder Eintrag mit `entityId`). Geometrie prüft die App.
```json
{"type": "haven/floorplan/get"} → {"plan": {"version": 1, "grid": {"cols": 20, "rows": 26, "cellMeters": 0.5}, "rooms": [], "placements": []}}
{"type": "haven/floorplan/get"} → {"plan": null}
{"type": "haven/floorplan/set", "plan": {"version": 1, "rooms": [], "placements": []}} → {"ok": true}
```

### `haven/widgets/get` und `haven/widgets/set`
Speicher: `.storage/haven.widgets` (`{"widgets": [...]}`). Format je Eintrag wie `HavenWidgetConfig.toJson` der App: `id`, `type` (`power`, `entityToggle`, `lowBattery`, `weather`), `entities`, `options`. Geprüft: Liste, `id` und `type` als Text, IDs eindeutig. Noch nie gespeichert: `null`.
```json
{"type": "haven/widgets/set", "widgets": [{"id": "power", "type": "power", "entities": [], "options": {}}]} → {"ok": true}
{"type": "haven/widgets/get"} → {"widgets": [{"id": "power", "type": "power", "entities": [], "options": {}}]}
```

### `haven/prefs/get` und `haven/prefs/set`
Speicher: `.storage/haven.prefs` (`{"prefs": {...}}`). Ein Dokument für App-Einstellungen, `set` ersetzt es ganz. `excluded_lights`: Licht-Entities, die die App ausblendet. Geprüft: Objekt, `excluded_lights` (optional) als Liste von Texten mit `light.`-Präfix, sonst `invalid_format`. Doppelte Einträge entfernt Haven still (erste Stelle bleibt), die Antwort enthält das gespeicherte Dokument. Unbekannte Schlüssel bleiben unverändert erhalten. `excluded_lights` ist in Antworten immer da, noch nie gespeichert: `[]`.
```json
{"type": "haven/prefs/get"} → {"prefs": {"excluded_lights": []}}
{"type": "haven/prefs/set", "prefs": {"excluded_lights": ["light.salon", "light.salon"]}}
→ {"ok": true, "prefs": {"excluded_lights": ["light.salon"]}}
```

## Entwicklung und Tests
```
cd integration && python3 -m pytest -q
```
Die Tests laden die HA-freien Module ohne Home Assistant (`tests/conftest.py`): `exposes`, `z2m`, `commands` (Payload-Bauer, Transaktions-Abgleich), `throttle`, `statecache`, `groupsync` (Raumwechsel), `appdata` (Validierung, Prefs), `lightgroups` (Gruppenerkennung), `rooms` (Zusammenstellung von `haven/rooms`). Testdaten: ein anonymisierter Bestand mit 42 Geräten in `tests/fixtures/inventory.json` (echte Z2M-Modelle und Exposes, erfundene Namen und IEEE-Adressen) und verkleinerte Lichtzustände in `tests/data/light_states.json`. Die HA-abhängigen Module (`__init__`, `config_flow`, `websocket_api`, `storage`) sind nur per `py_compile` geprüft. Version 0.1.0 lief auf echtem HA (lesende Befehle), 0.2.0 noch nicht.

Registry-Zugriffe laufen ohne `dev_reg.devices.values()` / `ent_reg.entities.values()` (ab HA 2027.9 veraltet): `haven/rooms` über `async_list_areas`, `dr/er.async_entries_for_area`, `er.async_entries_for_device`, Geräte ohne Bereich über `dr.async_entries_for_config_entry` aller Config-Entries, Lichter ohne Bereich über `hass.states.async_all("light")`. Die HA-Zuordnung der Zigbee-Geräte (`area_id`, `ha_entities`) liest nur Geräte der MQTT-Config-Entries. Geräte ganz ohne Config-Entry fehlen in `unassigned`.

Neue Z2M-Property: Eintrag in `LABELS` (`exposes.py`) ergänzen. Ohne Eintrag läuft sie über den generischen Rückfall (`source: "generic"`, auch in `classify()["generic"]` aufgelistet).

## Offen
- Gruppenerkennung über `group_entities` stammt aus den Zuständen von HA mit Z2M 2.14.3. Mit anderen HA-Versionen ungeprüft, dafür gibt es die Namensregel als Rückfall.
- Version 0.2.0 ist nur mit den Tests (ohne laufendes HA) geprüft, die lesenden Befehle liefen bereits in 0.1.0 auf echtem HA.

## Lizenz
MIT, siehe `LICENSE`.

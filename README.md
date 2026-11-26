# Haven: Home-Assistant-Integration

*English: `haven` is a Home Assistant custom integration that is the backend for the Haven Android app ([phiph-s/haven](https://github.com/phiph-s/haven)). It exposes rooms, Zigbee2MQTT devices, groups, floor plan and app preferences through the Home Assistant WebSocket API. It needs the MQTT integration and Zigbee2MQTT 2.x. Install it via HACS as a custom repository (`https://github.com/phiph-s/haven-ha`, category Integration) or copy `custom_components/haven` manually.*

Die Integration ist die Gegenstelle der Flutter-App [Haven](https://github.com/phiph-s/haven). Die App spricht ausschließlich über die HA-WebSocket-API mit ihr. Zigbee2MQTT wird über die MQTT-Verbindung von HA gelesen und gesteuert (`<base>/bridge/request/...`).

## Was sie tut
- Liefert Räume (HA-Bereiche) mit Lampen, Lichtgruppen, Klima- und Bewegungssensoren und den Geräten je Raum.
- Liest die Zigbee2MQTT-Geräte und -Gruppen (`bridge/devices`, `bridge/groups`) und klassifiziert die Exposes für die App (Hauptbedienung, Einstellungen, Diagnose, deutsche Beschriftungen).
- Schreibt zurück: Geräte steuern, umbenennen, Bereich setzen (mit Gruppenabgleich), Anlernen, Interview, Entfernen, Netzwerkkarte.
- Speichert Grundriss, Widgets und App-Einstellungen (Lichter, die die App ausblendet) zentral in `.storage`, damit mehrere Geräte denselben Stand sehen.
- Zeigt die Wohnung als Bild im aktuellen Licht (Image-Entities `image.haven_karte_*`, je Perspektive und Design) für Widgets und Displays. Die App liefert dafür nach Kartenänderungen Lichtebenen, Home Assistant setzt sie ohne GPU zusammen (braucht numpy und Pillow).

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

Abonnierte Topics: `<base>/+/availability` (Z2M-Verfügbarkeit, nur vorhanden wenn in Z2M an; Namen mit `/` wie bei den Zuständen exakt), `<base>/bridge/devices`, `bridge/groups`, `bridge/info`, `bridge/event`, `bridge/state` (online/offline), `bridge/response/#` (Antworten auf Requests) und `<base>/+` (Gerätezustände, `bridge` wird gefiltert). Für Friendly-Names mit `/` greift `+` nicht: Haven abonniert dafür das exakte Topic `<base>/<name>` und gleicht die Abos bei jeder neuen `bridge/devices` an.

## WebSocket-Befehle
Alle Befehle: `{"id": N, "type": "...", ...}`, Antwort im HA-üblichen Format `{"id": N, "type": "result", "success": true, "result": ...}`. Fehler: `success: false`, `error.code` (`not_found` wenn Haven nicht geladen oder Gerät unbekannt). Texte sind deutsch. Die Beispiele sind gekürzt.

### `haven/info`
```json
{"domain": "haven", "version": "0.3.0", "api_version": 1,
 "features": ["light_groups", "room_devices", "prefs", "data_events", "networkmap_cache", "networkmap_background", "ghost_detection", "scenes"], "loaded": true,
 "z2m": {"base_topic": "zigbee2mqtt", "bridge_state": "online", "info_received": true,
         "version": "2.x", "coordinator_type": "...", "permit_join": false,
         "channel": 15, "pan_id": 6754,
         "device_count": 42, "group_count": 4, "devices_received": true, "groups_received": true,
         "last_seen_enabled": true, "availability_enabled": false}}
```
(`z2m` ist `null`, wenn der Entry nicht geladen ist. `channel` und `pan_id` kommen aus `bridge/info` → `network`, sonst `null`.) `api_version` steigt nur bei inkompatiblen Änderungen. Neues kommt additiv und steht in `features` (ab 0.2.0, fehlt bei 0.1.0): `light_groups` (`light_groups`, `group_entity` in `haven/rooms`), `room_devices` (`devices` in `haven/rooms`), `prefs` (`haven/prefs/*`), `data_events` (Event `haven_data_updated`, ab 0.3.0, auch `layout`/`updated_at` bei Widgets und Grundriss), `networkmap_cache` (Cache übersteht einen HA-Neustart, ab 0.3.0), `networkmap_background` (Netzmessung im Hintergrund, ab 0.5.1), `ghost_detection` (`last_seen`, `last_seen_source`, `availability`, `haven/zigbee/enable_last_seen`, ab 0.5.2), `scenes` (`haven/zigbee/scene_*`, `look` an den Szenen von `haven/zigbee/groups`, ab 0.6.0), `heating` (`haven/heating/*`, Heizungsregelung je Raum), `render` (`haven/render/*`, Bilder der Wohnung), `render_files` (`haven/render/file`, Ebenen zurücklesen), `scene_sync` (`haven/zigbee/scene_check`, `scene_adopt`, `scene_repair`, `sync`/`checked_at` an den Szenen von `haven/zigbee/groups`). `z2m.last_seen_enabled` ist `config.advanced.last_seen != "disable"` aus `bridge/info`, `z2m.availability_enabled` ist `config.availability` bzw. bei einem Objekt dessen `enabled` (Z2M 2.x; Objekt ohne `enabled` = an; Z2M: Standard aus; vor 0.6.4 galt jedes Objekt als an); beide `null`, solange `bridge/info` fehlt. `version` kommt aus `manifest.json`.

### `haven/rooms`
Raum = HA-Bereich. Bereich einer Entity: eigener Bereich vor Gerätebereich. Gezählt werden nur Haupt-Entities (ohne `entity_category`, nicht deaktiviert, nicht versteckt).
- `lights`: Lampen als Entity-IDs, ohne Lichtgruppen. `zigbee_lights`: IEEEs der Zigbee-Lampen darunter, `light_ieee`: Entity-ID -> IEEE dazu.
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

Geräte anderer Funkprotokolle bekommen ab 0.6.34 `protocol` (`"matter"` oder `"zwave"`, aus den Kennungen der Geräte-Registry) und `sw_version`, z. B. `{"device_id": "...", "name": "Stehleuchte", "manufacturer": "Nanoleaf", "model": "Essentials A19", "ieee": null, "protocol": "matter", "sw_version": "3.5.10", "entities": ["light.stehleuchte"]}`. Zigbee erkennt die App an `ieee`. Feature-Flag: `device_protocol`.

### `haven/zigbee/devices`
```json
{"devices": [{"ieee": "0x00124b000000000a", "friendly_name": "Bewegungsmelder Waschraum", "type": "EndDevice",
              "area_id": "waschraum", "area_name": "Waschraum", "ha_entities": ["binary_sensor.bewegungsmelder_waschraum_occupancy"],
              "vendor": "HOBEIAN", "model": "ZG-204ZL", "description": "Luminance motion sensor",
              "power_source": "Battery", "supported": true, "disabled": false, "interview_completed": true,
              "image": "https://www.zigbee2mqtt.io/images/devices/ZG-204ZL.png",
              "device_type": "motion", "capabilities": [],
              "last_seen": "2026-04-29T08:15:00+00:00", "last_seen_source": "z2m", "availability": "online"}]}
```
**Zuletzt gesehen** (ab 0.5.2, Feature `ghost_detection`), je Gerät, in `haven/zigbee/devices` und `haven/zigbee/device`:
- `last_seen`: ISO 8601 (UTC) oder `null`. Quellen: (1) `last_seen` im Gerätezustand, wenn die Z2M-Option `advanced.last_seen` an ist (ISO-Text oder Millisekunden; das ist Z2Ms eigener Wert, auch nach einem Z2M-Neustart richtig). Gilt, wenn vorhanden. Sonst der jüngere Zeitpunkt aus (2) Empfang der letzten echten Gerätemeldung (ab 0.6.3) und (3) `nodes[].lastSeen` (ms) der zuletzt gemessenen Netzkarte (Cache bzw. Store, nur ein Schnappschuss der Messung). Beim Empfang zählen keine retained Meldungen und nichts in den ersten 60 s, nachdem die Bridge `online` meldet: Z2M veröffentlicht beim Start die gespeicherten Zustände aller Geräte, das ließe Geister frisch aussehen. Der Empfang lebt nur im Speicher (nach einem HA-Neustart erst wieder mit der nächsten Meldung).
- `last_seen_source`: `"z2m"`, `"message"` (ab 0.6.3), `"networkmap"` oder `null`.
- `networkmap_at`: Zeitstempel der Netzmessung, wenn `last_seen_source` `"networkmap"` ist, sonst `null` (ab 0.6.4). Der Wert ist ein Schnappschuss: Aussagekräftig ist, wie lange das Gerät zum Zeitpunkt der Messung schon schwieg, nicht der Abstand zu jetzt.
- `availability`: Retained Verfügbarkeitsmeldungen, die vor `bridge/devices` ankommen, ordnet Haven nachträglich zu (ab 0.6.3, vorher blieb sie bis zur nächsten Änderung `null`).
- `availability`: `"online"`, `"offline"` oder `null` (Z2M-Verfügbarkeit aus `<base>/<name>/availability`, Payload `{"state":"online"}` oder Text; `null` ohne Meldung). Änderungen kommen in `haven/subscribe_zigbee` als `{"kind": "availability", "ieee", "availability"}`.
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
             "scenes": [{"id": 0, "name": "Full", "look": null}]}]}
```
`look` (Feature `scenes`): Farben beim Speichern über Haven, sonst `null`, siehe `haven/zigbee/scene_store`. `sync` und `checked_at` (Feature `scene_sync`) siehe Szenen-Abgleich.

### `haven/subscribe_zigbee`
Antwort sofort `result: null`, danach Events (`{"id": N, "type": "event", "event": {...}}`) bis `unsubscribe_events` mit der Abo-ID. `kind`:
- `devices`: `{"kind": "devices", "devices": [...]}` wie `haven/zigbee/devices`
- `groups`: `{"kind": "groups", "groups": [...]}`
- `info`, `state`: `{"kind": "info", ...}` mit dem Inhalt von `haven/info.z2m` (so auch der Bridge-Zustand `kind: "state"`)
- `state` mit `ieee`: Gerätezustand `{"kind": "state", "ieee": "0x...", "values": {"state": "ON", "brightness": 120}}`. Höchstens 4 Meldungen pro Sekunde und Gerät, übersprungene Werte werden zusammengeführt nachgeliefert. Unterscheidung vom Bridge-Zustand: nur dieser hat `ieee`.
- `event`: `{"kind": "event", "event": {"type": "device_joined", "data": {...}}}` (Z2M-Bridge-Event roh)

## Schreibende Befehle
Alle Befehle in diesem Abschnitt außer den Lesebefehlen (`state`, `floorplan/get`, `widgets/get`, `prefs/get`, `networkmap` ohne `refresh`) erfordern einen HA-Administrator. Befehle über `bridge/request/...` melden Erfolg erst, wenn Z2M auf `<base>/bridge/response/...` bestätigt hat (Abgleich über das Feld `transaction`, Präfix `haven-`). Timeout 10 s (Netzwerkkarte 10 min). Fehler: `success: false`, `error.code` und deutscher `error.message`. Codes: `timeout`, `z2m_error` (Z2M lehnt ab, Text von Z2M steht in der Meldung), `bridge_offline`, `mqtt_error`, `not_found`, `not_supported`, `name_taken`, `busy`, `invalid_format`, `unauthorized`, `conflict` (`base_revision` veraltet, siehe „Revisionen“).

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

### `haven/zigbee/create_room_group` (`{"area_id": "..."}`, Feature `room_groups`)
Legt für einen Bereich ohne Z2M-Gruppe eine an (Szenen brauchen eine): Name ist der Bereichsname (bei Kollision mit einem Gerät oder einer Gruppe `<Bereich> Gruppe`, dann mit Nummer; `#`, `+`, `/` werden zu Leerzeichen), Mitglieder sind alle Zigbee-Lichter mit diesem HA-Gerätebereich (`bridge/request/group/add`, dann `group/members/add` je Licht). Antwort `{ok, group: {id, friendly_name}, added: [ieee], failed: [{ieee, error}]}`. Fehler: `not_found` (Bereich), `no_lights` (keine Zigbee-Lichter), `group_exists` (eine Gruppe enthält schon Lichter des Bereichs, Haven nimmt dann diese). Danach ordnet `haven/rooms` die Gruppe wie gewohnt über die Lichter zu; spätere Raumwechsel halten sie über `set_area` mit `sync_group` aktuell.

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

### `haven/zigbee/enable_last_seen`
Nur Admin, ab 0.5.2. Schaltet die Z2M-Option `advanced.last_seen` ein. Topic `bridge/request/options`, Payload `{"options": {"advanced": {"last_seen": "ISO_8601"}}}`. Annahme (Z2M 2.x, nur aus dem Gedächtnis, nicht an einem laufenden Z2M geprüft): Z2M führt die Option mit den gespeicherten Einstellungen zusammen und meldet in der Antwort `restart_required`. Ob die Option ohne Neustart greift, hängt von Z2M ab; Haven reicht `restart_required` durch.
```json
{"type": "haven/zigbee/enable_last_seen"} → {"ok": true, "restart_required": true}
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
Z2M braucht für `bridge/request/networkmap` (Payload `{type: "raw", routes: false}`) bei 40 Geräten rund 78 s und wartet bei nicht erreichbaren Geräten auf jedes einzelne. Deshalb gibt es drei Formen:

- **`refresh: true, background: true`** (nur Admin, ab 0.5.1, Feature `networkmap_background`): Antwort sofort `{status: "started"|"running", started_at}`. Die Messung läuft als Task in der Integration und wartet bis zu 10 Minuten auf `bridge/response/networkmap` (Abgleich über `transaction`). Nur eine Messung zur Zeit: Ein zweiter Start meldet `running` mit dem `started_at` der laufenden Messung. Ist Z2M offline, kommt `bridge_offline`.
- **`refresh: true`** ohne `background` (alte Apps): blockiert wie bis 0.5.0, jetzt mit 10 min Wartezeit; läuft schon eine Messung, `busy`.
- **ohne `refresh`**: Cache plus Status: `{nodes, links, timestamp, running, started_at, last_error}`. Ohne Cache sind `nodes` und `links` leer und `timestamp` ist `null`. `started_at` ist nur bei `running` gesetzt, `last_error` bleibt bis zur nächsten erfolgreichen Messung stehen.

Ende der Hintergrundmessung: HA-Ereignis `haven_networkmap_updated` mit `{ok: true, timestamp}` oder `{ok: false, error}`. Bei Erfolg sind Cache und `.storage/haven.networkmap` (Store-Version 1, verzögert geschrieben, beim Start geladen) schon aktuell. Bei Fehler (Timeout, Z2M lehnt ab, Z2M offline) bleibt der alte Cache erhalten. Der Text kommt auf Deutsch, den Z2M-Fehler gibt er weiter, nennt Z2M ein nicht antwortendes Gerät in Anführungszeichen, steht dort „Gerät X nicht erreichbar“. Liefert Z2M `failed`-Einträge bei einzelnen Knoten, ist das ein Teilergebnis und gilt als Erfolg; die `failed`-Felder bleiben in `nodes` erhalten. Aus `data.value` werden `nodes` und `links` mit Zeitstempel (UTC, ISO 8601) gehalten.
```json
{"type": "haven/zigbee/networkmap", "refresh": true, "background": true}
→ {"status": "started", "started_at": "2026-11-16T12:00:00+00:00"}

{"type": "haven/zigbee/networkmap"}
→ {"nodes": [{"ieeeAddr": "0x...", "friendlyName": "...", "type": "Router", "failed": []}],
   "links": [{"source": {"ieeeAddr": "0x..."}, "target": {"ieeeAddr": "0x..."}, "lqi": 120}],
   "timestamp": "2026-11-15T12:00:00+00:00",
   "running": false, "started_at": null, "last_error": null}
```
Ungeprüft auf echtem HA: der Ablauf mit echtem Z2M (Version 0.5.1 ist nur mit den Tests geprüft).

### Szenen: `haven/zigbee/scene_store`, `scene_rename`, `scene_remove`, `scene_recall` (Feature `scenes`)
Szenen liegen in Z2M auf den Geräten einer Gruppe. Haven vergibt die IDs selbst, die App arbeitet nur mit Namen. Befehle gehen an `<base>/<gruppe>/set`, Z2M antwortet darauf nicht. Haven wartet deshalb bis 5 s, bis `bridge/groups` die Änderung zeigt, und meldet das als `confirmed`.

| Befehl | Parameter | Antwort |
|---|---|---|
| `scene_store` (Admin) | `{group_id, name?, scene_id?, look?}` | `{ok, group_id, scene_id, name, created, confirmed}` |
| `scene_rename` (Admin) | `{group_id, scene_id, name}` | `{ok, group_id, scene_id, name, confirmed}` |
| `scene_remove` (Admin) | `{group_id, scene_id}` | `{ok, group_id, scene_id, confirmed}` |
| `scene_recall` | `{group_id, scene_id}` | `{ok}` |

- `scene_store` speichert den **aktuellen Zustand** aller Gruppenmitglieder (`scene_store` von Z2M). Ohne `scene_id` entsteht eine neue Szene mit der kleinsten freien ID (0 bis 255), der Name muss frei sein (Groß- und Kleinschreibung zählt nicht), sonst `name_taken`. Mit `scene_id` wird die Szene überschrieben, ID und (ohne `name`) Name bleiben, Haven-Automationen mit dieser ID laufen also weiter.
- `look`: `{colors: ["#rrggbb", ...] (1–3), brightness: 0–100, kelvin: 1000–10000|null}`, beschreibt die Szene für die Karten der App. Haven speichert es in `.storage/haven.scenes` mit `stored_at`. Speichern ohne `look` löscht ein altes. Looks von Szenen, die es nicht mehr gibt, fallen beim nächsten Speichern weg.
- `scene_recall` braucht keinen Admin (wie `scene.turn_on`) und hängt nicht an den Szenen-Entities von HA, die nach einer Umbenennung ihre alte `entity_id` behalten.
- Fehler: `not_found` (Gruppe oder Szene), `name_taken`, `full`, `invalid_format`, `bridge_offline`.

Ungeprüft auf echtem HA: ob Z2M nach jeder Szenenänderung `bridge/groups` neu sendet (sonst `confirmed: false` nach 5 s) und wie ausgeschaltete Lampen in der Szene landen.

### Szenen-Abgleich: `scene_check`, `scene_adopt`, `scene_repair` (Feature `scene_sync`)
Z2M kann Szenen nicht von den Lampen zurücklesen. Lampen, die später in die Gruppe kamen, beim Speichern offline waren oder neu gekoppelt wurden, kennen eine Szene nicht. Haven merkt sich deshalb je Szene ein **Soll** (Zustand jeder Mitgliedslampe) und misst Szenen, indem es sie aufruft.

| Befehl (alle Admin) | Parameter | Antwort |
|---|---|---|
| `scene_check` | `{group_id, scene_id}` | Messergebnis (siehe unten) |
| `scene_adopt` | `{group_id, scene_id}` | `{ok: true, look: {colors, brightness, kelvin, stored_at}}` |
| `scene_repair` | `{group_id, scene_id}` | Messergebnis wie `scene_check` |

```
{"group_id": 3, "scene_id": 2, "name": "Abend", "has_target": true, "status": "differs",
 "members": [{"entity_id": "light.x", "name": "Stehlampe",
              "expected": {"on": true, "brightness": 80, "kelvin": null, "rgb": "#ff0000"},
              "measured": {"on": true, "brightness": 100, "kelvin": 4000, "rgb": null}, "status": "missing"}],
 "measured_look": {"colors": ["#ffcea6"], "brightness": 100, "kelvin": 4000},
 "checked_at": "2026-11-19T20:00:00+00:00"}
```
- **Lampenzustand:** `{on, brightness: 0–100|null, kelvin: int|null, rgb: "#rrggbb"|null}`, bei `on: false` sonst alles `null`. Weiß: `kelvin`, Farbe: `rgb`, nur dimmbar: beides `null`.
- **Soll:** `scene_store` speichert es zusätzlich automatisch (aktuelle HA-Zustände der Licht-Mitglieder nach dem Speichern, `source: "store"`), `scene_adopt` übernimmt die letzte Messung (`source: "adopt"`, ohne Messung misst es zuerst) und setzt den Look auf `measured_look`. Liegt in `.storage/haven.scenes` unter `targets` (`{members: {entity_id: Lampenzustand}, ieees, stored_at, source}`), die letzte Messung je Szene unter `checks`. `scene_remove` löscht beides mit.
- **Mitglieder:** die Licht-Mitglieder der Z2M-Gruppe (Taster für Bindings zählen nicht), je Gerät das `light.*` aus den HA-Entities des Geräts (bei mehreren Endpunkten das erste nach Namen). Geräte ohne HA-Licht fehlen.
- **Messen:** Zustand aller Mitglieder merken, `scene_recall`, 2 s warten, dann jede erreichbare Lampe lesen lassen (`<base>/<friendly_name>/get` mit `state` und, was die Lampe kann, `brightness`, `color`, `color_temp`). Als Antwort zählt eine neue Gerätemeldung von Z2M (bis 5 s), danach bis 0,5 s Ruhe (höchstens 2 s) für die übrigen Lesungen. Gemessen wird der HA-Zustand danach, nicht der Wert, den Z2M nach dem Aufruf aus seinem Gedächtnis meldet. Zum Schluss kommen die gemerkten Zustände zurück (`light.turn_on`/`turn_off` mit den alten Werten), Haven wartet bis 5 s, bis HA sie zeigt. Ein Check dauert so etwa 3–8 s, die Lampen schalten kurz um.
- **Vergleich je Lampe:** `ok`; `missing` (unverändert gegenüber vorher und anders als das Soll, ohne Soll: unverändert, obwohl andere Lampen reagierten); `differs` (reagiert, aber Helligkeit mehr als 8 Prozentpunkte daneben oder Farbe merklich anders: ΔRGB > 40 in einem Kanal, Δkelvin > 300, Weiß gegen Farbe über die RGB-Farbe der Farbtemperatur); `unreachable` (`unavailable` oder keine Antwort auf `get`). `status` der Szene: `unknown` ohne Soll, `differs` bei einer Lampe mit `missing`/`differs`, sonst `ok` (`unreachable` allein macht keine Abweichung).
- **Abgleichen:** setzt alle Mitglieder auf ihr Soll (ohne Eintrag, z. B. neu in der Gruppe: aus), wartet bis 5 s je Lampe (parallel) auf HA, `scene_store` mit gleicher ID und gleichem Namen, Zustand zurück, dann messen. Das Soll umfasst danach die aktuellen Mitglieder. Ohne Soll: Fehler `no_target`.
- **In `haven/zigbee/groups`** (und im Abo) trägt jede Szene `sync`: `unknown` (kein Soll), `stale` (Licht-Mitglieder der Gruppe seit dem Soll geändert), `differs` (letzte Messung nach dem Soll wich ab), sonst `ok`; dazu `checked_at` der letzten Messung oder `null`.
- Je Gruppe läuft ein Check oder Abgleich zur Zeit. `scene_store`, `scene_rename`, `scene_remove` und `scene_recall` derselben Gruppe warten so lange. Fehler wie bei den übrigen Szenenbefehlen (`not_found`, `bridge_offline`, `mqtt_error`) plus `no_target`.

Ungeprüft auf echtem HA: ob jede Lampe auf `get` auch dann eine Meldung auslöst, wenn sich nichts ändert (sonst `unreachable`), und ob 2 s nach dem Aufruf für lange Übergänge reichen.

### Revisionen: `revision` und `base_revision` (Feature `revisions`)
Grundriss, Widgets und Prefs tragen eine **Revision**, die nur Haven vergibt: `0` heißt „nie mit Revision gespeichert“, jedes erfolgreiche `set` zählt `+1`. Die `get`-Antworten, die `set`-Antworten und das Ereignis `haven_data_updated` enthalten `revision`.

Ein Client schickt beim `set` optional `base_revision`, die Revision, auf der seine Fassung beruht. Ist sie nicht mehr die gespeicherte (ein anderes Gerät hat inzwischen gespeichert), lehnt Haven mit Code `conflict` ab und speichert nichts. Der Client lädt dann neu, führt zusammen und speichert mit der neuen Revision. Prüfen und Speichern laufen unter einer Sperre, von zwei gleichzeitigen `set` auf derselben Revision gewinnt genau eins. Uhrzeiten der Geräte (`updated_at`) zählen dafür nicht, das Feld wird nur durchgereicht.

Ohne `base_revision` (ältere Apps) speichert Haven wie bisher ohne Prüfung, die Revision steigt trotzdem. So bemerken neuere Geräte die Änderung.
```
{"type": "haven/floorplan/get"} → {"plan": {...}, "updated_at": 1763208000000, "revision": 4}
{"type": "haven/floorplan/set", "plan": {...}, "updated_at": 1763208005000, "base_revision": 4} → {"ok": true, "updated_at": 1763208005000, "revision": 5}
{"type": "haven/floorplan/set", "plan": {...}, "base_revision": 4} → Fehler conflict
{"type": "haven/prefs/get"} → {"prefs": {...}, "revision": 2}
```

### `haven/floorplan/get` und `haven/floorplan/set`
Speicher: `.storage/haven.floorplan` (Store-Version 1, Inhalt `{"plan": {...}, "updated_at": ...}`). `plan` ist das Dokument von `HavenFloorPlan.toJson` der App. Geprüft wird nur: Objekt, `version` (Zahl), `rooms` (Liste, jeder Eintrag mit `id`), `placements` (Liste, jeder Eintrag mit `entityId`), optional `furniture` (Liste, jeder Eintrag mit `id`). Geometrie prüft die App. Unbekannte Felder im `plan` bleiben unverändert erhalten.

**Möbel** (`furniture`, ab App 0.6, nur Deko): Neue Apps schicken das Feld immer, auch leer. Fehlt es (App bis 0.5), übernimmt Haven die gespeicherten Möbel in den neuen Stand, damit ein älteres Gerät sie beim Speichern nicht löscht. `furniture: []` löscht dagegen alle. Das Schema bleibt Version 2.

Optional (ab 0.3.0) `updated_at` neben `plan`: Text (ISO 8601) oder Zahl (Millisekunden), wird nur durchgereicht und nicht ausgewertet. Fehlt es, nimmt Haven ein vorhandenes Feld `updatedAt` aus dem `plan`, sonst `null`. `get` liefert immer `plan` und `updated_at`. Alte Clients ohne `updated_at` funktionieren unverändert, alte Speicherstände geben `updated_at: null`.
```json
{"type": "haven/floorplan/get"} → {"plan": {"version": 1, "grid": {"cols": 20, "rows": 26, "cellMeters": 0.5}, "rooms": [], "placements": []}, "updated_at": "2026-11-15T12:00:00Z"}
{"type": "haven/floorplan/get"} → {"plan": null, "updated_at": null}
{"type": "haven/floorplan/set", "plan": {"version": 1, "rooms": [], "placements": []}, "updated_at": "2026-11-15T12:00:00Z"} → {"ok": true}
```

### `haven/widgets/get` und `haven/widgets/set`
Speicher: `.storage/haven.widgets` (`{"widgets": [...], "layout": {...}|null, "updated_at": ...|null}`). Format je Eintrag wie `HavenWidgetConfig.toJson` der App: `id`, `type` (`power`, `entityToggle`, `lowBattery`, `weather`), `entities`, `options`. Geprüft: Liste, `id` und `type` als Text, IDs eindeutig. Unbekannte Felder je Widget bleiben erhalten. Noch nie gespeichert: `widgets: null`.

Optional (ab 0.3.0): `layout` (beliebiges Objekt oder `null`, Haven prüft nur den Typ) und `updated_at` (Text oder Zahl, nur durchgereicht). Fehlt `layout` im `set`, bleibt das bisherige Layout erhalten (alte Clients löschen es nicht), `layout: null` löscht es. `updated_at` wird nicht übernommen, wenn es fehlt (dann `null`). `get` liefert immer alle drei Felder.
```json
{"type": "haven/widgets/set", "widgets": [{"id": "power", "type": "power", "entities": [], "options": {}}], "layout": {"columns": 2}, "updated_at": 1763208000000} → {"ok": true}
{"type": "haven/widgets/get"} → {"widgets": [{"id": "power", "type": "power", "entities": [], "options": {}}], "layout": {"columns": 2}, "updated_at": 1763208000000}
{"type": "haven/widgets/get"} → {"widgets": null, "layout": null, "updated_at": null}
```

### `haven/prefs/get` und `haven/prefs/set`
Speicher: `.storage/haven.prefs` (`{"prefs": {...}}`). Ein Dokument für App-Einstellungen, `set` ersetzt es ganz. `excluded_lights`: Licht-Entities, die die App ausblendet. Geprüft: Objekt, `excluded_lights` (optional) als Liste von Texten mit `light.`-Präfix, sonst `invalid_format`. Doppelte Einträge entfernt Haven still (erste Stelle bleibt), die Antwort enthält das gespeicherte Dokument. Unbekannte Schlüssel bleiben unverändert erhalten. `excluded_lights` ist in Antworten immer da, noch nie gespeichert: `[]`.
```json
{"type": "haven/prefs/get"} → {"prefs": {"excluded_lights": []}}
{"type": "haven/prefs/set", "prefs": {"excluded_lights": ["light.salon", "light.salon"]}}
→ {"ok": true, "prefs": {"excluded_lights": ["light.salon"]}}
```

### Bilder der Wohnung: `haven/render/*` (Feature `render`)
Die App rendert nach Kartenänderungen je Perspektive (Übersicht 2D, Übersicht 3D, je Raum) und Design (`light`, `dark`) einen **Bildsatz**: `base.png` (alle Lampen aus), `room_<raum>.png` (Ausschnitt „Raum an, ohne Licht“: Möbellinien blenden in hellen Räumen aus), `lamp_<entity>.png` (Ausschnitt mit nur dieser Lampe in gedämpftem Weiß) und `meta.json` (Größe, Ausschnitte, Belichtung `exposure`, Helligkeit der Ebenen `gain`). Licht ist additiv, deshalb setzt `render_compose.py` jeden Lichtzustand ohne GPU zusammen: Grundbild mit den Raumausschnitten heller Räume, dazu je Lampe `farbe · helligkeit / gain · (T⁻¹(ebene) − T⁻¹(raum)) / exposure` in linearem Licht, am Ende das Tone-Mapping T (PBR Neutral wie in der App). Helligkeit und Farbe kommen aus dem HA-Zustand wie in der App (`brightness`, `rgb_color`, sonst `color_temp_kelvin`, sonst 2700 K; mindestens 5 %).

Upload in Stücken über die WebSocket-Verbindung (Admin): `begin` verwirft einen laufenden Upload, `chunk` hängt an (`offset` = bisherige Dateigröße, `data` Base64, höchstens 1 MiB je Stück), `commit` prüft jeden Satz und ersetzt den Bestand in `.storage/haven_render/sets` vollständig. Satz- und Dateinamen: Satz `[A-Za-z0-9_-][A-Za-z0-9_.-]*`, Dateien `meta.json`, `base.png`, `room_*.png`, `lamp_*.png`. `key` ist der Render-Schlüssel der App (Hash aus Plan, Perspektiven, Format): Am `key` aus `status` sieht jedes Gerät, ob die Bilder zum aktuellen Plan passen.
```json
{"type": "haven/render/status"} → {"available": true, "key": "1-…", "updated_at": "…", "sets": {"uebersicht_3d_dark": {"view": "uebersicht_3d", "name": "Übersicht 3D", "room": null, "theme": "dark", "width": 1024, "height": 768, "lamps": ["light.salon"]}}}
{"type": "haven/render/begin", "key": "1-…"} → {"upload": "…", "max_chunk": 1048576}
{"type": "haven/render/chunk", "upload": "…", "path": "uebersicht_3d_dark/base.png", "offset": 0, "data": "iVBOR…"} → {"size": 262144}
{"type": "haven/render/commit", "upload": "…"} → {"ok": true, "sets": ["uebersicht_3d_dark"]}
{"type": "haven/render/abort"} → {"ok": true}
{"type": "haven/render/file", "set": "raum_salon_dark", "name": "base.png", "offset": 0} → {"size": 182044, "offset": 0, "data": "iVBOR…"}   (Feature render_files, höchstens 512 KB je Stück, ohne Admin)
```
Je Satz entsteht eine Image-Entity `image.haven_karte_<name>_<hell|dunkel>` (Attribute `view`, `room`, `theme`, `width`, `height`). Das Bild entsteht beim Abruf (`entity_picture`) im Executor, mit Cache je Lichtzustand. Schaltet eine Lampe des Satzes, ändert sich `image_last_updated` (höchstens einmal je Sekunde), Clients laden dann neu. `available: false`: numpy oder Pillow fehlen, `begin` antwortet dann `not_supported`.

## Event `haven_data_updated` (ab 0.3.0)
Nach jedem erfolgreichen `haven/floorplan/set`, `haven/widgets/set` und `haven/prefs/set` feuert Haven auf dem HA-Event-Bus `haven_data_updated`, damit andere Geräte neu laden können.
```json
{"event_type": "haven_data_updated", "data": {"kind": "floorplan", "updated_at": "2026-11-15T12:00:00Z", "revision": 5}}
```
- `kind`: `floorplan`, `widgets`, `prefs`, `automations` (nach `haven/automations/set|delete`), `heating` (nach `haven/heating/set|delete|display`) oder `render` (nach `haven/render/commit`).
- `updated_at`: nur wenn bekannt (aus dem Befehl, bei Grundriss und Prefs sonst aus dem Feld `updatedAt` des Dokuments), unverändert als Text oder Zahl. Sonst fehlt der Schlüssel.
- `revision`: bei `floorplan`, `widgets` und `prefs` die neue Revision (Feature `revisions`, siehe „Revisionen“).
- Die App abonniert per `subscribe_events` mit `event_type: "haven_data_updated"`.
- Einschränkung: HA erlaubt Nicht-Administratoren `subscribe_events` nur für Event-Typen aus der `SUBSCRIBE_ALLOWLIST` von HA. `haven_data_updated` steht nicht darin, das Abo funktioniert also nur mit einem Administrator-Konto (die App nutzt dessen Long-Lived-Token). Das stammt aus dem Wissen über den HA-Quellcode, nicht aus einem Test.

## Haven-Automationen
Komplexere Abläufe laufen direkt in Haven statt als HA-Automation. Es gibt die Typen `room_knob` und `room_motion` (Bewegungslicht, eigener Abschnitt unten). `room_knob`: Drehregler und Taster (Z2M-Geräte mit `action`) steuern das Licht eines Raums. `haven/info.features` enthält dann `automations`.

**Modell** (`.storage/haven.automations`, Store-Version 1.3, Migration von 1.1 und 1.2 in `automations.migrate_store`):
```json
{"id": "3f9a1c0b2e4d", "type": "room_knob", "name": "Drehregler Salon", "enabled": true,
 "room_area_id": "salon", "devices": ["0x00124b0000000016"],
 "options": {"gestures": {"rotate": {"action": "brightness"}, "single": {"action": "toggle"},
                          "double": {"action": "next_scene"}, "hold": {"action": "scene", "scene": 2}},
             "scenes": [3, 0, 1], "on_action": "last", "on_scene": null,
             "step": 35, "transition": 0.2, "min_brightness": 3, "off_at_min": false}}
```
- Gesten: `rotate` (`none`, `brightness`, `scene_step` = rechts nächste, links vorige Szene, eine je Drehbewegung, ab 0.6.39, Feature `knob_scene_step`), `single`/`double`/`hold` (`none`, `toggle`, `next_scene`, `scene` mit `scene`, `off` = alles aus im Raum, `lights` = eigene Aktion, ab 0.6.36, Feature `knob_lights`).
- `lights`: bestimmte Lampen aus beliebigen Räumen auf einen festen Zustand, z. B. `{"action": "lights", "lights": [{"entity_id": "light.a", "on": true, "brightness": 60, "kelvin": 2700}, {"entity_id": "light.b", "on": true, "rgb": [255, 138, 31]}, {"entity_id": "light.c", "on": false}]}`. Je Lampe `on`, wenn an wahlweise `brightness` (1 bis 100 %) und `kelvin` (1000 bis 10000) oder `rgb`; fehlt beides, bleibt die Farbe. Höchstens 50 Lampen. Ausführung per `light.turn_on`/`light.turn_off` mit der `transition` der Automation, Lampen mit gleichem Zustand in einem Aufruf.
- `scenes`: Szenen-IDs der Z2M-Raumgruppe in Umlauf-Reihenfolge, `null` = alle. `on_action`: `last` (zuletzt) oder `scene` (`on_scene`) beim Einschalten per Ein/Aus.
- `step` 1 bis 255 (von 255), `transition` 0 bis 10 s, `min_brightness` 1 bis 254 (Grenze beim Runterdrehen), `off_at_min`: am Minimum nach links drehen schaltet aus. `dim_exclude` (ab 0.6.39, Feature `knob_dim_exclude`): Lampen, die beim Drehen nicht mitdimmen. Steckt eine davon in der Raumgruppe (oder eine global ausgenommene Lampe), dimmt Haven die übrigen einzeln über HA statt über die Gruppe.
- Lampen des Raums, die nicht in der Z2M-Gruppe sind (WLAN, Matter, in der Gruppe fehlende Zigbee-Lampen), schalten bei Drehen, Ein/Aus und `off` über HA mit. Der Raum gilt als an, wenn die Gruppe oder eine dieser Lampen an ist.
- Ein Gerät darf nur in einer aktiven Automation stecken (`device_taken`). Der Raum braucht eine Z2M-Gruppe oder Lichter (`no_lights`).

**Laufzeit:** Haven hört über die vorhandenen Abos auf `<base>/<friendly_name>` (die Geräte stehen per IEEE in der Automation, eine Umbenennung in Z2M greift mit dem nächsten `bridge/devices`). `action` wird normalisiert, leere Werte ignoriert. Event-Modus: `rotate_left/right`, `single`, `double`, `hold`. Command-Modus: `brightness_step_up/down` = Drehen, `toggle`/`on`/`off` = Drücken, Farbtemperatur- und Farbton-Befehle werden ignoriert. Drehungen: die erste sofort, weitere in einem Fenster von 120 ms zusammengefasst. Befehle gehen per MQTT an die Raumgruppe (`<base>/<Gruppe>/set` mit `brightness_step` und `transition`, `state` `ON`/`OFF` bzw. `TOGGLE` bei unbekanntem Zustand, `scene_recall`). Ohne Gruppe: `light.turn_on` mit `brightness_step`, `light.turn_off`/`light.toggle` auf die Lichter des Raums ohne die ausgenommenen. Der Szenen-Zähler je Raum steht im Speicher und im Store. Jede Geste feuert `haven_automation_triggered` `{id, action, room, room_name, device, source, at}` (`source`: `device` oder `test`).

| Befehl | Payload | Antwort |
|---|---|---|
| `haven/automations/list` | – | `{automations: [{..., last_triggered: {at, action}\|null}], rooms: {area_id: {group, group_id, scenes, lights, has_lights}}, device_automations: {ieee: ["automation.x"]}, running}` |
| `haven/automations/set` (Admin) | `{automation: {...}}` (ohne `id` = neu) | `{ok, automation}` |
| `haven/automations/delete` (Admin) | `{automation_id}` | `{ok}` |
| `haven/automations/test` (Admin) | `{automation_id, action}` mit `rotate_left`, `rotate_right`, `single`, `double`, `hold` (Drehregler) bzw. `motion`, `clear`, `takeover` (Bewegungslicht) | `{ok, plan: {action, mqtt, services, scene, note}}`, bei Bewegungslicht zusätzlich `motion` (Zustand) |

`device_automations` nennt je Drehregler/Taster die HA-Automationen, die das Gerät verwenden (`automation.automations_with_device`), damit die App doppelte Auslösung erkennt. Die ID heißt `automation_id`, weil `id` bei HA die Nummer der Nachricht ist. Nach `set` und `delete` feuert `haven_data_updated` mit `kind: "automations"`. Fehler: `invalid_format`, `not_found`, `no_lights`, `device_taken`.

### Bewegungslicht (`room_motion`)
Melder (Z2M-Geräte mit `occupancy` oder `presence`) schalten das Licht eines Raums, aber nur unter Bedingungen. Wichtigster Punkt: Haven weiß, **wem das Licht gerade gehört**. Wer es heller dreht, bekommt nicht wieder die Szene.

```json
{"id": "a1b2c3d4e5f6", "type": "room_motion", "name": "Bewegungslicht Bad", "enabled": true,
 "room_area_id": "bad", "devices": ["0x00124b0000000030"],
 "options": {"windows": [{"from": "18:00", "to": "07:00"}],
             "sun": {"from": {"event": "sunset", "offset": -30}, "until": {"event": "sunrise", "offset": 60}},
             "lux": {"below": 30, "entity": null},
             "light": {"mode": "scene", "scene": 2, "transition": 3},
             "night": {"from": "23:00", "to": "06:00", "light": {"mode": "brightness", "brightness": 25, "color_temp": 2200}},
             "delay": 120, "warn": true, "warn_seconds": 20, "relight": true, "manual_delay": null}}
```
- **Bedingungen** (alle müssen gelten, leer = keine Einschränkung): `windows` (HH:MM, auch über Mitternacht, mehrere), `sun` (`from`/`until` mit `event` `sunrise`/`sunset` und `offset` in Minuten, von Ereignis bis Ereignis, auch über Nacht), `lux` (`below` in lx, `entity` ein `sensor.`, `null` = `illuminance`-Sensor des Melders). Ein unbekannter Lux-Wert oder fehlende Sonnenzeiten blockieren nicht.
- **Licht:** `mode: "scene"` mit `scene` (Z2M-Szene der Raumgruppe) oder `scene_entity` (`scene.*` aus HA), oder `mode: "brightness"` mit `brightness` 1 bis 254 und optional `color_temp` in Kelvin. `transition` in Sekunden. `night` wählt im zweiten Zeitfenster ein anderes Licht, sonst gelten dieselben Bedingungen.
- **Nachlauf:** `delay` Sekunden (Standard 120) ohne Bewegung bis Aus. Meldet der Melder `presence`/`occupancy` an, läuft der Nachlauf erst ab dem Abfallen (zur Sicherheit spätestens nach 6 h).
- **Optionen:** `warn` (20 s vor dem Aus auf 50 % dimmen, Bewegung stellt den vorherigen Zustand wieder her), `relight` (Bewegung bis 30 s nach dem automatischen Aus schaltet sofort wieder ein, ohne Bedingungen, Standard an), `manual_delay` (Sekunden, nach denen auch manuell übernommenes Licht ausgeht, Standard `null` = nie).
- **Helligkeit nur bei Licht aus:** Ist das Licht an, misst der Sensor das eigene Licht. Darum zählt `lux` nur beim Einschalten.

**Besitz-Logik** (`motion.step`, rein und getestet, Zustand je Raum `owner`, Zeitgeber, zuletzt gesetztes Licht):

| `owner` | Bedeutung | Bewegung | Nachlauf vorbei |
|---|---|---|---|
| `none` | niemand (Licht aus, oder war schon an) | Licht aus und Bedingungen erfüllt: einschalten, `owner = motion`. Licht an: nichts tun | nichts |
| `motion` | der Melder hat eingeschaltet | nur Nachlauf verlängern, **nie erneut die Szene setzen** | ausschalten, `owner = none` |
| `manual` | jemand hat das Licht verstellt | nichts | nichts (außer `manual_delay`) |

Übergänge: `motion` zu `manual` bei Übernahme, `motion`/`manual` zu `none`, sobald das Licht aus ist.

**Erkennung der Übernahme:**
1. Eigene Befehle öffnen ein Erwartungsfenster (1,5 s plus Übergangszeit): passende `state_changed` des Raums (Gruppe und Lampen) sind eigene und setzen nur die Vergleichshelligkeit.
2. Danach zählt als Übernahme: Helligkeit weicht um mehr als 10 % (von 254) von der Vergleichshelligkeit ab, die Farbe ändert sich (Farbtemperatur um 20 mired, Farbton/Sättigung um 15, xy um 0,03) oder das Licht geht aus (dann `none`). Bei Szenen ist die erste Meldung danach die Vergleichsbasis.
3. Dienstaufrufe der Engine tragen einen eigenen HA-`Context`. Ein `state_changed` mit fremdem `context.user_id` gilt sofort als Übernahme, auch ohne Abweichung. Wandschalter und Z2M-Bedienung ohne HA-Kontext erkennt Punkt 2.
4. Haven-Drehregler im selben Raum melden die Übernahme direkt intern (`room_knob` ruft `motion.handle_knob`): Licht an danach heißt `manual`, Licht aus heißt `none`.

**Laufzeit:** `motion_runtime.py`. Den Melder liest Haven direkt aus den Z2M-Meldungen des Geräts (`presence` vor `occupancy`, nur Wechsel zählen), wie bei den Reglern. Die `binary_sensor`-Entity aus den `ha_entities` (`device_class` `presence`, `occupancy` oder `motion`) dient nur der Anzeige (`detectors`) und als Rückfall beim Start. Lux der `illuminance`-Sensor desselben Geräts oder `lux.entity`. Sonnenzeiten aus `homeassistant.helpers.sun`. Der Nachlauf läuft über `async_call_later`. Befehle gehen wie bei `room_knob` per MQTT an die Raumgruppe (`scene_recall`, `state`/`brightness`/`color_temp`/`transition`), `scene_entity` und Räume ohne Gruppe über HA-Dienste. Der Zustand steht im Speicher und unter `runtime.motion` im Store. Nach einem Neustart setzt Haven den Nachlauf neu an, wenn der Melder gerade nicht aktiv ist. Steht ein Raum auf „manuell“, das Licht ist aber aus (z. B. während des Neustarts ausgeschaltet), gehört es wieder niemandem.

**Validierung:** Ein Melder darf nur in einer aktiven Bewegungslicht-Automation stecken (`device_taken`, „X meldet schon für Y“). Knob und Bewegungslicht im selben Raum sind erlaubt und verzahnt. Weitere Fehler: `invalid_format`, `not_found`, `no_lights`.

**Ereignis** `haven_automation_triggered` mit `action` `motion_on`, `motion_extend`, `motion_off`, `takeover` oder `warn` und `state: {owner, phase, occupied, off_at, inactive, night, lux, detectors, lux_entity}`. `haven/automations/list` liefert dasselbe als `motion` je Automation. `phase`: `idle`, `motion`, `warn`, `manual`, `inactive` (`inactive`: `time`, `sun`, `lux`).

## Heizungsregelung (Feature `heating`)
Haven übernimmt, wofür man sonst Better Thermostat installiert: Die Heizkörperthermostate (TRV, Zigbee über Z2M) eines Raums laufen als Gruppe mit **einem** Ziel und regeln nach einem oder mehreren **Raumthermometern** statt nach ihrem Fühler am Heizkörper. Dazu kommen Fensterkontakte, eine Sommerpause nach der Außentemperatur und Betriebsarten des Raums. Logik HA-frei in `heating.py` (getestet in `tests/test_heating.py`), Laufzeit in `heating_runtime.py` (`tests/test_heating_runtime.py`, nachgebautes HA wie beim Bewegungslicht), Befehle in `heating_ws.py`.

**Einstellungen je Raum** (`.storage/haven.heating`, `{"rooms": {area_id: config}, "state": {area_id: {...}}, "displays": [ieee, ...]}`):
```json
{"area_id": "salon", "enabled": true, "trvs": ["0x00124b0000000030"],
 "sensors": ["sensor.sensor_salon_temperature", "sensor.sensor_salon_fenster_temperature"],
 "windows": ["binary_sensor.fenster_salon_contact"], "window_open_delay": 60, "window_close_delay": 120,
 "calibration": "offset", "outdoor": {"entity": "weather.forecast_home", "threshold": 16.0},
 "presets": {"comfort": 21.0, "eco": 18.0, "away": 16.0}, "lock_manual": false, "display": null}
```
- `trvs`: 1 bis 8 Zigbee-Thermostate (IEEE), ein TRV gehört höchstens zu einem Raum (`device_taken`). `sensors`: 0 bis 8 `sensor.*`, Mittelwert der gültigen (nicht `unknown`/`unavailable`, plausibel, Meldung höchstens 4 h alt). Ohne gültigen Wert gilt das Mittel der TRV-Messungen, dann wird nichts korrigiert (die letzte Kalibrierung bleibt).
- `calibration`: `offset` = `local_temperature_calibration` auf Raum − Rohmessung des TRV (Rohmessung = `local_temperature` − aktuelle Kalibrierung), gerundet auf den Schritt des Geräts, in dessen Grenzen. Geschrieben ab 0,3 °C Abweichung, höchstens alle 10 min, und nach einer Änderung erst, wenn das TRV eine neue Messung geschickt hat (spätestens nach 30 min). Ob ein Gerät die kalibrierte oder die rohe Temperatur meldet, erkennt Haven an der ersten Änderung ab 0,8 °C (springt die Messung mit, gilt sie als kalibriert; bis dahin wird das angenommen). TRVs ohne beschreibbare Kalibrierung fallen auf `target` zurück. `target` = Sollwert am TRV = Ziel + (TRV-Messung − Raum), auf den Schritt gerundet, automatische Anpassungen höchstens alle 10 min (sofort bei Änderungen am Ziel oder ab 1,5 °C). `none` = keine Korrektur. Wechselt man weg von `offset`, setzt Haven seine Kalibrierung auf 0 zurück, ebenso beim Entfernen der Regelung oder eines TRV.
- `windows`: `binary_sensor.*` (`on` = offen, `unavailable` zählt als zu). Pause nach `window_open_delay` (0 bis 1800 s) offen, weiter nach `window_close_delay` (0 bis 3600 s) zu.
- `outdoor`: `sensor.*` oder `weather.*` (Attribut `temperature`), geglättet (EMA, τ = 4 h, Neustart nach 12 h Lücke). Sommerpause ab `threshold` (5 bis 30 °C), Ende 1 °C darunter.
- `presets`: Temperaturen der Betriebsarten Komfort, Eco, Abwesend (5 bis 30 °C). `lock_manual`: Änderungen am TRV zurücksetzen statt übernehmen.
- `display`: IEEE eines als Display markierten Wandthermostats (z. B. Beok BOT-R9V-ZB) oder `null`. Markiert wird mit `haven/heating/display` (Liste `displays` im Store). Ein Display regelt und kalibriert Haven nie, es steht in keinem Raum unter `trvs` (`device_taken`) und gehört höchstens zu einem Raum (`device_taken`); nicht markiert: `invalid_format`. Es ist die Bedienung des Raums: ein neuer Sollwert am Display wird das Ziel des Raums (Betriebsart `heat`), Aus am Display schaltet den Raum aus, Ein (bzw. `manual`/`heat`) aus Aus oder einem Preset auf `heat`. Das gilt auch mit `lock_manual` und in Fenster- oder Sommerpause (ein Sollwert am ausgeschalteten Display zählt nicht). Haven spiegelt das wirksame Ziel (auch Eco/Abwesend) auf das Display (Schritt und Grenzen des Geräts) und schaltet es bei Betriebsart Aus und in der Fensterpause ab (dann ohne Sollwert), in der Sommerpause bleibt es an. **Fensterpause:** Wer das Display bei offenem Fenster wieder einschaltet, heizt trotzdem (`window.override: true`): die Pause endet sofort, Betriebsart und Ziel bleiben (aus Aus wird `heat`), und solange ein Fenster offen ist, pausiert der Raum nicht wieder. Sind alle Fenster zu (oder geht der Raum auf Aus), endet die Übersteuerung, die nächste Öffnung pausiert wie gewohnt. Ein Aus am Display während der Pause schaltet den Raum nicht aus. Ein/Aus geht über `system_mode`, wenn der Regelmodus (`preset` manual/auto/eco) kein Aus kennt. **Eco und Wochenplan** (Displays mit `system_mode` und eigenem `preset`, z. B. Beok): Eco am Display ist die Betriebsart Eco des Raums und umgekehrt, das Display regelt dann nach seiner `eco_temperature` (Haven sendet keinen Sollwert und hält die `eco_temperature` gleich der Eco-Temperatur des Raums; eine Änderung am Gerät stellt Haven zurück). Von Eco zurück auf Manuell am Display: Betriebsart `heat` mit dem gemeldeten Sollwert. Der eigene Wochenplan des Geräts (`auto`) wird nicht übernommen, Haven stellt das Display auf Manuell (bzw. Eco) zurück; Sollwerte aus dem Plan oder in Eco zählen nicht als Bedienung. Bei Displays mit Modus über `preset` (manual/auto/off) wird `auto`/Wochenplan ignoriert und Presets wie `eco` werden zur Betriebsart. Die Messung des Displays zählt wie ein Raumthermometer (Mittel mit den `sensors`, allein `room_source: "display"`), sein `running_state` zählt nicht als Heizen.

**Regelung:** Betriebsart des Raums `heat` (Manuell, Ziel `target`), `comfort`, `eco`, `away`, `off`. Die TRVs laufen dabei im Gerätemodus `preset: on` (bzw. `system_mode: heat` bei Geräten ohne passendes `preset`). Aus, Fenster und Sommer schalten `preset`/`system_mode` auf `off`, Geräte ohne Aus bekommen ihren kleinsten Sollwert. Dreht jemand am TRV (Sollwert ändert sich, nicht auf eigenen Befehl, Gerät im Modus `on`), wird das das neue Ziel für den ganzen Raum (bei `target` ohne die Verschiebung); eine Modustaste am Gerät (`eco`, `comfort`, `antifrost`, `off`) schaltet die Betriebsart des Raums, Haven setzt das Gerät danach wieder auf `on` mit der Temperatur des Raums. Unbestätigte Befehle gehen nach 5 min erneut raus. Auswertung bei jeder TRV-Meldung, bei Änderungen der Sensoren und Kontakte, zu den Fenster-Verzögerungen und alle 2 min. Befehle: `<base>/<name>/set`. Der TRV-eigene Wochenplan gilt nicht, solange Haven regelt.

| Befehl | Payload | Antwort |
|---|---|---|
| `haven/heating/list` | – | `{rooms: [{config, status}], running, displays: [ieee]}` |
| `haven/heating/set` (Admin) | `{config: {...}}` | `{ok, config, status}` |
| `haven/heating/delete` (Admin) | `{area_id}` | `{ok}` |
| `haven/heating/control` (Admin) | `{area_id, target?, mode?}` (Ziel ohne `mode` schaltet auf `heat`) | `{ok, status}` |
| `haven/heating/display` (Admin) | `{ieee, display: bool}` (markieren: bekanntes Thermostat, `not_found`; in keinem Raum unter `trvs`, `device_taken`. Aufheben entfernt es aus allen Räumen) | `{ok, displays: [ieee]}` |

`status`: `{area_id, enabled, mode, target, effective_target, room_temperature, room_source ("sensors"|"display"|"trv"|null), sensors: [{entity_id, value}], reason ("off"|"window"|"summer"|null), heating, window: {open, paused, override, pause_at, resume_at, contacts: [{entity_id, open}]}, outdoor: {entity_id, value, current, threshold, summer}|null, trvs: [{ieee, name, available, local_temperature, setpoint, calibration, mode, heating, valve, method, correction, desired_setpoint, desired_calibration, pending}], display: {ieee, name, available, local_temperature, setpoint, mode, preset, eco_temperature, battery, pending}|null, running}`. Bei jeder Änderung feuert `haven_heating_updated` `{area_id, status}`, nach `set`/`delete`/`display` `haven_data_updated` mit `kind: "heating"`. Fehler: `invalid_format`, `not_found`, `device_taken`.

Nicht enthalten (gegenüber Better Thermostat): lernende Regler (PID, TPI, MPC, „AI time based“), Ventil direkt stellen, Ventilpflege, Kühlgeräte, Feuchte, Türen mit eigenen Zeiten, eigener Wochenplan.

## Seitenleisten-Eintrag „Haven“ (Web-App in Home Assistant)
Liegt im Ordner `custom_components/haven/frontend/` ein Web-Build der App, meldet Haven beim Laden des Config Entries einen Eintrag „Haven“ in der Seitenleiste an. Die App läuft dort direkt in Home Assistant, ohne Token und ohne Onboarding, im Hell- oder Dunkelmodus von HA.

- **Panel:** Custom Panel über `panel_custom.async_register_panel` (`frontend_url_path: haven`, Element `haven-panel`, `module_url: /haven_static/<version>/haven-panel.js`, `embed_iframe: false`, `require_admin: false`). Beim Entladen `frontend.async_remove_panel`. Konfiguration als reine Funktion in `panel.py`, die Anmeldung in `panel_setup.py`. Fehler dabei kosten nur den Eintrag, nicht die Integration.
- **Dateien:** `hass.http.async_register_static_paths([StaticPathConfig("/haven_static/<version>", frontend/, cache_headers=True)])`. Die Version im Pfad sorgt fürs Cache-Busting, HA darf lange cachen. Statische Pfade lassen sich in HA nicht abmelden, sie bleiben bis zum Neustart.
- **Anmeldung:** Das Modul `haven-panel.js` liest das Access-Token aus `hass.auth` und gibt es an die App weiter (vor jeder neuen WebSocket-Verbindung frisch, abgelaufene Tokens erneuert es über `hass.auth.refreshAccessToken()`). Der WebSocket ist dieselbe Herkunft (`/api/websocket`). Nicht-Administratoren sehen den Eintrag, schreibende Befehle bleiben wie gehabt Admins vorbehalten.
- **Abhängigkeiten:** `frontend`, `http`, `panel_custom` (in `manifest.json`).
- **Build:** In der App `tool/build_ha_panel.sh` ausführen (siehe README der App). Die Version wird aus dieser `manifest.json` gelesen und muss dazu passen, nach jeder neuen Version also neu bauen. Die Dateien sind eingecheckt, sonst fehlen sie bei HACS. Größe etwa 13 MB (CanvasKit 7 MB, App 4 MB, Lizenztexte 1,4 MB).
- Annahmen zur HA-API sind am Quelltext von HA 2026.2 geprüft, nicht an einem laufenden HA. Falls Tastatur oder Eingabefelder im Panel hängen: `EMBED_IFRAME = True` in `panel.py` (HA lädt das Modul dann in einem iframe).

### Als App installieren (PWA)

Unter `https://<dein-ha>/haven_app/` liefert die Integration Haven als eigenständige, installierbare Web-App aus. Chrome und Edge bieten dort „Installieren“ an, auf Android „Zum Startbildschirm hinzufügen“, auf dem iPhone in Safari „Teilen › Zum Home-Bildschirm“. Haven läuft dann im eigenen Fenster ohne die Oberfläche von HA. In der App führt *Einstellungen › Als App installieren* (im Seitenleisten-Eintrag) dorthin.

- **Anmeldung:** wie die Apps von HA über den Login von HA (OAuth2, `/auth/authorize` und `/auth/token`). Client-ID und Rücksprung sind `https://<dein-ha>/haven_app/`, HA erlaubt das ohne Einrichtung, weil beides auf demselben Host liegt. Die Tokens liegen im Browser dieser Adresse. Das Zugriffstoken wird vor Ablauf erneuert, *Einstellungen › Abmelden* widerruft die Anmeldung (`/auth/revoke`). In HA erscheint sie unter *Profil › Sicherheit › Aktualisierungs-Token*.
- **Voraussetzung:** HTTPS (Browser installieren Web-Apps nur über eine sichere Verbindung), z. B. Nabu Casa, ein Reverse Proxy oder ein Tunnel. Über `http://…:8123` läuft die Seite auch, lässt sich aber nicht installieren.
- **Dateien:** Seite, `manifest.webmanifest` und `sw.js` unter der festen Adresse (Views ohne Anmeldung, nichts Geheimes darin, `panel.app_html` und `panel.app_manifest`), alles andere aus dem versionierten Build (`haven-app.js`). Der Service Worker speichert nichts zwischen, Zustände und Bilder kommen immer frisch aus HA. Nach einem Update der Integration lädt die Seite die neue Version.
- **iOS:** Web-Apps auf dem Home-Bildschirm laufen, bekommen aber keine Hintergrundaktualisierung.

## Entwicklung und Tests
```
cd integration && python3 -m pytest -q
```
Die Tests laden die HA-freien Module ohne Home Assistant (`tests/conftest.py`): `exposes`, `z2m`, `commands` (Payload-Bauer, Transaktions-Abgleich, Zustand und Ablauf der Hintergrund-Netzmessung), `throttle`, `statecache` (auch Verfügbarkeits-Topics), `lastseen` (Quellen-Priorität, Formate, Verfügbarkeit, Option), `groupsync` (Raumwechsel), `appdata` (Validierung, Prefs, Event-Daten, Speicherformat mit `updated_at`/`layout`, Networkmap-Cache, Revisionen mit zwei simulierten Geräten in `test_revisions.py`), `lightgroups` (Gruppenerkennung), `rooms` (Zusammenstellung von `haven/rooms`), `panel` (Panel- und Static-Konfiguration, Möbel im Grundriss), `automations` (Normalisierung, Planung, Entprellen, Szenen-Umlauf, Validierung, Store-Migration), `motion` (Bedingungen, Besitz-Automat, Befehle, Validierung, Migration), `scenesync` (Lampenzustand, Vergleich, Look, `sync`-Status) und `scenesync_runtime` (Messen, Übernehmen, Abgleichen, Wiederherstellen mit nachgebautem HA und Lampen). Testdaten: ein anonymisierter Bestand mit 42 Geräten in `tests/fixtures/inventory.json` (echte Z2M-Modelle und Exposes, erfundene Namen und IEEE-Adressen) und verkleinerte Lichtzustände in `tests/data/light_states.json`. Die HA-abhängigen Module (`__init__`, `config_flow`, `websocket_api`, `storage`, `automations_runtime`, `motion_runtime`, `automations_ws`, `panel_setup`) sind nur per `py_compile` geprüft. Version 0.1.0 lief auf echtem HA (lesende Befehle), 0.2.0 und 0.3.0 noch nicht.

Registry-Zugriffe laufen ohne `dev_reg.devices.values()` / `ent_reg.entities.values()` (ab HA 2027.9 veraltet): `haven/rooms` über `async_list_areas`, `dr/er.async_entries_for_area`, `er.async_entries_for_device`, Geräte ohne Bereich über `dr.async_entries_for_config_entry` aller Config-Entries, Lichter ohne Bereich über `hass.states.async_all("light")`. Die HA-Zuordnung der Zigbee-Geräte (`area_id`, `ha_entities`) liest nur Geräte der MQTT-Config-Entries. Geräte ganz ohne Config-Entry fehlen in `unassigned`.

Neue Z2M-Property: Eintrag in `LABELS` (`exposes.py`) ergänzen. Ohne Eintrag läuft sie über den generischen Rückfall (`source: "generic"`, auch in `classify()["generic"]` aufgelistet).

## Offen
- Gruppenerkennung über `group_entities` stammt aus den Zuständen von HA mit Z2M 2.14.3. Mit anderen HA-Versionen ungeprüft, dafür gibt es die Namensregel als Rückfall.
- Version 0.3.0 ist nur mit den Tests (ohne laufendes HA) geprüft, die lesenden Befehle liefen bereits in 0.1.0 auf echtem HA. Ungeprüft auf echtem HA: Event `haven_data_updated`, Laden und verzögertes Schreiben von `haven.networkmap`, neue optionale Felder.
- `subscribe_events` für `haven_data_updated` mit Nicht-Administratoren ist nicht erlaubt (siehe Event-Abschnitt), ungetestet.

## Lizenz
MIT, siehe `LICENSE`.

# Mitgelieferte Drittbibliotheken

Die Kartenbibliotheken werden lokal ausgeliefert, damit der Offlinebetrieb keinen CDN-Zugriff benötigt:

- **Leaflet 1.9.4** — BSD-2-Clause; Lizenz unter `krakendf/static/vendor/leaflet/LICENSE`.
- **milsymbol 2.2.0** — MIT; Lizenz unter `krakendf/static/vendor/milsymbol/LICENSE`.

Versionen, Bezugsquellen und Integritätsmetadaten: `krakendf/static/vendor/DEPENDENCIES.json`.

Python-Laufzeitabhängigkeiten werden durch pip aus `pyproject.toml` installiert und behalten ihre jeweiligen Lizenzen. Das Wheel enthält diese Pakete nicht.

Der optionale OpenStreetMap-Rasterhintergrund benötigt die angezeigte OSM-Attribution und unterliegt den Tile-Nutzungsbedingungen. Die App ruft ausschließlich betrachtete Einzelkacheln nach explizitem Online-Umschalten ab; kein Massenvorabruf. Importierte MBTiles behalten die vom Kartenanbieter verlangte Attribution. Der Nutzer stellt das passende Kartenpaket bereit.

Decoder und TCP-Protokolladapter sind eigenständige Implementierungen. Seit Version 0.2 enthält das ZIP zusätzlich den nativen Kraken-DAQ zum lokalen Build:

- **KrakenRF Heimdall DAQ**, Commit `1efc252e24501a6bd091699c4f22f140ee71d901`: Originalquellen und GPL-3.0-or-later-Lizenz unter `vendor/heimdall`.
- **KrakenRF librtlsdr**, Commit siehe `vendor/SOURCE_MANIFEST.json`: Originalquellen und GPL-Lizenztexte unter `vendor/librtlsdr`.
- **KrakenDF Unity-Anpassung** der GPL-Decimatorstufe: nachvollziehbarer Patch `patches/heimdall-unity.patch`, dazu `patches/krakendf_unity.h`, gekennzeichnet als Änderung für diesen Build. Sie erlaubt ausschließlich full-rate/ratio1; die RF-Kohärenzkorrektur bleibt erhalten.

Die unveränderten Originalquellen sind durch Dateihashes und Commitpins in `vendor/SOURCE_MANIFEST.json` dokumentiert. Die lokalen Build-/Installationsskripte und Anpassungsquellen werden mitgeliefert. Die ausführbaren DAQ-Dateien entstehen bei der Erstinstallation für den jeweiligen Host; sie werden im Quell-ZIP nicht als vorab geprüfte Hardwarefirmware ausgegeben.

Die optionale externe LoRa-Testaufnahme wird nicht weiterverteilt. Die Projektquellen von SimpleSDR sind nicht Teil des Anwendungscodes.

# Automatischer Erststart unter Linux

`bash ./start.sh` ruft bei fehlender App-/DAQ-Installation automatisch
`bash ./setup.sh` auf. Ein angeschlossener Kraken ist für die Installation nicht
erforderlich. Beim anschließenden Live-Start werden USB-Geräte, Ports,
Speicherbedarf und Prozessbereitschaft getrennt geprüft. Es wird kein
erfolgreicher Empfang behauptet, nur weil die Installation abgeschlossen ist.

Der vollständige lokale DAQ-Stack ist für 64-bit Linux x86_64 und aarch64
vorbereitet. Der native Build wurde hier auf x86_64 tatsächlich kompiliert;
aarch64 benötigt die Hardware-/Buildabnahme am Zielrechner. Empfohlene Systeme
sind Debian12+ oder Ubuntu24.04+ mit Python3.11–3.13. Unter Windows erfolgt der
USB-Livebetrieb über die mitgelieferte WSL2-Einrichtung; ein separater
vorinstallierter Heimdall-Rechner ist dafür nicht nötig.

## Was das Setup tut

1. Fehlende apt-Pakete ermitteln und ausschließlich diese installieren:
   `python3`, `python3-venv`, `ca-certificates` sowie beim vollen Stack
   `build-essential`, `cmake`, `pkg-config`, `patch`, `libusb-1.0-0-dev`,
   `libzmq3-dev`, `python3-dev`, `udev`, `usbutils`.
2. Eine eigene `.venv` erstellen und App-/GPS-/DAQ-Python-Abhängigkeiten mit
   pip auflösen. Keine globale `sudo pip`-Installation. NumPy wird für den
   gepinnten synchronizer/Numba-Stand auf `<2.3` begrenzt.
3. Die mitgelieferten KrakenRF-Quellbäume anhand der SHA-256-Einträge in
   `vendor/SOURCE_MANIFEST.json` prüfen. Es wird kein fremdes Installationsskript
   aus dem Netz ausgeführt. Die Quellen sind im Paket enthalten.
4. KrakenRF `librtlsdr` unter `.runtime/native` bauen. `INSTALL_UDEV_RULES=OFF`
   vermeidet die breit gefassten 0666-Upstreamregeln; eigene Regeln folgen.
   `DETACH_KERNEL_DRIVER=ON` erlaubt das Ablösen eines belegenden DVB-Treibers
   am geöffneten Gerät. Es wird keine globale DVB-Blacklist angelegt.
5. Heimdall unter `.runtime/firmware` bauen. Der dokumentierte Unity-Patch
   unterstützt ausschließlich `decimation_ratio=1` und `fir_tap_size=1`.
   Die unveränderte DATA-/CAL-Konvertierung arbeitet mit vollem IQ-Takt; die
   App übernimmt die eigentliche Fünfkanal-Kanalfilterung. Andere native
   Dezimationsmodi werden vor dem Start abgelehnt. KFR und Ne10 sind für diesen
   gezielten Aufbau nicht erforderlich.
6. Dynamische Bibliotheken und tatsächliche Pythonimporte prüfen. Erst danach
   atomar `.runtime/manifest.json` mit `ready:true` schreiben.
7. Gezielt USB-/GPS-Rechte für den installierenden lokalen Nutzer einrichten
   und den endlichen USB-Transferpuffer vorbereiten.

Die Python-/Systempakete werden beim Erstsetup aus ihren Paketquellen geladen.
Ein späterer normaler Start benötigt für den Stack keinen Internetzugang. Die
Karte verwendet unabhängig davon ihren lokalen Offlinebestand.

## Privilegierte Änderungen auf dem Zielhost

Das Setup verwendet sudo nur für die fehlenden Systempakete und diese Dateien:

- `/etc/udev/rules.d/70-krakendf.rules`: Eigentümer-/0660-Rechte und uaccess für
  Realtek0bda:2838/2832 mit den fünf Kraken-Werksseriennummern1000..1004 sowie
  u-blox1546-Seriellgeräte. Generische RTL-Dongles werden nicht umprogrammiert.
- `/usr/lib/udev/krakendf-usb-memory`: kleiner Helper, der beim Kraken-Anstecken
  das USB-Transferbudget bei Bedarf auf256MiB anhebt. Bereits größere oder
  unbegrenzte Werte bleiben erhalten. Das Setup wendet ihn einmal sofort an.

Es gibt keine pauschalen 0666-USB-Rechte, keine EEPROM-Änderungen, kein globales
Prozess-Abschießen, kein Drop-Caches und keinen dauerhaft geänderten
Echtzeit-Scheduler. Der Livebetrieb läuft anschließend als normaler Nutzer.
Falls udev nicht aktiv ist, meldet das Setup, dass sein Dienst beziehungsweise
USB-Anschluss vor dem Livebetrieb noch hergestellt werden muss.

Die Regeln sind für einen lokalen Demo-Bediener bestimmt. Eine erneute
Installation mit einem anderen Nutzer ersetzt diesen Eigentümer. Bei einem
VK-162-Exemplar mit anderem USB-Seriell-Chip als u-blox kann weiterhin der Port
manuell gewählt werden; dessen normale Betriebssystemrechte sind dann zu
prüfen.

## Diagnose und spezielle Modi

```bash
bash ./setup.sh --check             # Read-only: App + native Build, kein USBtest
bash ./setup.sh --check --app-only  # Nur App/GPS-Abhängigkeiten prüfen
bash ./setup.sh --dry-run           # Geplante Schritte ohne Änderungen zeigen
bash ./setup.sh --rebuild           # Gepinnte native Quellen neu kompilieren
bash ./setup.sh --app-only          # Simulation/externen DAQ ohne lokalen Stack vorbereiten
```

`--skip-system` setzt bereits vorhandene System-/Entwicklungsabhängigkeiten
voraus und überspringt apt sowie Geräterechtregeln. `--no-device-rules` ist für
bereits vom Administrator eingerichtete USB-Zugriffe gedacht. Beide sind
Expertenoptionen und bedeuten keine automatische Hardwarebereitschaft.

Mit `KRAKENDF_PYTHON=/pfad/python3.12` lässt sich vor der ersten venv-Erstellung
ein anderer geeigneter Interpreter wählen. `KRAKENDF_RUNTIME` überschreibt das
Runtimeverzeichnis. Nach Verschieben des Projekts erkennt die Prüfung den
abweichenden venv-Pfad und baut die Runtime passend neu.

## Tatsächlich geprüfter Stand

- Alle190 gepinnten Upstreamdateien bestehen die Quellhash-Prüfung.
- KrakenRF-librtlsdr und alle vier Heimdall-C-Programme wurden mit GCC14 auf
  x86_64 gebaut; `ldd` fand keine fehlende Bibliothek.
- Eine frische venv wurde vollständig durch pip aufgelöst; tatsächliche
  App-/DAQ-Importe und `pip check` bestanden.
- `setup.sh --check` und `--check --app-only` bestanden ohne angeschlossene
  USB-Geräte.
- Acht zusätzliche Tests prüfen Bootstrap, Manipulations-/Markererkennung,
  Regeln und die unveränderte, tatsächlich als C kompilierte DATA-/CAL-
  Konvertierung für die Eingangsbytes0,127,128,255.

Die tatsächlichen apt-/udev-/USB-Kerneländerungen wurden in dieser Cloud-
Umgebung nicht ausgeführt. Für die Buildprüfung wurden Entwicklungsheader und
CMake lokal unter `/tmp` bereitgestellt; der Host wurde nicht umkonfiguriert.

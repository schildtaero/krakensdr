# Erstinstallation und integrierter Livebetrieb

Version 0.2 enthält die KrakenRF-Treiber- und Heimdall-Quellen mit festen Commitständen, Prüfsummen, Lizenztexten und einer gekennzeichneten Buildanpassung. Es ist kein separates Git-Klonen oder manuelles Bearbeiten der Heimdall-Konfiguration erforderlich.

## Linux

Vorgesehene Zielsysteme: Ubuntu 24.04 oder neuer, Debian 12 oder neuer, x86_64/aarch64, Python 3.11–3.13 für den DAQ. Der native Build wurde auf x86_64 tatsächlich ausgeführt; ein ARM- oder physischer USB-Test wurde hier nicht durchgeführt.

```bash
./start.sh
```

Der Starthelfer prüft die lokale Installation und ruft bei fehlenden Bestandteilen automatisch `setup.sh` auf. Manuell vorbereiten lässt sich der Host mit:

```bash
./setup.sh
./setup.sh --check
```

Der Einrichtungsablauf:

1. Prüft Plattform und Pythonversion. Installiert ausschließlich fehlende apt-Pakete für Python, venv, Compiler, CMake, pkg-config, libusb, ZeroMQ, udev und USB-Diagnose. Keine Fremdrepositories oder Distributionsupgrades.
2. Erstellt `.venv` und installiert die App mit USB-GPS- und DAQ-Abhängigkeiten. NumPy/Numba werden auf kompatible Versionsbereiche begrenzt. Kein globales `sudo pip`.
3. Prüft die gebündelten Quelldateien gegen `vendor/SOURCE_MANIFEST.json`.
4. Baut den KrakenRF-librtlsdr-Fork lokal nach `.runtime/native`. Kernel-USB-Treiber werden bei Bedarf vom Gerät getrennt; eine globale DVB-Blacklist ist nicht notwendig.
5. Baut vier Heimdall-C-Programme sowie die zugehörige Python-Synchronisationskette. Prüft dynamische Bibliotheken und tatsächliche Pythonimporte. Erst danach entsteht der Runtime-Marker mit `ready: true`.
6. Installiert gezielte udev-Regeln für die Kraken-Seriennummern 1000–1004 und native u-blox-GPS-Geräte. Der ausführende normale Benutzer erhält Zugriff, ohne weltweit schreibbare USB-Geräte. Ein endliches USB-Transferbudget von 256 MiB wird bei Bedarf eingerichtet; größere oder unbegrenzte vorhandene Werte bleiben erhalten.

Für apt und die gezielten Systemregeln wird bei Bedarf das sudo-Passwort abgefragt. Compiler, Pythonpakete, DAQ und normale Anwendung laufen als Benutzer. Die Erstinstallation benötigt Netz für apt/PyPI; die Kraken-Quellen sind bereits im ZIP enthalten. Nach erfolgreicher Einrichtung prüft ein normaler Start lokal, ohne erneut Paketdownloads anzustoßen.

## Lokaler DAQ

Live ist sowohl im Startskript als auch im Webinterface der Standard. Der Supervisor startet ausschließlich seine eigenen Prozesse mit privater Konfiguration und eigenen FIFOs. Er prüft USB-Geräte, Seriennummern, Rechte, Ports und freien Shared Memory. Bei Ende oder Quellenwechsel räumt er eigene Prozesse und zugeordnete Speichersegmente auf. Ein fremder laufender DAQ wird nicht beendet; die Anwendung meldet den Konflikt.

Die App liest die tatsächlichen IQ-Header zurück und wartet auf passende Frequenz, Gain und Kohärenz. Prozessstart allein bedeutet keine RF-Bereitschaft. Ohne angeschlossenen Kraken bleibt die Oberfläche mit konkreter Fehlermeldung erreichbar. Es gibt keinen stillen Wechsel auf Simulation und keine Aufnahme bei gescheitertem Empfängerstart.

Die eingebaute native Konverterstufe erlaubt nur Dezimierung 1 und FIR-Länge 1. Das ist der vorgesehene Vollratenbetrieb; die Anwendung filtert und kanalisiert alle fünf Antennen identisch. Die Quellen und die entsprechende GPL-Anpassung sind mitgeliefert.

## Betriebsarten und Diagnose

```bash
./start.sh --source simulation
./start.sh --source idle
./start.sh --daq-mode external --daq-host 192.168.1.50
./setup.sh --dry-run
./setup.sh --rebuild
```

Simulation oder externer Empfänger benötigen keinen lokalen nativen DAQ. `start.sh` verwendet dafür automatisch den `--app-only`-Installerpfad. `setup.sh --check` verändert keine Pakete und benötigt keine Hardware. Ein Fehlerstatus bei unvollständiger Installation ist beabsichtigt.

Für administrativ vorbereitete Systeme gibt es `--skip-system` und `--no-device-rules`. Dann muss der Betreiber die ausgelassenen Abhängigkeiten beziehungsweise Rechte bereits eingerichtet haben. `start.sh --no-setup` überspringt die automatische Paketprüfung, verlangt aber eine vorhandene `.venv`. `--help` startet keinen Installer.

Standardpfade:

- `.venv`: isolierte App-/DAQ-Pythonumgebung.
- `.runtime/manifest.json`: erfolgreicher Build, Quellpins, Hostarchitektur und Interpreterpfad.
- `.runtime/native`: lokal gebaute Treiberbibliothek und Werkzeuge.
- `.runtime/firmware`: gebaute Heimdall-Programme und Pythonmodule.
- `.runtime/runs`: private Laufkonfigurationen/FIFOs des Supervisors.
- `~/.krakendf/logs/daq`: DAQ-Diagnoseprotokolle.

`KRAKENDF_RUNTIME` beziehungsweise `--runtime-dir` überschreibt den Laufzeitpfad; `--data-dir` den Anwendungsdatenpfad. Runtime und venv werden absichtlich nicht als hostunabhängige Binärinstallation im ZIP verteilt. Beim Verschieben des Projektordners `setup.sh` erneut ausführen, damit absolute venv-Pfade neu erzeugt werden.

## Windows

`start.cmd` ruft bei Bedarf das Windows-Setup auf. Standard ist der lokale Linux-Stack in WSL2/Ubuntu 24.04. WSL-Erstinstallation, Windows-Neustart, Linux-Benutzerkonto und USB-Durchreichung können Eingaben des Nutzers erfordern. Das Setup meldet diese Schritte ausdrücklich und startet nicht eigenmächtig neu. [Windows-Anleitung](windows-setup.md).

Für einen nativen Windows-Client an einem Linux-Empfänger im Netz:

```bat
setup.cmd -Mode Native
start.cmd --daq-host 192.168.1.50
```

## Hiesige Prüfung

Die Linux-Paketauflösung wurde in einer frischen lokalen venv tatsächlich ausgeführt. Kraken-librtlsdr und alle vier C-Programme wurden kompiliert und auf auflösbare Bibliotheken geprüft. Der Installationstest `setup.sh --check` besteht. System-USB-Regeln wurden auf diesem Cloud-Host nicht installiert; ihre Erzeugung ist separat getestet. Die physische Kraken-/VK-162-Abnahme sowie ein echter Windows-/WSL-Installerlauf stehen aus.

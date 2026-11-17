# KrakenDF Demo 0.2.3

Lokales Webinterface für einen feststehenden KrakenSDR, eine ARCELI VK-162 GPS-Maus und DL7CR/BFT-Tracker auf LilyGO T-Deck 433. Dieselben POCSAG-/LoRa-Positionspakete werden decodiert und mit fünf kohärenten Antennen gepeilt. Emitter werden anhand des alphanumerischen Rufzeichens zugeordnet.

**Der vollständige lokale Kraken-Stack ist eingebunden; Live ist der Standard.** Das Repository enthält die fest versionierten Heimdall-/Kraken-Treiberquellen, Buildanpassungen und Erststart-Skripte. Die Anwendung übernimmt deren Konfiguration und Prozessverwaltung. Physische Kraken-/433-MHz-/VK-162-Abnahme steht weiterhin aus.

Zentrales Repository: **https://github.com/schildtaero/krakensdr**. Version 0.2.3 zeigt GPS-Verbindung, NMEA-Datenfluss, Satellitensignale und Positionsfix getrennt an, ergänzt einen GPS-Detaildialog und eine Neuverbindung. Die Profile **Pegasus 95** und **Pentagon 105**, Paket-SNR, Diagnose-ZIP und beschleunigte Empfangsverarbeitung aus 0.2.2 bleiben enthalten.

## Schnellstart

Unter **Ubuntu 24.04+ oder Debian 12+**, 64-Bit x86/ARM, im entpackten Projektordner:

```bash
./start.sh
```

Direkt von GitHub beziehen:

```bash
git clone https://github.com/schildtaero/krakensdr.git
cd krakensdr
./start.sh
```

Beim ersten Start werden fehlende Systempakete ermittelt, eine lokale Pythonumgebung eingerichtet und der mitgelieferte DAQ gebaut. Systempakete und USB-Regeln benötigen gegebenenfalls Ihr sudo-Passwort; die normale Anwendung läuft ohne root. **Internet ist für die Erstinstallation der Paketabhängigkeiten erforderlich.** Nach erfolgreicher Einrichtung benötigt ein normaler Start keine Paketdownloads.

Anschließend **http://127.0.0.1:8080** öffnen. Kraken mit Datenkabel und geeigneter Stromversorgung sowie die GPS-Maus anschließen. Unter **Windows 11**:

```bat
start.cmd
```

Der Windows-Erststart richtet den vollständigen lokalen Stack über **WSL2/Ubuntu 24.04** ein. Neue WSL-Komponenten können einen Windows-Neustart und das einmalige Anlegen eines Linux-Benutzers erfordern. Kraken und VK-162 müssen mittels `usbipd` an WSL durchgereicht werden; das Setup zeigt die dafür benötigten Schritte. Kein automatischer Neustart. Siehe [Windows-Anleitung](docs/windows-setup.md).

Softwareinstallation separat vorbereiten oder prüfen:

```bash
./setup.sh                  # fehlende Abhängigkeiten auflösen und DAQ bauen
./setup.sh --check          # nur prüfen, keine Installation und kein USB nötig
./setup.sh --dry-run        # geplante Schritte anzeigen
./start.sh --source simulation  # ausdrücklich Demo ohne Hardware
```

Die Simulation verwendet synthetische Fünfkanal-IQ-Daten mit echter MUSIC-Berechnung. Deren Telegrammidentität kommt aus Fixture-Metadaten und gilt nicht als HF-Decodernachweis. Für eine reine Simulations-/Remote-Installation reicht `./setup.sh --app-only`; der Starthelfer erkennt diese Betriebsarten selbst. JavaScript, CSS und Symbolbibliotheken werden lokal ausgeliefert, ohne CDN und ohne Frontend-Build.

Weitere Optionen:

```bash
python -m krakendf --source idle --port 8080 --data-dir ./data
python -m krakendf --host 0.0.0.0 --port 8080
python -m krakendf --source live --daq-mode external --daq-host 192.168.1.50
```

Standardmäßig bindet die Anwendung nur an localhost. Die zweite Variante dient der Bedienung im eigenen lokalen Netz. Es gibt keine Benutzeranmeldung. Mehrere Backendprozesse auf demselben Datenordner werden nicht unterstützt.

## Funktionen

- **Azimutdiagramm:** gemessene Peilung und GPS-Sollrichtung je Rufzeichen, klare Live-/Replay-/Simulationskennzeichnung.
- **Karten-Tab:** APP-6(B)-kompatible Symbolauswahl und SIDC je Rufzeichen, Empfängerposition, Heading und Peillinien. Einstellungen werden im Backend gespeichert.
- **Offlinekarte:** standardmäßig offline. Raster-MBTiles mit PNG-/JPEG-Kacheln importieren oder nach explizitem Online-Umschalten betrachtete OSM-Kacheln lokal puffern. Kein Massenvorabruf. Ohne vorhandene Kacheln erscheint ein beschriftetes Koordinatenraster statt eines behaupteten Kartenhintergrunds.
- **Spektrum/Wasserfall** und einstellbare RF-Mitte, Kanalbandbreite, Gain und FFT-Länge. Empfangspegel sind relative digitale Werte, keine kalibrierten dBm.
- **Fünf Antennenprofile:** Pegasus 95 (Kreuz mit 95 mm Armlänge plus Mitte), Pentagon 105 (105 mm Radius), Kreis Ø30 cm, Kreis Ø56 cm und Quadrat 32 cm plus Mitte. Beide neuen Profile haben CH0 vorne und die übrigen äußeren Kanäle im Uhrzeigersinn. Vorhandene Konfigurationen werden nicht automatisch umgestellt; das passende Profil im Setup auswählen.
- **Paket-SNR im Tracking:** geschätztes Signal-/Rauschleistungsverhältnis im jeweiligen RF-Kanal, auch für negative dB-Werte. Ohne geeignete Rauschreferenz steht „—“. Verfahren und Rohleistungen werden mit der Aufnahme gespeichert; die Simulation kennzeichnet ihren bekannten synthetischen Rauschpegel gesondert.
- **Status und Fehlerprotokoll:** Download eines Diagnose-ZIPs mit Ereignis-/Fehlerlog, Tracebacks, begrenzten DAQ-Logauszügen und Verarbeitungskennzahlen. Im Statusdialog stehen RF-/Decoderwarteschlangen, Pipelinezeit im Verhältnis zur RF-Dauer und der verwendete DSP-Pfad.
- **Heading:** manuell oder Abgleich an einem bekannten, frisch empfangenen BFT mit gültiger Position.
- **Stationsposition:** manuell oder GPS. Der stationäre GPS-Standort wird über zehn zeitlich verschiedene gültige Fixmeldungen gemittelt und anschließend fixiert. GPS-Kurs über Grund wird nicht als Array-Heading benutzt.
- **GPS-Status:** Verbindung, Datenfluss und Fix getrennt; Satelliten mit gemeldetem Signal, im Sichtfeld und im Fix auch ohne Positionsfix sichtbar. Klick auf die GPS-Kachel öffnet Port/Baudrate, Datenalter, C/N0-Balken je Satellit, HDOP, Ankerfortschritt und „GPS neu verbinden“. Fehlende Werte bleiben „—“ und veraltete Daten werden kenntlich gemacht.
- **Record/Replay:** Messwerte, Profile, Qualitätsflags, Spektrum und kohärente Fünfkanal-IQ-Ausschnitte je zugeordnetem Paket; erneute MUSIC-Auswertung beim Replay.
- **Kalibrierung:** periodische Winkelkorrektur, Azimutabdeckung und unabhängiger Prüflauf. Ungeprüfte oder verschlechternde Modelle können nicht aktiviert werden.
- **Excel:** `.xlsx` mit Einzelmessungen, Gesamt-RMS, RMS nach Referenzazimut, Profilen und Kalibrierbefunden. Empfangstexte werden als Text und nicht als Excel-Formeln exportiert.

## Erster Demolauf

1. Mit Hardware normal starten; ohne Hardware `./start.sh --source simulation` verwenden oder im Quelldialog ausdrücklich Simulation wählen. Nach wenigen Sekunden erscheinen dort drei synthetische Emitter, beschleunigt und markiert.
2. Zwischen **Azimut** und **Karte** wechseln. Ein Symbol auf der Karte anklicken, um Zuordnung, Kategorie oder 15-stelligen APP-6(B)-SIDC zu ändern.
3. Über **Aufnehmen** eine Messung starten, einige Pakete sammeln und stoppen.
4. Unter **Aufnahmen** dieselbe Sitzung wiedergeben oder nach Excel exportieren.
5. Für eine Beispielkalibrierung einen Trainingslauf bis zur ausreichenden Richtungsabdeckung aufnehmen, stoppen und eine neue unabhängige Runde als Prüflauf aufnehmen. Beide unter **Kalibrierung** auswählen. Die Mindestabdeckung beträgt im Training drei gültige Pakete in jedem der 24 Sektoren, im Prüflauf mindestens eines je Sektor. Der Algorithmus verlangt getrennte Lauf-IDs und dasselbe Messprofil.

Für echte Messungen danach Datenquelle **Live** wählen. Fehlende Hardware erzeugt einen sichtbaren Fehlerzustand; es erfolgt kein stiller Rückfall auf Simulation.

## Kraken-Hardware anschließen

1. KrakenSDR anschließen und `./start.sh` beziehungsweise `start.cmd` ausführen. Die Erstinstallation übernimmt Paketauflösung, Treiber und DAQ-Build. Keine separate Heimdall-Installation und keine manuelle INI-Bearbeitung erforderlich.
2. **Live → KrakenSDR am USB-Anschluss** ist voreingestellt. Die App erzeugt eine private Laufkonfiguration, aktiviert die interne Kohärenz-/Rauschquellenkalibrierung und startet sechs eigene Prozesse. Beim Stoppen werden nur diese Prozesse beendet. Ein bereits anderweitig laufender DAQ wird nicht übernommen oder beendet.
3. Antennenprofil, BFT-Frequenzen und diskreten Gain einstellen. Standardrate ist 2,4 MS/s. Änderungen im lokalen Livebetrieb starten den verwalteten DAQ passend neu. Tatsächliche Frequenz, Rate, Gain und Locks werden aus IQ-Headern geprüft; ein gestarteter Prozess gilt nicht als Kohärenznachweis.
4. Stationsposition über VK-162 übernehmen oder manuell setzen, Heading an einem bekannten BFT abgleichen oder manuell eintragen, dann messen.

Die fünf werkseitigen Tuner-Seriennummern **1000–1004** werden geprüft. Kein automatisches EEPROM-Umprogrammieren. USB-Regeln sind auf diese Geräte und native u-blox-GPS-Geräte begrenzt. Der installierte USB-Helfer erhöht bei Bedarf das endliche Transferbudget auf 256 MiB; es wird weder ein globales DVB-Blacklistfile angelegt noch der Host neu gestartet. Bei fehlenden Rechten oder Hardware zeigt das UI den konkreten Fehler und wechselt nicht still auf Simulation.

Alle BFT-Kanäle einschließlich LoRa-Bandbreiten und Filterrändern müssen gemeinsam ins Aufnahmefenster passen. Die anfänglichen App-Kanäle sind **Beispiele** und müssen zu den Trackern passen. Die fünf Antenneneingänge sind gemeinsam abgestimmt.

Für einen vorhandenen Empfänger im Netz im Quelldialog **Externer Empfänger** wählen oder `--daq-mode external --daq-host …` verwenden. Dessen Heimdall wird nicht von der App verwaltet. Der Adapter verwendet IQ-Port 5000, Controllerport 5001 und little-endian **Heimdall-Header v7**. Ein nativer Windows-USB-DAQ ist nicht enthalten; der lokale Windows-Weg ist WSL2, ein nativer Windows-Client kann alternativ einen Linux-DAQ im LAN verwenden.

Die gebündelte native Stufe ist absichtlich auf **Dezimierung 1 und FIR-Länge 1** begrenzt. Die identische Kanalfilterung erfolgt in der Anwendung. Dadurch sind für diese Demo keine zusätzlichen KFR-/Ne10-Builds nötig. Originalquellen, gekennzeichnete GPL-Anpassung und Buildskripte sind enthalten. Siehe [DAQ-Verwaltung](docs/managed-daq.md) und [Erstinstallation](docs/setup.md).

## ARCELI VK-162

USB-GPS-Unterstützung wird mit installiert. Eine eindeutig erkannte VK-162/u-blox-GPS-Maus wird automatisch ausgewählt. Alternativ im Setup **GPS-Ports suchen** verwenden oder den Port eintragen, unter Linux häufig `/dev/ttyACM0` oder `/dev/ttyUSB0`, beim nativen Windows-Client `COM3` o.ä. Einstieg mit **9600 Baud**, bei abweichender Geräteinstellung anpassen. Unter WSL muss auch die Maus an Linux durchgereicht sein. Das konkrete Exemplar und seine Baudrate wurden hier nicht verifiziert.

NMEA-RMC/GGA liefern Position und Fix, GSA/GSV ergänzen Fixart und Satelliten-/Signalangaben. Alle Meldungen werden auf Prüfsumme geprüft; Satellitendaten verjüngen keinen alten Positionsfix. RMC und GGA derselben UTC-Epoche zählen für die Standortmittelung gemeinsam als ein Fix. `GET /api/gps/ports` zeigt erkannte Ports, wenn pyserial installiert ist. Unter Linux benötigt der ausführende Benutzer Zugriff auf den seriellen Port. NMEA-Zeitstempel liefern keinen PPS-synchronisierten RF-Sampletakt; die Paket-/DF-Zuordnung erfolgt über IQ-Sampleindizes.

Die GPS-Kachel zeigt die tatsächliche Hardwaretelemetrie unabhängig von manueller, simulierter oder aufgezeichneter Stationsreferenz. **„Port offen“**, **„gültige NMEA-Daten“**, **„Signal gemeldet“** und **„Positionsfix“** sind verschiedene Zustände. Ein bereits fixierter stationärer GPS-Anker bleibt bei einem späteren Empfangsausfall erhalten. **GPS neu verbinden** erhält diesen Anker und lässt die RF-Quelle unverändert; Aufnahme oder Replay vorher beenden. Beschreibung aller Zustände und Satellitenzahlen: [GPS-Anzeige](docs/gps-anzeige.md).

## Verarbeitung bei 2,4 MS/s und Fehlerdiagnose

Die lokale DAQ-Installation enthält bereits Numba. Version 0.2.2 fasst Frequenzmischung, den unveränderten kausalen SOS-Filter und die Dezimierung in einem kompilierten Durchlauf zusammen. Das spart große temporäre Fünfkanalarrays. Die Vorbereitung erfolgt vor dem Empfang; bei fehlender oder nicht verfügbarer Beschleunigung bleibt der SciPy-Pfad nutzbar und wird im Status kenntlich gemacht. Paketdecodierung und MUSIC laufen getrennt von der laufenden RF-Kanalverarbeitung in einer begrenzten Warteschlange.

**Status → Verarbeitung** zeigt `Pipeline / RF-Zeit`: Anhaltende Werte über 100 % bedeuten, dass diese Stufe langsamer als der eintreffende RF-Strom ist. Es handelt sich nicht um die Gesamt-CPU-Auslastung. Eine übervolle RF- oder Decoderwarteschlange bleibt ein sichtbarer Fehler; die Software überspringt keine Datenlücken als vermeintlich kohärenten Empfang.

Über **Status → Fehlerprotokoll herunterladen** oder `GET /api/diagnostics/download` ein ZIP für die Diagnose speichern. Es enthält maximal 4 MiB Logauszüge plus Metadaten; pro Datei wird höchstens das Ende mit 256 KiB übernommen. Das Anwendungslog rotiert unter `~/.krakendf/logs/application.log`, die DAQ-Protokolle liegen unter `~/.krakendf/logs/daq`. Aufnahmen und Kartendaten werden nicht eingepackt. Der Diagnose-Snapshot lässt Stations-/Emitterkoordinaten, Nutzlasten und TX-Notizen aus.

Messaufbau, Benchmark, Bedeutung des SNR und praktische Grenzen: [Verarbeitung und SNR](docs/verarbeitung-und-snr.md). Eine Benchmarkreserve des Entwicklungsrechners ist keine Echtzeitzusage für jeden Windows-/WSL-/Linux-Host.

## Aufnahmen und Daten

Standard-Datenverzeichnis ist `~/.krakendf`; `--data-dir` oder `KRAKENDF_DATA` überschreibt es. Darin liegen Konfiguration, Symbole, Korrekturmodelle, Sitzungsmanifeste, Ereignislogs, IQ-Clips und Kartencache. Kein automatisches Löschen alter Aufnahmen.

Aufnahmen speichern die ausgewählten kanalisierten IQ-Paketfenster, **keine kontinuierliche Breitbandaufzeichnung**. Das Replay erlaubt neue Peilberechnungen innerhalb dieser Fenster, aber kein nachträgliches Empfangen anderer RF-Kanäle. Empfangsunterbrechungen setzen die Capture-Pipeline zurück; beschädigte Pakete werden nicht als gültige Referenzen verwendet. Eine direkte Vollbandaufzeichnung und dauerhafte Aufnahme aller anonymen/undecodierbaren Bursts sind noch nicht als Bedienoption implementiert.

Beim Profilwechsel wird die laufende Statistik getrennt und die Korrektur deaktiviert. Aufnahme und Replay sperren Profiländerungen. Korrekturmodelle werden bei korrigierten Aufnahmen mit den Beobachtungen gesichert. Trainings- und Prüfläufe mit verschiedenen Datenarten oder Profilen dürfen nicht kombiniert werden.

## Testen und Grenzen

```bash
python -m unittest discover -s tests -v
node --check krakendf/static/app.js
node --check krakendf/static/map.js
```

Die Tests prüfen Protokolle, DSP, Bursts über Framegrenzen, Decodierung, Samplezuordnung, HTTP/WebSocket-API, Record/Replay, Excel, Offlinecache und MBTiles. Die optionale reale LoRa-Referenzaufnahme liegt im externen SimpleSDR-Repository und wird ohne dieses Fixture übersprungen. Sie ist eine einkanalige Pluto-Aufnahme bei 868 MHz, kein Kraken-433-MHz-Genauigkeitsnachweis.

LoRa bleibt als **experimentell / Hardwareabnahme offen** gekennzeichnet. Die reale Referenzaufnahme decodiert mit Header- und Payload-CRC; Empfindlichkeit, Echtzeitleistung des Zielhosts und Winkel-RMS müssen am Aufbau gemessen werden. Die Browserdarstellung wurde statisch geprüft; die Cloud-Umgebung bot das vorgeschriebene Browserwerkzeug nicht an.

## Dokumentation

- [PDF-Handbuch: Funktion und Bedienung](docs/KrakenDF-Funktion-und-Bedienung.pdf) — Grundlagen mit Softwarestand 0.2.1: Peilprinzip, Antennenaufbau, Heading, Livebetrieb, Kalibrierung und RMS; [bearbeitbare Quelle](docs/df-handbuch.md), [PDF neu erstellen](docs/pdf-erstellen.md). Ergänzungen ab 0.2.2 stehen unter Antennenprofilen sowie Verarbeitung und SNR.
- [Spezifikation](docs/spezifikation.md) — ursprünglicher Entwurf; Umsetzung und Abweichungen siehe diese README.
- [Antennenmaße und Kanalzuordnung](docs/antennen.md)
- [Hardwareprotokoll](docs/hardware.md)
- [Erstinstallation und Paketauflösung](docs/setup.md)
- [Integrierter DAQ](docs/managed-daq.md)
- [Windows/WSL2](docs/windows-setup.md)
- [Decoderstatus](docs/decoder-status.md)
- [Offlinekarten und MBTiles](docs/maps.md)
- [Prüfstand](docs/validierung.md)
- [Verarbeitung, SNR und Diagnose](docs/verarbeitung-und-snr.md)
- [GPS-Verbindung, Satelliten und Fix](docs/gps-anzeige.md)
- [Drittbibliotheken](THIRD_PARTY.md)

OpenAPI-Dokumentation läuft lokal unter `/docs`. Externe SimpleSDR-/Kraken-Quellcodeverzeichnisse sind zur Laufzeit nicht erforderlich.

## Projekt aktualisieren

Backend mit Strg+C beenden, im Git-Checkout `git pull --ff-only` ausführen und erneut `start.cmd` beziehungsweise `./start.sh` starten. Unter Windows aktualisiert eine neue Projektversion die WSL-Kopie über den vorhandenen Starthelfer. Keine WSL-Abschaltung nötig; USB-Durchreichung bleibt so erhalten. Nach Änderungen an der Oberfläche die Seite mit Strg+F5 neu laden.

Alternativ die [aktuelle ZIP-Datei von GitHub](https://github.com/schildtaero/krakensdr/archive/refs/heads/main.zip) in einen neuen Ordner entpacken und dort `start.cmd` bzw. `./start.sh` ausführen. Die neue Paketversion 0.2.3 löst bei einer vorhandenen Windows-Installation die Aktualisierung der Linux-Projektkopie aus. Danach unter **Setup → Antennenprofil** ausdrücklich Pegasus 95 oder Pentagon 105 auswählen; gespeicherte alte Profile werden nicht still ersetzt.

Eigene Änderungen vor dem Pull mit `git status` prüfen und sichern. Aufnahmen, `.runtime`, `.venv`, lokale Karten und Konfigurationen werden nicht über Git synchronisiert. Nach neuen abgeschlossenen Änderungen wird der geprüfte Quellcode in dieses Repository übertragen.

# Prüfung der Implementierung 0.2.3

Stand: 17. November 2026. **189 automatisierte Tests bestanden** in 11,9 Sekunden.
Zusätzlich zum vorherigen Stand sind geprüft:

- 15 GPS-Telemetrietests: prüfsummengültige RMC/GGA/GSA/GSV, mehrteilige vollständige Satellitenzyklen, C/N0, gemeldete Nullwerte und unbekannte Angaben, mehrere Signalbänder/GNSS-Systeme, unabhängiges Altern von Position und Satellitendaten, Datenzähler und Fehlerfälle. Konstellationsbezogener Fixverlust verwirft keine weiterhin gültige kombinierte Position eines anderen Systems.
- Sieben GPS-Serviceprüfungen: Portöffnung, Daten ohne gültiges NMEA, Satelliten ohne Fix, veraltete Daten, Port-/Lesefehler, zehn verschiedene UTC-Fixepochen mit RMC/GGA-Deduplizierung, automatische Suche beim Wechsel von manuell auf GPS sowie GPS-Diagnose ohne Koordinaten.
- API-Neuverbindung mit unveränderter RF-Konfiguration und erhaltenem Stationsanker; Neuverbindung während Aufnahme oder Replay wird abgewiesen.
- JavaScript-/Python-/Shellsyntax, DOM-Verweise und ein Node-Renderharness für unbekannte/Null-Satellitenwerte, Fix/kein Fix/veraltete Daten, manuelle/Simulation-/Replay-Referenz, unterbrochene Backend-Verbindung und gesperrte Neuverbindung.

Ein interaktiver Browserlauf und die physische VK-162-/Windows-/WSL-Abnahme wurden nicht durchgeführt. Die Anzeige wertet die tatsächlich vom Empfänger gemeldeten NMEA-Felder aus; fehlen GSV-Angaben, bleiben Sichtfeld und C/N0 unbekannt. Bestehende DF-, Decoder-, Aufnahme-/Replay- und Performanceregressionen bestehen weiterhin. Bedienung und Feldbedeutung: [GPS-Anzeige](gps-anzeige.md).

## Vorheriger Prüfstand 0.2.2

Stand: 16. November 2026. **166 automatisierte Tests bestanden** in 14,6 Sekunden.
Die vollständige Suite umfasst zusätzlich zu den unten dokumentierten 0.2.1-Prüfungen:

- Alle fünf Antennenprofile einschließlich Pegasus 95 und Pentagon 105; identische Quell-/Paketkoordinaten und MUSIC mit tatsächlicher Kanalzuordnung.
- Neun SNR-Prüfungen mit bekannten Rauschleistungen bis −10 dB, fehlenden oder kontaminierten Referenzen, negativen Werten und ungültigen Daten; Übernahme in Trackingdaten, Aufnahme/Replay und numerischen Excel-Export.
- Acht Diagnoseprüfungen: persistente Logs/Tracebacks, Download-ZIP, fehlende Logs, Rotation, Größenlimits, gesperrte Symlinks/FIFOs, ausgelassene Positionsdaten und HTTP-Fehlerprotokollierung.
- Sieben Tests der getrennten RF-/Decoder-Worker: fortlaufende RF-Verarbeitung bei blockiertem Decoder, sichtbare Queueüberläufe, Abbruch ohne verspätete Messwerte, korrekte Aufnahme-/Laufzuordnung und Spektren sowie Aufräumen trotz Aufnahmeabschlussfehler.
- Zwei zusätzliche DSP-Prüfungen: beschleunigter und SciPy-Pfad gegen den ursprünglichen kausalen Filter bei 2,4 MS/s, mehreren Bandbreiten und beliebigen Framegrenzen sowie nutzbarer Fallback ohne Numba. Der vollständige POCSAG-/LoRa-IQ-Empfangspfad mit Record/Replay bei 2,4 MS/s besteht weiterhin.

JavaScript-, Shell- und Python-Syntaxprüfung bestanden. Die neuen Frontendfelder wurden außerdem mit einem kleinen Node-DOM-/Canvas-Harness auf fehlendes/0/negatives SNR und korrekte Profilbeschriftungen geprüft. Dies ersetzt keinen interaktiven Browserlauf. Der kontrollierte Softwarebenchmark erreicht auf diesem Entwicklungsrechner etwa 1,82-fachen Kanalverarbeitungsdurchsatz; Umfang und Grenzen siehe [Verarbeitung und SNR](verarbeitung-und-snr.md).

Die physische Abnahme des Zielhosts bei 2,4 MS/s einschließlich Kraken, USB/WSL, realer BFT-Sendungen und VK-162 steht weiterhin aus. Die folgenden 0.2.1-Befunde werden als vorheriger Prüfstand aufbewahrt; der GitHub-Workflow prüft inzwischen auch PowerShell-Syntax auf Windows, jedoch keine reale Windows-/WSL-USB-Installation.

## Vorheriger Prüfstand 0.2.1

Stand: 16. November 2026. **140 automatisierte Tests bestanden** in der durch die Erstinstallation aufgelösten lokalen Pythonumgebung. Zusätzlich Python-Bytecodeprüfung und JavaScript-/Shellsyntaxprüfung bestanden. Die Tests mit TCP-/asyncio-Selbstkommunikation wurden außerhalb der restriktiven Socket-Sandbox ausgeführt; es wurden dabei keine physischen Empfänger oder externen Kartendienste verwendet. Der optionale Test mit der externen LoRa-Aufnahme wird übersprungen, wenn das Referenzrepository nicht vorhanden ist.

| Bereich | Geprüfter Befund |
|---|---|
| DSP | MUSIC/Bartlett, alle drei Geometrien, Nordübergang, WGS84-Referenzen, Sektorstatistik und unabhängige Kalibrierprüfung |
| Heimdall | Fragmentierte Header/Payloads, v7-Format, IQ-Locks, CAL/DUMMY, Retuning, Controllerpakete, Sample-Epochen und geschätzte Frameanfangszeit |
| NMEA | RMC/GGA, Prüfsummen, Fixverlust, Koordinaten und südliche Hemisphäre |
| Decoder | POCSAG1200 mit BCH/Parität, Rufzeichen/Payloadparser, unabhängiger LoRa-PHY einschließlich Header/CRC und Frequenzversatz |
| Reales LoRa-Fixture | Neue Implementierung decodiert `DL7CR TESTmessageLoRaTST` mit gültigem Header und CRC aus der vorhandenen einkanaligen 868,3-MHz-Aufnahme |
| Vollständiger Empfangspfad | Gleichzeitige POCSAG-/LoRa-RF-Kanäle im gemeinsamen synthetischen 2,4-MS/s-Fünfkanalstrom, CPI-Übergänge, Decoder, Rufzeichen/GPS, MUSIC mit Heading, Recording und Replay |
| Fehlerfall | LoRa mit falschem CRC wird erfasst, erzeugt aber keinen Emitter-/Positionsdatensatz |
| Record/Replay | Dauerhafte IQ-Clips, atomare Metadaten, Fehlererholung, Pfadprüfung, erneute MUSIC-Peilung und gespeicherte Korrekturmodelle |
| API | Konfiguration, manuelle Position/Heading, Quellenwechsel, Aufnahme, Replay, Kalibrierung, Symbolpersistenz, WebSocket und Excel |
| Karte | Offline ohne Netzabruf, MBTiles-Import, TMS/XYZ, PNG/JPEG, Cache, HTTP-Fehlerfallback und sichere Attribution |
| Paketierung | Installierbares Wheel enthält Antennenprofile, Weboberfläche, Leaflet/milsymbol und Drittbibliothekslizenzen |
| Nativer DAQ-Build | KrakenRF-librtlsdr und vier Heimdall-C-Programme mit GCC tatsächlich kompiliert, dynamische Bibliotheken geprüft, DAQ-Pythonimporte erfolgreich |
| Erststart | Fehlende Abhängigkeiten, Quellhashes, Runtime-Marker, Installer-Dry-Run, scoped udev und unveränderte DATA-/CAL-Konvertergleichung geprüft |
| Prozessverwaltung | Eigene DAQ-Kindprozesse, Start/Stop, Timeoutbereinigung, Konflikte und Schutz fremder Prozesse/Speichersegmente geprüft |
| Venv-Interpreter | Reale temporäre venv mit Symlink: gestarteter DAQ-Interpreter behält `sys.prefix` und kann ein ausschließlich in der venv vorhandenes Modul importieren; `.resolve()` wird dafür ausdrücklich vermieden |
| Live als Standard | Fehlender Empfänger oder fehlende Runtime bleibt als Fehler im erreichbaren UI sichtbar; kein Simulationsfallback, keine Aufnahme eines gescheiterten Starts |
| GPS-Automatik | Eindeutige Geräteerkennung, mehrere passende Geräte und fehlende serielle Abhängigkeit mit Mocks geprüft |

Nicht geprüft sind die physische Kraken-/433-MHz-/VK-162-Integration, echte HF-Empfindlichkeit, tatsächlicher Winkel-RMS und Echtzeitreserve des späteren Hosts. Windows-/WSL-/PowerShell-Ausführung und ein nativer ARM-Build wurden hier ebenfalls nicht durchgeführt. LoRa bleibt als experimentell/hardwareungeprüft gekennzeichnet. Die frühere Pluto-Aufnahme belegt Decoderinteroperabilität, keine Fünfkanalpeilung.

Browserdarstellung: JavaScript-Syntax, DOM-IDs, ARIA-Verweise, Assetpfade und API-Verträge geprüft. Eine interaktive Browserprüfung war nicht möglich, weil die ausgewählte Cloud-Umgebung das vorgeschriebene `cua_repl.js`-Werkzeug nicht angeboten hat. Remote-Desktop war als enabled/unknown gemeldet; es wurde keine alternative Browserroute verwendet.

## Historische Quellprüfung vor Implementierung

Die folgenden Resultate beschreiben den ursprünglichen SimpleSDR-/BFT-Bestand und werden zur Nachvollziehbarkeit behalten. Die negative Decodierung betrifft den damaligen SimpleSDR-Decoder; die neue Implementierung ist oben beschrieben.

| Prüfung | Ergebnis und Aussagegrenze |
|---|---|
| BFT-Quellparameter | SF9/BW125k/CR4/5/Syncword0x12 und POCSAG1200 bestätigt; installierter Gerätestand noch unbekannt |
| Airtimerechnung | 44-Byte-Beispiel bei explizitem Header/CRC: 287,744 ms; RadioLib-Defaults am Gerät verifizieren |
| Antennen-JSON | Einheiten, fünf eindeutige Kanäle/Positionen, Radien, Nachbarabstände und Aperturen rechnerisch geprüft |
| Ideale Arrayantworten | 0,5°-Azimutgitter, kein identischer Steuervektor bei >=30° getrennten Richtungen auf diesem Gitter; kein realer Genauigkeitsnachweis |
| POCSAG-Selbsttest | Synthetisches FSK-IQ wird als `DL7CR JO31AB` decodiert, Prozess erfolgreich beendet |
| LoRa-PHY | 13 synthetische Roundtrips bestanden, einschließlich CFO ±1500 Hz; interne Konsistenz allein beweist keine RadioLib-Kompatibilität |
| LoRa-Streaminghelfer | Synthetisches `END-TO-END` mit gültigem CRC; nur synchrone Helfer erfolgreich geprüft |
| Bestehender asynchroner LoRa-Gesamttest | Nicht zum Abschluss gekommen und Diagnoseversuch beendet; Ursache nicht bewiesen, kein bestandener Gesamttest |
| Reales LoRa-Repository-Fixture | 40 Mio. complex64-Samples, einkanalig, 16 s, 2,5 MS/s, 868,3 MHz; Burst gefunden, eigener PHY scheitert bei drei geprüften SFD-Ausrichtungen am Header-Checksum |
| GNU Radio/gr-lora_sdr | GNU Radio und PMT hier nicht vorhanden, deshalb nicht ausgeführt |
| Kraken-/433-MHz-Peilung | Keine entsprechende Hardware oder kohärente Fünfkanalaufnahme vorhanden; nicht geprüft |
| DF-Kalibrierverbesserung | Keine Messdaten für unabhängige Vorher-/Nachher-Validierung; keine RMS-Zusage |

Die negative Decodierung der gespeicherten 868-MHz-Aufnahme ist ein Integrationsbefund, kein Nachweis, dass LoRa mit KrakenSDR grundsätzlich nicht peilbar wäre. Es fehlen weiterhin echte kohärente 433-MHz-Pakete und die Hardware-Abnahme.

Details und Quellen stehen in `quellen-decoder.md`, `quellen-kraken.md` und `antennen.md`. Der vorgesehene Softwareumfang und die Abnahmekriterien stehen in `spezifikation.md`.

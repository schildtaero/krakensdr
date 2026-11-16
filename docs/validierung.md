# Prüfung der Implementierung 0.2.1

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

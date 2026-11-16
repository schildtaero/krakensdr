# Integrierter Kraken-DAQ

Der lokale Live-Modus startet den mitgelieferten und durch `setup.sh` gebauten Heimdall-Stack selbst. Ein gesondertes Ausführen oder Bearbeiten der ursprünglichen `daq_start_sm.sh` ist nicht nötig. Die Anwendung nutzt weiterhin die gleichen geprüften TCP-IQ-/Controllerprotokolle; der Prozessmanager ist von der Decodierung und Peilauswertung getrennt.

## Runtime-Vertrag

Erst nach einer vollständigen Installation liegt unter `.runtime/manifest.json` ein gültiger Marker:

```json
{
  "schema_version": 1,
  "ready": true,
  "firmware_dir": "firmware",
  "native_prefix": "native",
  "python": "/absolute/project/.venv/bin/python"
}
```

`firmware/_daq_core/` enthält die vier gebauten Programme `rtl_daq.out`, `rebuffer.out`, `decimate.out` und `iq_server.out` sowie die Python-Module des gepinnten Heimdall-Stands. `native/lib` enthält den Kraken-spezifischen librtlsdr-Treiber. Python und native Bibliotheken bleiben im Projekt; der normale Live-Start benötigt kein sudo.

`ManagedDAQ(runtime_dir, log_dir)` bietet `status()`, `await start(config)` und `await stop()`. Fehlende Installation und nicht angeschlossene Hardware ergeben einen sichtbaren Fehler; der Supervisor simuliert keinen funktionierenden Empfänger.

## Startfolge

Vor dem Start werden Linux/64-Bit-Architektur, Installation, diskreter Gain, unterstützte RTL-Abtastrate, freie Ports 1130/5000/5001, `/dev/shm` und fünf lesbare eindeutige Seriennummern 1000–1004 geprüft. Der Manager verändert keine EEPROM-Seriennummern.

Jeder Lauf erhält ein privates Arbeitsverzeichnis `.runtime/runs/<zufällige-ID>` mit eigener Konfiguration und eigenen FIFOs. RF-Mitte, Abtastrate und Gain stammen aus dem aktiven App-Profil. Eingeschaltet bleiben fünf Kanäle, Rauschquellensteuerung und Kohärenzkalibrierung. ADPIS ist ausgeschaltet. Die Integration verwendet volle Aufnahmerate ohne zusätzliche Heimdall-Dezimierung; Kanalfilterung übernimmt die Anwendung.

Gestartet werden:

1. `rtl_daq.out` mit Pipe nach `rebuffer.out`.
2. `decimate.out` für die korrigierbare Fünfkanal-Datenkette.
3. `delay_sync.py` für Sample- und Phasenkohärenz.
4. `hw_controller.py` für Rauschquelle, Retuning und Gain.
5. `iq_server.out` als lokale TCP-Ausgabe.

Der Supervisor prüft innerhalb von 45 Sekunden, ob alle eigenen Prozesse laufen und IQ-/Controllerport lauschen. Er öffnet dafür keinen konkurrierenden IQ-Client. Das ist eine Startprüfung, keine Bestätigung bereits erreichter RF-Kohärenz: gültige Locks, Gain, Frequenz und Framequalität werden anschließend anhand der tatsächlichen IQ-Header geprüft.

## Stoppen und Diagnose

Beim Quellenwechsel oder Beenden werden ausschließlich die sechs vom eigenen Supervisor gestarteten Prozessgruppen beendet. Es gibt kein `pkill`, kein Beenden einer bereits laufenden fremden Heimdall-Instanz, keinen Cacheflush und keine normalen Runtime-sudo-Aufrufe.

Heimdall verwendet feste globale POSIX-Shared-Memory-Namen. Bereits vorhandene unbekannte Segmente führen deshalb zu einer Diagnose statt zu ihrer Löschung. Während eines eigenen Starts werden nur Segmente erfasst, die von eigenen Kindprozessen geöffnet sind. Aufräumen löscht nur deren unveränderte, dem Benutzer gehörende Dateiinodes. Ersetzte Segmente und fremde Prozesse bleiben unangetastet. Nach einem unkontrollierten Hostabbruch können deshalb verwaiste Segmente einen Neustart blockieren; laufende Empfänger prüfen beziehungsweise den Host neu starten.

Die Programmprotokolle liegen im konfigurierten Diagnoseverzeichnis, standardmäßig unter dem App-Datenverzeichnis `logs/daq`. Der Supervisor arbeitet ohne Echtzeitprioritätszwang; tatsächliche Reserven des Hosts müssen mit dem realen Kraken geprüft werden. Für die Standard-CPI sind mindestens ungefähr 292 MiB freies `/dev/shm` erforderlich; kleine Container-Defaults von 64 MiB reichen nicht.

Unter nativem Windows ist dieser USB-DAQ nicht ausführbar. Der mitgelieferte Windows-Startweg verwendet WSL2; das USB-Gerät muss dort verfügbar sein. Alternativ kann der bestehende externe Linux-DAQ-Modus verwendet werden. Ohne WSL/USB-Passthrough wird kein nativer Windows-USB-Livebetrieb behauptet.

## Ausgeführte Tests

15 Manager-Tests prüfen Runtime-/Parameterfehler, Geräte- und Portdiagnosen, private FIFOs, Erhalt der Kalibrierparameter, echte harmlose Kindprozesse, Timeout-/Prozessfehleraufräumung, idempotentes Stoppen und den Schutz fremder Prozesse beziehungsweise geänderter Shared-Memory-Inodes. Es handelt sich um Software-/Prozessprüfungen, nicht um eine physische RF-Hardwareabnahme.

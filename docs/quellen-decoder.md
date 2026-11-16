# Decoderprüfung für KrakenSDR-Demo

Stand: 2026-11-12. Nur Quellenprüfung und vorhandene Tests; keine Decoderquellen verändert, keine Pakete installiert.

Referenzen:

- SimpleSDR Commit `ae4434f9f942f2194a4c10c64cfca434f2726daf`.
- BFT Commit `4a670550cbb82a48a0e244c3bfeb102feac510cb`.
- Keine AGENTS.md in `/workspace` gefunden.

## Bestätigte Funktionen

**SimpleSDR hat derzeit keinen POCSAG-Decoder.** Die Registrierung enthält RTTY, AX.25-AFSK, AX.25-FSK, LoRa und optional LoRa-grlora (`SimpleSDR/backend/main.py:112`). Suche im Anwendungscode ohne eingebuchtes venv findet keine POCSAG-Implementierung. Ein passender eigenständiger Offline-Decoder existiert dagegen in **BFT/tools/pocsag_rx.py**: quadrature FM demod, Resampling, Takt-/Polaritätssuche, BCH und alphanumerische Nachricht (`:153`, `:179`, `:215`). Dieser ist noch kein Streaming-Decoder und gibt nur den besten Nachrichtentext zurück (`:243`). Mehrere Pakete pro Aufnahme werden nicht als getrennte Ereignisse mit Samplegrenzen ausgegeben. `_correct` korrigiert nur einzelne Bitfehler und prüft den BCH-Syndromteil, nicht die gesamte POCSAG-Parität (`:53`). `decode_bits` kann Nutzdaten über weitere Adresscodewörter hinweg sammeln (`:114` bis `:149`); für allgemeine Mehrsender-/Mehrnachrichtenverarbeitung muss die Framinglogik überarbeitet werden. Für die Demo mit einem Burst pro Kanal ist ein isolierter Burst als Decoderinput sinnvoll.

**LoRa hat zwei Wege:**

1. `SimpleSDR/backend/decoders/lora.py`: numpy/scipy, optional pyfftw; Preamble-Detektor plus `lora_phy.py` für eigenen PHY. Unterstützte Suchwerte standardmäßig BW 125/250/500 kHz und SF7–12 (`lora.py:65`), konfigurierbar. Bekannte feste BFT-Einstellungen statt Sweep verwenden.
2. `lora_grlora.py`: native GNU-Radio-/PMT-/gr-lora_sdr-Abhängigkeit (`:32`), dauerhafter Flowgraph (`:212`). Ohne GNU Radio bleibt der Decoder inaktiv (`:332`). Windows-Ausführung ist damit nicht belegt; Linux als DSP-Host und Windows als Browserclient bleibt der verlässlichere Integrationsweg. Änderungen an SF/BW/CR/Syncword bauen den Flowgraph neu (`:290`, `:398`). Die Quelle enthält einen dokumentierten Workaround für Segfaults bei unvollständigen Symbolblöcken (`:59`, `:220`); für eine stabile Demo einen separaten Decoderprozess erwägen.

BFT-Vorgaben aus tatsächlichem Quellcode: **SF9, BW 125 kHz, CR 4/5, privates Syncword 0x12** (`BFT/src/LoraModem.h:11`). `begin` setzt **8 Präambelsymbole** (`LoraModem.cpp:12`). RadioLib-CR `5` und gr-lora_sdr-CR `1` meinen beide 4/5; nicht ungeprüft denselben Zahlenwert übernehmen. Der gr-Wrapper setzt expliziten Header und CRC standardmäßig voraus (`SimpleSDR/backend/decoders/lora_grlora.py:258`). CRC-/Headerdefaults des verwendeten BFT-RadioLib-Builds durch echtes Paket verifizieren.

## Kritische Lücken für Paket ↔ Peilung

- DecoderBase verarbeitet einen **einkanaligen complex64-IQ-Vektor** (`SimpleSDR/backend/decoders/__init__.py:56`); kein fünfkanaliges Array und keine Sampleindexmetadaten.
- `DecodedMessage.timestamp` ist ausdrücklich **Dekodierzeit**, nicht RF-Empfangszeit (`__init__.py:21`). Eigener LoRa-Weg setzt `datetime.now()` (`lora.py:533`), gr-Weg ebenso beim Leeren der Ergebnisqueue (`lora_grlora.py:352`). Die Callbackzeit ist daher keine ausreichende Zuordnung zu einem RF-Fenster.
- Eigener LoRa-Decoder findet intern SFD/Framepositionen (`lora.py:321`, `:354`), gibt diese aber nicht aus. `DemodResult` enthält keine Start-/Endindizes (`lora_phy.py:400`). Die globale Rolling-Buffer-SFD-Suche sucht außerdem das stärkste passende Ereignis im ganzen Puffer, nicht garantiert den gerade detektierten Burst.
- gr-MessageBridge extrahiert bei `(meta . u8vector)` nur den Payload und verwirft `meta` (`lora_grlora.py:182`). Etwaige Zeit-/Qualitätsinformationen gehen dabei verloren. Welche Samplepositionstags upstream gr-lora_sdr liefern kann, ist noch an der einzusetzenden Version zu prüfen.
- LoRa-`DemodResult.ok=True` kann trotz `crc_ok=False` entstehen (`lora_phy.py:615`, `:624`). Ein erfolgreicher Header allein darf deshalb keine Rufzeichen-/Positionsreferenz für RMS legitimieren. gr-Wrapper meldet pauschal `confidence=1.0` ohne explizites CRC-Ergebnis (`lora_grlora.py:366`).
- Decoderqueues werfen bei Überlauf alte Chunks weg (`main.py:555`, gr-Quelle `lora_grlora.py:99`), ohne Samplelücken als eigene Streamereignisse weiterzureichen. Damit kann der Decoder nicht zwischen tatsächlicher Kontinuität und zusammengeschnittenen Abschnitten unterscheiden. In der neuen Pipeline: Lücke melden, Synchronisation/Decoderzustand zurücksetzen, betroffenen Burst als ungültig markieren.
- Bestehende Frequenzverschiebung startet die Mischeroszillatorphase in jedem Chunk bei null und nutzt blockweises `filtfilt` (`main.py:245` bis `:271`). Für die neue Pipeline besser kontinuierlichen NCO und zustandsbehaftete Filter verwenden. Für DF müssen alle fünf Antennen denselben Mixer, Filter und Dezimationszeitpunkt verwenden.

## Empfohlene Schnittstelle

Die zentrale Aufnahme besitzt `stream_epoch`, absoluten `sample_start`, Samplezahl, Samplefrequenz, Mittenfrequenz, gültigen Kohärenzstatus und Konfigurations-ID. Der Block ist `(5, N)` complex64. Alle fünf Antennen werden gemeinsam und identisch pro RF-Kanal kanalisiert.

Jeder RF-Kanal erhält eine eigene Burst-/Decoderinstanz; bekannte SF9/BW125k statt Blindscan. Ein Kanal oder ein festgelegter Diversitypfad kann die Nutzdaten decodieren; die DF nutzt weiterhin alle fünf kohärenten Kanäle im **selben absoluten Samplefenster**. Phasenunbekannte Summierung der Antennen vermeiden.

Ein `PacketObservation` enthält `burst_id`, `stream_epoch`, `sample_start/end`, Frequenz, Modulation, Payloadbytes, CRC/BCH-/Parserqualität, Rufzeichen, Positions-UTC und Empfangszeit. Decoderlatenz bleibt ein separates Diagnosefeld. Erst nach validierter Nutzlast wird das Rufzeichen am Burst und seiner Peilung ergänzt. Bei fehlgeschlagener Dekodierung bleibt eine Peilung unzugeordnet; keine Zuordnung über das zuletzt gesehene Rufzeichen. Ungültige/fehlende Positionen erlauben kein Referenz-RMS.

Speichern und Replay müssen kohärente Mehrkanaldaten, Lücken und Konfigurationswechsel erhalten. Replay verwendet die ursprünglichen RF-Samplezeiten; dadurch ist es unabhängig von Rechnergeschwindigkeit und liefert dieselbe Paket-/DF-Zuordnung.

## Tatsächlich ausgeführte Prüfungen

Umgebung: vorhandenes System-Python, numpy 2.3.5, scipy 1.17.0. GNU Radio und PMT sind nicht installiert; gr-lora_sdr konnte hier nicht ausgeführt werden.

- `python BFT/tools/pocsag_rx.py --selftest`: **OK**, erzeugtes synthetisches FSK-Signal wird als `DL7CR JO31AB` decodiert. Kein HF-/Kraken-Nachweis.
- Komponenten und `test_full_packet` aus `_lora_phy_test.py` direkt ausgeführt: **13/13 synthetische PHY-Roundtrips bestehen**, einschließlich CFO ±1500 Hz. Auch dies belegt interne Konsistenz von synthetischem Sender und Empfänger, keine RadioLib-Kompatibilität.
- Synchrone Helfer des LoRa-Streamingdecoders mit dem vorhandenen synthetischen End-to-End-Testsignal: SF7/BW125k detektiert, Payload `END-TO-END`, CRC **True**.
- Der vollständige vorhandene `asyncio.run`-End-to-End-Test kam hier nicht zum Abschluss. Nach begrenztem Diagnoseversuch beendet; Stack zeigte wartende asyncio-Auswahl und einen inaktiven Executorworker. **Keine Ursache bewiesen**, daher weder ein bestätigter Decoderfehler noch bestandener Gesamttest.
- Reale Repository-Datei `SimpleSDR/data/lora_capture.npy` **read-only** per memmap geladen: **40.000.000 einkanalige complex64-Samples, 16 s, 2,5 MS/s**, Metadaten **868,3 MHz, SF9, BW125k**. Das ist eine frühere Pluto-/868-MHz-Aufnahme, keine Aufnahme der Nutzer-Kraken-Hardware bei 433 MHz.
- Leistungsbasierte Suche findet einen deutlichen Burst ungefähr **12,10–12,32 s**. Im Ausschnitt 11,95–12,47 s liefert vorhandene SFD-Suche `sfd_pos=488248` relativ zum Ausschnitt (absolut 12,1452992 s). Bei den vorhandenen nominalen Startoffsets **2,25 / 2,50 / 2,00 Symbole scheitert der eigene PHY jeweils am Header-Checksum**. Damit sind weder Rufzeichen noch Positionspayload noch CRC für diese Aufnahme bestätigt.
- Die mitgelieferte `data/lora_analysis.txt` beschreibt eine andere ältere **1-s-/2,5-Mio-Samples-Aufnahme** und darf nicht als Testergebnis für die aktuelle 16-s-Datei gelten.

## Wiederverwendung

SimpleSDR besitzt im README nur den Lizenzplatzhalter `[Add license information]` (`README.md:157`); im untersuchten Anwendungscode wurde keine projektweite Lizenz gefunden. Vor Weitergabe einer Ableitung die konkrete Berechtigung/Lizenz klären; die Quellenprüfung selbst und eine eigenständige Schnittstellenspezifikation sind davon unabhängig. Drittanbieter-Lizenzen aus dem eingebuchten venv sind keine Lizenz des Projekts. Exakte Version und Lizenz eines übernommenen gr-lora_sdr-Pakets ebenfalls dokumentieren.

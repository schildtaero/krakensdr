# Verarbeitung, SNR und Fehlerdiagnose ab 0.2.2

## Warum 2,4 MS/s bisher überlasten konnte

Fünf kohärente Antennen mit 2,4 MS/s ergeben 12 Millionen komplexe Samples pro
Sekunde. Der bisherige Kanalpfad erzeugte pro RF-Kanal große komplex128-Arrays
für Mischung und Filterung, bevor er die meisten Samples bei der Dezimierung
verwarf. Bei der Standardkonfiguration mit POCSAG und LoRa konnte diese Arbeit
bereits ohne Paketdecodierung länger als die verfügbare RF-Zeit dauern.
Zusätzlich wartete die RF-Verarbeitung auf die Decodierung jedes fertigen
Bursts. Eine langsamere Abtastrate reduziert diese Last, muss aber weiterhin
alle aktiven Frequenzen einschließlich Kanalbandbreiten und Filterrändern
enthalten.

Die neue Umsetzung:

- Ein optionaler Numba-Kern verbindet Mischung, SOS-Filter und Dezimierung und
  schreibt nur die benötigten Ausgangssamples. Er behält float64-Koeffizienten,
  complex128-Filterzustände, den gemeinsamen Oszillator und die Samplezuordnung.
  Es gibt kein Fast-Math und keine unabhängige Zeitverschiebung der Antennen.
- Numba ist bereits Bestandteil der verwalteten DAQ-Installation. Kompilierung
  und Vorbereitung erfolgen vor dem Empfang, ohne Schreibzugriff auf einen
  JIT-Diskcache. Ohne Numba bleibt SciPy nutzbar; auch dessen Oszillator wird
  für gleich große Frames wiederverwendet.
- Ein separater asynchroner Worker decodiert fertige Pakete und berechnet
  deren MUSIC-Peilung. Die laufende RF-Verarbeitung muss darauf nicht warten.
  Die Grenzen bleiben drei wartende RF-Frames und acht fertige Bursts.
- Echte Überlast wird weiter gemeldet. Es erfolgt kein stilles Weglassen von
  Frames und kein Zusammenfügen unzusammenhängender IQ-Fenster. Paketaufträge
  behalten ihre Lauf-/Aufnahmezuordnung auch über Aufnahmewechsel hinweg.

## Reproduzierbarer Softwarevergleich

Gemessen am 16. November 2026 auf dem Entwicklungsrechner mit NumPy/SciPy und
Numba: zwei RF-Kanäle bei 2,4 MS/s, fünf Antennen, 1.048.576 Samples pro Frame,
zwei Aufwärmframes und acht gemessene Frames. Pro Frame liegen 436,9 ms RF-Zeit
vor. Die Werte sind Mediane eines kontrollierten Vergleichslaufs:

| Kanalverarbeitung mit Spektrum und Burst-Erkennung | Zeit je Frame | Verarbeitung / RF-Zeit |
|---|---:|---:|
| Ursprünglicher vollständiger Mix-/SciPy-Filterpfad | 471,7 ms | 108 % |
| SciPy mit wiederverwendetem Oszillator | 414,7 ms | 95 % |
| Zusammengefasster Numba-Pfad | 259,4 ms | 59 % |

Der neue beschleunigte Pfad erreicht hier etwa **1,82-fachen Durchsatz**. Die
einmalige Vorbereitung wurde separat mit rund 0,93 s gemessen. Eine reine
App-/Remote-Installation ohne Numba kann weniger Reserve haben.

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python scripts/benchmark-pipeline.py --frames 8 --warmup 2
```

Das Skript verwendet denselben deterministischen Fünfkanal-Rauschstrom für
alle Varianten und enthält eine eigene Referenz des früheren Kanalfilters.
Gemessen werden Mischung, Filterung, Dezimierung, Spektrum und Burst-Erkennung.
**USB/Heimdall, Nutzlastdecodierung, MUSIC, Aufnahme und Browserdarstellung
sind nicht Teil dieses Benchmarks.** Er ist weder ein Hardwaretest noch eine
allgemeine Echtzeitzusage. Last durch echte Signale und andere Prozesse kann
abweichen. Die End-to-End-Tests mit synthetischem gleichzeitigem POCSAG-/LoRa-
Empfang bei 2,4 MS/s prüfen separat Decodierung, Peilung und Samplegrenzen.

## Status auf dem Zielrechner bewerten

Im Webinterface **Status → Verarbeitung** öffnen:

- **Pipeline / RF-Zeit**: geglättetes Verhältnis der Pipelinezeit zur RF-Dauer.
  Dauerhaft über 100 % bedeutet Rückstau dieser Stufe. Auch unter 100 % können
  Decoder-, USB- oder andere Lastspitzen Probleme verursachen. Der Wert ist
  keine Gesamt-CPU-Auslastung.
- **RF-Warteschlange**: wartende Frames und Wartezeit des zuletzt entnommenen
  Frames. Ein anhaltender Anstieg bedeutet zunehmende Latenz.
- **Letzter Burst / Überläufe**: Zeit für Decodierung, MUSIC und gegebenenfalls
  Aufnahme des letzten Bursts sowie gezählte Queueüberläufe.
- **Burst-Warteschlange**: wartende Paketausschnitte und Zahl bearbeiteter
  Bursts. Bearbeitung bedeutet nicht zwangsläufig erfolgreiches Decodieren.

Bei weiterer Überlast zuerst Diagnose-ZIP sichern und den dort dokumentierten
DSP-Pfad prüfen. Danach nicht benötigte RF-Kanäle deaktivieren und die Rate nur
so weit reduzieren, wie das gemeinsame Empfangsfenster erlaubt. Die
FFT-Länge beeinflusst vor allem Anzeigeaufwand und Spektrumsauflösung; sie ist
kein Ersatz für eine ausreichend schnelle Kanalverarbeitung. WSL und andere
gleichzeitig laufende Programme teilen sich die verfügbaren CPU-/Speicherressourcen.

## Was das SNR-Feld bedeutet

Das Tracking zeigt pro Paket einen **unkalibrierten Schätzwert im konfigurierten
Empfangskanal**. Der Schätzer mittelt die Leistungen aller fünf Antennen
nichtkohärent. Die Paketleistung enthält Signal und Hintergrund; der
Hintergrund wird aus ruhigen Bereichen außerhalb der erkannten Aktivität und
der Decoder-Paketgrenzen geschätzt:

`SNR_dB = 10 * log10((P_Paket − P_Rauschen) / P_Rauschen)`

Es werden 2 ms Abstand zu den Aktivitätsgrenzen und mindestens 5 ms bzw.
64 Samples je Rauschfenster verlangt. Stark schwankende oder widersprüchliche
Rauschfenster werden verworfen. Ein ungeprüfter historischer Detektor-
Rauschwert wird nicht einfach als sichere Rauschreferenz übernommen.

Negative dB-Werte sind gültig. Ohne passende Referenz oder bei nicht positiv
auflösbarer Signalleistung erscheint **„—“** mit einem gespeicherten
Statusgrund. Das SNR ist kein DF-Eigenwertscore, keine Winkelunsicherheit,
keine kalibrierte dBm-Messung und nicht das nach Dechirping gewonnene LoRa-SNR.
Störungen im Referenzfenster gehen gegebenenfalls in den Hintergrund ein;
Werte verschiedener Kanalbandbreiten sind deshalb nicht unmittelbar gleichzusetzen.

Wert, Verfahren und Qualitätsdetails bleiben in Aufnahme und Replay erhalten.
Alte Aufnahmen ohne SNR zeigen „—“. Excel ergänzt die Spalten **SNR geschätzt
dB**, **SNR-Verfahren** und **SNR-Status**. In der Simulation stammt das SNR
aus dem bekannten künstlichen Signal-/Rauschmodell und ist als
`synthetic_known_noise` gekennzeichnet, nicht als RF-Messung.

## Fehlerprotokoll herunterladen

**Status → Fehlerprotokoll herunterladen** lädt `KrakenDF-Diagnose.zip` von
`GET /api/diagnostics/download`. Enthalten sind:

- persistentes Anwendungslog und rotierte Vorgänger mit Fehler-Tracebacks;
- begrenzte Auszüge der neuesten DAQ-Prozesslogs;
- Empfängereinstellungen, Paket-/Systemversionen, Prozesszustände,
  Beschleunigungspfad und numerische Verarbeitungskennzahlen;
- die letzten sichtbaren Ereignisse und Hinweise zu den Exportgrenzen.

Das Anwendungslog liegt unter `~/.krakendf/logs/application.log` und rotiert
bei 512 KiB mit drei Vorgängern. Der Download übernimmt maximal 256 KiB je
Datei und insgesamt 4 MiB Logdaten. Er ist ein Diagnoseauszug, kein vollständiges
Archiv aller früheren Fehler. Aufnahme-/IQ-Dateien und Karten werden nicht
exportiert. Die Konfigurationszusammenfassung lässt Koordinaten, Empfangstexte,
Symbole und TX-Notizen aus; Dateiauswahl und Logpfade sind serverseitig festgelegt.

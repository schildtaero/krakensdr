# Passivradar mit KrakenSDR: DAB+ und UKW

Softwarestand **0.3.0**. Der zusätzliche Modus vergleicht zwei kohärente Empfangskanäle des vorhandenen KrakenSDR: das direkte Rundfunksignal und dessen Reflexionen. Das Ergebnis ist eine Verzögerungs-Doppler-Karte mit einer Liste auffälliger Spitzen. Live-Empfang, ausdrücklich gewählte Simulation, IQ-Aufzeichnung, erneute Auswertung beim Replay und Excel-Export stehen zur Verfügung.

Die Verarbeitung wird mit synthetischen IQ-Signalen geprüft. Eine Abnahme mit realem KrakenSDR, Rundfunksender und Flugzeugreflexionen steht aus. Die Software liefert keine zugesicherte Reichweite oder Detektionswahrscheinlichkeit. Das vorhandene [PDF-Handbuch](KrakenDF-Funktion-und-Bedienung.pdf) beschreibt weiterhin den DF-Stand **0.2.3**; dieses Dokument ergänzt den neuen Radarmodus.

## Geeigneter erster Versuch

**Größere Flugzeuge sind ein sinnvoller erster Zieltyp.** Sie bieten im Allgemeinen einen günstigeren Einstieg als kleine Drohnen. Ein freier Blick in einen regelmäßig durchflogenen Himmelssektor erleichtert den Versuch. Eine Anzeige allein identifiziert ein Flugzeug nicht: Mehrwegeausbreitung, andere bewegte Objekte und Störungen können ebenfalls Spitzen erzeugen. Eine unabhängige Beobachtung hilft beim Zuordnen.

Für den Anfang ist **DAB+** interessant, weil die größere Signalbandbreite feinere Unterschiede im zusätzlichen Signalweg auflösen kann. **UKW/FM** ist ebenfalls auswählbar und kann durch einen starken örtlichen Sender gut verfügbar sein; seine nutzbare Bandbreite und damit die Laufzeitauflösung hängen jedoch vom tatsächlichen Programm und der Modulation ab.

Der Modus empfängt die Rundfunkwellenform direkt als IQ-Signal. Er benötigt weder eine Audiodecodierung noch die Decodierung von DAB+-Programminhalten. DAB+ und UKW werden nacheinander auf der jeweils gewählten Frequenz untersucht.

## Antennen und Anschlüsse

Mindestens zwei verschiedene Kraken-Eingänge werden benötigt:

| Funktion | Empfohlener Anfang | Aufgabe |
|---|---|---|
| Referenz | CH0, passende Richtantenne zum Rundfunksender | Das direkte Sendersignal möglichst sauber empfangen |
| Beobachtung | CH1, passende Antenne zum Himmelssektor | Reflexionen empfangen; den direkten Sender möglichst schwach aufnehmen |

Beide Kanäle können im UI aus **CH0 bis CH4** gewählt werden. Referenz und Beobachtung müssen verschieden sein. In dieser Ausbaustufe wird jeweils **ein** Beobachtungskanal ausgewertet; es entsteht keine Radar-Azimutpeilung aus den übrigen Kanälen.

Für UKW werden zum Bereich **88–108 MHz** passende Antennen benötigt, für DAB+ im üblichen VHF-Band III Antennen für **174–240 MHz**. Die vorhandenen kurzen 433-MHz-Magnetfußantennen und das kompakte DF-Array sind dafür kein unverändert übernehmbarer Radaraufbau. Eine passende Yagi oder logarithmisch-periodische Antenne erleichtert die Trennung von direktem Signal und Beobachtungssektor. Antennenpolarisation auf den gewählten Rundfunksender abstimmen.

Die Beobachtungsantenne so ausrichten und aufstellen, dass sie den Referenzsender möglichst unterdrückt. Das verbessert die Bedingungen bereits vor dem Empfänger: Software kann ein übersteuertes Signal nicht wiederherstellen. Gain zunächst mäßig einstellen und auf gültige, nicht übersteuerte Frames achten. Fest verlegte, möglichst vergleichbare Kabel und unbewegte Antennen helfen, Messungen vergleichbar zu halten. Unterschiedliche Kabel- und Antennenlaufzeiten können einen Offset erzeugen; die interne Kraken-Kohärenzkalibrierung vermisst diese äußeren Signalwege nicht.

Der Kraken bleibt mit seinen fünf Tunern kohärent in Betrieb. Der Radarmodus wählt daraus das Paar für die Auswertung. **DF und Radar teilen sich den Empfänger und laufen deshalb nacheinander.** Ein Radarstart beendet den DF-Empfang; laufende Aufnahmen müssen vorher abgeschlossen werden. Das bloße Öffnen des Radar-Tabs startet keinen zweiten Empfänger.

## Start ohne Hardware

1. Anwendung ohne lokalen DAQ starten, zum Beispiel `./start.sh --source idle --daq-mode external` oder in einer vorhandenen Installation eine bereits laufende DF-Quelle auf **Standby** setzen. Eine für Simulation eingerichtete Installation kann ebenfalls verwendet werden.
2. In der linken Navigation **Passivradar** öffnen.
3. **DAB+ Beispiel** oder **UKW Beispiel** auswählen und die Einstellungen übernehmen. Diese Presets demonstrieren Parameter; sie suchen keinen örtlichen Sender.
4. Unter Quelle ausdrücklich **Simulation** wählen und **Radar starten** drücken.
5. Nach vollständigen Auswertefenstern erscheinen eine Karte und synthetische Reflexionskandidaten. Die Simulation erzeugt verzögerte und Doppler-verschobene kohärente IQ-Signale und führt sie durch die Radarauswertung. Die Anzeige wird als Simulation gekennzeichnet.
6. Eine kurze Radaraufnahme starten, beenden und anschließend als **Replay** erneut auswerten. Danach **Excel exportieren** wählen.

Es gibt keinen automatischen Wechsel von fehlgeschlagenem Live-Empfang zu Simulation. Die ursprüngliche DF-Simulation erzeugt BFT-Emitter; die Radar-Simulation wird im eigenen Radarbereich gestartet.

## Erster Live-Versuch

1. Kraken wie in [Erstinstallation](setup.md) bzw. [Windows/WSL2](windows-setup.md) beschrieben anschließen. Unter Windows müssen alle fünf Kraken-Tuner an WSL durchgereicht sein. Eine GPS-Maus ist für die reine Verzögerungs-Doppler-Auswertung nicht erforderlich.
2. Einen örtlich empfangbaren Rundfunksender auswählen und passende Referenz-/Beobachtungsantennen anschließen. Zunächst einen dominanten, möglichst sauberen Referenzsender verwenden.
3. Im Radarbereich Beleuchter, **tatsächliche Senderfrequenz**, Antennenkanäle, Bandbreite, Abtastrate und diskreten Gain einstellen. **220,352 MHz** für DAB+ und **100,000 MHz** für UKW sind ausschließlich Beispielfrequenzen, keine standortbezogene Empfehlung.
4. Einstellungen übernehmen, **Live · KrakenSDR** wählen und **Radar starten** drücken. Der Radarstart übernimmt den zuvor gewählten lokalen oder externen DAQ-Weg. Ein externer DAQ muss zu Frequenz, Abtastrate und Gain passen; seine Istwerte werden geprüft.
5. Start und interne Synchronisation abwarten. Nur gültige kohärente Daten fließen in die Auswertung ein. Kalibrierframes, verlorene Locks oder Datenlücken verwerfen ein unvollständiges Auswertefenster; ein neues Fenster beginnt mit zusammenhängenden Daten.
6. Referenzpegel, Beobachtungspegel, Qualitätsanzeige und Rechenzeit prüfen. Bei Übersteuerung Gain verringern. Ein sauberer Referenzpegel allein beweist noch keine empfangbare Zielreflexion.
7. Einige kurze Aufnahmen mit unverändertem Aufbau machen. Danach Antennenrichtung oder Beobachtungssektor gezielt verändern und vergleichen. Anhaltende Spitzen bei nahezu null Doppler können verbliebener Direktempfang oder stationäre Reflexionen sein.

Zurück zur BFT-Peilung: Radaraufnahme beenden, Radar **stoppen**, **Lagebild** öffnen und dort die DF-Datenquelle wieder starten. Das separate Radarprofil überschreibt weder BFT-Kanalfrequenzen noch Antennengeometrie und Heading.

## Parameter verstehen

| Einstellung | Wirkung und Abwägung |
|---|---|
| Beleuchter / Frequenz | Ein DAB+-Block oder ein UKW-Sender innerhalb des gemeinsamen Kraken-Empfangsfensters |
| Abtastrate | Für einen vollständigen DAB+-Block mindestens **2,048 MS/s**, anfänglich **2,4 MS/s**; UKW erlaubt geringere Raten, sofern Signal und Filterränder hineinpassen |
| Signalbandbreite | Nominal **1 536 kHz** bei DAB+ bzw. beispielsweise **200 kHz** bei UKW; bestimmt Filterung und die angezeigte theoretische Laufzeitauflösung |
| CPI / Integrationszeit | Längere zusammenhängende Beobachtung verbessert die nominale Dopplerauflösung; Bewegung während des Fensters und Rechnerlast begrenzen den Nutzen |
| Verzögerungszellen | Zahl der untersuchten positiven Verzögerungen; mehr Zellen erweitern den Zusatzwegbereich und erhöhen den Rechenaufwand |
| Dopplerbereich | Untersuchte positive und negative Frequenzverschiebungen; zu kleiner Bereich blendet mögliche Reflexionen aus |
| Direktsignal-Unterdrückung / Taps | Schätzt aus verzögerten Referenzkopien den direkten und kurz verzögerten stationären Anteil im Beobachtungssignal und zieht ihn ab |
| Detektionsschwelle | Mindestkontrast einer lokalen Spitze gegenüber dem geschätzten Hintergrund; kein kalibrierter SNR-Wert und keine garantierte Fehlalarmrate |

Die beiden Antennenkanäle werden gleich gefiltert und bei schmaleren Signalen gemeinsam dezimiert. Die Kreuzkorrelation mit nachfolgender Dopplerauswertung berechnet die Karte. Angezeigt werden die tatsächlichen Zellenabstände und die tatsächliche Integrationsdauer. Eine feinere Darstellung durch FFT-Stützstellen schafft keine zusätzliche physikalische Auflösung.

Die Unterdrückung des Direktsignals kann auch stationäre oder sehr langsam veränderliche Zielreflexionen abschwächen. Ein Vergleich mit ausgeschalteter Unterdrückung kann den Effekt zeigen; dafür Radar vorher stoppen. Einstellungen lassen sich nur im gestoppten Radarmodus ändern.

## Was die Karte misst

### Zusätzlicher Signalweg statt direkter Zielentfernung

Seien `T` der Rundfunksender, `Z` ein reflektierendes Ziel und `R` der Empfänger. Gemessen wird die Verzögerung gegenüber dem direkten Sendersignal:

```text
τ = (|T−Z| + |Z−R| − |T−R|) / c
Zusatzweg = c × τ
```

Dabei ist `c = 299 792 458 m/s`. Es gibt hier **keine Division durch zwei**. Beispielsweise entsprechen 10 µs Verzögerung rund **3,0 km zusätzlichem Signalweg**. Diese Zahl ist weder die Entfernung des Ziels vom Kraken noch dessen Flugstrecke.

Ein gleicher Zusatzweg beschreibt bei bekannten Sender-/Empfängerstandorten eine Ellipse möglicher Orte, räumlich ein Ellipsoid. Ohne zusätzliche unabhängige Messung lässt sich daraus kein eindeutiger Zielort bestimmen. Diese Ausbaustufe zeigt deshalb keine aus Radarspitzen erfundenen Flugzeugpositionen auf der BFT-Karte. Stations-GPS und Array-Heading ändern daran nichts.

### Auflösung und Zellenabstand

Die Größen sind verschieden:

| Größe | Näherung | Beispiel |
|---|---|---|
| Auflösung im zusätzlichen Signalweg | `c / B` mit nutzbarer Signalbandbreite `B` | DAB+ bei 1,536 MHz: etwa **195 m**; FM bei tatsächlich nutzbaren 200 kHz: etwa **1,5 km** |
| Zellenabstand im zusätzlichen Signalweg | `c / f_s` mit der Auswerte-Abtastrate nach gemeinsamer Dezimierung | Ohne Dezimierung bei 2,4 MS/s: etwa **125 m** |
| Nominale Dopplerauflösung | `1 / CPI` | 0,25 s: etwa **4 Hz**; 0,5 s: etwa **2 Hz** |

Dies sind Größenordnungen der Auflösung, keine zugesicherten Messfehler. Fensterung, Signalstruktur, SNR, Mehrwegeausbreitung und Kalibrierung beeinflussen das Ergebnis. Die verwendete Hann-Fensterung verbreitert Dopplerspitzen; ihre erste Nullstelle liegt bei ungefähr `2/CPI` Abstand vom Spitzenzentrum. Gerade bei UKW kann die tatsächlich angeregte Bandbreite erheblich unter der eingestellten Filterbreite liegen. Die Software schätzt aus dem eingestellten Profil `c/B`; sie misst damit keine garantierte Senderbandbreite.

Bei 128 Zellen und 2,4 MS/s ohne Dezimierung reicht die positive Verzögerungsachse von null bis ungefähr **15,9 km zusätzlichem Signalweg**. 512 Zellen ergeben unter denselben Bedingungen etwa **63,8 km**. Nach Dezimierung ändern sich Zellenabstand und Achsenende entsprechend. Das ist der Auswertebereich, keine Reichweitenzusage.

### Doppler statt Fluggeschwindigkeit

Für einen unbewegten Sender und Empfänger hängt Doppler von der Änderung des gesamten reflektierten Wegs ab:

```text
Wellenlänge λ = c / Sendefrequenz
d(|T−Z| + |Z−R|) / dt = −λ × f_D
```

Positive Dopplerverschiebung entspricht bei dieser Konvention einem kürzer werdenden Gesamtweg. Diese Wegänderungsrate ist geometrieabhängig und **keine direkte Fluggeschwindigkeit**. Die Beziehung eines monostatischen Radars mit dem Faktor zwei wird hier nicht verwendet. In ungünstiger bistatischer Geometrie kann auch ein schnelles Flugzeug wenig Doppler erzeugen.

### Farben und Kandidaten

Die Farbskala wird relativ zum Maximum des jeweiligen Auswertefensters dargestellt. **0 dB ist dessen stärkste Zelle**, kein absoluter Empfangspegel. Werden zwei Karten jeweils auf ihr eigenes Maximum normiert, bedeuten gleiche Farben nicht dieselbe Reflexionsleistung.

Die Kandidatenliste enthält lokale Spitzen mit Verzögerung, zusätzlichem Signalweg, Doppler und Kontrast zum Hintergrund. Dieser Kontrast ist **kein thermisch kalibriertes SNR** und nicht mit dem BFT-Paket-SNR gleichzusetzen. Eine Schwellenüberschreitung ist weder eine Identifikation noch bereits ein über mehrere Fenster bestätigter Track.

Die Kandidatensuche lässt standardmäßig die ersten **zwei Verzögerungszellen** und Doppler mit **Betrag unter 8 Hz** aus, um Reste des Direktsignals nicht als Ziele zu melden. Die Karte zeigt diese Bereiche weiterhin. Zusammen mit der Direktsignal-Unterdrückung bedeutet das: Gerade kleine Verzögerungen und nahezu null Doppler sind keine verlässlich detektierten Bereiche.

## Aufnahme, Replay und Excel

Radaraufnahmen sind von BFT-Sitzungen getrennt. Sie liegen standardmäßig unter `~/.krakendf/radar-sessions`; das Radarprofil liegt in `radar-config.json`. Ein anderes Anwendungsdatenverzeichnis gilt auch hier.

Eine Aufnahme speichert die ausgewählten **zwei kanalisierten kohärenten IQ-Kanäle**, einschließlich der für positive Verzögerungen nötigen Vorgeschichte, der tatsächlichen Abtastrate, Samplegrenzen, Qualitäts-/Lückenangaben, des Profils und der Auswerteergebnisse. Es ist keine freie spätere Auswertung aller fünf Breitbandkanäle und kein Empfang nachträglich ausgewählter Sender möglich.

Beim Replay wird die Karte aus diesen gespeicherten IQ-Fenstern mit dem aufgezeichneten Profil erneut berechnet. Die Simulation bleibt auch im Replay als synthetische Quelle gekennzeichnet. Replay benötigt keinen Kraken. Die Abspielgeschwindigkeit steuert die zeitliche Wiedergabe; sie erzeugt keine andere physikalische Dopplermessung.

**Pro Aufnahme gelten 4 GiB Speicherlimit; zusätzlich bleiben mindestens 256 MiB frei.** Wird das Limit vor dem nächsten Fenster erreicht, beendet die Software die Aufnahme und lässt die laufende Radarauswertung weiterarbeiten. Sie löscht keine älteren Aufnahmen automatisch. Bei zwei komplexen 64-Bit-Kanälen und 2,4 MS/s fallen ohne Dezimierung etwa **38,4 MB/s IQ-Daten** an, zusätzlich zu Ergebnisdateien und Metadaten. Das 4-GiB-Limit reicht dann größenordnungsmäßig für weniger als zwei Minuten. Im UI werden die aktuelle Datenmenge und das Limit angezeigt.

**Excel exportieren** bezieht sich auf die ausgewählte Radaraufnahme. Exportiert werden unter anderem Kandidaten mit Verzögerung, Zusatzweg, Doppler und Kontrast sowie Einstellungen und Datenherkunft. Die Tabelle enthält keine aus diesen Werten abgeleiteten fiktiven Flugzeugkoordinaten oder Fluggeschwindigkeiten.

## Qualität und Leistungsgrenzen

- Ein starkes direktes Signal im Beobachtungskanal begrenzt die Dynamik. Erst Antennenaufstellung und Gain verbessern, dann die Unterdrückung vergleichen.
- DAB-Gleichwellennetze können mehrere direkte Signalwege zum Empfänger liefern. Auch unbewegte Gebäude und Gelände erzeugen Mehrwegeanteile; deren Spitzen sind keine automatisch erkannten Flugzeuge.
- Für schnelle Flugzeuge kann der voreingestellte Dopplerbereich von ±200 Hz zu klein sein. Bei DAB ist beispielsweise ein Vergleich mit ±500 Hz sinnvoll; ein größerer Bereich erhöht den Rechenaufwand. Auch ausgeprägte Korrelationsnebenkeulen können zusätzliche Kandidaten liefern.
- Kalibrier-/Dummyframes, verlorene Kohärenz, Übersteuerung und Samplelücken dürfen nicht als durchgängige CPI zusammengesetzt werden. Nach einer Unterbrechung sind ausreichend neue, zusammenhängende Daten nötig.
- Rechnerlast hängt unter anderem von Abtastrate, Verzögerungsbereich, Dopplerbereich und Taps ab. Weniger Verzögerungszellen oder Taps reduzieren Aufwand. Bei UKW ist eine geringere passende Abtastrate möglich; DAB+ darf dabei nicht abgeschnitten werden.
- Rechenzeit im Verhältnis zur RF-Dauer zeigt, ob der Host hinterherkommt. Begrenzte Warteschlangen verhindern unbegrenztes Anwachsen; Überlast und Datenlücken werden sichtbar gemeldet. Ein Softwarebenchmark ersetzt keinen Test des eigenen Hosts und USB-/WSL-Wegs.
- Das Verfahren liefert in dieser Stufe Karten und einzelne Detektionskandidaten. Automatische Flugzeugidentifikation, ADS-B-Abgleich, mehrkanalige Radarpeilung, geografische Zielortung und bestätigte Zieltracks sind nicht enthalten.

Fehler können über **Status → Fehlerprotokoll herunterladen** untersucht werden. Die Installations- und USB-Fehlersuche bleibt in den vorhandenen [DAQ-](managed-daq.md) und [Windows-Anleitungen](windows-setup.md) beschrieben.

## Quellen und Einordnung

- [ITU-R BS.2214-6, Abschnitt 7.4.3](https://www.itu.int/dms_pub/itu-r/opb/rep/R-REP-BS.2214-6-2022-PDF-E.pdf): DAB-Blockraster und nominelle Bandbreite von 1,536 MHz. Die Auflösungswerte oben sind daraus mit `c/B` berechnete Näherungen.
- [ETSI EN 300 401](https://www.etsi.org/deliver/etsi_en/300400_300499/300401/01.03.03_60/en_300401v010303p.pdf): technische Grundlagen der DAB-Wellenform; die Audiocodierung von DAB+ wird für die IQ-Korrelation nicht benötigt.
- [Stephen Cass, „Build a Passive Radar With Software-Defined Radio“, IEEE Spectrum](https://spectrum.ieee.org/passive-radar-with-sdr): praktischer Kraken-Aufbau mit getrennten Richtantennen und Flugzeugen als beobachteten Objekten. Dieser fremde Aufbau ist kein Nachweis für die hier implementierte Software oder eine bestimmte Reichweite am eigenen Standort.

Die hier beschriebene Radarauswertung ist ein eigener zusätzlicher Verarbeitungspfad in KrakenDF. Zur Laufzeit werden weder fremde Beispielprojekte noch eine separat installierte Radaroberfläche benötigt.

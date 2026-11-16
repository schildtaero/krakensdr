# Antennenlayouts und Kalibrierung für die 433-MHz-DF-Demo

Stand: 2026-11-16. Fünf auswählbare Profile; keine an Hardware bestätigte Genauigkeitszusage.

## Konventionen

- 433 MHz: Wellenlänge 0,692361 m, halbe Wellenlänge 0,346181 m.
- Koordinaten im horizontalen Array-System: **x nach rechts**, **y nach vorne**. Vorne ist die markierte 0°-Richtung; positiver Azimut dreht im Uhrzeigersinn nach rechts. Das geografische Heading dieser Markierung wird gesondert gesetzt.
- Kanalnummern **CH0 bis CH4** sind physische Kraken-Eingänge und Bestandteil jedes gespeicherten Profils. Beschriftete Kabel und wiederholbare Positionsmarkierungen sind wichtig. Eine Änderung der Kanalzuordnung ist eine Änderung der Geometrie.
- Antennenkoordinaten beziehen sich auf die Mitte der jeweiligen senkrechten Antenne, alle auf gleicher Höhe. Identische, senkrechte Strahler mit vergleichbarer Umgebung verwenden.

## Ursprüngliche Vergleichsprofile

### 1. Kreis kompakt — Durchmesser 300 mm

Fünf Antennen auf Radius 150 mm; CH0 vorne, dann im Uhrzeigersinn im Abstand von 72°. Abstand benachbarter Antennen 176,34 mm = 0,255 Lambda bei 433 MHz.

| Kanal | x [mm] | y [mm] |
|---|---:|---:|
| CH0 | 0,000 | 150,000 |
| CH1 | 142,658 | 46,353 |
| CH2 | 88,168 | -121,353 |
| CH3 | -88,168 | -121,353 |
| CH4 | -142,658 | 46,353 |

Kompakt und geringer Platzbedarf, aber kleinere Apertur und deshalb bei gleicher Phase-/Rauschqualität geringere Winkelempfindlichkeit. Nah beieinander stehende Strahler können sich stärker gegenseitig beeinflussen. Magnetfußgröße vorab prüfen.

### 2. Kreis groß — Durchmesser 560 mm (empfohlener Ausgangspunkt)

Fünf Antennen auf Radius 280 mm; gleiche Kanalreihenfolge. Abstand benachbarter Antennen 329,16 mm = 0,475 Lambda bei 433 MHz; auch bei 440 MHz noch kleiner als eine halbe Wellenlänge. Der tatsächliche größte Antennenabstand des Fünfecks beträgt etwa 532,59 mm.

| Kanal | x [mm] | y [mm] |
|---|---:|---:|
| CH0 | 0,000 | 280,000 |
| CH1 | 266,296 | 86,525 |
| CH2 | 164,580 | -226,525 |
| CH3 | -164,580 | -226,525 |
| CH4 | -266,296 | 86,525 |

Größere Apertur verbessert die theoretische Winkelempfindlichkeit, kann aber markantere Nebenmaxima haben. Die Software muss die vollständige Fünfkanal-Antwort auswerten. Das Überschreiten von Lambda/2 auf einzelnen langen Baselines beweist allein keine Mehrdeutigkeit des Gesamtarrays; umgekehrt garantiert ein kurzer Nachbarabstand keine störungsfreie reale Messung.

### 3. Quadrat mit Mitte — Seitenlänge 320 mm

CH0 vorne links, CH1 vorne rechts, CH2 hinten rechts, CH3 hinten links, CH4 in der Mitte. Ecke-Mitte-Abstand 226,27 mm, Seitendistanz 320 mm, Diagonale 452,55 mm. Benachbarte Eckantennen liegen bei 433 MHz 0,462 Lambda auseinander.

| Kanal | x [mm] | y [mm] |
|---|---:|---:|
| CH0 | -160,000 | 160,000 |
| CH1 | 160,000 | 160,000 |
| CH2 | 160,000 | -160,000 |
| CH3 | -160,000 | -160,000 |
| CH4 | 0,000 | 0,000 |

Der Algorithmus benötigt hierfür eine explizite Koordinatenliste. Dieses Layout lässt sich nicht korrekt als Kreisarray mit anderem Durchmesser behandeln. Alle drei Vorschläge sind für eine horizontale 360°-Azimutschätzung geeignet, sofern das vollständige Kanalmodell verwendet wird. Ein planares Array kann die Spiegelung der Elevation oberhalb/unterhalb seiner Ebene prinzipiell nicht auflösen; für die geplante horizontale Demo wird eine entsprechende Quellengeometrie angenommen.

## Benannte kompakte Aufbauten

### Pegasus 95 — Kreuz mit Mittelantenne

Profil-ID `pegasus_95`. Vier äußere Antennen liegen jeweils **95 mm von CH4 in der Mitte** entfernt. **CH0 zeigt nach vorne**, danach CH1 rechts, CH2 hinten und CH3 links. In der vom Nutzer bereitgestellten Halterzeichnung ist **links die Vorderseite**; die folgende Tabelle ist bereits in das lokale Array-System (x rechts, y vorne) gedreht.

| Kanal | x [mm] | y [mm] |
|---|---:|---:|
| CH0 | 0,000 | 95,000 |
| CH1 | 95,000 | 0,000 |
| CH2 | 0,000 | -95,000 |
| CH3 | -95,000 | 0,000 |
| CH4 | 0,000 | 0,000 |

Gegenüberliegende äußere Antennen stehen 190 mm auseinander; der Abstand benachbarter äußerer Antennen beträgt 134,35 mm. Die äußeren Antennen bilden ein um 45° gedrehtes Quadrat mit **134,35 mm Seitenlänge**, kein Quadrat mit 190 mm Seitenlänge. Die größte Baseline und die Boundingbox der Antennenmitten sind 190 mm beziehungsweise 190 × 190 mm. Das geografische Heading beschreibt die Richtung **von CH4 zu CH0**.

### Pentagon 105 — Radius 105 mm

Profil-ID `pentagon_105`. Fünf Antennen auf Radius **105 mm**, entsprechend einem Kreisdurchmesser von **210 mm**. CH0 liegt vorne; CH1 bis CH4 folgen im Uhrzeigersinn in Schritten von 72°. Es gibt keine Mittelantenne.

| Kanal | x [mm] | y [mm] |
|---|---:|---:|
| CH0 | 0,000 | 105,000 |
| CH1 | 99,861 | 32,447 |
| CH2 | 61,717 | -84,947 |
| CH3 | -61,717 | -84,947 |
| CH4 | -99,861 | 32,447 |

Der Nachbarabstand beträgt 123,43 mm, die größte Baseline 199,72 mm. Die Boundingbox der Antennenmitten beträgt 199,72 × 189,95 mm. Das geografische Heading beschreibt die Richtung von der geometrischen Arraymitte zu CH0. Der Profilname bezieht sich auf den **Radius**, nicht auf den Durchmesser oder Nachbarabstand.

Beide kompakten Aufbauten verwenden dieselbe explizite Fünfkanal-Geometrieberechnung wie die ursprünglichen Profile. Wegen der kleinen Abstände können Antennenkopplung und Montageeinflüsse stärker ins Gewicht fallen. Das passende Profil im Setup auswählen; ein Wechsel ersetzt keinen Heading-Abgleich und keine unabhängige Prüfung einer Winkelkorrektur. Die bisherigen Profile bleiben auswählbar; die Standardauswahl wird durch das Hinzufügen der neuen Profile nicht geändert.

## Einfacher numerischer Plausibilitätscheck

Für die drei ursprünglichen Vergleichsprofile wurde mit einem idealen isotropen Fernfeldmodell bei 433 MHz die normierte komplexe Steuervektor-Korrelation für 720 Azimute (0,5°-Raster) berechnet; eine beliebige gemeinsame Signalphase ist dabei berücksichtigt. Für Richtungen mit mindestens 30° Abstand ergab sich:

| Layout | Höchste Korrelation bei >=30° Richtungsabstand | Höchstes sekundäres lokales Maximum |
|---|---:|---:|
| Kreis 300 mm | 0,880 | 0,163 |
| Kreis 560 mm | 0,612 | 0,565 |
| Quadrat 320 mm + Mitte | 0,795 | 0,578 |

Es erschien kein identischer Steuervektor für deutlich verschiedene Azimute auf diesem Raster. Das ist ein Plausibilitätscheck, kein Nachweis realer Genauigkeit oder kontinuierlicher Eindeutigkeit. Die Werte zeigen auch die Abwägung zwischen einer breiten Hauptkeule des kleinen Arrays und ausgeprägteren Nebenmaxima der größeren Geometrien. Kabel, Grundfläche, Eigenkopplung und Reflexionen sind in diesem Modell nicht enthalten.

## Montage und Messbedingungen

- Gleiche elektrische Kabellängen und identische Antennen verwenden. Nicht als gegeben annehmen, dass die interne Kraken-Phasenkalibrierung externe Kabel, Antennenkopplung oder einen unsymmetrischen Montageort korrigiert.
- Magnetfußantennen benötigen eine geeignete metallische Grundfläche. Bei Layoutvergleichen Grundfläche, Antennenhöhe und Kabelverlegung dokumentieren und möglichst konstant halten. Beim Ändern der Positionen kann sich die Kopplung ändern; deshalb eigene Korrekturprofile pro Layout/Montage.
- Ein Datum wie "Durchmesser" in der UI muss den Kreis beschreiben, nicht einen einzelnen Antennenabstand. Die UI sollte eine maßstäbliche Skizze samt Kanalbeschriftung und Maßen zeigen.
- Die reine Fernfeldabschätzung 2D²/Lambda liegt für D=0,56 m unter 1 m. Praktisch entscheiden Bodenreflexion, Umgebung, Höhendifferenz und die GPS-Winkelunsicherheit über sinnvolle Messabstände. Für die Statistik müssen insbesondere zu nahe oder veraltete GPS-Referenzen gekennzeichnet werden.
- Kein Layout liefert eine garantierte Winkel-RMS. Der große Kreis ist ein sinnvoller Start; die anderen Layouts dienen dem messbaren Vergleich.

## LoRa und räumliche Schmalbandannahme

Die absolute LoRa-Bandbreite ist für diese Aperturen kein grundsätzlicher Hinderungsgrund. Bei 560 mm Apertur beträgt die größte Laufzeitdifferenz höchstens etwa 1,87 ns. Selbst bei 500 kHz Gesamtbandbreite ändert sich die räumliche Phase über das ganze Band um höchstens etwa 0,0059 rad. Die übliche räumliche Schmalbandannahme ist deshalb eine gute Ausgangsnäherung. Das bedeutet nicht, dass ein Decoder oder die Burst-Erkennung automatisch funktioniert.

Die tatsächliche Burstlänge hängt unter anderem von SF, Bandbreite, Präambel und Nutzlast ab. Als allgemeines Rechenbeispiel (nicht als bestätigte BFT-Einstellung): Bei SF7/BW125k dauert ein Symbol 1,024 ms, acht Präambelsymbole allein 8,192 ms; bei SF12/BW125k sind es 32,768 ms bzw. 262,144 ms. Eine Erfassung muss alle fünf Kanäle aus genau demselben gültigen, kohärenten Burstfenster verwenden und darf nicht an einer langsamen Anzeigeaktualisierung hängen.

## Kalibrierung und Validierung

1. RF-Kohärenz, Heading und gelernte Winkelkorrektur als getrennte Zustände behandeln. Heading an einem bekannten Emitter mit mehreren guten Paketen abgleichen; während des folgenden Laufs festhalten.
2. Winkelabweichungen zyklisch nach [-180°,180°) bilden. Referenzrichtung aus Empfängerposition und zeitlich passender Emitterposition berechnen. Burstzeit, Positionsalter und GPS-Qualität speichern.
3. Die Kalibrierdaten sollten alle 360° abdecken. Vorschlag für eine erste UI-Regel: 24 Sektoren zu 15°, je mindestens drei gültige, möglichst unabhängige Pakete. Das ist lediglich eine Datenabdeckungsregel, keine statistische Genauigkeitsgarantie. Nicht abgedeckte Sektoren klar anzeigen.
4. Eine glatte periodische Korrektur mit wenigen Parametern (z.B. niedrige Fourier-Ordnung mit Regularisierung) ist als Einstieg angemessener als eine frei interpolierte Korrektur für jede Messung. Modellkomplexität anhand unabhängiger Daten wählen. Unabgedeckte Richtungen nicht mit einer vermeintlich gemessenen Verbesserung auszeichnen.
5. Trainings- und Prüfdaten nach **ganzen Läufen bzw. Zeitblöcken** trennen, nicht durch zufällige benachbarte Pakete derselben Bewegung. Mindestens ein unabhängiger Prüflauf; nach Möglichkeit andere Radien und erneute Runde. Erst Vorher/Nachher-RMS auf diesen zurückgehaltenen Daten als Verbesserung berichten.
6. Das Profil an Geometrie, Kanalzuordnung, Montage und Frequenzbereich binden. Korrekturversion sowie Roh- und korrigierte Winkel speichern. Neue Profile müssen deaktivierbar bleiben. Eine Korrektur kann ortsabhängige Mehrwegefehler mitlernen und ist deshalb nicht ohne Prüfung auf andere Standorte übertragbar.
7. Gesamt-RMS und RMS je Azimutsegment stets zusammen mit Anzahl, Abdeckung und Ausschlussgründen anzeigen. Ungefilterte und für die Auswertung akzeptierte Pakete behalten; Qualitätsfilter transparent machen.

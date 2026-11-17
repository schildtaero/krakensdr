# GPS-Verbindung, Satelliten und Positionsfix ab 0.2.3

Die GPS-Kachel zeigt den Zustand der angeschlossenen Maus bereits vor einem
Positionsfix. Daneben steht die Quelle der Stationsreferenz: GPS-Anker,
manuelle Position, Simulation oder Aufnahme. So ist erkennbar, ob die Maus
gerade Daten liefert und welche Position tatsächlich zur Fehlerauswertung
verwendet wird.

## Die Anzeige lesen

| Anzeige | Bedeutung / nächster Schritt |
|---|---|
| GPS nicht eingerichtet | Einen Port im Setup auswählen oder bei GPS-Positionsquelle die automatische Suche verwenden. |
| GPS-Maus nicht gefunden | Kein eindeutiger passender serieller Port gefunden. USB-Verbindung und unter Windows die WSL-Durchreichung prüfen. |
| Mehrere GPS-Geräte · Port wählen | Automatische Auswahl ist nicht eindeutig. Den gewünschten Port im Setup festlegen. |
| GPS-Port wird geöffnet | Die serielle Verbindung wird aufgebaut. |
| GPS verbunden · noch keine Daten | Port offen; bislang keine Bytes angekommen. Port, Baudrate und Gerät prüfen. |
| Daten empfangen · kein gültiges NMEA | Bytes kommen an, aber keine aktuellen prüfsummengültigen NMEA-Meldungen. Baudrate oder Port kann falsch sein. |
| NMEA aktiv · noch kein Fix | Gültige Empfängermeldungen kommen an; eine gültige Position fehlt noch. |
| Satellitensignal · noch kein Fix | Mindestens ein positiver C/N0-Wert wird gemeldet. Für einen Fix reichen die bisherigen Informationen noch nicht. |
| NMEA aktiv · kein Signal gemeldet | Der Empfänger meldet aktuell keine positiven C/N0-Werte. Freie Sicht zum Himmel herstellen. |
| GPS-Fix gültig / 2D / 3D | Eine aktuelle gültige Position liegt vor. Die Fixart wird nur ergänzt, wenn sie gemeldet wird. |
| GPS-Daten veraltet | Daten- oder Positionsmeldungen sind seit mehr als fünf Sekunden ausgeblieben. |
| GPS-Verbindungsfehler / getrennt | Portöffnung oder Lesen ist gescheitert, etwa nach Abziehen. Fehlermeldung im Detaildialog beachten. |

Ein offener serieller Port bestätigt den Portzugriff. Prüfsummengültige
NMEA-Meldungen zeigen einen aktiven Datenstrom. Positive C/N0-Werte zeigen
gemeldete Satellitensignale; ein Positionsfix ist ein weiterer eigener Zustand.

## Drei Satellitenzahlen

In der Hauptanzeige steht zum Beispiel:

**Signal 3 · Sichtfeld 9 · Fix 0**

- **Signal:** Satelliten mit einem gemeldeten C/N0-Wert größer als null.
- **Sichtfeld:** vom Empfänger in GSV-Meldungen gemeldete Satelliten. Nicht
  jeder davon muss bereits einen messbaren Signalwert haben.
- **Fix:** laut GGA/GSA für die Positionslösung verwendete Satelliten.

**0** ist eine tatsächlich gemeldete Null. **„—“** bedeutet unbekannt, noch
nicht gemeldet oder veraltet. Liefert die Maus nur RMC/GGA, kann die Position
gültig sein, während Sichtfeld und Signalwerte unbekannt bleiben. Ein fehlendes
GSV-Feld bedeutet daher nicht automatisch, dass kein Satellit empfangen wird.

Mehrteilige GSV-Meldungen werden erst als vollständiger Zyklus übernommen.
Mehrere Signalbänder oder überlappende GNSS-Meldungen sollen denselben
Satelliten nicht mehrfach zählen. Nach zehn Sekunden ohne passende Aktualisierung
werden die Satellitenwerte entwertet. Bei getrenntem Port gelten frühere
Werte nicht als aktuelle Hardwaretelemetrie.

## Details öffnen

Auf die GPS-Kachel klicken. Der Dialog zeigt:

- seriellen Port und Baudrate, Verbindung und Daten-/NMEA-Alter;
- Satellitenzahlen und C/N0-Balken je gemeldetem Satelliten in **dB-Hz**;
- Positionsfix, Fixart und HDOP, sofern verfügbar;
- Fortschritt zum festen Stationsanker;
- empfangene Bytes, gelesene Zeilen, prüfsummengültige und fehlerhafte Meldungen.

Die GPS-Signalbalken beschreiben C/N0 des GNSS-Empfängers. Das SNR im
BFT-Tracking ist eine separate Schätzung für die 433-MHz-Pakete. Leere
C/N0-Felder werden als unbekannt dargestellt. Selbst bei unterbrochener
Backend-Verbindung altern sichtbare Werte im Browser weiter und bleiben
nicht unbegrenzt als aktueller grüner Fix stehen.

## Stationsanker und Neuverbindung

Das feste Array verwendet den Mittelwert von zehn zeitlich verschiedenen,
gültigen Positionsmeldungen. RMC und GGA derselben UTC-Epoche zählen einmal;
GSA/GSV und serielle Timeouts zählen nicht als neue Position. Vor der
ersten Aufnahme den Anker abwarten. Während einer Aufnahme pausiert die
Mittelung, solange noch kein Anker vorhanden ist.

Ein vorhandener Anker bleibt bei späterem GPS-Ausfall erhalten, damit die
feste Station ihre Referenz behält. Der aktuelle Verbindungs-/Fixstatus wird
trotzdem separat angezeigt. **GPS neu verbinden** öffnet den konfigurierten
Port erneut beziehungsweise wiederholt bei leerem Port und GPS-Positionsquelle
die automatische Erkennung. Der Stationsanker bleibt erhalten. Aufnahme und
Replay vorher beenden; dadurch bleibt das gespeicherte Messprofil eindeutig.
Bei einem tatsächlichen Standortwechsel den Backendprozess neu starten,
den Anker neu bestimmen lassen und das Heading prüfen.

Unter Windows muss die Maus über `usbipd` an WSL angebunden sein. Dort einen
Linux-Port wie `/dev/ttyACM0` verwenden. `COM3` ist ein Windows-Port und gehört
zum nativen Windows-Client mit externem Linux-DAQ. Für eine VK-162 mit
9600 Baud beginnen; die konkrete Gerätekonfiguration kann abweichen. Beim
Erststart unter freiem Himmel kann der erste Fix mehrere Minuten benötigen.

Das Diagnose-ZIP enthält GPS-Verbindungszustand, Zähler, Satellitenwerte und
Fehlermeldungen. GPS-Koordinaten und rohe NMEA-Zeilen werden der
Diagnosezusammenfassung nicht hinzugefügt.

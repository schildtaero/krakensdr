# Hardwareadapter und Inbetriebnahme

Der Adapter `krakendf.hardware` ist unabhängig implementiert und spricht das im geprüften Heimdall-Quellstand dokumentierte TCP-Protokoll. Er öffnet keine fünf unabhängigen RTL-SDR-Geräte: benötigt wird der Ausgang hinter Heimdalls Kohärenzkorrektur.

## Kraken / Heimdall

- Ziel: Linux x86_64/ARM64 mit Heimdall, Headerformat Version 7, little-endian C-Layout mit 64-Bit-Ausrichtung.
- Heimdall-Konfiguration `out_data_iface_type=eth` verwenden und den DAQ-Dienst starten. Der genaue Dienststart hängt von der vorhandenen Kraken-Installation ab.
- IQ-TCP-Port standardmäßig 5000; Controller standardmäßig 5001. Der IQ-Server dieses Stands bedient einen Verbraucher. Eine parallel laufende Original-DoA-Anwendung kann den Port bereits belegen.
- Der Adapter sendet `streaming`, liest 1024-Byte-Header und kanalweise complex64-IQ und fordert jeden weiteren Frame mit `IQDownload` an. Fragmentierte TCP-Lieferungen werden mit exakten begrenzten Reads zusammengesetzt.
- Erwartet werden fünf Antennenkanäle. Unbekannte Header-Versionen, Formate oder Größen brechen den Stream mit einer verständlichen Fehlermeldung ab. Es wird keine unbekannte Datenstruktur geraten.
- DATA mit IQ-/Delay-Lock, ohne Rauschquelle, Übersteuerung oder ungültige Zahlen ist gültig. CAL/RAMP/DUMMY/TRIGW bleiben Diagnoseframes, ihre Nutzdaten werden korrekt konsumiert.
- CPI-Lücken, ungültige Frames, rücklaufende Hostzeit, Frequenz-/Sampleratenwechsel und Verbindungsneustarts beginnen eine neue Stream-Epoche. Sampleindizes gelten nur innerhalb dieser Epoche.
- Headerzeiten sind **DAQ-Hostzeit in Millisekunden**, kein nachgewiesener GPS/PPS-Zeitstempel.

Der DAQ setzt seine Hostzeit nach Bereitstellung eines vollständigen Rohdatenpuffers; der Rebuffer zieht die hinter dem ausgegebenen Frame verbleibenden Samples ab. Die rohe Headerzeit ist damit näherungsweise das **Frameende**, einschließlich unbekannter USB-/Hostlatenz. `IQFrame.timestamp` schätzt den ersten Samplezeitpunkt als `header_timestamp_ms/1000 − sample_count/sample_rate`. Die rohe Headerzeit und diese Semantik bleiben im Qualitätsobjekt erhalten. Decoder und DF werden exakt über Sampleindizes zugeordnet; die daraus abgeleitete Uhrzeit ist ohne externe Zeitreferenz keine genaue GNSS-/PPS-Zeit.

`configure(center_frequency_hz=..., gain_db=...)` nutzt die dokumentierten 128-Byte-Controllerbefehle FREQ und GAIN. Die Controllerantwort FNSD bestätigt die Anfrage. Erst die nachfolgenden IQ-Header belegen tatsächliche Mittenfrequenz, Gain und erneuten Kohärenz-Lock. Gain wird als gleicher manueller Wert auf alle fünf Kanäle angefordert. Unterstützte diskrete Gainwerte stehen in `MANUAL_GAINS_DB`; andere Werte werden mit Angabe des nächsten passenden Werts abgelehnt, weil der geprüfte Heimdall-Controller sie sonst protokolliert und ignoriert. Samplerate und DAQ-Bandbreite werden über die Heimdall-Konfiguration eingestellt, nicht über einen erfundenen Livebefehl.

## GPS

GPS ist optional. `GPSReader(port, baud=9600)` benötigt das Pythonpaket `pyserial`; ohne Paket oder zugänglichen Port gibt es eine konkrete Fehlermeldung. Baudrate und Port sind auf die verwendete GPS-Maus abzustimmen.

`parse_nmea` unterstützt RMC/GGA beliebiger Talker-ID, prüft den NMEA-XOR-Checksum, Koordinaten, Fixstatus und endliche Zahlenwerte. RMC-Kurs wird nur als Diagnosewert geliefert und nicht als Heading des feststehenden Arrays interpretiert. Der Status markiert den letzten Fix nach fünf Sekunden ohne Empfang als veraltet. Dieser Empfangszeitbezug ist kein Beweis des tatsächlichen GNSS-Positionsalters.

## Geprüft und offen

Der automatische Test verwendet einen lokalen Mockserver und prüft fragmentierte Frames, Kanalordnung, Sampleindizes, Kalibrier-/Dummydaten, Lock-/Indexlücken, Übersteuerung, Retuning und Controllerpakete. GPS-Sätze werden mit gültigen/ungültigen Checksummen und Fixzuständen geprüft. Ein physischer Kraken, USB-GPS und echte 433-MHz-Signale waren für diese Tests nicht angeschlossen. Deshalb bleiben Kabel-/Kanalzuordnung, tatsächliche Controllerkompatibilität und HF-Peilgenauigkeit Teil der Hardwareabnahme.

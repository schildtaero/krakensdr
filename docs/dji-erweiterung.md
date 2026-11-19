# DJI-Erweiterung: Machbarkeit und benötigte Eingangsdaten

**Status: technische Vorprüfung, noch keine implementierte DJI-Livequelle.**
Der bestehende Kraken-/BFT-Empfang ist Softwarestand 0.2.3. Eine DJI-
Erweiterung benötigt zusätzliche Empfangshardware und einen zum konkreten
Drohnen-/Fernsteuerungsmodell passenden Decoder. Modell, Firmwarestand und
vorhandene Zusatzhardware sind noch zu klären.

## Hardwaregrenzen

KrakenRF spezifiziert 24–1766 MHz. Die Mavic Air verwendet 2,4 bzw. 5,8 GHz;
die aktuelle 2,4-MS/s-Kraken-Pipeline kann diese Signale nicht direkt empfangen
und auch keine vollständigen breiteren DJI-Kanäle decodieren. Eine höhere
Frequenzgrenze in der Konfiguration schafft keine entsprechende Hardware.

| Generation | Belegter Ausgangspunkt | Noch zu prüfen |
|---|---|---|
| Mavic Air 1 | Enhanced Wi-Fi; Kismet enthält einen DJI-DroneID-Parser mit Mavic-Air-Modellkennung. Eine experimentelle Studie beschreibt 5-MHz-Empfang mit geeigneter Monitor-Hardware. | Tatsächlich ausgesendete Pakete, Firmware, Band/Kanal und 5-MHz-Unterstützung des Empfängers. |
| Mavic Air 2 | OcuSync 2.0; RUB-SysSec testete DroneID mit Mavic Air 2 und USRP B205-mini. Der Forschungsreceiver verwendet 50-MHz-Captures. | Hardwaretreiber, Echtzeitleistung, konkreter Firmwarestand und empfangbare Felder. |
| Air 2S | DJI O3; Modell und Übertragbarkeit der jeweiligen Decoder unterscheiden sich. | Keine Zusage aus einem Air-2-Test ableiten; reales Empfangsfixture erforderlich. |

Ein gewöhnlicher WLAN-Adapter garantiert keinen Empfang des Enhanced-Wi-Fi-
Modus. Kanalbreite, Monitor-Modus und Treiber müssen passen. WSLs virtuelle
Netzwerkschnittstelle stellt keine ungefilterten WLAN-Funkframes bereit.
Ein einzelner breitbandiger SDR kann zur Decodierung geeignet sein, liefert
aber keine kohärente Fünfkanalpeilung.

## Zwei getrennte Aufgaben

1. **Decodierung:** geeignete Wi-Fi-/SDR-Quelle, vollständige gültige
   Protokollpakete und modellabhängiger Decoder. Nur tatsächlich enthaltene
   gültige Positionsfelder übernehmen.
2. **RF-Peilung:** geeigneter kohärenter GHz-Mehrkanalempfang, passende
   Antennen, Kalibrierung und sichere Zuordnung von Downlink und Uplink.
   Eine einzelne Station liefert zunächst einen Winkel, keine Entfernung.

Fünf phasenkohärente Downconverter mit gemeinsamem LO wären ein möglicher
Versuchsaufbau für eine schmale GHz-Signalscheibe am Kraken. Dafür ist eine
zusätzliche Kalibrierung des externen Empfangswegs nötig. Die 2,4-MHz-Grenze
bleibt bestehen; ein Downconverter ersetzt keinen breitbandigen Decoder.
Im Peilmodell muss die ursprüngliche GHz-RF-Frequenz von der Zwischenfrequenz
getrennt werden, da die Antennenphase von der ursprünglichen Wellenlänge abhängt.

Die bisherigen 433-MHz-Strahler und Antennenprofile sind dafür neu zu bewerten.
Die Wellenlänge beträgt ungefähr 125 mm bei 2,4 GHz und 52 mm bei 5,8 GHz;
die vorhandenen Abstände können mehrdeutige Arrayantworten erzeugen. Bei
fliegenden Quellen muss außerdem Elevation berücksichtigt werden; das
vorhandene horizontale MUSIC-Modell ist kein automatisch gültiges 3D-Modell.

## Positionsarten und Paketzuordnung

Folgende Angaben müssen in UI, Aufzeichnung und Export getrennt bleiben:

- **Drohnenpeilung:** RF-Richtung eines verifiziert zugeordneten Drohnensignals.
- **Fernsteuerungspeilung:** RF-Richtung separat zugeordneter Controller-Signale.
- **Drohnenposition:** ausgesendete und erfolgreich decodierte Koordinate.
- **App-/Operatorposition:** separat bezeichnetes übertragenes Feld, abhängig
  von Quelle und Gültigkeitsflags; nicht automatisch die Fernsteuerungsposition.
- **Home-/Startpunkt:** eigener Positionstyp, nicht als aktueller Controllerort
  verwenden. Standardisierte Remote-ID-Systemmeldungen unterscheiden Startort,
  Live-GNSS und fest hinterlegte Operatorposition.

**Eine vom Fluggerät gesendete Meldung wird zum Fluggerät gepeilt**, auch wenn
die Nutzlast App- oder Operator-Koordinaten enthält. Eine zweite Koordinate
erzeugt keine zweite RF-Peilung. Auch ein DJI-OUI oder eine Kanalbelegung allein
beweist weder das konkrete Modell noch die Rolle Drohne/Fernsteuerung.

Wenn Decodierung und Peilung aus getrennten Empfängern stammen, benötigen sie
eine geprüfte Zeit-/Paketzuordnung. Eine Zuordnung anhand der letzten UI-
Aktualisierung wäre unzureichend. Für die Freigabe eines Livepfads sind daher
ein realer Capture und ein beschrifteter Versuch mit Drohne und Controller an
bekannten unterschiedlichen Richtungen nötig. Schnittstellen und Parser
können zuvor mit dokumentierten Testfixtures vorbereitet werden.

## Geprüfte Primärquellen

- [KrakenRF: KrakenSDR-Spezifikation](https://www.krakenrf.com/product-page/krakensdr)
- [DJI: Mavic Air](https://www.dji.com/support/product/mavic-air)
- [DJI: Mavic Air 2](https://www.dji.com/mavic-air-2/specs)
- [DJI: Air 2S und Mavic Air 2 im Vergleich](https://store.dji.com/content/dji-air-2s-mavic-air-2-comparison)
- [Studie: Drone Detection and Tracking Using RF Identification Signals](https://www.mdpi.com/1424-8220/23/17/7650)
- [Kismet: DJI DroneID](https://www.kismetwireless.net/development/droneid/)
- [Kismet: tatsächlicher DJI-Parser](https://github.com/kismetwireless/kismet/blob/master/dot11_parsers/dot11_ie_221_dji_droneid.h)
- [RUB-SysSec: DroneSecurity](https://github.com/RUB-SysSec/DroneSecurity)
- [OpenDroneID: Positionstypen](https://github.com/opendroneid/opendroneid-core-c/blob/master/libopendroneid/opendroneid.h)

Für die Umsetzung fehlen das genaue Mavic-/RC-Modell sowie die Entscheidung,
welcher zusätzliche Wi-Fi-/SDR- und kohärente DF-Empfangsweg verwendet wird.

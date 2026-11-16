# Windows: Erstinstallation und Livebetrieb

Der lokale Kraken-USB-DAQ-Stack ist Linux-Software. Der normale Windows-Weg ist deshalb **Windows 11 + WSL2 + Ubuntu 24.04**, inklusive USB-Durchreichung. Die Windows-Skripte geben den nativen Python-Prozess nicht als Kraken-USB-Treiber aus.

## Ohne KrakenSDR und GPS-Maus starten

Beide Geraete sind fuer die Simulation **nicht erforderlich**:

```bat
start.cmd --source simulation
```

Bei einer bestehenden WSL-Installation bleibt der Start in WSL; falls dort Software fehlt, verwendet der Dispatcher nur den App-Installer (`-AppOnly`). Auf einem frischen Windows-System ohne Projektinstallation startet diese ausdrueckliche Simulation den nativen Windows-Client und richtet dessen Python-Abhaengigkeiten ein. Dafuer sind WSL2, usbipd und ein nativer DAQ-Build nicht notwendig.

Die Softwareinstallation wird nicht durch leere oder fehlschlagende `lsusb`-Ausgaben blockiert. WSL ohne durchgereichte USB-Geraete kann bei `lsusb` Exitcode 1 liefern. Das ist ein Diagnosehinweis, kein fehlgeschlagenes Software-Setup. `usb_ready: false` und eine entsprechende Meldung bleiben im Marker erhalten.

## Standardweg: kompletter Stack auf demselben Windows-PC

Im entpackten Projektordner `setup.cmd` ausfuehren. `start.cmd` startet bei fehlender Einrichtung dieselbe Erstinstallation automatisch.

```bat
setup.cmd
```

Das Setup prueft WSL2 und Ubuntu 24.04, installiert fehlende unterstuetzte Komponenten und `usbipd-win`, kopiert das Projekt in das native Linux-Dateisystem unter `~/.local/share/krakendf/project` und fuehrt dort den mitgelieferten Linux-Installer aus. Dieser installiert fehlende System-/Python-Pakete und baut den gebuendelten DAQ-Stack. Eine vorhandene WSL-venv und Runtime werden beim Kopieren behalten; eine Windows-venv wird nicht nach Linux kopiert.

`usbipd-win` wird erst automatisch installiert, wenn mit `-UsbBusId` konkrete eigene USB-Geraete ausgewaehlt werden. Ohne solche Auswahl bleibt die Softwareinstallation erfolgreich und nennt die spaeteren USB-Schritte. Fuer eine reine App-Installation in einer vorhandenen WSL-Distribution gibt es:

```bat
setup.cmd -AppOnly
```

Dieser Weg baut keinen nativen DAQ und verlangt keine WSL2-USB-Unterstuetzung. `setup.cmd` ohne `-AppOnly` bereitet weiterhin den vollstaendigen spaeteren Livebetrieb vor. Bereits vorhandene Runtime, venv und Benutzerdaten bleiben erhalten.

Neue Windows-Features koennen Administratorrechte und einen Neustart erfordern. Ein Neustart wird nicht automatisch ausgeloest. Bei einer erstmalig installierten Ubuntu-Distribution einmal `wsl -d Ubuntu-24.04` oeffnen und ein regulaeres Linux-Konto samt Passwort anlegen. Anschliessend `setup.cmd` wiederholen. Falls die Distribution versehentlich als root startet, in Ubuntu ein regulaeres Konto anlegen und als Standard setzen, zum Beispiel mit `ubuntu2404.exe config --default-user BENUTZER` bei der Store-Distribution, oder in `/etc/wsl.conf`:

```ini
[user]
default=BENUTZER
```

Danach Ubuntu verlassen, `wsl --terminate Ubuntu-24.04` ausfuehren und das Setup wiederholen. Bereits vorhandene WSL1-Distributionen werden nicht still konvertiert; das Skript nennt dafuer `wsl --set-version Ubuntu-24.04 2`.

## KrakenSDR und GPS an WSL durchreichen

`usbipd list` zeigt Windows-USB-Geraete. KrakenSDR meldet fuenf RTL-SDR-Interfaces; die dazugehoerigen BUSIDs erkennen und nur diese auswaehlen. Ein zusaetzlicher beliebiger RTL-Dongle darf nicht versehentlich mit ausgewaehlt werden.

Einmal pro zugehoeriger BUSID in einer **Administrator-PowerShell**:

```powershell
usbipd bind --busid 4-1
```

Bei laufendem WSL und angeschlossenem Geraet:

```powershell
usbipd attach --wsl --busid 4-1
```

Fuer alle fuenf Interfaces wiederholen. Die Beispiel-BUSID `4-1` durch die tatsaechliche ID ersetzen. Nach erneutem Anstecken oder WSL-Neustart kann ein erneutes `attach` noetig sein. Aus CMD beziehungsweise `setup.cmd` werden mehrere IDs als **ein zitierter, kommaseparierter String** uebergeben:

```bat
setup.cmd -UsbBusId "4-1,4-2,4-3,4-4,4-5"
```

In einer direkten PowerShell-Sitzung funktioniert auch `./setup.ps1 -UsbBusId 4-1,4-2,4-3,4-4,4-5`. Das Skript trennt beide Formen selbst und arbeitet nur mit **ausdruecklich angegebenen** IDs. Bereits durchgereichte Geraete werden nicht erneut angehaengt; Bind braucht weiterhin Administratorrechte.

Die **ARCELI VK-162** ebenfalls an WSL durchreichen, wenn der Linux-Prozess sie lesen soll. Sie ist dann nicht gleichzeitig am Windows-COM-Port verfuegbar. Im Webinterface den Linux-Port der Maus eintragen, typischerweise `/dev/ttyACM0` oder `/dev/ttyUSB0`; 9600 Baud ist die Anfangseinstellung und bleibt aenderbar. `lsusb` bzw. der Statusbereich hilft bei der Geraetepruefung.

Die Softwareinstallation benoetigt keinen angeschlossenen Kraken. Das Skript unterscheidet `installation_complete`, `daq_installed` und `usb_ready`. Fehlende USB-Geraete oder eine nicht verfuegbare USB-Auflistung beenden eine erfolgreiche Installation mit **Exitcode 0**. Sie werden als offener Live-USB-Schritt gemeldet und blockieren keine Simulation. Fuenf sichtbare Interfaces allein sind wiederum kein Kohaerenznachweis: Der Laufzeitcheck prueft den Zugriff, und die Anwendung wartet auf gueltige IQ-/Delay-Locks.

## Starten

```bat
start.cmd
```

Die Datei `.windows-setup.json` speichert Distribution, Projektpfad und installierte Projektversion. Bei einer neuen Paketversion oder einem alten Marker ohne Versionsfeld wird die WSL-Kopie vor dem Start erneut abgeglichen und der Installer ausgefuehrt. Eine unveraenderte, gesunde Installation braucht dabei keinen erneuten Download. Der Dispatcher startet dort `bash ./start.sh`; Livebetrieb mit dem verwalteten lokalen Linux-Stack ist der Standard. Der Browser kann unter Windows **http://127.0.0.1:8080** verwenden (WSL-Localhostweiterleitung).

Beispiele fuer App-Argumente:

```bat
start.cmd --source simulation
start.cmd --source idle --port 8081
setup.cmd -Check
```

`-Check` prueft die bereits installierte Software ohne Paketinstallation oder Hardwarepflicht. Bei einem App-only-Marker werden nur App-Abhaengigkeiten geprueft. Eine andere vorhandene Distribution kann beim Setup mit `setup.cmd -Distribution Ubuntu-24.04` angegeben werden; sie muss die Voraussetzungen des gewaehlten Linux-Installerpfads erfuellen. Ein spaeterer ausdruecklicher lokaler Live-Start erweitert eine App-only-Installation um den nativen DAQ.

## Alternative: nativer Windows-Client mit Linux-DAQ im LAN

```bat
setup.cmd -Mode Native
start.cmd --daq-host 192.168.1.50
```

Dieser Modus installiert bei Bedarf Python 3.12 ueber `winget`, erstellt eine Windows-venv und installiert App und GPS-Paketabhaengigkeiten. Der Startdispatcher verwendet dann `--daq-mode external`. Der Linux-Heimdall-Host muss unter der angegebenen Adresse erreichbar sein; seine IQ-/Controllerports sind standardmaessig 5000/5001. Eine native VK-162 kann in diesem Modus an ihrem Windows-COM-Port gelesen werden.

`setup.cmd -Mode Wsl` wechselt zur lokalen WSL-Integration. `start.cmd -Mode Native ...` bzw. `start.cmd -Mode Wsl ...` waehlt den Modus ausdruecklich; falls dessen Einrichtung fehlt, wird sie gestartet. Native und WSL-Installationen verwenden getrennte Pythonumgebungen und Datenverzeichnisse.

Der native Start prueft vor jedem Lauf Pythonversion, importierbare Pakete, installierte App-Version und `pip check`. Bei fehlenden oder inkonsistenten Paketabhaengigkeiten startet er den Installer erneut. Eine defekte oder zu alte projektlokale venv wird gesichert und neu erstellt. Die an `start.cmd` uebergebenen App-Argumente bleiben als einzelne Argumente erhalten; nur der Windows-Wrapper-Schalter `-Mode` wird herausgenommen. GNU-Schalter wie `--source`, `--daq-host` oder `--data-dir` werden nicht als PowerShell-Parameter interpretiert.

## Grenzen der hiesigen Pruefung

Die PowerShell-Skripte wurden unter Linux statisch geprueft. Auf diesem Host waren weder PowerShell noch Windows, WSL oder USB-Hardware verfuegbar. Daher wurde kein realer Windows-Installerlauf, keine UAC-Anforderung, keine USB-Durchreichung und keine Windows-/WSL-Hardwareabnahme behauptet. Linux-Protokoll-/App-Tests belegen diese Windows-Schritte nicht.

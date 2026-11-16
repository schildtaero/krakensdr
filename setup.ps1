# Windows 11 / Windows PowerShell 5.1 or newer.
# WSL is the default: Heimdall's USB DAQ runs on Linux, not in a native
# Windows Python process. -Mode Native installs the network DAQ client only.
[CmdletBinding()]
param(
    [ValidateSet("Wsl", "Native")]
    [string]$Mode = "Wsl",
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Distribution = "Ubuntu-24.04",
    [string[]]$UsbBusId = @(),
    [switch]$AppOnly,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MarkerPath = Join-Path $ProjectRoot ".windows-setup.json"
$versionMatch = [regex]::Match([System.IO.File]::ReadAllText((Join-Path $ProjectRoot "pyproject.toml")), '(?m)^\s*version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) { throw "Projektversion fehlt in pyproject.toml." }
$ProjectVersion = $versionMatch.Groups[1].Value
$NativeHealthCode = "import sys; assert sys.version_info >= (3,11); import fastapi, uvicorn, numpy, scipy, openpyxl, httpx, serial, krakendf; import importlib.metadata as metadata; assert metadata.version('kraken-df-demo') == sys.argv[1]"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Befehl fehlgeschlagen ($LASTEXITCODE): $Program $($Arguments -join ' ')"
    }
}

function Write-SetupMarker {
    param([object]$Value)
    $temporary = "$MarkerPath.tmp"
    $json = $Value | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $MarkerPath -Force
}

function Install-WingetPackage {
    param([string]$Id)
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget fehlt. Microsoft App Installer installieren/aktualisieren, dann setup.cmd erneut starten."
    }
    Write-Host "Installiere fehlende Abhaengigkeit: $Id"
    Invoke-Checked $winget.Source @("install", "--id", $Id, "--exact", "--source", "winget", "--silent", "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity")
}

function Find-Python {
    $candidates = @([PSCustomObject]@{Executable=(Join-Path $ProjectRoot ".venv\Scripts\python.exe"); Prefix=@()})
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += [PSCustomObject]@{Executable=$launcher.Source; Prefix=@("-3.12")}
        $candidates += [PSCustomObject]@{Executable=$launcher.Source; Prefix=@("-3")}
    }
    foreach ($version in @("314", "313", "312", "311")) {
        if ($env:LOCALAPPDATA) {
            $candidates += [PSCustomObject]@{Executable=(Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"); Prefix=@()}
        }
        if ($env:ProgramFiles) {
            $candidates += [PSCustomObject]@{Executable=(Join-Path $env:ProgramFiles "Python$version\python.exe"); Prefix=@()}
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    # Skip Windows Store aliases; probing them can open an unrelated store UI.
    if ($python -and $python.Source -notmatch '\\WindowsApps\\') {
        $candidates += [PSCustomObject]@{Executable=$python.Source; Prefix=@()}
    }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Executable -PathType Leaf)) { continue }
        $prefix = @($candidate.Prefix)
        try {
            $answer = & $candidate.Executable @prefix -c "import sys; print(sys.executable) if sys.version_info >= (3,11) else sys.exit(3)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $answer) {
                return [PSCustomObject]@{Executable=([string]($answer | Select-Object -Last 1)).Trim(); Prefix=@()}
            }
        } catch { continue }
    }
    return $null
}

function Resolve-Usbipd {
    $command = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if ($env:ProgramFiles) {
        $installed = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
        if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    }
    return $null
}

function Show-UsbInstructions {
    param([string]$Usbipd)
    Write-Host ""
    Write-Host "USB fuer WSL vorbereiten (KrakenSDR hat fuenf RTL-SDR-Interfaces):"
    if ($Usbipd) { & $Usbipd list }
    Write-Host "In einer Administrator-PowerShell fuer jede zugehoerige BUSID einmal:"
    Write-Host "  usbipd bind --busid <BUSID>"
    Write-Host "Danach bei angeschlossenem Geraet und laufendem WSL:"
    Write-Host "  usbipd attach --wsl --busid <BUSID>"
    Write-Host "Auch die VK-162 an WSL durchreichen, wenn sie dort gelesen werden soll."
    Write-Host "Keine fremden USB-Geraete aus der Liste auswaehlen. Nach erneutem Anstecken kann attach erneut noetig sein."
}

function Get-UsbDeviceState {
    param([string]$Usbipd, [string]$BusId)
    try {
        $stateJson = & $Usbipd state 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $state = ($stateJson -join "`n") | ConvertFrom-Json
        return @($state.Devices | Where-Object { $_.BusId -eq $BusId }) | Select-Object -First 1
    } catch { return $null }
}

try {
    if ($env:OS -ne "Windows_NT") {
        throw "setup.ps1 ist fuer Windows. Unter Linux bitte bash ./setup.sh verwenden."
    }
    Set-Location -LiteralPath $ProjectRoot

    if ($Mode -eq "Native") {
        $python = Find-Python
        if ($Check) {
            $venv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $venv)) { throw "Windows-venv fehlt. setup.cmd -Mode Native ausfuehren." }
            Invoke-Checked $venv @("-c", $NativeHealthCode, $ProjectVersion)
            Invoke-Checked $venv @("-m", "pip", "check")
            Write-Host "Native Clientinstallation geprueft. Live-DAQ benoetigt einen Linux-Heimdall-Host."
            exit 0
        }
        if (-not $python) {
            Install-WingetPackage "Python.Python.3.12"
            $python = Find-Python
            if (-not $python) { throw "Python nach Installation nicht gefunden. Ein neues Terminal oeffnen und setup.cmd -Mode Native wiederholen." }
        }
        $venv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venv) {
            $usable = $false
            try {
                & $venv -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 3)" 2>$null
                $usable = $LASTEXITCODE -eq 0
            } catch { $usable = $false }
            if (-not $usable) {
                # Preserve a damaged/old project environment instead of
                # deleting it or trying to reuse incompatible site-packages.
                $backup = Join-Path $ProjectRoot (".venv-before-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss"))
                Move-Item -LiteralPath (Join-Path $ProjectRoot ".venv") -Destination $backup
                Write-Host "Alte Pythonumgebung gesichert: $backup"
            }
        }
        if (-not (Test-Path -LiteralPath $venv)) {
            Invoke-Checked $python.Executable @("-m", "venv", (Join-Path $ProjectRoot ".venv"))
        }
        Invoke-Checked $venv @("-m", "ensurepip", "--upgrade")
        Invoke-Checked $venv @("-m", "pip", "install", ".[gps]")
        Invoke-Checked $venv @("-c", $NativeHealthCode, $ProjectVersion)
        Invoke-Checked $venv @("-m", "pip", "check")
        Write-SetupMarker ([ordered]@{schema_version=1; mode="native"; source_version=$ProjectVersion; installation_complete=$true; daq_installed=$false; usb_ready=$false; python=$venv; updated_at=[DateTime]::UtcNow.ToString("o")})
        Write-Host "Native Clientinstallation abgeschlossen."
        Write-Host "Live mit Linux-DAQ im LAN: start.cmd --daq-host <LINUX-IP>"
        Write-Host "Der native Windows-Prozess enthaelt keinen Linux-USB-Treiber. Fuer lokales Kraken-USB: setup.cmd -Mode Wsl"
        exit 0
    }

    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        throw "WSL fehlt. Windows 11 aktualisieren und in Administrator-PowerShell 'wsl --install -d $Distribution' ausfuehren; danach Windows bei Bedarf neu starten."
    }
    $distributions = @(& $wsl.Source --list --quiet 2>$null | ForEach-Object { ([string]$_ -replace "`0", "").Trim() } | Where-Object { $_ })
    if ($distributions -notcontains $Distribution) {
        if ($Check) { throw "WSL-Distribution $Distribution ist nicht installiert." }
        Write-Host "Installiere WSL2-Distribution $Distribution. Ein eventuell verlangter Neustart erfolgt nicht automatisch."
        & $wsl.Source --install --distribution $Distribution --no-launch
        $installExit = $LASTEXITCODE
        if ($installExit -ne 0 -and $installExit -ne 3010) {
            throw "WSL-Installation fehlgeschlagen ($installExit). Diesen Befehl in Administrator-PowerShell ausfuehren: wsl --install -d $Distribution"
        }
        Write-Host "WSL ist angelegt oder wird nach einem Neustart bereit."
        Write-Host "Einmal 'wsl -d $Distribution' starten, ein regulaeres Linux-Benutzerkonto anlegen und anschliessend setup.cmd erneut ausfuehren."
        exit 2
    }

    $uid = & $wsl.Source --distribution $Distribution --exec id -u 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "WSL noch nicht startbereit. 'wsl -d $Distribution' starten; eine ausstehende Installation bzw. einen Neustart abschliessen."
    }
    if (([string]($uid | Select-Object -Last 1)).Trim() -eq "0") {
        throw "$Distribution startet als root. Ein regulaeres Linux-Konto initialisieren und als Standardbenutzer setzen, dann setup.cmd wiederholen. Anleitung: docs/windows-setup.md"
    }
    if (-not $AppOnly -and -not $Check) {
        $kernel = & $wsl.Source --distribution $Distribution --exec uname -r 2>$null
        if ($LASTEXITCODE -ne 0 -or ([string]$kernel -notmatch "WSL2")) {
            throw "WSL2 wird fuer USB benoetigt. Fuer Simulation reicht 'setup.cmd -AppOnly'. Fuer Live einmal 'wsl --set-version $Distribution 2' ausfuehren."
        }
    }

    $usbipd = Resolve-Usbipd
    if ($Check) {
        if (-not (Test-Path -LiteralPath $MarkerPath)) { throw "WSL-Projektinstallation fehlt. setup.cmd ausfuehren." }
        $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
        if ($marker.mode -ne "wsl" -or $marker.distribution -ne $Distribution) { throw "Setup-Marker passt nicht zur gewaehlten WSL-Distribution." }
        if ($marker.source_version -ne $ProjectVersion) { throw "WSL-Projektkopie ist nicht auf Version $ProjectVersion. setup.cmd zum Aktualisieren ausfuehren." }
        $checkArguments = @("--distribution", $Distribution, "--cd", [string]$marker.project_linux_path, "--", "bash", "./setup.sh", "--check")
        if ($AppOnly -or ($marker.PSObject.Properties.Name -contains "daq_installed" -and -not $marker.daq_installed)) {
            $checkArguments += "--app-only"
        }
        Invoke-Checked $wsl.Source $checkArguments
        Write-Host "Softwareinstallation geprueft. KrakenSDR, GPS-Maus und USB-Werkzeuge sind fuer Simulation nicht erforderlich."
        if (-not $usbipd) { Write-Host "usbipd-win ist noch nicht installiert. Erst fuer spaeteren lokalen USB-Livebetrieb erforderlich." }
        exit 0
    }
    if (-not $usbipd -and $UsbBusId.Count -gt 0) {
        Install-WingetPackage "dorssel.usbipd-win"
        $usbipd = Resolve-Usbipd
        if (-not $usbipd) { throw "usbipd-win ist installiert, aber noch nicht aufrufbar. Neues Terminal oeffnen und setup.cmd wiederholen." }
    }

    # Selected bus IDs are explicit input. Never bind all generic RTL devices.
    foreach ($entry in $UsbBusId) {
        foreach ($bus in $entry.Split(",")) {
            $bus = $bus.Trim()
            if ($bus -notmatch '^\d+-\d+(\.\d+)*$') { throw "Ungueltige USB-BUSID: $bus" }
            $device = Get-UsbDeviceState $usbipd $bus
            if (-not $device -or -not $device.PersistedGuid) {
                Invoke-Checked $usbipd @("bind", "--busid", $bus)
            }
            if (-not $device -or -not $device.ClientIPAddress) {
                Invoke-Checked $usbipd @("attach", "--wsl", "--busid", $bus)
            } else { Write-Host "USB $bus ist bereits durchgereicht." }
        }
    }

    # Build in WSL's native filesystem instead of sharing a Windows venv.
    $linuxSetupArguments = @("--distribution", $Distribution, "--cd", $ProjectRoot, "--", "bash", "./scripts/setup-wsl.sh")
    if ($AppOnly) { $linuxSetupArguments += "--app-only" }
    Invoke-Checked $wsl.Source $linuxSetupArguments
    $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
    # lsusb can return 1 when WSL exposes no USB devices. Neither hardware nor
    # this diagnostic utility is a prerequisite for a software/simulation setup.
    $devices = @()
    $usbProbeExit = -1
    try {
        $devices = @(& $wsl.Source --distribution $Distribution --exec lsusb 2>$null)
        $usbProbeExit = $LASTEXITCODE
    } catch { $devices = @() }
    $rtlCount = @($devices | Where-Object { [string]$_ -match '(?i)0bda:(2832|2838)' }).Count
    $usbReady = $usbProbeExit -eq 0 -and $rtlCount -ge 5
    $marker | Add-Member -NotePropertyName usb_ready -NotePropertyValue $usbReady -Force
    $marker | Add-Member -NotePropertyName rtl_usb_interface_count -NotePropertyValue $rtlCount -Force
    $marker | Add-Member -NotePropertyName usb_probe_exit_code -NotePropertyValue $usbProbeExit -Force
    $probeMessage = if ($usbReady) { "Fuenf RTL-USB-Interfaces sichtbar; Kohaerenz noch ungeprueft." } elseif ($usbProbeExit -eq 0) { "KrakenSDR nicht vollstaendig an WSL durchgereicht." } else { "Keine USB-Auflistung verfuegbar. Simulation bleibt nutzbar." }
    $marker | Add-Member -NotePropertyName usb_probe_message -NotePropertyValue $probeMessage -Force
    Write-SetupMarker $marker
    if (-not $usbReady) {
        Write-Host "Softwareinstallation erfolgreich. KrakenSDR/GPS sind fuer Simulation nicht erforderlich."
        Write-Host "USB-Hinweis: $probeMessage"
        Write-Host "Ohne Hardware starten: start.cmd --source simulation"
        Write-Host "Der normale automatische Start verwendet bei fehlendem Kraken die gekennzeichnete Simulation."
        if (-not $usbipd) { Write-Host "USB-Livebetrieb spaeter: usbipd-win installieren und die eigenen Geraete an WSL durchreichen." }
        exit 0
    }
    Write-Host "Softwareinstallation abgeschlossen; fuenf RTL-USB-Interfaces sind in WSL sichtbar."
    Write-Host "Live starten: start.cmd. Der Laufzeitcheck prueft anschliessend Geraetezugriff und Kohaerenz."
    Write-Host "VK-162 bei Bedarf ebenfalls an WSL durchreichen; GPS-Port im Webinterface einstellen."
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

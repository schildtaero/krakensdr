# Windows dispatcher. No parameter binder is used here: unknown GNU-style
# application flags must remain literal argv, including --source and --port.
# Only this wrapper's explicit -Mode value is removed from the forwarded list.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MarkerPath = Join-Path $ProjectRoot ".windows-setup.json"
$Mode = "Auto"
$forwarded = New-Object 'System.Collections.Generic.List[string]'
for ($index = 0; $index -lt $args.Count; $index++) {
    $argument = [string]$args[$index]
    if ($argument -ieq "-Mode") {
        $index++
        if ($index -ge $args.Count) { throw "-Mode benoetigt Auto, Wsl oder Native." }
        $Mode = [string]$args[$index]
    } elseif ($argument -imatch '^-Mode:(.*)$') {
        $Mode = $Matches[1]
    } else { $forwarded.Add($argument) }
}
if ($Mode -notin @("Auto", "Wsl", "Native")) { throw "-Mode muss Auto, Wsl oder Native sein." }
$AppArgs = @($forwarded.ToArray())
$versionMatch = [regex]::Match([System.IO.File]::ReadAllText((Join-Path $ProjectRoot "pyproject.toml")), '(?m)^\s*version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) { throw "Projektversion fehlt in pyproject.toml." }
$ProjectVersion = $versionMatch.Groups[1].Value
$requestedSource = if ($env:KRAKENDF_SOURCE) { $env:KRAKENDF_SOURCE } else { "live" }
$requestedDaqMode = if ($env:KRAKENDF_DAQ_MODE) { $env:KRAKENDF_DAQ_MODE } else { "managed" }
for ($index = 0; $index -lt $AppArgs.Count; $index++) {
    $argument = [string]$AppArgs[$index]
    if ($argument -eq "--source" -and $index + 1 -lt $AppArgs.Count) {
        $requestedSource = [string]$AppArgs[$index + 1]
    } elseif ($argument -like "--source=*") {
        $requestedSource = $argument.Substring(9)
    } elseif ($argument -eq "--daq-mode" -and $index + 1 -lt $AppArgs.Count) {
        $requestedDaqMode = [string]$AppArgs[$index + 1]
    } elseif ($argument -like "--daq-mode=*") {
        $requestedDaqMode = $argument.Substring(11)
    }
}
$appOnlyRequested = $requestedSource -in @("simulation", "idle") -or $requestedDaqMode -eq "external"

function Test-NativeRuntime {
    $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    $health = "import sys; assert sys.version_info >= (3,11); import fastapi, uvicorn, numpy, scipy, openpyxl, httpx, serial, krakendf; import importlib.metadata as metadata; assert metadata.version('kraken-df-demo') == sys.argv[1]"
    try {
        & $python -c $health $ProjectVersion 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $null = & $python -m pip check 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

try {
    if ($env:OS -ne "Windows_NT") { throw "Dieser Starthelfer ist fuer Windows." }
    $marker = $null
    if (Test-Path -LiteralPath $MarkerPath) {
        $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
    }
    if ($Mode -eq "Auto") {
        # An existing installation keeps its platform. A first explicit
        # simulation needs only the native Windows app, never WSL USB support.
        $Mode = if ($marker -and $marker.mode -eq "native") { "Native" } elseif ($marker -and $marker.mode -eq "wsl") { "Wsl" } elseif ($requestedSource -in @("simulation", "idle")) { "Native" } else { "Wsl" }
    }
    $needsSetup = -not $marker -or -not $marker.installation_complete -or $marker.mode -ne $Mode.ToLowerInvariant() -or $marker.source_version -ne $ProjectVersion
    if ($Mode -eq "Wsl" -and $requestedSource -eq "live" -and $requestedDaqMode -ne "external" -and $marker -and
        $marker.PSObject.Properties.Name -contains "daq_installed" -and -not $marker.daq_installed) {
        $needsSetup = $true
    }
    if ($Mode -eq "Native" -and -not $needsSetup) {
        Set-Location -LiteralPath $ProjectRoot
        $needsSetup = -not (Test-NativeRuntime)
    }
    if ($needsSetup) {
        $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $setupArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ProjectRoot "setup.ps1"), "-Mode", $Mode)
        if ($Mode -eq "Wsl" -and $marker -and $marker.mode -eq "wsl" -and $marker.distribution) {
            $setupArguments += @("-Distribution", [string]$marker.distribution)
        }
        if ($Mode -eq "Wsl" -and $appOnlyRequested) { $setupArguments += "-AppOnly" }
        & $powershell @setupArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Erstinstallation noch nicht abgeschlossen (Exit $LASTEXITCODE). Die oben genannte Software-/WSL-Meldung pruefen; fehlende USB-Hardware blockiert die Simulation nicht."
        }
        $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
    }
    if ($Mode -eq "Wsl") {
        if (-not $marker.distribution -or -not $marker.project_linux_path) { throw "Ungueltiger WSL-Setup-Marker. setup.cmd wiederholen." }
        & wsl.exe --distribution ([string]$marker.distribution) --cd ([string]$marker.project_linux_path) -- bash ./start.sh @AppArgs
    } else {
        $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $python)) { throw "Windows-venv fehlt. setup.cmd -Mode Native wiederholen." }
        Set-Location -LiteralPath $ProjectRoot
        & $python -m krakendf --daq-mode external @AppArgs
    }
    exit $LASTEXITCODE
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

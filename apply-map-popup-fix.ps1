# One-time UI-only update for an existing KrakenDF installation.
# No dependencies, setup, USB checks or receiver configuration are changed.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
    foreach ($asset in @("map.js", "map.css")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "krakendf\static\$asset") -PathType Leaf)) {
            throw "Das Fix-ZIP zuerst in den bestehenden KrakenDF-Projektordner entpacken. krakendf\static\$asset fehlt."
        }
    }
    $markerPath = Join-Path $ProjectRoot ".windows-setup.json"
    if (Test-Path -LiteralPath $markerPath) {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ($marker.mode -eq "wsl") {
            if (-not $marker.distribution -or -not $marker.project_linux_path) {
                throw "Der vorhandene WSL-Marker enthaelt keine Distribution oder keinen Projektpfad."
            }
            $target = ([string]$marker.project_linux_path).TrimEnd("/") + "/krakendf/static"
            $wslArguments = @("--distribution", [string]$marker.distribution)
            if ($marker.linux_user) { $wslArguments += @("--user", [string]$marker.linux_user) }
            $wslArguments += @("--cd", $ProjectRoot)
            & wsl.exe @wslArguments --exec /usr/bin/test -d $target
            if ($LASTEXITCODE -ne 0) { throw "WSL-Projektordner nicht erreichbar: $target" }
            foreach ($asset in @("map.js", "map.css")) {
                & wsl.exe @wslArguments --exec /bin/cp -- "./krakendf/static/$asset" "$target/$asset"
                if ($LASTEXITCODE -ne 0) { throw "Konnte $asset nicht in die WSL-Installation kopieren." }
            }
            Write-Host "Kartenfix in der WSL-Installation aktualisiert."
        } else {
            Write-Host "Kartenfix im lokalen Windows-Projekt aktualisiert."
        }
    } else {
        throw "Die Datei .windows-setup.json fehlt. Dieses Skript im bereits eingerichteten Projektordner starten."
    }
    Write-Host "Jetzt die Browserseite mit Strg+F5 neu laden. Ein erneutes Setup ist nicht erforderlich."
    exit 0
} catch {
    Write-Host ("Kartenfix fehlgeschlagen: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

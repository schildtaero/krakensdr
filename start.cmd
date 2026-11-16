@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-windows.ps1" %*
set "KRAKENDF_EXIT_CODE=%errorlevel%"
if not "%KRAKENDF_EXIT_CODE%"=="0" (
  echo.
  echo KrakenDF-Start fehlgeschlagen. Exitcode: %KRAKENDF_EXIT_CODE%
  echo Bitte die Fehlermeldung oben kopieren oder fotografieren.
  pause
)
exit /b %KRAKENDF_EXIT_CODE%

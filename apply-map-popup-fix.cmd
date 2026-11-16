@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply-map-popup-fix.ps1"
set "KRAKENDF_FIX_EXIT_CODE=%errorlevel%"
echo.
pause
exit /b %KRAKENDF_FIX_EXIT_CODE%

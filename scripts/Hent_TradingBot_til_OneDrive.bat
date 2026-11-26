@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-local-onedrive-backup.ps1" -TargetDirectory "C:\Users\Emil\OneDrive - Straye AS\AI\tradingbot"
if errorlevel 1 (
  echo.
  echo Kopien ble ikke ferdig. Data paa Render er beholdt.
)
echo.
pause

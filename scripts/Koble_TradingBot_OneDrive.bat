@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0connect-onedrive-v2.ps1"
if errorlevel 1 (
  echo.
  echo Oppsettet ble ikke fullfort. Ingen aktive handelsdata er slettet.
)
echo.
pause

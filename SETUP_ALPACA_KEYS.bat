@echo off
setlocal
cd /d "%~dp0"
title Tradingbot - Alpaca API setup

set "VENV=%CD%\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"

where py >nul 2>nul
if errorlevel 1 (
  echo FEIL: Python Launcher ^(py^) ble ikke funnet.
  echo Installer Python 3 fra python.org og huk av Add Python to PATH.
  pause
  exit /b 1
)

if not exist "%PYEXE%" (
  echo Oppretter lokalt Python-miljo...
  py -3 -m venv "%VENV%"
  if errorlevel 1 goto failed
)

"%PYEXE%" -c "import requests" >nul 2>nul
if errorlevel 1 (
  "%PYEXE%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto failed
)

"%PYEXE%" setup_alpaca.py --configure
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Oppsettet ble ikke fullfort.
pause
exit /b %RC%

:failed
echo FEIL: Klarte ikke sette opp Python-miljoet.
pause
exit /b 1

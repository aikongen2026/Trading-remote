@echo off
setlocal
cd /d "%~dp0"
title Tradingbot Launcher

chcp 65001 >nul 2>nul

echo ==========================================
echo   TRADINGBOT REMOTE 24/7 - LOCAL PC START
echo ==========================================
echo.

where py >nul 2>nul
if errorlevel 1 goto no_python

set "VENV=%CD%\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"

if not exist "%PYEXE%" (
  echo [1/4] Oppretter lokalt Python-miljo...
  py -3 -m venv "%VENV%"
  if errorlevel 1 goto setup_failed
) else (
  echo [1/4] Python-miljo OK.
)

"%PYEXE%" -c "import flask, requests, tzdata, waitress" >nul 2>nul
if errorlevel 1 (
  echo [2/4] Installerer/oppdaterer nodvendige pakker...
  "%PYEXE%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto setup_failed
) else (
  echo [2/4] Pakker OK.
)

if not exist ".env" (
  >.env echo ALPACA_KEY=
  >>.env echo ALPACA_SECRET=
  >>.env echo ALPACA_PAPER=true
  >>.env echo ALPACA_DATA_FEED=iex
)

echo [3/4] Tester Alpaca-tilkobling...
"%PYEXE%" setup_alpaca.py --check
if errorlevel 1 (
  echo.
  echo API-noklene virker ikke. Jeg starter oppsettet na.
  echo Dette er grunnen til 401-feilene i forrige versjon.
  echo.
  "%PYEXE%" setup_alpaca.py --configure
  if errorlevel 1 goto auth_failed
)

findstr /I /R /C:"^ALPACA_PAPER=false$" ".env" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo ADVARSEL: LIVE TRADING er aktivert i .env.
  choice /C JN /N /M "Start LIVE trading? [J/N]: "
  if errorlevel 2 exit /b 0
)

echo [4/4] Starter bot og dashboard...

rem Lukk gamle vinduer med samme tittel for a unnga port-konflikt ved restart.
taskkill /FI "WINDOWTITLE eq TradingBot Engine*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq TradingBot Dashboard*" /T /F >nul 2>nul
timeout /t 1 /nobreak >nul

start "TradingBot Engine" /min cmd /k ""%PYEXE%" "%CD%\trading_bot.py""
start "TradingBot Dashboard" /min cmd /k ""%PYEXE%" "%CD%\control_server.py""

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5050"

echo.
echo KLAR. Lokalt dashboard er apnet.
echo Trykk START TRADING i dashboardet for a aktivere handel.
echo Hvis START-vinduet fortsatt er synlig kan det lukkes.
timeout /t 2 /nobreak >nul
exit /b 0

:no_python
echo FEIL: Python Launcher ^(py^) ble ikke funnet.
echo Installer Python 3 fra python.org og huk av Add Python to PATH.
pause
exit /b 1

:setup_failed
echo FEIL: Klarte ikke sette opp Python-miljoet.
echo Se feilmeldingen over.
pause
exit /b 1

:auth_failed
echo.
echo FEIL: Alpaca godkjente ikke API-noklene, derfor startes ikke tradingmotoren.
echo Kjor SETUP_ALPACA_KEYS.bat nar du har nye PAPER-nokler.
pause
exit /b 2

@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TradingBot - Push til GitHub

echo ==========================================
echo  TRADINGBOT - LAST OPP TIL PRIVATE GITHUB
echo ==========================================
echo.
where git >nul 2>nul
if errorlevel 1 (
  echo FEIL: Git er ikke installert.
  echo Installer Git for Windows, opprett et PRIVAT GitHub-repo, og kjor filen igjen.
  pause
  exit /b 1
)

if not exist ".git" (
  git init
  git branch -M main
)

git add .
git commit -m "Tradingbot remote 24-7" >nul 2>nul

for /f "delims=" %%R in ('git remote get-url origin 2^>nul') do set "HASREMOTE=%%R"
if not defined HASREMOTE (
  echo Opprett forst et TOMT PRIVAT repository pa GitHub.
  echo Kopier HTTPS-adressen, for eksempel https://github.com/bruker/tradingbot-remote.git
  set /p REPOURL="Lim inn GitHub repo URL: "
  if "%REPOURL%"=="" goto missing
  git remote add origin "%REPOURL%"
)

echo.
echo Pusher til GitHub...
git push -u origin main
if errorlevel 1 (
  echo.
  echo Push feilet. GitHub kan be deg logge inn i nettleseren / Git Credential Manager.
  pause
  exit /b 2
)

echo.
echo FERDIG. Repoet er lastet opp uten .env/API-nokler.
echo Neste steg: Render - New - Blueprint - velg repoet.
pause
exit /b 0

:missing
echo Mangler repo URL.
pause
exit /b 1

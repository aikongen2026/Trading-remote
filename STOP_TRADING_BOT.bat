@echo off
setlocal
echo Stopper TradingBot...
taskkill /FI "WINDOWTITLE eq TradingBot Engine*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq TradingBot Dashboard*" /T /F >nul 2>nul
echo Ferdig.
timeout /t 2 /nobreak >nul

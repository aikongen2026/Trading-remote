TRADINGBOT REMOTE 24/7 - START HER
=================================

Denne utgaven kan brukes på TO mater:

A) LOKALT PA PC
---------------
1. Dobbeltklikk START_TRADING_BOT.bat
2. Hvis Alpaca-nokler mangler, lim inn PAPER API Key + Secret nar du blir spurt.
3. Nettleseren apner http://127.0.0.1:5050
4. Trykk START TRADING i dashboardet.
5. STOP_TRADING_BOT.bat stopper de lokale prosessene.

Nar PC-en er avslatt, stopper lokal bot.

B) 24/7 VIA GITHUB + RENDER (ANBEFALT FOR TELEFON)
--------------------------------------------------
Dette er oppsettet der hjemme-PC-en kan vare helt avslatt.
GitHub lagrer koden. Render er serveren som faktisk kjorer tradingmotoren 24/7.
Telefon og PC bruker samme sikre web-app/PWA.

Les DEPLOY_GITHUB_RENDER.md og PHONE_SETUP.md.

VIKTIG SIKKERHET
----------------
- Start alltid med ALPACA_PAPER=true.
- Ikke legg Alpaca API-nokler i GitHub.
- Render-noklene skal settes som Environment Variables / secrets.
- DASHBOARD_PASSWORD skal vare et langt, unikt passord.
- For ekte 24/7 ma Render bruke betalt compute. Gratis web services kan sovne.
- Forste cloud-oppstart er DISARMED/PAUSED. Trykk START TRADING i appen.
- Med persistent disk huskes kontrollstatus gjennom vanlige server-restarts.

MOBILAPP
--------
Dashboardet er en PWA. Etter deploy:
1. Apne Render-URL-en pa telefonen.
2. Logg inn med DASHBOARD_PASSWORD.
3. Trykk Installer app, eller bruk nettleserens Legg til pa hjemskjerm.
4. Deretter ligger TradingBot som et app-ikon pa telefonen.

KONTROLLKNAPPER
---------------
START TRADING: armed=true, paused=false, kill=false
STOPP TRADING: armed=false, paused=true
NODSTOPP: armed=false, paused=true, kill=true

Nodstopp blokkerer nye handler, men lukker ikke automatisk alle eksisterende posisjoner.

FILER
-----
trading_bot.py          selve tradingmotoren
control_server.py       sikkert PC/mobil-dashboard + PWA
cloud_app.py            cloud launcher + watchdog + auto-restart
control.json            strategi/risiko/marked
render.yaml             ferdig Render Blueprint
Dockerfile              alternativ VPS/Docker deploy
docker-compose.yml      alternativ lokal/VPS Docker deploy
.github/workflows/test.yml  automatisk test i GitHub
static/                  PWA manifest, service worker og app-ikoner

KJOR SELF_TEST.bat etter lokale endringer.

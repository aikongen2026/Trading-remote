# Tradingbot 24/7 via GitHub + Render

Dette er den enkleste anbefalte skyveien i denne pakken. GitHub lagrer og versjonerer koden. Render kjører tradingmotoren og dashboardet kontinuerlig.

## 1. Pakk ut ZIP-en

Pakk ut hele `Tradingbot_REMOTE_24_7` til en egen mappe.

**Ikke legg en lokal `.env` med API-nøkler i GitHub.** `.gitignore` er allerede satt opp til å ignorere `.env`.

## 2. Lag et privat GitHub-repository

1. Logg inn på GitHub.
2. Opprett et **Private** repository, f.eks. `tradingbot-remote`.
3. Legg inn alle filene fra denne mappen i roten av repoet.
4. Kontroller at disse filene ligger i roten: `render.yaml`, `cloud_app.py`, `trading_bot.py`, `control_server.py`, `requirements.txt`.
5. Push/commit.

`.github/workflows/test.yml` kjører selvtest automatisk ved push. GitHub Actions er bare test/deploy-hjelp; tradingmotoren kjører ikke i GitHub Actions.

## 3. Opprett Render Blueprint

1. Logg inn på Render.
2. Velg **New -> Blueprint**.
3. Koble til GitHub og velg det private repoet.
4. Render finner `render.yaml` automatisk.
5. Under opprettelsen blir du bedt om hemmelige variabler (`sync: false`). Fyll inn:

   - `ALPACA_KEY` = PAPER API Key ID
   - `ALPACA_SECRET` = PAPER Secret Key
   - `DASHBOARD_PASSWORD` = et langt unikt passord du vil bruke på PC/telefon

`DASHBOARD_SECRET_KEY` genereres automatisk av Render.

## 4. Bruk paid compute for ekte 24/7

`render.yaml` er satt til den minste betalte web compute-profilen (`0.5c-512mb`) og en liten persistent disk.

Dette er med vilje:
- gratis Render web services kan gå i dvale etter inaktivitet
- persistent disk gjør at `control.json`, `status.json` og START/STOP-state overlever restart/deploy
- tradingmotoren skal ikke være avhengig av at en nettleser står åpen

## 5. Vent til deploy er grønn

Når deploy er ferdig får du en URL omtrent som:

`https://tradingbot-remote-xxxx.onrender.com`

Render gjør HTTPS automatisk.

Åpne URL-en på PC først. Du skal få innloggingssiden, ikke selve dashboardet direkte.

## 6. Første start

Første cloud-oppstart er bevisst trygg:

- Engine kjører
- Alpaca kobles til
- Trading er STOPPET/PAUSET

Logg inn og trykk **START TRADING** når status er grønn.

Med persistent disk bevares denne kontrollstatusen gjennom vanlige restarts. Hvis du vil stoppe handel, trykk **STOPP TRADING**. `NØDSTOPP` blokkerer nye handler umiddelbart.

## 7. Senere oppdateringer

Når du endrer boten:

1. Commit/push til GitHub.
2. GitHub-testen kjører.
3. Render auto-deployer den nye versjonen fra repoet.

API-nøklene ligger i Render og trenger ikke ligge i GitHub.

## 8. PC og mobil samtidig

Du trenger ikke kjøre `START_TRADING_BOT.bat` hjemme når Render-versjonen er i drift.

- PC: åpne Render-URL-en i Edge/Chrome.
- Telefon: åpne samme URL og installer den som PWA/app.
- Begge styrer samme bot på samme server.

## 9. Hvis noe går galt

I Render:

- `Logs`: se oppstart og Alpaca-feil
- `/healthz`: Render bruker denne automatisk som health check
- hvis engine-tråden krasjer, `cloud_app.py` har watchdog som prøver å starte den igjen
- ved `AUTH_ERROR` stopper watchdog fra å spamme Alpaca; rett API-nøklene i Render og redeploy

## 10. Live trading senere

Ikke bytt til live før paper-resultatene er dokumentert over mange handler.

Når du eventuelt skal bruke live må `ALPACA_PAPER` endres i Render og LIVE-nøkler brukes. Gjør dette som en bevisst separat endring.

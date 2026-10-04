# Telefonoppsett

Etter at Render-deployen er ferdig har du én HTTPS-adresse til tradingboten. Den samme adressen virker på PC og telefon.

## Android / Chrome eller Edge

1. Åpne Render-URL-en.
2. Logg inn med `DASHBOARD_PASSWORD`.
3. Trykk **Installer app** øverst i TradingBot-dashboardet.
4. Hvis nettleseren ikke viser installasjonsdialog: meny `⋮` -> **Installer app** / **Legg til på startskjermen**.
5. Et TradingBot-ikon legges på hjemskjermen.
6. Åpne ikonet og trykk **START TRADING**.

## iPhone / Safari

1. Åpne Render-URL-en i Safari.
2. Logg inn.
3. Trykk Del-knappen.
4. Velg **Legg til på Hjem-skjerm**.
5. Bekreft navnet TradingBot.
6. Åpne ikonet og trykk **START TRADING**.

## Hva knappene gjør

- **START TRADING**: starter/aktiverer strategien uten å starte en ny serverprosess.
- **STOPP TRADING**: pauser og disarmer boten. Serveren forblir online.
- **NØDSTOPP**: blokkerer nye handler med `kill=true`.
- **AUTO 24/7**: krypto kontinuerlig + amerikanske aksjer i støttede sessions.

## Viktig

Telefonen trenger ikke stå på. Appen er bare fjernkontrollen. Tradingmotoren kjører på Render-serveren selv om telefon og PC er avslått.

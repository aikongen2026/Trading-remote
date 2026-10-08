# TradingBot Research Edge V4.2

Remote Alpaca paper-trading bot med enkelt PC/mobil-dashboard, intelligence/expected-value-lag og ny ordrekoordinator.

V4.2 beholder V4/V4.1-funksjonene og legger særlig til:

- vedvarende én-exit-per-symbol koordinering
- `client_order_id` og avstemming før retry etter timeout
- delutførelses-sikkerhet
- restart-sikker exit-state
- signal-reset før ny inngang etter avsluttet handel
- kort bekreftelse på myke signal/intelligence-exits for å redusere buy/sell-churn
- broker-side bracket TP/SL i ordinær aksjehandel
- dashboard-status for ordresikkerhet
- Alpaca portfolio-equity som grunnlag for resultatpanelet

## Deploy/update

`START_TRADINGBOT.bat` i nedlastingspakken kopierer `UPLOAD_TO_GITHUB` til eksisterende GitHub-repo, pusher `main`, venter på riktig `VERSION`, og åpner dashboardet. Render Environment beholder Alpaca-nøkler/passord og de blir ikke lagt i GitHub.

## Viktig

PAPER bør brukes til nok data viser positiv forventning etter spread, slippage og gebyrer. Ingen strategi garanterer gevinst.

# TradingBot Intelligence V3

Remote 24/7 Alpaca paper-trading bot with a deliberately simple PC/mobile dashboard.

V3 adds an intelligence/veto layer on top of the existing deterministic execution engine:

- Alpaca real-time news WebSocket + REST fallback
- Alpaca stock/crypto live data streams where the account feed allows them
- GDELT global geopolitical/event radar
- Federal Reserve, BLS and EIA official feeds
- BLS scheduled macro-event blackout around high-impact releases
- SEC 8-K / 10-Q / 10-K radar for watched stocks
- Cross-market regime using SPY, QQQ, IWM, XLE, GLD, TLT, HYG, USO, UUP and VIXY
- Multi-timeframe trend/momentum, relative strength and live microstructure confirmation
- Cost/spread/liquidity and portfolio concentration vetoes
- Per-symbol Intelligence Score 0-100
- SQLite learning journal with 5/15/60-minute outcomes for later walk-forward validation

The intelligence layer **does not place orders directly**. It scores and can veto candidates; the risk/execution engine remains the final gate.

## Deploy

This repository is linked to Render. By default, Render auto-deploys when `main` changes.
Existing Alpaca keys and dashboard password stay in Render Environment and must never be committed to GitHub.

## Important

No trading system can predict markets with certainty or guarantee profit. Keep PAPER enabled until the strategy has enough out-of-sample data to demonstrate positive expectancy after spread, slippage and fees.

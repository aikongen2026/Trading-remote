# TradingBot Research Edge V4

Remote Alpaca paper-trading bot with a deliberately simple PC/mobile dashboard and a research-backed intelligence + expected-value layer.

V4 keeps the same controls, while the engine adds:

- Alpaca real-time news and live market streams with REST fallbacks
- GDELT + Fed + BLS + EIA + SEC event intelligence
- market regime, multi-timeframe trend and relative strength
- medium-horizon industry/residual momentum and momentum-crash guard
- live bid/ask microstructure / order-flow confirmation
- volatility-adaptive take-profit / stop-loss
- estimated probability + expected value after spread, slippage and fees
- volatility/risk-based position sizing and portfolio group limits
- broker-side bracket TP/SL for regular-hours stock entries
- persistent SQLite learning journal with 5/15/60-minute outcomes
- conservative live-validation gate to reduce the risk of backtest/selection overfitting

The intelligence/profit layers never bypass the deterministic risk gates. A BUY signal can still become WAIT_INTEL, WAIT_EDGE, WAIT_VALIDATION, SPREAD_TOO_WIDE, RISK_BLOCKED, etc.

## Deploy/update

The repository is linked to Render. `START_TRADINGBOT.bat` in the downloadable package copies this folder into the existing GitHub repo, pushes `main`, waits for Render to serve the matching VERSION, then opens the dashboard.

Alpaca keys and dashboard password remain in Render Environment and are not committed to GitHub.

## Important

No trading system predicts markets with certainty or guarantees profit. PAPER mode should remain enabled until enough out-of-sample observations demonstrate positive expectancy after spread, slippage and fees.

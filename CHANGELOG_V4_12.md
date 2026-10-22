# TradingBot V4.12.0 — Sector Rotation + Crypto Tiering

Paper-only release based on the latest 3-day audit and Alpaca order cross-check.

## Evidence-driven changes
- Prefer trend logic over defensive scalp fallback in risk-off conditions.
- Make dynamic universe sector momentum directional for a long-only engine.
- Add explicit sector ETF benchmarks: XLK, XLF, XLI, XLV, XLY, XLP, XLC, XLU, XLB, XLE.
- Add missing sector ETFs to the tradable universe.
- Crypto Tier 1: BTC/USD, ETH/USD, SOL/USD.
- Tier 2 crypto requires higher intelligence score, higher EV, one extra confirmation, and half notional.
- Persist tier-adjusted crypto sizing in trade plans.
- Prefer /var/data/tradingbot on Render so journals survive redeploys/restarts.
- Normalize Render metrics timestamps to RFC3339 Z to avoid API parser errors.

## Paper-only safety
- render.yaml keeps ALPACA_PAPER=true.
- Adds TRADINGBOT_PAPER_ONLY=true.
- V4.12 refuses to start the trading engine against Alpaca live endpoints when paper-only mode is enabled.

## Preserved
- Render Usage Guard
- bracket/risk supervision
- START/STOP/NØDSTOPP
- existing equity/crypto execution path
- no options execution

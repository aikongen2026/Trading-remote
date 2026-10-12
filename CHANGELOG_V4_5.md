# TradingBot V4.5 - Adaptive Edge

V4.5 builds on V4.4.2 and keeps the same simple dashboard and one-click cloud updater.

## New in V4.5

- Best-available Alpaca stock feed: probes SIP automatically and falls back to IEX when the account is not entitled. Existing Render installs that still contain `ALPACA_DATA_FEED=iex` are upgraded to automatic selection unless `ALPACA_DATA_FEED_AUTO=false` is explicitly set.
- SIP-aware stock WebSocket: all configured stock symbols are streamed when SIP is available; Basic/IEX remains capped to a liquid 30-symbol live core with REST coverage for the remaining universe.
- Enhanced Order Flow Imbalance: top-of-book bid/ask size changes, normalized OFI acceleration, quote momentum and spread quality are added to the microstructure score.
- Adaptive strategy router: each stock can be routed automatically to trend, mean-reversion, event/hype or scalp logic based on market regime, symbol news, risk and medium-horizon factor context.
- Mean-reversion signal: only used in neutral regimes and requires an oversold deviation plus a reversal confirmation before a long signal.
- Dynamic universe: all configured stocks remain monitored, but only the strongest opportunity subset is eligible for new entries. Existing positions are always managed. The active universe shrinks automatically when global risk is high.
- Explicit sector confirmation: sector momentum and the appropriate benchmark (QQQ/XLE/SPY) provide an additional long-entry confirmation/veto.
- Champion/Challenger shadow lab: three challenger gates run in shadow beside the champion. They never place orders. Their 15-minute outcomes are stored in `learning_v3.sqlite` for later comparison.
- Better empirical probability calibration: historical BUY decisions are weighted by both Intelligence score and the probability the model actually predicted. A reliability/Brier snapshot is exposed in status.
- AI analysis export v3: the single uploadable AI ZIP now includes Champion/Challenger raw shadow records and summary performance.
- New broker client-order revision tag: new orders contain `v450`, allowing later audits to separate V4.5 trades from older revisions.

## Important

None of the new challenger models can place an order. Only the existing deterministic champion path can trade, after Intelligence, sector, EV, risk, cooldown, spread and execution guards have all passed.

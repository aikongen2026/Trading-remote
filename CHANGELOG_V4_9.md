# TradingBot V4.9.0 - Alpaca Edge 24x7

Built on V4.8.2 persistent-edge-v2.

## Alpaca-native improvements

- Preserves Alpaca `group_id` and `group_role` on bracket/OCO/OTO orders and trade updates.
- Adds a realtime Alpaca News WebSocket sidecar (`v1beta1/news`) with a bounded, fast-decaying signal boost.
- Adds a Corporate Action Guard using Alpaca Market Data API. Severe actions near the trade date veto new entries; ex-date dividends are also guarded.
- Adds an Alpaca v3 NYSE/BOATS clock and NYSE calendar cross-check while preserving the legacy v2 clock shape consumed by the existing trading loop.
- Adds a centralized REST soft budget and 429 telemetry to reduce avoidable API throttling.
- Adds a persistent `alpaca_edge_v49.sqlite` event recorder for decisions, trade updates, realtime news, session state and overnight shadow observations.
- Adds an overnight/BOATS shadow collector. It records evidence only and never submits overnight orders.
- The new recorder is automatically included in the existing StateBackup checkpoint list at runtime.
- SIP auto-detection remains enabled. If the Alpaca account has SIP permission the existing bot may use it; otherwise IEX remains the fallback.

## Existing V4.8.2 logic retained

- TP-before-SL probability gate and cost-aware break-even gate.
- Two-stage stock entry confirmation.
- Symbol/group loss cooldown.
- Soft-exit consensus and broker bracket protection.
- Broker-persistent entry fingerprints.

## Safety

This release does not enable LIVE trading automatically. It preserves the account's existing Paper/Live configuration and never stores API secrets in the repository or audit databases.

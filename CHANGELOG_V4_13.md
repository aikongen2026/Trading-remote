# TradingBot V4.13.0 — Saxo SIM Norway

Paper-only Norway expansion.

## Safety
- Alpaca remains hard-locked to PAPER by TRADINGBOT_PAPER_ONLY=true.
- Saxo adapter is hard-coded to the Saxo SIM OpenAPI gateway.
- LIVE Saxo endpoints are refused by the adapter.
- Norway module is disabled by default until SIM credentials are supplied.
- Norway module pauses already at Render 60% bandwidth usage.
- Independent Norway traffic budgets: 12 MB/day and 250 MB/month by default.

## Norway SIM
- Separate side engine; Alpaca PAPER remains independent.
- Initial liquid Oslo universe: EQNR, DNB, KOG, AKRBP, NHY, MOWI, YAR, TEL, ORK, TOM, SALM, SUBC.
- 120-second quote/position cycle.
- 10-minute chart refresh.
- 5-minute trend data.
- Small default position size and max 3 simultaneous Norway SIM positions.
- Status shown in dashboard as Norge SIM.

## Important
SAXO_SIM_ENABLED remains false until Saxo SIM credentials are configured in Render.

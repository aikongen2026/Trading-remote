# 4.17.0 – PAPER research and bounded resources

The account's positive seven-day equity change was not closed-trade profit:
the supplied 18–25 November export had zero fills and one manual AEP position.
The release keeps the existing PAPER order routing, risk thresholds, stop orders,
Saxo SIM restrictions, authentication and Render plan/disk size.

## Runtime improvements

- Prefer valid, fresh existing stream quotes. REST recovers missing symbols only;
  a REST failure does not discard the other fresh stream prices.
- Use the existing minute-bar stream to fill the production candle cache. Forming
  bars are excluded, original timestamps retained, and a cold cache still gets
  its initial REST history. A dead stream cannot suppress REST recovery.
- Compact, gzip and conditionally revalidate dashboard status. The supplied
  status snapshot measured 102,994 uncompressed bytes versus 13,497 bytes with
  compact gzip (86.9% reduction). This measures a payload, not monthly savings.
- Do not duplicate observational decision snapshots in the second SQLite journal.
  Entry/exit records and broker history remain intact.
- Count state files, SQLite WAL and snapshots against a local 850 MiB budget,
  preserve 96 MiB filesystem headroom, suppress optional telemetry near the
  ceiling and block new entries before essential state runs out of space.
- Check snapshot staging headroom before copying. Stream compression avoids
  loading a full SQLite file and compressed copy into RAM simultaneously.
- Display `KJØP SPERRET` when the bandwidth/disk guard blocks new entries.
  Label account equity change separately from strategy results.

## Strategy workshop

A collapsed PAPER workshop compares fixed, explicit hypotheses using existing
closed BTC/ETH/SOL minute candles. The bounded cache holds at most 4,096 candles
per symbol and a 2 MiB compressed file. No new subscriptions, market-data
downloads, databases or services are created. Computation runs on demand,
at most once per 15 minutes and never submits orders or changes controls.

- 5m and 15m signals; trend strategies require a completed rising 1h EMA50.
- SMA reference, trend pullback, Donchian breakout and range reversion.
- ADX trend/range filter, relative-volume confirmation, ATR risk geometry.
- Compare market and resting-limit models with missed fills; limit orders do
  not automatically receive cheaper maker fees.
- Compare fixed targets and ATR trailing. Both use the same cost assumptions.
- Chronological 70/30 split, fixed parameters, no future higher-timeframe bars,
  next-minute entries, stop-first resolution of ambiguous candles and no
  invented results where a price path is missing.
- Holdout net expectancy, profit factor, drawdown and sample count. Unit-notional
  research drawdown is not account drawdown. Results never auto-promote a model.
- A local broker-activity review distinguishes exactly attributed bot trades
  from manual/unattributed FIFO legs. Gross and unverified net remain separate.
- Trade-plan diagnostics expose gross expectation, estimated costs, required
  net edge and the unvalidated nature of the TP-before-SL probability prior.
  EV thresholds and risk sizing are not relaxed to force more trades.

The seven-day export does not include enough continuous crypto OHLCV to run
the new strategies retrospectively. The workshop honestly shows WAIT_HISTORY
until its existing feed has accumulated sufficient data; it does not infer
candles from sparse decision prices.

## TradeSkill review

Reviewed the public feature overview, simulator, journal, replay, learning
library, strategy rules, session-liquidity playbook, order-flow tools and risk
guides. These are educational hypotheses, not established profitable systems.
Relevant source pages:

- https://www.thetradeskill.com/
- https://www.thetradeskill.com/learn/library
- https://www.thetradeskill.com/learn/library/trading-strategies/trend-pullback-strategy
- https://www.thetradeskill.com/learn/library/trading-strategies/donchian-channel-breakout-strategy
- https://www.thetradeskill.com/learn/library/trading-strategies/range-mean-reversion-strategy
- https://www.thetradeskill.com/learn/session-liquidity-playbook
- https://www.thetradeskill.com/trading-replay
- https://www.thetradeskill.com/free-order-flow-heatmap
- https://www.thetradeskill.com/free-footprint-chart
- https://www.thetradeskill.com/free-cvd-indicator

Binance futures heatmaps, liquidation estimates and true footprint/CVD require
different venue data. They are not fabricated from Alpaca candles and no heavy
external feed is added under the Render resource limit. Existing top-of-book
confirmation remains available. TradeSkill replay does not fill historical
orders, and its displayed 0.05% taker fee is not substituted for Alpaca's 0.25%.

## Validation

86 standard-library unit tests, 61 existing OAuth/export/backup/stability tests,
entry/exit safety checks, service-worker checks, JavaScript parsing and critical
Python lint. The legacy `v47_test.py` additionally has two failures on the
unchanged upstream base (old 18-second confirmation and crypto EV assumptions);
this release does not weaken current guards to satisfy those stale assumptions.

An already-used monthly bandwidth allowance cannot be restored by a deploy.
The guard stays active; exceeding the quota is not hidden or reset locally.
Only Trading-remote is changed. Other workspace services and a Windows-PC
migration require their own deployment work; none are silently moved.

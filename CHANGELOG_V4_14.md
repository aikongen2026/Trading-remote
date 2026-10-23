# TradingBot V4.14 Stability/Guard

- Preserve ALPACA_PAPER=true and TRADINGBOT_PAPER_ONLY=true in the Render blueprint. Refuse live Alpaca clients and live REST endpoints regardless of environment overrides. Never retry ambiguous order mutations.
- Keep Saxo fixed to SIM, refuse redirected requests, confirm the configured account against Saxo, and show state, connection, resolved/requested symbols, positions, daily/monthly traffic and last successful poll in the dashboard. No resolved instruments means NO_RESOLVED_SYMBOLS, not RUNNING_SIM.
- Emit SAXO_SIM startup, state transition/error and successful poll summaries in Render logs. Keep the 60% Norway traffic cutoff and persist Saxo byte counters across restarts.
- Remove unsupported cas_region. Both corporate-action collectors share a circuit with one recovery probe, no immediate retries, 15-minute minimum failure delay, exponential backoff up to two hours, and Retry-After support. Preserve previously collected corporate-action vetoes on failure. Failed main-loop refreshes are also scheduled no more than once per 15 minutes.
- Main-loop heartbeat logs are limited to once per minute; status files remain current. Local cooldown refusals do not consume the shared REST call budget or inflate HTTP 429 counts.
- General Alpaca HTTP 429 responses open an endpoint cooldown for at least 60 seconds and respect Retry-After. Definitive HTTP failures are not retried.
- Recognize resolved descendants of the blueprint disk mount /var/data on Render even where ismount is unreliable. Unrelated directories still produce the ephemeral warning.

Validation: new offline regression suite, execution safety suite, Python compilation, targeted fatal-error lint, and dashboard JavaScript syntax check. Linux CI also checks disk detection. Full legacy discovery has the same five failures on unchanged main (v442/v45 revision-tag assumptions, two confirmation-timing assumptions, and crypto edge-threshold assumption); no new legacy failures were introduced.

No real orders are used for validation. Deployment verification uses dashboard/status/health reads only. Render Usage Guard requires a valid RENDER_API_KEY for automatic metering; a missing key is reported as UNMETERED rather than zero usage.

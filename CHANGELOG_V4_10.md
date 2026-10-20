# TradingBot V4.10.0 - Smart Edge

Built on V4.9.0 Alpaca Edge 24x7. Dashboard layout, buttons, controls, and user workflow are unchanged.

## What changed

- Stronger bracket-first exit discipline for stock positions:
  - broker bracket protection remains primary;
  - discretionary soft exits on bracket-protected positions now require at least 5 minutes hold time;
  - soft exits on bracket-protected positions require 3 independent exit votes before protection can be cancelled.
- Decision/audit linkage is extended with:
  - entry_client_order_id
  - Alpaca group_id
  - Alpaca group_role
- Broker history archives Alpaca order group fields directly.
- Intelligence now computes data-source confidence from Alpaca News, GDELT, Fed, BLS, EIA, SEC, market-regime and daily-factor health.
  - external news/macro contributions are automatically reduced when feeds are reconnecting or unavailable;
  - technical/price signals keep operating instead of treating missing news as genuine neutral news.
- High-impact Alpaca corporate actions are checked before fresh stock entries.
- Existing Alpaca V4.9 sidecar remains active for:
  - realtime Alpaca News;
  - corporate-action veto;
  - REST rate-budget telemetry;
  - market clock/calendar cross-checks;
  - persistent Alpaca audit recording;
  - overnight shadow observations.
- Existing persistent Render disk and evidence database backup logic are preserved.

## Why

The latest one-day paper analysis had only four completed round trips, so this release deliberately avoids aggressive strategy retuning from a tiny sample. It improves evidence quality, protection hierarchy and learning reliability first.

## Safety

- Existing dashboard HTML/CSS/layout was not modified by this release.
- No API keys or secrets are stored in GitHub.
- Paper/live mode is not changed.
- Existing START/STOP/NØDSTOPP behavior is retained.

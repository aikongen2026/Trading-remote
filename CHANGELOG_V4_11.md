# TradingBot V4.11.0 - Render Usage Guard + Liquid ETF Expansion

Built on V4.10.0 Smart Edge. Existing stock/crypto execution, bracket protection,
risk supervision, START/STOP/NØDSTOPP behavior and paper/live setting are preserved.

## Render cost protection

- Adds a read-only Render bandwidth monitor using the official Render API.
- Aggregates bandwidth across every Render service visible to the configured API key,
  because included bandwidth is workspace-level.
- Default monthly bandwidth cap is 5 GB.
- Automatic stages:
  - 60% WARN
  - 75% SAVE
  - 85% STRONG
  - 95% HARD
  - 100% OVER
- SAVE/STRONG/HARD progressively reduce engine polling and bar refresh traffic.
- HARD/OVER blocks NEW stock and crypto entries while continuing supervision and exits
  for already-open positions.
- The dashboard refresh interval is reduced from a fixed 2.5 seconds to an adaptive
  10-60 seconds depending on bandwidth state.
- Dashboard shows current Render data usage and cap.
- Usage is refreshed every 15 minutes by default.

## Why the bot does NOT auto-downgrade Render to Free

The current deployment uses a persistent Render disk for trading state, evidence databases
and recovery. Render Free web services do not support persistent disks. Automatically
switching the service to Free could therefore break persistence and unattended protection.
V4.11 fails safe by reducing traffic and blocking new entries before overage instead.

## Required Render secret

Add this secret in Render Environment:

- RENDER_API_KEY

No Render API key is stored in GitHub. Without it, trading still works, but the dashboard
shows Render usage as not connected and automatic bandwidth metering is disabled.

## New equity instruments

The existing stock engine now also includes these Alpaca-tradable liquid ETFs:

- IWM
- DIA
- SMH
- XLK
- XLF
- XLI
- GLD
- TLT

These use the existing equity execution/risk path. No options trading is enabled by this
release.

## Safety

- No automatic Render plan mutation.
- No automatic live/options enablement.
- New-entry blocking is separate from pause/kill so open positions continue to be supervised.
- Existing custom symbols are preserved during migration.

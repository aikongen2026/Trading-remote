# 4.18.0 — OneDrive archive offload and bounded daily backups

Target the user's verified Straye OneDrive folder AI/tradingbot, with Archives
and Backups subfolders. Existing unrelated files are untouched.

- Offload strict, verified Edge observation archives plus manifests. Stream-read
  remote copies with rclone check --download before writing a durable receipt
  and deleting only the local compressed archive copy. Never upload credentials,
  controls or active execution state. Failures retain local archives.
- Replace the 64 MiB in-memory database snapshot limit on the rclone path with
  integrity-checked SQLite snapshots in ephemeral working space and streaming
  gzip/hash. Supports the existing 333 MiB learning DB without loading it into
  Python RAM. Active source databases remain read-only and are never removed.
- Full backups run daily, with hourly retry backoff. OneDrive uploads reserve
  bandwidth before sending, stop below the trading guard, and are capped at
  150 MiB/day on Render. Metering must be current and verified.
- One-time Windows setup saves its own launcher under the requested OneDrive
  directory and pairs directly with Render over HTTPS after Microsoft sign-in.
  Tokens and the dashboard password never go through ChatGPT.

PAPER settings, broker orders, strategies, Render package/disk size and risk
thresholds are unchanged. No upload occurs until the user completes sign-in.

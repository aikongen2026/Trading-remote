# 4.17.1 — recover space within the existing 1 GB Render disk

The legacy Edge database (495 MiB) plus learning (333 MiB) and audit data
already exceeded the 850 MiB state budget. Stopping new duplicate writes did
not reclaim existing SQLite pages, so the disk guard continued to block buys.

- Before any startup writer opens the Edge database, stream old non-execution
  DECISION/SNAPSHOT rows into a gzip JSONL archive with columns, row count and
  SHA-256 manifest. Preserve the complete original rows, including timestamps
  and IDs; verify the archive by reading it back.
- Build a smaller SQLite database; verify all retained rows, indexes and
  integrity before an atomic replacement. Preserve entry/exit decisions,
  anything carrying order IDs, broker updates, news and overnight data.
- On space pressure, locked databases or verification failure, keep the
  original and leave the disk guard active. Never resize the disk or change
  Render billing, PAPER settings, strategy, position ownership or risk limits.
- Log disk use/headroom/blocking with each heartbeat. Optional Edge news and
  overnight recording now follow the same reserve as other optional telemetry.

Validation: archive reconstruction, execution/news/index/sequence preservation,
idempotence, low disk space, corrupt verification, unknown schema, concurrent
WAL writer and startup order; existing safety and unit checks.

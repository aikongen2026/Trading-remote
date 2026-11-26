# 4.17.3 — archive immutable session observations too

The legacy archive's safety reserve still stopped staging while retaining
non-decision Edge events. Historical SESSION_CLOCK_V3 and SESSION_CALENDAR_V3
observations are now moved to the same lossless, verified archive. Live session
authority is fetched/cached independently and never restored from that journal.
Broker updates, corporate-action events and execution records remain in SQLite.
Startup diagnostics now report per-kind row counts and payload sizes.

96 unit tests, including complete session-observation archive reconstruction.

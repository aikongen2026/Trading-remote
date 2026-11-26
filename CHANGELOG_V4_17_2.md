# 4.17.2 — include pre-event-type observational records in the archive

The first live archival attempt safely deferred on its working-space reserve:
the original database remained in place and no rows were lost. Older Edge
records can have a null/missing event_type. Explicit legacy WAIT/HOLD and
other non-execution observation actions are now eligible for the same verified,
lossless archive. Unknown actions, BUY/SELL/EXIT actions, order IDs and explicit
entry/exit event types remain in SQLite.

Maintenance diagnostics identify the current table, archived/retained row counts
and whether the staging reserve was reached, without logging account payloads.
95 unit tests pass, including legacy observations versus execution preservation.

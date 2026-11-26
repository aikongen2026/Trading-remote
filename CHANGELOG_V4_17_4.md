# 4.17.4 — bound the historical overnight shadow table

Live diagnostics showed all 220,569 eligible event observations could be staged,
but 15,616 historical overnight shadow rows exhausted the reserve. Keep the latest
shadow row per symbol in SQLite and archive older immutable observations in full.
This research-only table never owns positions or drives the live session clock.

The gzip archive now identifies each row's source table; its manifest lists each
table's exact columns and moved row counts. Every retained row and archived byte
is still verified before replacing the database. Orders, broker history, learning,
credentials, strategy, PAPER settings, risk guards and Render disk size stay intact.

97 unit tests, including multi-table archive reconstruction and latest-shadow
retention. If working space is insufficient the original database still wins.

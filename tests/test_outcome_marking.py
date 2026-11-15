"""Regression tests: learning labels come from historical completed candles only."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from outcome_marking import bar_marks, horizon_price
from intelligence_engine import SignalJournal
from adaptive_edge import ShadowLab

UTC = timezone.utc


class CandleOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(UTC)
        self.entry_ts = self.now - timedelta(minutes=65)
        self.entry = self.entry_ts.isoformat()

    def candles(self):
        # Returned timestamps represent the *start* of one-minute bars.
        return [
            {"t": (self.entry_ts + timedelta(minutes=5) - timedelta(seconds=60)).isoformat(), "c": 101},
            {"t": (self.entry_ts + timedelta(minutes=15) - timedelta(seconds=60)).isoformat(), "c": 102},
            {"t": (self.entry_ts + timedelta(minutes=60) - timedelta(seconds=60)).isoformat(), "c": 103},
        ]

    def test_all_horizons_marked_at_correct_completed_candle(self):
        marks = bar_marks(self.candles(), self.now)
        self.assertEqual(horizon_price(self.entry, 5, marks), 101)
        self.assertEqual(horizon_price(self.entry, 15, marks), 102)
        self.assertEqual(horizon_price(self.entry, 60, marks), 103)

    def test_missing_bar_is_not_fabricated_by_later_quote(self):
        marks = bar_marks([
            {"t": (self.entry_ts + timedelta(minutes=27)).isoformat(), "c": 152}
        ], self.now)
        self.assertIsNone(horizon_price(self.entry, 15, marks))

    def test_incomplete_bar_cannot_label_signal(self):
        future_bar = [{"t": (self.now-timedelta(seconds=20)).isoformat(), "c": 10000}]
        self.assertEqual(bar_marks(future_bar, self.now), [])

    def test_journal_settles_rejected_candidate_instead_of_only_buys(self):
        path = str(Path(self.tmp.name) / "learning_v3.sqlite")
        journal = SignalJournal(path)
        journal.log({
            "ts": self.entry, "symbol": "BTC/USD", "market": "CRYPTO",
            "price": 100.0, "signal": 1, "action": "WAIT_EDGE:-25bp",
            "event_type": "SNAPSHOT", "composite": 72.0,
        })
        journal.update_outcomes("BTC/USD", 103.0, self.candles())
        with sqlite3.connect(path) as db:
            row = db.execute("select outcome_5m,outcome_15m,outcome_60m from decisions").fetchone()
        self.assertAlmostEqual(row[0], 0.01)
        self.assertAlmostEqual(row[1], 0.02)
        self.assertAlmostEqual(row[2], 0.03)

    def test_shadow_model_settles_from_time_aligned_candle(self):
        lab = ShadowLab(self.tmp.name)
        lab.observe(symbol="BTC/USD", price=100., signal=1,
                    intel={"score": 76.0}, plan={
                        "probability_up": 0.61, "expected_value_bps": 15.0},
                    router_mode="trend", sector={"ok": True, "score": 1},
                    champion_allowed=True, bot_version="test",
                    min_interval_sec=0)
        # Change observation to have a matured timestamp without changing price.
        with sqlite3.connect(lab.path) as db:
            db.execute("update shadow_signals set ts=?", (self.entry,))
            db.commit()
        lab.update_outcomes("BTC/USD", 103.0, self.candles())
        with sqlite3.connect(lab.path) as db:
            vals = db.execute("select outcome_15m from shadow_signals where model='champion_gate'").fetchall()
        self.assertEqual(len(vals), 1)
        self.assertAlmostEqual(vals[0][0], 0.02)


if __name__ == "__main__":
    unittest.main()

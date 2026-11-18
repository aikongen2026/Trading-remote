"""Regression checks for reliable strategy selection in the dashboard.

Tests deliberately do not start the trading engine or access any broker.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control_server
import trading_bot


class StrategySelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "control.json"
        self.status_path = Path(self.temporary.name) / "status.json"
        self.patcher = patch.multiple(
            control_server, CONTROL_PATH=self.path, STATUS_PATH=self.status_path
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.path.write_text(
            '{"strategy_mode":"scalp","crypto_strategy_mode":"scalp",'
            '"activity_profile":"active","armed":true,"paused":false}',
            encoding="utf-8"
        )
        # The status intentionally lags behind the newly selected control.
        self.status_path.write_text(
            '{"engine_state":"RUNNING","strategy_mode":"scalp",'
            '"crypto_strategy_mode":"scalp"}',
            encoding="utf-8"
        )
        self.client = control_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["auth"] = True

    def test_strategy_post_and_status_read_persisted_choice(self):
        result = self.client.post(
            "/api/control", json={
                "strategy_mode":"trend", "crypto_strategy_mode":"trend"
            }
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["strategy_mode"], "trend")
        self.assertEqual(result.json["crypto_strategy_mode"], "trend")
        self.assertTrue(result.json["armed"])
        self.assertFalse(result.json["paused"])
        self.assertEqual(control_server.safe_read_json(self.path, {})["strategy_mode"], "trend")
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["selected_strategy_mode"], "trend")
        self.assertEqual(status.json["selected_crypto_strategy_mode"], "trend")
        self.assertEqual(status.json["strategy_mode"], "scalp")  # older heartbeat

    def test_profile_settings_do_not_override_saved_strategy(self):
        self.client.post("/api/control", json={
            "strategy_mode": "cross", "crypto_strategy_mode": "cross"
        })
        response = self.client.post("/api/control", json={
            "activity_profile": "balanced",
            "intelligence_entry_score": 67
        })
        self.assertEqual(response.json["strategy_mode"], "cross")
        self.assertEqual(response.json["crypto_strategy_mode"], "cross")

    def test_dashboard_code_does_not_reset_strategy_or_undo_user_edits(self):
        page = control_server.HTML
        self.assertIn("strategyChoiceDirty=true", page)
        self.assertIn("s.selected_strategy_mode||s.strategy_mode", page)
        self.assertNotIn("strategy_mode:'scalp',fast_sma", page)
        self.assertNotIn("crypto_strategy_mode:'scalp',crypto_cooldown_sec", page)
        self.assertNotIn("document.getElementById('strategySel').value=s.strategy_mode||'scalp'", page)
        engine_source = Path(trading_bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "safe_write_json(CONTROL_PATH, control)\n            except Exception:",
            engine_source
        )


if __name__ == "__main__":
    unittest.main()

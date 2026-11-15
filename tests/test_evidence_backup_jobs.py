"""Async PAPER backup job guard tests (no network or Render activity)."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from evidence_backup_jobs import BackupJobManager
from local_backup_export import EVIDENCE_FILES


class BackupJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mockenv = patch.dict(os.environ, {"ALPACA_PAPER": "true"})
        self.mockenv.start()
        self.addCleanup(self.mockenv.stop)
        for name in EVIDENCE_FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table evidence (x integer)")
                db.execute("insert into evidence values (1)")
        self.manager = BackupJobManager()
        self.meter(2.97)

    def meter(self, used, source="render_api"):
        (self.root / "render_usage.json").write_text(json.dumps({
            "source": source, "used_gb": used, "cap_gb": 5,
            "checked_at": datetime.now(timezone.utc).isoformat()
        }))

    def test_async_build_ready_download_and_cleanup(self):
        result, code = self.manager.start(self.root)
        self.assertEqual(code, 202)
        self.assertEqual(result["phase"], "building")
        for _ in range(100):
            status = self.manager.status()
            if status["phase"] != "building":
                break
            time.sleep(0.02)
        self.assertEqual(status["phase"], "ready")
        self.assertTrue(status["zip_sha256"])
        with self.manager.lock:
            job, err = self.manager.begin_download(self.root)
        self.assertFalse(err)
        self.assertEqual(job["job_id"], status["id"])
        self.assertEqual(job["handle"].read(2), b"PK")
        self.manager.finish_download(job["job_id"])
        self.assertEqual(self.manager.status()["phase"], "idle")

    def test_budget_low_blocks_before_any_archiving(self):
        self.meter(4.96)
        result, code = self.manager.start(self.root)
        self.assertEqual(code, 429)
        self.assertEqual(result["error"], "render_bandwidth_budget_low")
        self.assertEqual(self.manager.status()["phase"], "idle")

    def test_metering_stale_or_unverified_fails_closed(self):
        self.meter(2, source="render_api_stale")
        result, code = self.manager.start(self.root)
        self.assertEqual(code, 503)
        self.assertEqual(result["error"], "render_metering_not_verified")

    def test_paper_only(self):
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            result, code = self.manager.start(self.root)
        self.assertEqual(code, 403)
        self.assertEqual(result["error"], "paper_only_required")

    def test_missing_database_fails_closed(self):
        (self.root / EVIDENCE_FILES[0]).unlink()
        result, code = self.manager.start(self.root)
        self.assertEqual(code, 503)
        self.assertEqual(result["error"], "evidence_database_missing")

    def test_same_ready_backup_reused_without_repeating_large_export(self):
        _, _ = self.manager.start(self.root)
        for _ in range(100):
            if self.manager.status()["phase"] != "building":
                break
            time.sleep(0.02)
        status, code = self.manager.start(self.root)
        self.assertEqual(code, 200)
        self.assertEqual(status["phase"], "ready")
        self.manager._discard()


if __name__ == "__main__":
    unittest.main()

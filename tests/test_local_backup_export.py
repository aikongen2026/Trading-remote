"""Tests for complete paper-only evidence exports, without network or real broker."""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from local_backup_export import make_archive, EvidenceArchiveError
from state_storage import FILES


class EvidenceBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ALPACA_PAPER": "true"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def create_data(self):
        for name in FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table evidence (n integer)")
                db.execute("insert into evidence values (123)")

    def test_archives_entire_sqlite_history_and_excludes_credentials(self):
        self.create_data()
        (self.root / "control.json").write_text("do not archive me")
        (self.root / "saxo_sim_tokens.json").write_text("secret token")
        (self.root / "trade_plans.json").write_text("private")
        stream, sha, manifest = make_archive(self.root)
        try:
            body = stream.read()
            self.assertEqual(hashlib.sha256(body).hexdigest(), sha)
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                self.assertEqual(set(archive.namelist()), set(FILES) | {"manifest.json"})
                self.assertEqual(manifest["mode"], "PAPER")
                self.assertEqual(len(manifest["files"]), len(FILES))
                for item in manifest["files"]:
                    data = archive.read(item["name"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
                    self.assertEqual(len(data), item["bytes"])
                    with tempfile.TemporaryDirectory() as testdir:
                        copy = Path(testdir) / "verify.sqlite"
                        copy.write_bytes(data)
                        with sqlite3.connect(copy) as db:
                            self.assertEqual(db.execute("select n from evidence").fetchone()[0], 123)
                            self.assertEqual(db.execute("pragma integrity_check").fetchone()[0], "ok")
            self.assertNotIn(b"secret token", body)
            self.assertNotIn(b"do not archive me", body)
        finally:
            stream.close()
        for name in FILES:
            self.assertTrue((self.root / name).exists())

    def test_refuses_live_trading(self):
        self.create_data()
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            with self.assertRaises(EvidenceArchiveError):
                make_archive(self.root)

    def test_missing_database_fails_without_partial_archive(self):
        with self.assertRaises(EvidenceArchiveError):
            make_archive(self.root)


if __name__ == "__main__":
    unittest.main()

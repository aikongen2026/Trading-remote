"""Google Drive paper-only backup tests; no real Google credentials used."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from google_drive_rclone import GoogleDriveBackup, GoogleDriveBackupError
from state_storage import FILES


class GoogleDriveBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"ALPACA_PAPER": "true",
             "GOOGLE_DRIVE_FOLDER": "Trading Bot/Lagring"})
        self.env.start()
        self.backup = GoogleDriveBackup(self.root)
        self.backup.binary = "fake-rclone"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def token(self):
        return {"access_token": "a"*30, "refresh_token": "b"*30,
                "expiry": "2026-11-16T00:00:00Z"}

    def test_connect_creates_scoped_folder_and_private_config(self):
        calls = []
        def fake_run(config, args, timeout=120):
            calls.append(args)
            return b""
        self.backup._run = fake_run
        self.backup.connect(self.token(), "1234567890-test.apps.googleusercontent.com", "GOCSPX-test-secret")
        self.assertTrue(self.backup.connected())
        self.assertEqual(self.backup.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("scope = drive.file", self.backup.path.read_text())
        self.assertIn("client_id = 1234567890-test.apps.googleusercontent.com", self.backup.path.read_text())
        self.assertIn("client_secret = GOCSPX-test-secret", self.backup.path.read_text())
        self.assertIn("type = drive", self.backup.path.read_text())
        self.assertEqual(calls[:2], [
            ["mkdir", "tradingbot_google:Trading Bot/Lagring"],
            ["lsf", "tradingbot_google:Trading Bot/Lagring"]])

    def test_rejects_shared_retiring_google_client(self):
        with self.assertRaises(GoogleDriveBackupError):
            self.backup.connect(self.token())
        self.assertFalse(self.backup.path.exists())

    def test_connection_failure_does_not_store_token(self):
        self.backup._run = lambda *a, **kw: (_ for _ in ()).throw(
            GoogleDriveBackupError("GOOGLE_DRIVE_COMMAND_FAILED"))
        with self.assertRaises(GoogleDriveBackupError):
            self.backup.connect(self.token(), "1234567890-test.apps.googleusercontent.com", "GOCSPX-test-secret")
        self.assertFalse(self.backup.path.exists())

    def test_verified_upload_leaves_originals_intact(self):
        remote = {}
        self.backup.path.write_text("[tradingbot_google]\ntype=drive\n")
        for name in FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table evidence (value integer)")
                db.execute("insert into evidence values (3)")
        (self.root / "control.json").write_text("NEVER_SHARE")
        (self.root / "saxo_sim_tokens.json").write_text("NEVER_SHARE")
        def fake_run(config, args, timeout=120):
            if args[0] == "copyto":
                remote[args[2]] = Path(args[1]).read_bytes()
                return b""
            if args[0] == "cat":
                return remote[args[1]]
            raise AssertionError(args)
        self.backup._run = fake_run
        self.assertTrue(self.backup.backup_once())
        self.assertEqual(len(remote), len(FILES)+1)
        self.assertNotIn(b"NEVER_SHARE", b"".join(remote.values()))
        for name in FILES:
            self.assertTrue((self.root / name).is_file())
        self.assertEqual(self.backup.status()["files_verified"], len(FILES))

    def test_failure_never_prunes(self):
        root = self.root / "evidence_snapshots"
        root.mkdir()
        for index in range(4):
            (root / f"learning_v3.sqlite.{index:04d}.gz").write_bytes(b"retain")
        self.backup.path.write_text("[tradingbot_google]\ntype=drive\n")
        self.assertFalse(self.backup.backup_once())
        self.assertEqual(len(list(root.glob("*.gz"))), 4)

    def test_live_mode_rejected(self):
        self.backup.path.write_text("[tradingbot_google]\ntype=drive\n")
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            self.assertFalse(self.backup.backup_once())
        self.assertEqual(self.backup.status()["last_error"], "PAPER_ONLY_REQUIRED")


if __name__ == "__main__":
    unittest.main()

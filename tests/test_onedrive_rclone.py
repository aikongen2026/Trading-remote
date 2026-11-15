"""No external network, rclone or broker access required."""
import gzip
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from onedrive_rclone import RcloneBackup
from onedrive_backup import OneDriveBackupError
from state_storage import FILES


class RcloneOnedriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.dict(os.environ, {"ALPACA_PAPER": "true",
                                             "ONEDRIVE_FOLDER": "AI/TR bots/Lagring",
                                             "ONEDRIVE_RCLONE_DRIVE_ID": "expected-drive-id"})
        self.patch.start()
        self.worker = RcloneBackup(self.root)
        self.worker.binary = "fake-rclone"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_connect_writes_private_config_after_validating_folder(self):
        seen = []
        def fake_run(path, args, timeout=120):
            seen.append(args)
            return b""
        self.worker._run = fake_run
        self.worker.connect({"access_token":"a"*25, "refresh_token":"b"*25,
                             "expiry":"2026-11-16T00:00:00Z"})
        self.assertTrue(self.worker.connected())
        self.assertEqual(self.worker.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(seen[0], ["lsf", "tradingbot:AI/TR bots/Lagring"])
        self.assertEqual(self.worker.status()["destination"], "AI/TR bots/Lagring")

    def test_wrong_drive_not_silently_saved(self):
        def rejected(path, args, timeout=120):
            raise OneDriveBackupError("ONEDRIVE_COMMAND_FAILED")
        self.worker._run = rejected
        with self.assertRaises(OneDriveBackupError):
            self.worker.connect({"access_token":"a"*25, "refresh_token":"b"*25,
                                 "expiry":"2026-11-16T00:00:00Z"})
        self.assertFalse(self.worker.path.exists())

    def test_verified_backup_and_no_upload_of_tokens(self):
        remote = {}
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\n")
        for name in FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table data (id int)")
                db.execute("insert into data values (1)")
        (self.root / "saxo_sim_tokens.json").write_text("SECRET_NEVER_UPLOAD")
        (self.root / "control.json").write_text("SECRET_NEVER_UPLOAD")
        def fake_run(config, args, timeout=120):
            if args[0] == "copyto":
                remote[args[2]] = Path(args[1]).read_bytes()
                return b""
            if args[0] == "cat":
                return remote[args[1]]
            raise AssertionError(args)
        self.worker._run = fake_run
        self.assertTrue(self.worker.backup_once())
        self.assertEqual(len(remote), len(FILES)+1)
        self.assertEqual(self.worker.status()["files_verified"], len(FILES))
        self.assertNotIn(b"SECRET_NEVER_UPLOAD", b"".join(remote.values()))
        self.assertTrue(any(k.endswith("manifest.json") for k in remote))

    def test_failed_verification_does_not_delete_local_snapshots(self):
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\n")
        folder = self.root / "evidence_snapshots"
        folder.mkdir()
        for i in range(4):
            (folder / f"learning_v3.sqlite.{i}.gz").write_bytes(b"retain")
        for name in FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table data (id int)")
        self.worker._run = lambda *a, **kw: b""
        self.assertFalse(self.worker.backup_once())
        self.assertEqual(len(list(folder.glob("*.gz"))), 4)

    def test_live_mode_fails_closed(self):
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\n")
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            self.assertFalse(self.worker.backup_once())
        self.assertEqual(self.worker.status()["last_error"], "PAPER_ONLY_REQUIRED")


if __name__ == "__main__":
    unittest.main()

"""Tests for optional offsite evidence backups. No real Graph credentials or calls."""
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onedrive_backup import OneDriveBackup, OneDriveBackupError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, body=b""):
        self.status_code = status_code
        self.payload = payload or {}
        self.body = body
        self.headers = {}

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=131072):
        yield self.body

    def close(self):
        pass


class FakeGraphSession:
    def __init__(self, email="christian@straye.no"):
        self.files = {}
        self.email = email

    def request(self, method, url, headers=None, **kwargs):
        if url.endswith("/me?$select=mail,userPrincipalName"):
            return FakeResponse(payload={"mail": self.email, "userPrincipalName": self.email})
        if "/me/drive/root:/" in url:
            return FakeResponse(payload={"id": "folder-id", "folder": {"childCount": 0}})
        if method == "PUT" and "/content" in url:
            name = url.split(":/")[-1].removesuffix(":/content")
            data = kwargs.get("data")
            file_id = f"file-{len(self.files)+1}"
            self.files[file_id] = data
            return FakeResponse(payload={"id": file_id, "size": len(data), "name": name})
        if method == "GET" and "/content" in url:
            file_id = url.split("/items/")[-1].split("/")[0]
            if file_id not in self.files:
                return FakeResponse(status_code=404)
            return FakeResponse(body=self.files[file_id])
        raise AssertionError((method, url))

    def post(self, url, data=None, **kwargs):
        return FakeResponse(payload={"access_token": "fake", "refresh_token": "R"*25})


class OneDriveBackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.env = patch.dict(os.environ, {
            "ONEDRIVE_CLIENT_ID": "test-id",
            "ONEDRIVE_CLIENT_SECRET": "test-secret",
            "ONEDRIVE_EXPECTED_EMAIL": "christian@straye.no",
            "ONEDRIVE_FOLDER": "AI/TR bots/Lagring",
        })
        self.env.start()
        self.session = FakeGraphSession()
        self.backup = OneDriveBackup(self.root, session=self.session)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_sqlite_snapshot_integrity(self):
        db = self.root / "learning_v3.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE learn (x TEXT)")
            conn.execute("INSERT INTO learn VALUES ('history')")
        compressed, checksum = self.backup._snapshot_db(db)
        raw = gzip.decompress(compressed)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), checksum)
        with tempfile.TemporaryDirectory() as dest:
            out = Path(dest) / "copy.sqlite"
            out.write_bytes(raw)
            with sqlite3.connect(out) as conn:
                self.assertEqual(conn.execute("SELECT x FROM learn").fetchone()[0], "history")
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_uploads_only_allowlisted_evidence(self):
        import state_storage
        self.backup._store_refresh_token("R"*25)
        for name in state_storage.FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("CREATE TABLE audit (x INTEGER)")
                db.execute("INSERT INTO audit VALUES (5)")
        for name in ("control.json", "saxo_sim_tokens.json", "trade_plans_v4.json"):
            (self.root / name).write_text("HIGHLY_SENSITIVE_DO_NOT_COPY", encoding="utf-8")
        self.assertTrue(self.backup.backup_once())
        all_uploaded = b"".join(self.session.files.values())
        self.assertNotIn(b"HIGHLY_SENSITIVE_DO_NOT_COPY", all_uploaded)
        self.assertEqual(len(self.session.files), len(state_storage.FILES) + 1)
        manifests = [v for v in self.session.files.values() if v.startswith(b"{")]
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0])
        self.assertEqual(manifest["mode"], "paper")
        self.assertEqual(set(x["source"] for x in manifest["files"]), set(state_storage.FILES))
        self.assertEqual(self.backup.status()["files_verified"], len(state_storage.FILES))

    def test_wrong_tenant_does_not_write(self):
        self.backup._store_refresh_token("R"*25)
        self.session.email = "other@example.com"
        self.assertFalse(self.backup.backup_once())
        self.assertFalse(self.session.files)
        self.assertEqual(self.backup.status()["last_error"], "WRONG_ONEDRIVE_ACCOUNT")

    def test_without_microsoft_credentials_stays_disabled(self):
        with patch.dict(os.environ, {"ONEDRIVE_CLIENT_ID": "", "ONEDRIVE_CLIENT_SECRET": ""}):
            safe = OneDriveBackup(self.root, session=self.session)
            self.assertFalse(safe.configured())
            self.assertFalse(safe.backup_once())

    def test_no_local_pruning_without_a_verified_remote_backup(self):
        folder = self.root / "evidence_snapshots"
        folder.mkdir()
        for index in range(4):
            (folder / f"learning_v3.sqlite.{index:04d}.gz").write_bytes(b"not-a-real-backup")
        self.backup._store_refresh_token("R"*25)
        self.assertFalse(self.backup.backup_once())
        self.assertEqual(len(list(folder.glob("*.gz"))), 4)


if __name__ == "__main__":
    unittest.main()

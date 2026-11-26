"""No external network, rclone or broker access required."""
import gzip
import hashlib
import json
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
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\ndrive_id=expected-drive-id\n")
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
            if args[0] == "check":
                name = args[args.index('--include')+1].lstrip('/')
                if (Path(args[1])/name).read_bytes() != remote[args[2]+'/'+name]:
                    raise OneDriveBackupError('REMOTE_CHECKSUM_MISMATCH')
                return b''
            raise AssertionError(args)
        self.worker._run = fake_run
        self.assertTrue(self.worker.backup_once())
        self.assertEqual(len(remote), len(FILES)+1)
        self.assertEqual(self.worker.status()["files_verified"], len(FILES))
        self.assertNotIn(b"SECRET_NEVER_UPLOAD", b"".join(remote.values()))
        self.assertTrue(any(k.endswith("manifest.json") for k in remote))

    def test_failed_verification_does_not_delete_local_snapshots(self):
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\ndrive_id=expected-drive-id\n")
        folder = self.root / "evidence_snapshots"
        folder.mkdir()
        for i in range(4):
            (folder / f"learning_v3.sqlite.{i}.gz").write_bytes(b"retain")
        for name in FILES:
            with sqlite3.connect(self.root / name) as db:
                db.execute("create table data (id int)")
        def fail_check(config, args, **kwargs):
            if args[0] == 'check':
                raise OneDriveBackupError('REMOTE_CHECKSUM_MISMATCH')
            return b''
        self.worker._run = fail_check
        self.assertFalse(self.worker.backup_once())
        self.assertEqual(len(list(folder.glob("*.gz"))), 4)

    def test_live_mode_fails_closed(self):
        self.worker.path.write_text("[tradingbot]\ntype=onedrive\ndrive_id=expected-drive-id\n")
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            self.assertFalse(self.worker.backup_once())
        self.assertEqual(self.worker.status()["last_error"], "PAPER_ONLY_REQUIRED")

    def make_archive(self):
        self.worker.path.write_text('[tradingbot]\ntype=onedrive\ndrive_id=expected-drive-id\n')
        folder = self.root/'edge_snapshot_archives'
        folder.mkdir()
        row = json.dumps({'table':'events','row':[1,'x','DECISION','BTCUSD','{}']}).encode()+b'\n'
        digest = hashlib.sha256(row).hexdigest()
        archive = folder/('snapshots-'+digest[:24]+'.jsonl.gz')
        archive.write_bytes(gzip.compress(row))
        Path(str(archive)+'.json').write_text(json.dumps({'format':'edge-observation-rows-v2','sha256_uncompressed':digest,'rows':1}))
        return archive

    def test_archive_offload_deletes_only_verified_copy(self):
        archive = self.make_archive()
        remote = {}
        def fake_run(config, args, **kwargs):
            if args[0] == 'copyto':
                remote[args[2]] = Path(args[1]).read_bytes()
            elif args[0] == 'check':
                name = args[args.index('--include')+1].lstrip('/')
                if (Path(args[1])/name).read_bytes() != remote[args[2]+'/'+name]:
                    raise OneDriveBackupError('REMOTE_CHECKSUM_MISMATCH')
            else:
                raise AssertionError(args)
            return b''
        self.worker._run = fake_run
        (self.root/'control.json').write_text('do-not-upload')
        self.assertTrue(self.worker.offload_archives())
        self.assertFalse(archive.exists())
        self.assertTrue(Path(str(archive)+'.offloaded.json').is_file())
        self.assertEqual(len(remote),2)
        self.assertTrue(all('/Archives/' in x for x in remote))
        self.assertEqual(self.worker.status()['archives_offloaded'],1)
        self.assertEqual((self.root/'control.json').read_text(),'do-not-upload')

    def test_archive_failure_keeps_local_copy(self):
        archive = self.make_archive()
        def fail(config, args, **kwargs):
            if args[0] == 'check':
                raise OneDriveBackupError('REMOTE_CHECKSUM_MISMATCH')
            return b''
        self.worker._run = fail
        self.assertFalse(self.worker.offload_archives())
        self.assertTrue(archive.is_file())
        self.assertFalse(Path(str(archive)+'.offloaded.json').exists())

    def test_wrong_saved_drive_is_not_a_connection(self):
        self.worker.path.write_text('[tradingbot]\ntype=onedrive\ndrive_id=wrong-drive\n')
        self.assertFalse(self.worker.connected())

    def test_snapshot_larger_than_old_64mb_limit_is_streamed(self):
        source = self.root/'large.sqlite'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE blobs(data BLOB)')
            for _ in range(65):
                db.execute('INSERT INTO blobs VALUES(zeroblob(1048576))')
        self.assertGreater(source.stat().st_size,64*1024**2)
        packed = self.root/'large.sqlite.gz'
        digest = self.worker._snapshot_to_file(source,packed)
        with gzip.open(packed,'rb') as stream:
            self.assertEqual(digest,hashlib.file_digest(stream,'sha256').hexdigest())
        self.assertTrue(packed.is_file())

    def test_bandwidth_reserve_rejects_upload_before_network(self):
        self.worker.path.write_text('[tradingbot]')
        from datetime import datetime,timezone
        (self.root/'render_usage.json').write_text(json.dumps({'source':'render_api','checked_at':datetime.now(timezone.utc).isoformat(),'cap_gb':10,'used_gb':7}))
        local = self.root/'archive.gz'
        local.write_bytes(b'123')
        with patch.dict(os.environ,{'RENDER':'true'}), patch.object(self.worker,'_run') as run:
            with self.assertRaises(OneDriveBackupError):
                self.worker._verified_upload(local,'AI/tradingbot/archive.gz')
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

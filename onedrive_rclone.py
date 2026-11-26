"""Cloud-only OneDrive evidence backup via rclone's built-in Microsoft sign-in.

Never change broker orders, dashboard settings, active databases, or execution
guard state. Transfer a strict allowlist of integrity-checked SQLite snapshots.
"""
from __future__ import annotations
import gzip
import configparser
import hashlib
import json
import os
import math
import re
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from urllib.parse import quote

from onedrive_backup import OneDriveBackupError

REMOTE = "tradingbot:"
EVERY_SEC = 86400
MAX_DB_BYTES = 1024 * 1024 * 1024
MAX_UPLOAD_BYTES = 80 * 1024 * 1024


class RcloneBackup:
    def __init__(self, state_dir):
        self.state = Path(state_dir)
        self.path = self.state / "onedrive-rclone.conf"
        self.health_path = self.state / "onedrive_rclone_health.json"
        self.folder = os.getenv("ONEDRIVE_FOLDER", "AI/tradingbot").strip("/")
        self.drive_id = os.getenv("ONEDRIVE_RCLONE_DRIVE_ID", "").strip()
        self.binary = shutil.which("rclone")
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        try:
            self.health = json.loads(self.health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.health = {}

    def connected(self):
        if not (self.drive_id and self.binary and self.path.is_file()):
            return False
        try:
            config = configparser.ConfigParser(interpolation=None)
            config.read(self.path, encoding='utf-8')
            return config.get('tradingbot','drive_id',fallback='').strip() == self.drive_id
        except (OSError, configparser.Error):
            return False

    def status(self):
        return {
            "connected": self.connected(),
            "destination": self.folder,
            "last_success_utc": self.health.get("last_success_utc"),
            "last_error": self.health.get("last_error", ""),
            "files_verified": int(self.health.get("files_verified", 0) or 0),
            "local_snapshots_pruned": int(self.health.get("local_snapshots_pruned", 0) or 0),
            "interval_sec": EVERY_SEC,
            "archive_last_success_utc": self.health.get("archive_last_success_utc"),
            "archives_offloaded": int(self.health.get("archives_offloaded", 0) or 0),
            "archive_error": self.health.get("archive_error", ""),
        }

    def _save_health(self):
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state,
                                             prefix=".rclone-health-", delete=False) as f:
                path = Path(f.name)
                json.dump(self.health, f)
            os.replace(path, self.health_path)
        except OSError:
            pass  # Full disk must not crash the trading engine.

    def _run(self, config, args, timeout=120):
        if not self.binary:
            raise OneDriveBackupError("RCLONE_NOT_INSTALLED")
        command = [self.binary, "--config", str(config), "--retries", "2",
                   "--low-level-retries", "2", "--log-level", "ERROR", *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout,
                                    check=False, env=os.environ.copy())
        except subprocess.TimeoutExpired as exc:
            raise OneDriveBackupError("ONEDRIVE_TIMEOUT") from exc
        except OSError as exc:
            raise OneDriveBackupError("ONEDRIVE_PROCESS_FAILED") from exc
        if result.returncode:
            # Never expose HTTP payloads, access tokens or remote server messages.
            raise OneDriveBackupError("ONEDRIVE_COMMAND_FAILED")
        return result.stdout

    def connect(self, token):
        if os.getenv("ALPACA_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
            raise OneDriveBackupError("PAPER_ONLY_REQUIRED")
        if not self.drive_id:
            raise OneDriveBackupError("ONEDRIVE_DRIVE_NOT_SET")
        if not self.binary:
            raise OneDriveBackupError("RCLONE_NOT_INSTALLED")
        if not isinstance(token, dict):
            raise OneDriveBackupError("TOKEN_INVALID")
        for field in ("access_token", "refresh_token", "expiry"):
            value = token.get(field)
            if not isinstance(value, str) or not value or len(value) > 8192:
                raise OneDriveBackupError("TOKEN_INVALID")
        if len(json.dumps(token)) > 16000:
            raise OneDriveBackupError("TOKEN_TOO_LARGE")
        with self.lock:
            config_content = ("[tradingbot]\n"
                              "type = onedrive\n"
                              "drive_id = " + self.drive_id + "\n"
                              "drive_type = business\n"
                              "access_scopes = Files.ReadWrite offline_access User.Read\n"
                              "token = " + json.dumps(token, separators=(",", ":")) + "\n")
            # Prove that the authorized account can read the exact OneDrive
            # folder before committing a refresh token to persistent storage.
            with tempfile.TemporaryDirectory(prefix="trbot-rclone-") as folder:
                probe = Path(folder) / "rclone.conf"
                probe.write_text(config_content, encoding="utf-8")
                os.chmod(probe, 0o600)
                self._run(probe, ["lsf", REMOTE + self.folder], timeout=90)
                # rclone may rotate the token during the probe; preserve its
                # rewritten config, not the outdated initial token.
                verified_config = probe.read_bytes()
                if len(verified_config) > 24000:
                    raise OneDriveBackupError("CONFIG_TOO_LARGE")
                self.state.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".onedrive-rclone-", dir=self.state)
                try:
                    os.chmod(temporary, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(verified_config)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                os.chmod(self.path, 0o600)
            self.health.update(last_error="", last_success_utc=None,
                               connected_utc=datetime.now(timezone.utc).isoformat())
            self._save_health()
            self.wake.set()

    def _verified_upload(self, local, remote_path):
        local = Path(local)
        if local.name != remote_path.rsplit('/', 1)[-1]:
            raise OneDriveBackupError("REMOTE_FILENAME_MISMATCH")
        self._reserve_transfer(local.stat().st_size)
        self._run(self.path, ["copyto", str(local), REMOTE + remote_path], timeout=450)
        # rclone streams both files for verification; no full-file capture in RAM.
        self._run(self.path, ["check", str(local.parent), REMOTE + remote_path.rsplit('/', 1)[0],
                             "--include", "/" + local.name, "--one-way", "--download", "--checkers", "1"], timeout=450)

    def _reserve_transfer(self, size):
        if size > MAX_UPLOAD_BYTES:
            raise OneDriveBackupError('BACKUP_TOO_LARGE')
        if os.getenv('RENDER', '').lower() not in ('true','1','yes'):
            return
        try:
            now = datetime.now(timezone.utc)
            usage = json.loads((self.state/'render_usage.json').read_text())
            stamp = datetime.fromisoformat(usage['checked_at'].replace('Z','+00:00')).timestamp()
            cap, used = float(usage['cap_gb']), float(usage['used_gb'])
            if (usage.get('source') != 'render_api' or not all(math.isfinite(v) for v in (cap,used,stamp))
                    or not 0 <= now.timestamp()-stamp <= 3600 or cap <= 0 or not 0 <= used <= cap):
                raise ValueError('metering')
            pending = [r for r in self.health.get('transfer_reservations', []) if r['ts'] > stamp]
            reserved = sum(r['bytes'] for r in pending)
            limit = min(cap*.70, cap-.75) * 1024**3
            day = now.strftime('%Y-%m-%d')
            day_bytes = self.health.get('transfer_day_bytes', 0) if self.health.get('transfer_day') == day else 0
            if used*1024**3 + reserved + size > limit or day_bytes + size > 150*1024**2:
                raise OneDriveBackupError('ONEDRIVE_BANDWIDTH_RESERVE')
            pending.append({'ts':now.timestamp(), 'bytes':size})
            self.health.update(transfer_reservations=pending, transfer_day=day, transfer_day_bytes=day_bytes+size)
            self._save_health()
        except OneDriveBackupError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise OneDriveBackupError('ONEDRIVE_METERING_UNAVAILABLE') from None

    @staticmethod
    def _snapshot_to_file(source, destination):
        source, destination = Path(source), Path(destination)
        size = source.stat().st_size
        working_limit = min(MAX_DB_BYTES, shutil.disk_usage(destination.parent).free - 32*1024**2)
        if size > working_limit:
            raise OneDriveBackupError('SNAPSHOT_WORKING_SPACE')
        started = time.monotonic()
        source_page_size = 4096
        def progress(status, remaining, total):
            if time.monotonic()-started > 120 or total*source_page_size > working_limit:
                raise OneDriveBackupError('SNAPSHOT_LIMIT')
        with tempfile.TemporaryDirectory(prefix='sqlite-copy-', dir=destination.parent) as tmp:
            copied = Path(tmp)/'snapshot.sqlite'
            with closing(sqlite3.connect('file:'+quote(source.resolve().as_posix(), safe='/')+'?mode=ro', uri=True, timeout=15)) as src, closing(sqlite3.connect(copied)) as target:
                source_page_size = src.execute('PRAGMA page_size').fetchone()[0]
                target.execute('PRAGMA max_page_count='+str(max(1,int(working_limit//source_page_size))))
                src.backup(target, pages=128, progress=progress, sleep=.01)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise OneDriveBackupError('SQLITE_INTEGRITY_FAILED')
            digest = hashlib.sha256()
            with copied.open('rb') as raw, gzip.open(destination, 'wb', compresslevel=3) as packed:
                while data := raw.read(256*1024):
                    digest.update(data)
                    packed.write(data)
            if destination.stat().st_size > MAX_UPLOAD_BYTES:
                raise OneDriveBackupError('BACKUP_TOO_LARGE')
            return digest.hexdigest()

    def offload_archives(self):
        if not self.connected() or os.getenv('ALPACA_PAPER','true').lower() not in ('true','1','yes','on'):
            return False
        with self.lock:
            try:
                from storage_maintenance import _archive_digest
                folder = self.state/'edge_snapshot_archives'
                if not folder.is_dir():
                    return True
                for packed in sorted(folder.glob('snapshots-*.jsonl.gz')):
                    if packed.is_symlink() or not re.fullmatch(r'snapshots-[a-f0-9]{24}\.jsonl\.gz', packed.name):
                        continue
                    manifest = Path(str(packed)+'.json')
                    if manifest.is_symlink():
                        raise OneDriveBackupError('ARCHIVE_MANIFEST_INVALID')
                    meta = json.loads(manifest.read_text())
                    original_stat, manifest_stat = packed.stat(), manifest.stat()
                    if meta.get('format') not in ('edge-snapshot-rows-v1','edge-observation-rows-v2'):
                        raise OneDriveBackupError('ARCHIVE_FORMAT_INVALID')
                    if _archive_digest(packed) != (meta['sha256_uncompressed'], meta['rows']):
                        raise OneDriveBackupError('ARCHIVE_CHECKSUM_MISMATCH')
                    remote = self.folder+'/Archives/'+packed.name
                    self._verified_upload(packed, remote)
                    self._verified_upload(manifest, remote+'.json')
                    # Keep a durable receipt before removing this local COPY.
                    receipt = {**meta, 'remote':remote, 'verified_utc':datetime.now(timezone.utc).isoformat()}
                    pending = folder/(packed.name+'.offloaded.tmp')
                    pending.write_text(json.dumps(receipt), encoding='utf-8')
                    with pending.open('rb') as stream:
                        os.fsync(stream.fileno())
                    os.replace(pending, folder/(packed.name+'.offloaded.json'))
                    directory = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    if (packed.stat().st_mtime_ns != original_stat.st_mtime_ns
                            or packed.stat().st_size != original_stat.st_size
                            or manifest.stat().st_mtime_ns != manifest_stat.st_mtime_ns):
                        raise OneDriveBackupError('ARCHIVE_CHANGED_DURING_UPLOAD')
                    packed.unlink()
                    self.health['archives_offloaded'] = self.health.get('archives_offloaded',0)+1
                self.health.update(archive_last_success_utc=datetime.now(timezone.utc).isoformat(), archive_error='')
                self._save_health()
                return True
            except Exception as exc:
                self.health['archive_error'] = str(exc) if isinstance(exc, OneDriveBackupError) else 'ARCHIVE_'+type(exc).__name__
                self._save_health()
                return False

    def backup_once(self):
        if os.getenv("ALPACA_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
            self.health["last_error"] = "PAPER_ONLY_REQUIRED"
            self._save_health()
            return False
        if not self.connected():
            return False
        self.offload_archives()
        with self.lock:
            try:
                from state_storage import FILES
                names = list(FILES)
                if any(not (self.state / name).is_file() for name in names):
                    raise OneDriveBackupError("EVIDENCE_DATABASES_MISSING")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                prefix = f"paper-tradingbot-{stamp}-"
                records = []
                with tempfile.TemporaryDirectory(prefix="trbot-backup-") as tmp:
                    for name in names:
                        path = Path(tmp) / (prefix + name + ".gz")
                        raw_hash = self._snapshot_to_file(self.state / name, path)
                        self._verified_upload(path, self.folder + "/Backups/" + path.name)
                        with path.open('rb') as packed:
                            packed_hash = hashlib.file_digest(packed, 'sha256').hexdigest()
                        records.append({"name": name, "file": path.name,
                                        "db_sha256": raw_hash,
                                        "gzip_sha256": packed_hash,
                                        "gzip_bytes": path.stat().st_size})
                        path.unlink()
                    manifest = Path(tmp) / (prefix + "manifest.json")
                    manifest.write_text(json.dumps({
                        "format_version": 1, "mode": "paper", "files": records,
                        "created_utc": datetime.now(timezone.utc).isoformat(),
                        "destination": self.folder
                    }, indent=2), encoding="utf-8")
                    self._verified_upload(manifest, self.folder + "/Backups/" + manifest.name)
                # The local source DBs and every live execution file stay intact.
                # Only redundant *local backup copies* may be removed.
                removed = 0
                snapshots = self.state / "evidence_snapshots"
                for name in names:
                    if snapshots.is_dir():
                        old = sorted(snapshots.glob(name + ".*.gz"))
                        for path in old[:-2]:
                            try:
                                path.unlink()
                                removed += 1
                            except OSError:
                                pass
                self.health.update(last_success_utc=datetime.now(timezone.utc).isoformat(),
                                   last_error="", files_verified=len(records),
                                   local_snapshots_pruned=removed)
                self._save_health()
                return True
            except Exception as exc:
                self.health["last_error"] = (str(exc) if isinstance(exc, OneDriveBackupError)
                                             else "BACKUP_" + type(exc).__name__)
                self._save_health()
                return False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def worker():
            last_attempt = 0
            while not self.stop.is_set():
                try:
                    last = datetime.fromisoformat(self.health["last_success_utc"]).timestamp()
                except (ValueError, TypeError, KeyError):
                    last = 0
                if self.connected() and time.time() - last >= EVERY_SEC and time.time()-last_attempt >= 3600:
                    last_attempt = time.time()
                    self.backup_once()
                self.wake.wait(300)
                self.wake.clear()
        self.thread = threading.Thread(target=worker, name="rclone-onedrive-backup", daemon=True)
        self.thread.start()

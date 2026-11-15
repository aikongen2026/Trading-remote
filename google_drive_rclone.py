"""Cloud-only Google Drive evidence backup via rclone's browser-based Google sign-in.

Never change broker orders, dashboard settings, active databases, or execution
guard state. Transfer a strict allowlist of integrity-checked SQLite snapshots.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from onedrive_backup import OneDriveBackup

class GoogleDriveBackupError(Exception):
    """Safe error code; never expose access tokens or rclone stderr."""

REMOTE = "tradingbot_google:"
EVERY_SEC = 3600


class GoogleDriveBackup:
    def __init__(self, state_dir):
        self.state = Path(state_dir)
        self.path = self.state / "google-drive-rclone.conf"
        self.health_path = self.state / "google_drive_rclone_health.json"
        self.folder = os.getenv("GOOGLE_DRIVE_FOLDER", "Trading Bot/Lagring").strip("/")
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
        return bool(self.binary and self.path.is_file())

    def status(self):
        return {
            "connected": self.connected(),
            "destination": self.folder,
            "last_success_utc": self.health.get("last_success_utc"),
            "last_error": self.health.get("last_error", ""),
            "files_verified": int(self.health.get("files_verified", 0) or 0),
            "local_snapshots_pruned": int(self.health.get("local_snapshots_pruned", 0) or 0),
            "interval_sec": EVERY_SEC,
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
            raise GoogleDriveBackupError("RCLONE_NOT_INSTALLED")
        command = [self.binary, "--config", str(config), "--retries", "2",
                   "--low-level-retries", "2", "--log-level", "ERROR", *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout,
                                    check=False, env=os.environ.copy())
        except subprocess.TimeoutExpired as exc:
            raise GoogleDriveBackupError("GOOGLE_DRIVE_TIMEOUT") from exc
        except OSError as exc:
            raise GoogleDriveBackupError("GOOGLE_DRIVE_PROCESS_FAILED") from exc
        if result.returncode:
            # Never expose HTTP payloads, access tokens or remote server messages.
            raise GoogleDriveBackupError("GOOGLE_DRIVE_COMMAND_FAILED")
        return result.stdout

    def connect(self, token, client_id=None, client_secret=None):
        if os.getenv("ALPACA_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
            raise GoogleDriveBackupError("PAPER_ONLY_REQUIRED")
        if not self.binary:
            raise GoogleDriveBackupError("RCLONE_NOT_INSTALLED")
        # The rclone shared Google client is retired; a personal OAuth desktop
        # client is required for continued token refresh in unattended mode.
        if (not isinstance(client_id, str) or
                not re.fullmatch(r"[A-Za-z0-9._-]{1,230}\\.apps\\.googleusercontent\\.com", client_id) or
                not isinstance(client_secret, str) or
                not re.fullmatch(r"[A-Za-z0-9._-]{5,256}", client_secret)):
            raise GoogleDriveBackupError("GOOGLE_OAUTH_CLIENT_REQUIRED")
        if not isinstance(token, dict):
            raise GoogleDriveBackupError("TOKEN_INVALID")
        for field in ("access_token", "refresh_token", "expiry"):
            value = token.get(field)
            if not isinstance(value, str) or not value or len(value) > 8192:
                raise GoogleDriveBackupError("TOKEN_INVALID")
        if len(json.dumps(token)) > 16000:
            raise GoogleDriveBackupError("TOKEN_TOO_LARGE")
        with self.lock:
            config_content = ("[tradingbot_google]\n"
                              "type = drive\n"
                              "scope = drive.file\n"
                              "client_id = " + client_id + "\n"
                              "client_secret = " + client_secret + "\n"
                              "token = " + json.dumps(token, separators=(",", ":")) + "\n")
            # Create/verify an app-owned folder. The drive.file scope cannot
            # inspect the user's unrelated files or folders.
            with tempfile.TemporaryDirectory(prefix="trbot-rclone-") as folder:
                probe = Path(folder) / "rclone.conf"
                probe.write_text(config_content, encoding="utf-8")
                os.chmod(probe, 0o600)
                self._run(probe, ["mkdir", REMOTE + self.folder], timeout=90)
                self._run(probe, ["lsf", REMOTE + self.folder], timeout=90)
                # rclone may rotate the token during the probe; preserve its
                # rewritten config, not the outdated initial token.
                verified_config = probe.read_bytes()
                if len(verified_config) > 24000:
                    raise GoogleDriveBackupError("CONFIG_TOO_LARGE")
                self.state.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".google-drive-rclone-", dir=self.state)
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
            self.health.update(last_error="", connected_utc=datetime.now(timezone.utc).isoformat())
            self._save_health()
            self.wake.set()

    def _verified_upload(self, local, remote_path):
        self._run(self.path, ["copyto", str(local), REMOTE + remote_path], timeout=450)
        received = self._run(self.path, ["cat", REMOTE + remote_path], timeout=450)
        raw = local.read_bytes()
        if hashlib.sha256(received).digest() != hashlib.sha256(raw).digest():
            raise GoogleDriveBackupError("REMOTE_CHECKSUM_MISMATCH")

    def backup_once(self):
        if os.getenv("ALPACA_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
            self.health["last_error"] = "PAPER_ONLY_REQUIRED"
            self._save_health()
            return False
        if not self.connected():
            return False
        with self.lock:
            try:
                from state_storage import FILES
                names = list(FILES)
                if any(not (self.state / name).is_file() for name in names):
                    raise GoogleDriveBackupError("EVIDENCE_DATABASES_MISSING")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                prefix = f"paper-tradingbot-{stamp}-"
                records = []
                with tempfile.TemporaryDirectory(prefix="trbot-backup-") as tmp:
                    for name in names:
                        content, raw_hash = OneDriveBackup._snapshot_db(self.state / name)
                        path = Path(tmp) / (prefix + name + ".gz")
                        path.write_bytes(content)
                        self._verified_upload(path, self.folder + "/" + path.name)
                        records.append({"name": name, "file": path.name,
                                        "db_sha256": raw_hash,
                                        "gzip_sha256": hashlib.sha256(content).hexdigest(),
                                        "gzip_bytes": len(content)})
                    manifest = Path(tmp) / (prefix + "manifest.json")
                    manifest.write_text(json.dumps({
                        "format_version": 1, "mode": "paper", "files": records,
                        "created_utc": datetime.now(timezone.utc).isoformat(),
                        "destination": self.folder
                    }, indent=2), encoding="utf-8")
                    self._verified_upload(manifest, self.folder + "/" + manifest.name)
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
                self.health["last_error"] = (str(exc) if isinstance(exc, GoogleDriveBackupError)
                                             else "BACKUP_" + type(exc).__name__)
                self._save_health()
                return False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def worker():
            while not self.stop.is_set():
                try:
                    last = datetime.fromisoformat(self.health["last_success_utc"]).timestamp()
                except (ValueError, TypeError, KeyError):
                    last = 0
                if self.connected() and time.time() - last >= EVERY_SEC:
                    self.backup_once()
                self.wake.wait(300)
                self.wake.clear()
        self.thread = threading.Thread(target=worker, name="rclone-google-drive-backup", daemon=True)
        self.thread.start()

"""Optional offsite evidence backups to a user's existing Microsoft OneDrive folder.

The *active* SQLite databases stay on Render's disk. Only consistent SQLite
snapshots and a checksum manifest are copied to OneDrive. Never upload
control.json, trading plans, credentials, SIM tokens, or live execution state.
Never restore broker/execution state automatically.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"
SCOPE = "offline_access Files.ReadWrite User.Read"
REDIRECT_URI = "https://trading-remote.onrender.com/onedrive/oauth/callback"
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
INTERVAL_SEC = 3600


class OneDriveBackupError(Exception):
    """Error codes only; never include an HTTP response body or token."""


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class OneDriveBackup:
    def __init__(self, state_dir, session=None):
        self.state = Path(state_dir)
        self.client_id = os.getenv("ONEDRIVE_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET", "").strip()
        self.tenant = os.getenv("ONEDRIVE_TENANT", "organizations").strip()
        self.folder = os.getenv("ONEDRIVE_FOLDER", "AI/tradingbot").strip("/")
        self.expected_email = os.getenv("ONEDRIVE_EXPECTED_EMAIL", "").strip().lower()
        self.token_path = self.state / "onedrive_tokens.json"
        self.health_path = self.state / "onedrive_backup_health.json"
        self.session = session if session is not None else requests.Session()
        self.lock = threading.RLock()
        self.backup_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._thread = None
        self.health_data = _read_json(self.health_path)
        if not re.fullmatch(r"organizations|[0-9a-fA-F-]{36}", self.tenant):
            self.tenant = "organizations"

    def configured(self):
        return bool(self.client_id and self.client_secret)

    def connected(self):
        return bool(_read_json(self.token_path).get("refresh_token"))

    def status(self):
        return {
            "configured": self.configured(),
            "connected": self.connected(),
            "destination": self.folder,
            "last_success_utc": self.health_data.get("last_success_utc"),
            "last_error": self.health_data.get("last_error", ""),
            "files_verified": self.health_data.get("files_verified", 0),
            "local_snapshots_pruned": self.health_data.get("local_snapshots_pruned", 0),
            "interval_sec": INTERVAL_SEC,
        }

    def _save_health(self):
        # A full Render disk must not crash the trading service.
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state,
                                             prefix=".onedrive-health-", delete=False) as tmp:
                temporary = Path(tmp.name)
                json.dump(self.health_data, tmp)
            os.replace(temporary, self.health_path)
        except OSError:
            pass  # The in-memory status remains available in this process.

    def _store_refresh_token(self, refresh_token):
        if not isinstance(refresh_token, str) or len(refresh_token) < 20:
            raise OneDriveBackupError("TOKEN_MISSING")
        self.state.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.state, prefix=".onedrive-token-")
            temporary = Path(tmp)
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"refresh_token": refresh_token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.token_path)
            os.chmod(self.token_path, 0o600)
        except OSError as exc:
            raise OneDriveBackupError("TOKEN_STORAGE_ERROR") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _token_endpoint(self):
        return f"{AUTH_BASE}/{self.tenant}/oauth2/v2.0/token"

    def _token_request(self, payload):
        if not self.configured():
            raise OneDriveBackupError("APP_CONFIGURATION_MISSING")
        try:
            response = self.session.post(
                self._token_endpoint(),
                data={"client_id": self.client_id, "client_secret": self.client_secret,
                      "scope": SCOPE, **payload},
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            raise OneDriveBackupError("TOKEN_NETWORK_ERROR") from exc
        if response.status_code != 200:
            raise OneDriveBackupError(f"TOKEN_HTTP_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OneDriveBackupError("TOKEN_RESPONSE_INVALID") from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise OneDriveBackupError("ACCESS_TOKEN_MISSING")
        return data

    def complete_login(self, code, verifier):
        """Complete an authorization-code + PKCE flow after state/cookie checks."""
        with self.lock:
            data = self._token_request({
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
            })
            refresh = data.get("refresh_token")
            access = data["access_token"]
            self._verify_user_and_folder(access)
            self._store_refresh_token(refresh)
            self.health_data.update(last_error="", connected_utc=datetime.now(timezone.utc).isoformat())
            self._save_health()
            self.wake.set()

    def _access_token(self):
        with self.lock:
            refresh = _read_json(self.token_path).get("refresh_token")
            if not refresh:
                raise OneDriveBackupError("NOT_CONNECTED")
            data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            replacement = data.get("refresh_token")
            if replacement and replacement != refresh:
                # Commit rotated refresh token before beginning any remote writes.
                self._store_refresh_token(replacement)
            return data["access_token"]

    def _graph(self, method, path, token, **kwargs):
        url = GRAPH + path
        headers = {"Authorization": "Bearer " + token, **kwargs.pop("headers", {})}
        for attempt in range(3):
            try:
                response = self.session.request(method, url, headers=headers,
                                                timeout=(8, 120), **kwargs)
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise OneDriveBackupError("GRAPH_NETWORK_ERROR") from exc
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                retry = response.headers.get("Retry-After", "")
                delay = int(retry) if retry.isdigit() else 2 * (attempt + 1)
                response.close()
                time.sleep(min(max(delay, 1), 15))
                continue
            if not 200 <= response.status_code < 300:
                status = response.status_code
                response.close()
                raise OneDriveBackupError(f"GRAPH_HTTP_{status}")
            return response
        raise OneDriveBackupError("GRAPH_RETRIES_EXHAUSTED")

    def _json_graph(self, method, path, token, **kwargs):
        response = self._graph(method, path, token, **kwargs)
        try:
            result = response.json()
        except ValueError as exc:
            raise OneDriveBackupError("GRAPH_JSON_ERROR") from exc
        finally:
            response.close()
        if not isinstance(result, dict):
            raise OneDriveBackupError("GRAPH_RESPONSE_INVALID")
        return result

    def _verify_user_and_folder(self, access):
        profile = self._json_graph("GET", "/me?$select=mail,userPrincipalName", access)
        if self.expected_email:
            emails = {str(profile.get(field) or "").lower()
                      for field in ("mail", "userPrincipalName")}
            if self.expected_email not in emails:
                raise OneDriveBackupError("WRONG_ONEDRIVE_ACCOUNT")
        remote = self._json_graph("GET", "/me/drive/root:/" + quote(self.folder, safe="/"), access)
        if not isinstance(remote.get("folder"), dict) or not remote.get("id"):
            raise OneDriveBackupError("DESTINATION_NOT_FOLDER")
        return remote["id"]

    @staticmethod
    def _snapshot_db(path):
        path = Path(path)
        if not path.is_file():
            raise OneDriveBackupError("DATABASE_MISSING")
        if path.stat().st_size > MAX_DB_BYTES:
            raise OneDriveBackupError("DATABASE_TOO_LARGE")
        try:
            with closing(sqlite3.connect("file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro",
                                         uri=True, timeout=15)) as src:
                with closing(sqlite3.connect(":memory:")) as dest:
                    src.backup(dest, pages=512, sleep=0.01)
                    result = dest.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        raise OneDriveBackupError("SQLITE_INTEGRITY_FAILED")
                    raw = dest.serialize()
        except sqlite3.Error as exc:
            raise OneDriveBackupError("SQLITE_SNAPSHOT_FAILED") from exc
        if len(raw) > MAX_DB_BYTES:
            raise OneDriveBackupError("DATABASE_TOO_LARGE")
        return gzip.compress(raw, compresslevel=3, mtime=0), hashlib.sha256(raw).hexdigest()

    def _upload_verified(self, access, folder_id, filename, data, content_type):
        if len(data) > MAX_UPLOAD_BYTES:
            raise OneDriveBackupError("BACKUP_TOO_LARGE")
        safe_name = quote(filename, safe="")
        endpoint = "/me/drive/items/" + quote(folder_id, safe="") + ":/" + safe_name + ":/content"
        remote = self._json_graph("PUT", endpoint, access, data=data,
                                  headers={"Content-Type": content_type})
        if not remote.get("id") or int(remote.get("size", -1)) != len(data):
            raise OneDriveBackupError("REMOTE_SIZE_MISMATCH")
        # Confirm actual remote content before claiming the backup is usable
        # or pruning even one old local snapshot.
        result = self._graph("GET", "/me/drive/items/" + quote(remote["id"], safe="") +
                             "/content", access, stream=True)
        digest = hashlib.sha256()
        total = 0
        try:
            for chunk in result.iter_content(chunk_size=128 * 1024):
                if chunk:
                    total += len(chunk)
                    if total > len(data):
                        raise OneDriveBackupError("REMOTE_CONTENT_TOO_LARGE")
                    digest.update(chunk)
        finally:
            result.close()
        if total != len(data) or digest.digest() != hashlib.sha256(data).digest():
            raise OneDriveBackupError("REMOTE_CHECKSUM_MISMATCH")
        return remote["id"]

    def _prune_local_snapshots(self, filenames, keep=2):
        # Only prune redundant, local rotated copies AFTER every database and
        # the manifest have been uploaded AND re-read successfully from OneDrive.
        root = self.state / "evidence_snapshots"
        if not root.is_dir():
            return 0
        removed = 0
        for name in filenames:
            snapshots = sorted(root.glob(name + ".*.gz"))
            for path in snapshots[:-keep]:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def backup_once(self):
        # Never export a live-trading instance as a paper-learning backup.
        if os.getenv("ALPACA_PAPER", "true").lower() not in ("1", "true", "yes", "on"):
            self.health_data["last_error"] = "PAPER_ONLY_REQUIRED"
            self._save_health()
            return False
        if not self.configured() or not self.connected():
            return False
        if not self.backup_lock.acquire(blocking=False):
            return False
        try:
            token = self._access_token()
            folder_id = self._verify_user_and_folder(token)
            from state_storage import FILES  # includes optional V4.9 recorder
            names = [name for name in FILES if (self.state / name).is_file()]
            if len(names) != len(FILES):
                raise OneDriveBackupError("EVIDENCE_DATABASES_MISSING")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            prefix = "paper-tradingbot-" + stamp + "-"
            entries = []
            for name in names:
                contents, sqlite_hash = self._snapshot_db(self.state / name)
                filename = prefix + name + ".gz"
                remote_id = self._upload_verified(token, folder_id, filename, contents, "application/gzip")
                entries.append({"filename": filename, "source": name, "remote_id": remote_id,
                                "sqlite_sha256": sqlite_hash, "gzip_sha256": hashlib.sha256(contents).hexdigest(),
                                "gzip_bytes": len(contents)})
            manifest = {"format_version": 1, "mode": "paper", "created_utc": datetime.now(timezone.utc).isoformat(),
                        "source": "Trading-remote", "target": self.folder, "files": entries}
            manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            manifest_name = prefix + "manifest.json"
            self._upload_verified(token, folder_id, manifest_name, manifest_data, "application/json")
            pruned = self._prune_local_snapshots(names, keep=2)
            self.health_data.update(last_success_utc=datetime.now(timezone.utc).isoformat(),
                                    last_error="", files_verified=len(entries),
                                    local_snapshots_pruned=pruned)
            self._save_health()
            return True
        except Exception as exc:
            self.health_data["last_error"] = (str(exc) if isinstance(exc, OneDriveBackupError)
                                              else "BACKUP_" + type(exc).__name__)
            self._save_health()
            return False
        finally:
            self.backup_lock.release()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        def worker():
            while not self.stop.is_set():
                try:
                    last = datetime.fromisoformat(self.health_data["last_success_utc"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    last = 0.0
                if self.connected() and time.time() - last >= INTERVAL_SEC:
                    self.backup_once()
                self.wake.wait(300)
                self.wake.clear()
        self._thread = threading.Thread(target=worker, name="onedrive-evidence-backup", daemon=True)
        self._thread.start()

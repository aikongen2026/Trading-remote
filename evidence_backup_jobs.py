"""Asynchronous, single-job, budget-guarded PAPER evidence backup export.

Build the archive before returning an HTTP download response, avoiding
long-running idle requests on Render. All archive files live in ephemeral
temporary storage; no original database, broker state or credentials change.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from local_backup_export import EVIDENCE_FILES, make_archive

# Reserve substantial bandwidth for normal trading and the rest of the user's
# Render workspace. The cap is a user-provided protective budget, not an
# assertion about Render's commercial billing terms.
RESERVE_BYTES = 750_000_000
MAX_METER_AGE_SEC = 3600
READY_LIFETIME_SEC = 1800


class BackupJobManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None
        self.sent_reserved_bytes = 0

    @staticmethod
    def _paper():
        return os.getenv("ALPACA_PAPER", "true").strip().lower() in ("true", "1", "yes", "on")

    def _size_and_budget(self, state_dir):
        if not self._paper():
            return 0, "paper_only_required"
        root = Path(state_dir)
        try:
            raw_size = 0
            for name in EVIDENCE_FILES:
                item = root / name
                if not item.is_file():
                    return 0, "evidence_database_missing"
                raw_size += item.stat().st_size
            metering = json.loads((root / "render_usage.json").read_text(encoding="utf-8"))
            if not isinstance(metering, dict) or metering.get("source") != "render_api":
                return raw_size, "render_metering_not_verified"
            checked = datetime.fromisoformat(str(metering["checked_at"]).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
            if age < -300 or age > MAX_METER_AGE_SEC:
                return raw_size, "render_metering_stale"
            used_gb = float(metering["used_gb"])
            cap_gb = float(metering["cap_gb"])
            if not 0 <= used_gb <= cap_gb or cap_gb <= 0:
                return raw_size, "render_metering_invalid"
            # Use raw SQLite size, not the smaller compressed estimate,
            # plus previously started downloads not yet visible in metering.
            free_budget = (cap_gb - used_gb) * 1_000_000_000
            if free_budget < raw_size + RESERVE_BYTES + self.sent_reserved_bytes:
                return raw_size, "render_bandwidth_budget_low"
            return raw_size, ""
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return 0, "render_metering_unavailable"

    def _snapshot(self):
        job = self.job
        if not job:
            return {"phase": "idle"}
        result = {
            "phase": job["phase"],
            "id": job["id"],
            "started_utc": job["started_utc"],
            "error": job.get("error", ""),
            "size_bytes": job.get("size_bytes"),
            "zip_sha256": job.get("zip_sha256"),
            "download_url": "/api/evidence-backup" if job["phase"] == "ready" else None,
        }
        return result

    def _discard(self):
        if self.job:
            handle = self.job.get("handle")
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass
        self.job = None

    def _build(self, job_id, state_dir):
        handle = None
        try:
            handle, digest, _manifest = make_archive(state_dir)
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(0)
            with self.lock:
                if not self.job or self.job["id"] != job_id:
                    handle.close()
                    return
                self.job.update(
                    phase="ready", handle=handle, zip_sha256=digest,
                    size_bytes=length, completed_monotonic=time.monotonic(),
                    error="",
                )
        except Exception as exc:
            if handle is not None:
                handle.close()
            # Only allow published error codes, no exception strings or paths.
            error = str(exc) if type(exc).__name__ == "EvidenceArchiveError" else "archive_build_failed"
            with self.lock:
                if self.job and self.job["id"] == job_id:
                    self.job.update(
                        phase="error", error=error,
                        completed_monotonic=time.monotonic(),
                    )

    def start(self, state_dir):
        with self.lock:
            if self.job:
                duration = time.monotonic() - self.job.get("completed_monotonic", time.monotonic())
                if self.job["phase"] == "building" or self.job["phase"] == "downloading":
                    return self._snapshot(), 202
                if self.job["phase"] == "ready" and duration < READY_LIFETIME_SEC:
                    return self._snapshot(), 200
                if self.job["phase"] == "error" and duration < 30:
                    return self._snapshot(), 503
                self._discard()
            _raw_size, error = self._size_and_budget(state_dir)
            if error:
                code = 403 if error == "paper_only_required" else 429 if "budget" in error else 503
                return {"phase": "blocked", "error": error}, code
            job_id = secrets.token_urlsafe(18)
            self.job = {
                "id": job_id,
                "phase": "building",
                "started_utc": datetime.now(timezone.utc).isoformat(),
            }
            threading.Thread(target=self._build, args=(job_id, state_dir),
                             name="one-time-evidence-export", daemon=True).start()
            return self._snapshot(), 202

    def status(self):
        with self.lock:
            if self.job and self.job["phase"] == "ready":
                age = time.monotonic() - self.job.get("completed_monotonic", 0)
                if age > READY_LIFETIME_SEC:
                    self._discard()
            return self._snapshot()

    def begin_download(self, state_dir):
        with self.lock:
            job = self.job
            if not job or job["phase"] != "ready":
                return None, "backup_not_ready"
            _raw_size, error = self._size_and_budget(state_dir)
            if error:
                return None, error
            self.sent_reserved_bytes += _raw_size
            job["phase"] = "downloading"
            handle = job["handle"]
            handle.seek(0)
            return {
                "handle": handle, "job_id": job["id"],
                "zip_sha256": job["zip_sha256"], "size_bytes": job["size_bytes"],
            }, ""

    def finish_download(self, job_id):
        with self.lock:
            if self.job and self.job["id"] == job_id:
                self._discard()


BACKUP_JOBS = BackupJobManager()

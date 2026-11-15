"""Create complete, consistent PAPER evidence archives without using Render's disk.

Only the three evidence SQLite databases are included. Secrets, OAuth tokens,
control.json, order intents and trade plans are deliberately excluded. Temporary
snapshots and the archive reside on the container's ephemeral /tmp, not /var/data.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from state_storage import FILES, MAX_BYTES

MAX_ARCHIVE_BYTES = len(FILES) * MAX_BYTES + 8 * 1024 * 1024


class EvidenceArchiveError(Exception):
    pass


def _file_hash(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def make_archive(state_dir):
    """Return (open temporary zip, sha256, manifest). Caller owns zip.close()."""
    if os.getenv("ALPACA_PAPER", "true").lower() not in ("true", "1", "yes", "on"):
        raise EvidenceArchiveError("PAPER_ONLY_REQUIRED")
    root = Path(state_dir)
    archive = tempfile.TemporaryFile(mode="w+b")
    try:
        with tempfile.TemporaryDirectory(prefix="tradingbot-evidence-") as work:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=2, allowZip64=True) as output:
                items = []
                for name in FILES:
                    source = root / name
                    if not source.is_file():
                        raise EvidenceArchiveError("EVIDENCE_DATABASE_MISSING")
                    if source.stat().st_size > MAX_BYTES:
                        raise EvidenceArchiveError("EVIDENCE_DATABASE_TOO_LARGE")
                    temp_db = Path(work) / name
                    try:
                        uri = "file:" + quote(source.resolve().as_posix(), safe="/") + "?mode=ro"
                        with closing(sqlite3.connect(uri, uri=True, timeout=15)) as src:
                            with closing(sqlite3.connect(str(temp_db), timeout=15)) as dst:
                                src.backup(dst, pages=512, sleep=0.01)
                        if temp_db.stat().st_size > MAX_BYTES:
                            raise EvidenceArchiveError("EVIDENCE_DATABASE_TOO_LARGE")
                        with closing(sqlite3.connect(str(temp_db), timeout=15)) as check:
                            integrity = check.execute("PRAGMA integrity_check").fetchone()
                            if not integrity or integrity[0] != "ok":
                                raise EvidenceArchiveError("EVIDENCE_DATABASE_INVALID")
                    except sqlite3.Error as exc:
                        raise EvidenceArchiveError("EVIDENCE_SNAPSHOT_FAILED") from exc
                    digest, size = _file_hash(temp_db)
                    output.write(temp_db, arcname=name)
                    temp_db.unlink()
                    items.append({"name": name, "sha256": digest, "bytes": size})
                manifest = {
                    "format": "tradingbot-paper-evidence-v1",
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "mode": "PAPER",
                    "files": items,
                    "note": "Only evidence databases. Never restore order intents automatically."
                }
                output.writestr("manifest.json", json.dumps(
                    manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.seek(0, os.SEEK_END)
        if archive.tell() > MAX_ARCHIVE_BYTES:
            raise EvidenceArchiveError("EVIDENCE_ARCHIVE_TOO_LARGE")
        archive.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
        archive.seek(0)
        return archive, digest.hexdigest(), manifest
    except Exception:
        archive.close()
        raise

"""Human-readable release versions that change on every new GitHub deploy.

Uses the existing Render-provided RENDER_GIT_COMMIT. No network calls,
filesystem writes, broker actions, or persistent state changes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BASE_VERSION = (Path(__file__).resolve().parent / "VERSION.txt").read_text(
    encoding="utf-8-sig"
).strip()


def runtime_version(commit: str | None = None) -> str:
    """Example: '4.16.0+31eba6a'; unchanged base version for local dev."""
    sha = (os.getenv("RENDER_GIT_COMMIT", "") if commit is None else commit).strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return f"{BASE_VERSION}+{sha[:7].lower()}"
    return BASE_VERSION

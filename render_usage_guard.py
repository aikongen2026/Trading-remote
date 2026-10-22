#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render bandwidth guard for TradingBot.

Read-only against Render: this module never changes the service plan.
It records workspace/service bandwidth usage and exposes a conservative
traffic policy to the trading engine. The engine remains responsible for
protecting existing positions.

Required for automatic metering on Render:
  RENDER_API_KEY=<Render API key>

RENDER_SERVICE_ID is provided automatically by Render. Multiple resource IDs
can be supplied with RENDER_USAGE_RESOURCE_IDS=srv-a,srv-b when the workspace
contains several bandwidth-consuming services.

Optional:
  RENDER_BANDWIDTH_CAP_GB=5
  RENDER_USAGE_WARN_PCT=60
  RENDER_USAGE_SAVE_PCT=75
  RENDER_USAGE_STRONG_PCT=85
  RENDER_USAGE_HARD_PCT=95
  RENDER_USAGE_CHECK_SEC=900
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests


API_URL = "https://api.render.com/v1/metrics/bandwidth"
SERVICES_URL = "https://api.render.com/v1/services"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return float(default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _render_time(dt: datetime) -> str:
    # Render's metrics endpoint documents RFC3339/date-time values and examples
    # use a Z suffix. Emit second precision to avoid parser-specific 400 errors.
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now_iso() -> str:
    return _render_time(datetime.now(timezone.utc))


def _month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return _render_time(datetime(now.year, now.month, 1, tzinfo=timezone.utc))


def _configured_resource_ids() -> List[str]:
    raw = str(os.getenv("RENDER_USAGE_RESOURCE_IDS") or "").strip()
    return list(dict.fromkeys(
        x.strip() for x in raw.replace(";", ",").split(",") if x.strip()
    ))


def _atomic_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _to_gb(value: float, unit: str) -> float:
    unit = str(unit or "GB").strip().upper()
    if unit in {"GB", "GIB"}:
        return float(value)
    if unit in {"MB", "MIB"}:
        return float(value) / 1024.0
    if unit in {"KB", "KIB"}:
        return float(value) / (1024.0 * 1024.0)
    if unit in {"B", "BYTE", "BYTES"}:
        return float(value) / (1024.0 ** 3)
    return float(value)


def policy_for_percent(percent: float, configured: bool = True) -> str:
    if not configured:
        return "UNMETERED"
    warn = _float_env("RENDER_USAGE_WARN_PCT", 60.0)
    save = _float_env("RENDER_USAGE_SAVE_PCT", 75.0)
    strong = _float_env("RENDER_USAGE_STRONG_PCT", 85.0)
    hard = _float_env("RENDER_USAGE_HARD_PCT", 95.0)
    p = max(0.0, float(percent))
    if p >= 100.0:
        return "OVER"
    if p >= hard:
        return "HARD"
    if p >= strong:
        return "STRONG"
    if p >= save:
        return "SAVE"
    if p >= warn:
        return "WARN"
    return "NORMAL"


def read_usage(path: str | os.PathLike[str]) -> Dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


class RenderUsageGuard:
    """Poll Render's bandwidth API at a low frequency and persist policy state."""

    def __init__(self, state_dir: str) -> None:
        self.state_path = Path(state_dir) / "render_usage.json"
        self.stop_event = threading.Event()
        self.session = requests.Session()
        self.api_key = str(os.getenv("RENDER_API_KEY") or "").strip()
        self.resources = _configured_resource_ids()
        self.cap_gb = max(0.1, _float_env("RENDER_BANDWIDTH_CAP_GB", 5.0))
        self.check_sec = max(300, _int_env("RENDER_USAGE_CHECK_SEC", 900))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _discover_resource_ids(self) -> List[str]:
        """Discover every service visible to the Render API key.

        Included bandwidth is workspace-level, so monitoring only the current
        TradingBot service can understate billable usage. An explicit
        RENDER_USAGE_RESOURCE_IDS list overrides discovery when desired.
        """
        if self.resources:
            return list(self.resources)

        ids: List[str] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(50):
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = self.session.get(SERVICES_URL, headers=self._headers(), params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError("Render services API returned a non-list payload")
            if not data:
                break
            next_cursor = ""
            for row in data:
                if not isinstance(row, dict):
                    continue
                service = row.get("service") if isinstance(row.get("service"), dict) else row
                sid = str((service or {}).get("id") or "").strip()
                if sid:
                    ids.append(sid)
                rc = str(row.get("cursor") or "").strip()
                if rc:
                    next_cursor = rc
            if len(data) < 100 or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        current = str(os.getenv("RENDER_SERVICE_ID") or "").strip()
        if current:
            ids.append(current)
        return list(dict.fromkeys(ids))

    def _request_series(self, resources: List[str]) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [
            ("startTime", _month_start_iso()),
            ("endTime", _utc_now_iso()),
        ]
        for resource_id in resources:
            params.append(("resource", resource_id))
        if not resources:
            raise ValueError("No Render services are visible to the API key")
        r = self.session.get(API_URL, headers=self._headers(), params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("Render bandwidth API returned a non-list payload")
        return data

    @staticmethod
    def _sum_series_gb(series: Iterable[dict[str, Any]]) -> float:
        total = 0.0
        for row in series:
            if not isinstance(row, dict):
                continue
            unit = str(row.get("unit") or "GB")
            vals = row.get("values") or []
            # Render returns bandwidth as a time series; each datapoint is usage
            # for its interval, so monthly usage is the sum of datapoints.
            for point in vals:
                if not isinstance(point, dict):
                    continue
                try:
                    total += _to_gb(float(point.get("value") or 0.0), unit)
                except Exception:
                    continue
        return max(0.0, total)

    def snapshot(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "configured": self.configured,
            "source": "render_api" if self.configured else "not_configured",
            "cap_gb": round(self.cap_gb, 3),
            "resources": list(self.resources),
            "scope": "configured_services" if self.resources else "all_visible_services",
            "warn_pct": _float_env("RENDER_USAGE_WARN_PCT", 60.0),
            "save_pct": _float_env("RENDER_USAGE_SAVE_PCT", 75.0),
            "strong_pct": _float_env("RENDER_USAGE_STRONG_PCT", 85.0),
            "hard_pct": _float_env("RENDER_USAGE_HARD_PCT", 95.0),
            "checked_at": _utc_now_iso(),
            "month_start": _month_start_iso(),
            "auto_downgrade": False,
            "auto_downgrade_reason": (
                "Disabled: this service uses a persistent Render disk, which is not available on Free."
            ),
        }
        if not self.configured:
            base.update({
                "used_gb": None,
                "percent": None,
                "remaining_gb": None,
                "policy": "UNMETERED",
                "error": "Set RENDER_API_KEY as a Render secret to enable automatic usage metering.",
            })
            return base

        try:
            resources = self._discover_resource_ids()
            base["resources"] = resources
            base["service_count"] = len(resources)
            used_gb = self._sum_series_gb(self._request_series(resources))
            percent = (used_gb / self.cap_gb) * 100.0
            base.update({
                "used_gb": round(used_gb, 4),
                "percent": round(percent, 2),
                "remaining_gb": round(max(0.0, self.cap_gb - used_gb), 4),
                "policy": policy_for_percent(percent, True),
                "error": "",
            })
        except Exception as exc:
            previous = read_usage(self.state_path)
            # Fail safe: keep the last known usage/policy instead of pretending
            # that failed metering means zero consumption.
            for key in ("used_gb", "percent", "remaining_gb", "policy"):
                if key in previous:
                    base[key] = previous[key]
            base.setdefault("used_gb", None)
            base.setdefault("percent", None)
            base.setdefault("remaining_gb", None)
            base.setdefault("policy", "METER_ERROR")
            base["source"] = "render_api_stale"
            base["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return base

    def refresh(self) -> Dict[str, Any]:
        data = self.snapshot()
        _atomic_json(self.state_path, data)
        return data

    def run(self) -> None:
        # Refresh immediately at boot, then at a deliberately low cadence.
        while not self.stop_event.is_set():
            try:
                self.refresh()
            except Exception:
                pass
            self.stop_event.wait(self.check_sec)

    def start(self) -> None:
        threading.Thread(target=self.run, name="render-usage-guard", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()

"""Time-aligned, causally valid outcomes from *completed* historical bars.

Used only for research/learning labels. Never affects orders or live gates.
A label at entry time T and horizon H uses a completed one-minute candle
at T+H (at most two minutes late), rather than a potentially stale or
much-later current price. If no suitable bar exists, return None.
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

UTC = timezone.utc
MAX_MARK_DELAY_SECONDS = 120.0


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def bar_marks(bars: Iterable[dict] | None, now: datetime | None = None) -> list[tuple[float, float]]:
    """(completed UTC bar-close epoch, positive close price), sorted and unique."""
    at = parse_utc(now) or datetime.now(UTC)
    marks: dict[float, float] = {}
    for bar in bars or ():
        if not isinstance(bar, dict):
            continue
        ts = parse_utc(bar.get("t") or bar.get("timestamp"))
        try:
            price = float(bar.get("c", bar.get("close", 0)))
        except (TypeError, ValueError, OverflowError):
            continue
        if ts is None or not (price > 0.0 and math.isfinite(price)):
            continue
        # Alpaca 1Min bar timestamps indicate start of candle; label only
        # when the full candle is completed (not a live forming bar).
        completed_at = ts + timedelta(seconds=60)
        if completed_at <= at:
            marks[completed_at.timestamp()] = price
    return sorted(marks.items())


def horizon_price(
    entry_ts: Any,
    horizon_minutes: int,
    marks: list[tuple[float, float]],
    *,
    max_delay_seconds: float = MAX_MARK_DELAY_SECONDS,
) -> float | None:
    """Earliest completed bar in [T+H, T+H+max_delay], otherwise no label."""
    ts = parse_utc(entry_ts)
    if ts is None or horizon_minutes <= 0 or not marks:
        return None
    target = ts.timestamp() + horizon_minutes * 60
    i = bisect.bisect_left(marks, (target, 0.0))
    if i >= len(marks):
        return None
    end_epoch, price = marks[i]
    if end_epoch > target + max_delay_seconds:
        return None
    return price

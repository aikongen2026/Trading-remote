"""Bounded local research/telemetry and compact dashboard responses.

Never deletes broker history, credentials, execution state or trade plans.
The 1 GB disk and 5 GB egress allowance are separate budgets.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path

MIB = 1024 * 1024
_CACHE = {}


def disk_budget(state_dir, *, refresh=False):
    root = Path(state_dir)
    key = str(root.resolve())
    cached = _CACHE.get(key)
    if not refresh and cached and time.monotonic() - cached[0] < 15:
        return dict(cached[1])
    # This is a ceiling, never a request to resize the Render disk.
    cap = min(1024 * MIB, max(64 * MIB, int(float(os.getenv('TRADINGBOT_STATE_BUDGET_MB', '850')) * MIB)))
    used = 0
    for p in root.rglob('*'):
        try:
            if p.is_file() and not p.is_symlink():
                used += p.stat().st_size
        except FileNotFoundError:
            continue  # atomic status/snapshot replacement raced the inventory
    free = shutil.disk_usage(root).free
    reserve = 96 * MIB
    remaining = max(0, min(cap - used, free - reserve))
    out = {'cap_mb': round(cap / MIB, 1), 'used_mb': round(used / MIB, 1),
           'remaining_mb': round(remaining / MIB, 1), 'reserve_mb': 96,
           'telemetry_allowed': remaining > 32 * MIB,
           'new_entries_blocked': remaining < 16 * MIB,
           'scope': 'state_directory_including_sqlite_wal_and_snapshots'}
    _CACHE[key] = (time.monotonic(), out)
    return dict(out)


def telemetry_allowed(state_dir):
    try:
        return disk_budget(state_dir)['telemetry_allowed']
    except (OSError, ValueError):
        return False


def stream_quotes_first(symbols, intelligence, stale_sec, rest_fetch):
    """REST is only used for symbols lacking a fresh, valid stream quote."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    live, missing = {}, []
    lock = getattr(intelligence, 'lock', None)
    if lock is not None:
        with lock:
            live = {s: dict(intelligence.live_quotes.get(s.upper().replace('/', '').replace('-', '')) or {}) for s in symbols}
    out = {}
    for symbol in symbols:
        q = live.get(symbol) or {}
        try:
            stamp = datetime.fromisoformat(str(q['ts']).replace('Z', '+00:00')).timestamp()
            bid, ask = float(q['bid']), float(q['ask'])
            if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid and -2 <= now - stamp <= stale_sec):
                raise ValueError('stale_or_invalid')
            out[symbol.upper()] = {'bp': bid, 'ap': ask, 't': q['ts']}
        except (ValueError, KeyError, TypeError, OverflowError):
            missing.append(symbol)
    if missing:
        try:
            out.update(rest_fetch(missing) or {})
        except Exception:
            if not out:
                raise
    return out


def merge_stream_bars(cache, symbols, intelligence, limit=160):
    """Drain bounded minute-bar history already received over the existing stream.

    No REST call and no invented timestamps. Forming bars are excluded.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    lock = getattr(intelligence, 'lock', None)
    if lock is None:
        return
    with lock:
        history = getattr(intelligence, 'stream_bar_history', {})
        rows = {s: list(history.get(s.upper().replace('/', '').replace('-', '')) or []) for s in symbols}
    for symbol, bars in rows.items():
        if not cache.get_closes(symbol):
            continue  # a stream fragment must not suppress the cold backfill
        valid = []
        for b in bars:
            try:
                stamp = datetime.fromisoformat(str(b['t']).replace('Z', '+00:00')).timestamp()
                if stamp + 60 <= now:
                    valid.append(b)
            except (ValueError, KeyError, TypeError):
                pass
        if valid:
            # merge() timestamps the refresh. Only merge when we actually got
            # a newer/revised bar so a dead stream cannot suppress REST recovery.
            old = cache.get_bars(symbol)
            indexed = {str(b.get('t')): b for b in old}
            if any(indexed.get(str(b.get('t'))) != b for b in valid):
                cache.merge(symbol, valid, limit)
                latest_end = max(datetime.fromisoformat(str(b['t']).replace('Z', '+00:00')).timestamp() + 60 for b in valid)
                if now - latest_end > 120:
                    cache.last_refresh_ts[symbol.upper()] = 0


def register_compact_responses(app):
    from flask import request

    @app.after_request
    def compress_dashboard(response):
        # Never compress credential pairing, login, OAuth or reflected errors.
        if request.method != 'GET' or request.path not in {'/', '/api/status', '/api/research'} or response.status_code != 200:
            return response
        if response.is_streamed or response.direct_passthrough:
            return response
        raw = response.get_data()
        if request.path == '/api/status':
            tag = hashlib.sha256(raw).hexdigest()
            response.set_etag(tag)
            response.headers['Cache-Control'] = 'private, no-cache'
            if request.if_none_match.contains(tag):
                response.status_code = 304
                response.set_data(b'')
                return response
        response.vary.add('Accept-Encoding')
        if len(raw) >= 1024 and request.accept_encodings['gzip'] > 0 and not response.headers.get('Content-Encoding'):
            packed = gzip.compress(raw, compresslevel=3, mtime=0)
            if len(packed) < len(raw):
                response.set_data(packed)
                response.headers['Content-Encoding'] = 'gzip'
        return response


def compact_status(status):
    """Keep every displayed field; omit verbose news payloads on routine polling."""
    out = dict(status)
    intel = dict(out.get('intelligence') or {})
    intel.pop('latest_news', None)
    out['intelligence'] = intel
    edge = dict(out.get('alpaca_edge_v49') or {})
    edge.pop('session_authority', None)
    out['alpaca_edge_v49'] = edge
    return out

"""Local audit evidence and indicator-independent stock risk supervision."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from execution_guard import flatten_orders, open_sell_orders


def epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def storage_health(state_dir):
    """Do not mistake a directory name or a writable file for a persistent disk."""
    render = str(os.getenv('RENDER', '')).lower() == 'true'
    resolved = Path(state_dir).resolve()
    persistent = not render
    # The blueprint declares /var/data as the disk mount. Render containers
    # may not expose it through ismount; use a strict resolved descendant check.
    disk = Path('/var/data').resolve()
    if render and os.name != 'nt' and disk.is_dir() and resolved.is_relative_to(disk):
        persistent = True
    if render and os.name != 'nt':
        for parent in (resolved, *resolved.parents):
            if str(parent) == '/':
                break
            if os.path.ismount(parent):
                persistent = True
                break
    return {'persistent': persistent, 'state_dir': str(resolved),
            'warning': '' if persistent else 'Render filesystem is ephemeral; audit history is lost on redeploy/restart.'}


class RuntimeAudit:
    def __init__(self, state_dir):
        self.path = str(Path(state_dir) / 'runtime_audit.sqlite')
        self.last_error = ''
        self.last_write = None
        self.last_capture = 0
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS bars(symbol TEXT,ts TEXT,feed TEXT,payload TEXT,PRIMARY KEY(symbol,ts,feed))')
                db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,symbol TEXT,payload TEXT)')
        except Exception as exc:
            self.last_error = type(exc).__name__ + ': ' + str(exc)

    def event(self, kind, symbol, payload):
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                           (datetime.now(timezone.utc).isoformat(), kind, symbol,
                            json.dumps(payload, ensure_ascii=False, default=str)))
            self.last_write = datetime.now(timezone.utc).isoformat()
            self.last_error = ''
        except Exception as exc:
            self.last_error = type(exc).__name__ + ': ' + str(exc)

    def capture_bars(self, bars_by_symbol, feed):
        if time.time() - self.last_capture < 60:
            return
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                rows = [(s, str(b.get('t') or b.get('timestamp')), feed, json.dumps(b))
                        for s, bars in bars_by_symbol.items() for b in bars
                        if isinstance(b, dict) and (b.get('t') or b.get('timestamp'))]
                db.executemany('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)', rows)
                db.execute("DELETE FROM bars WHERE ts < ?", (datetime.fromtimestamp(time.time()-35*86400, timezone.utc).isoformat(),))
                db.execute("DELETE FROM events WHERE ts < ?", (datetime.fromtimestamp(time.time()-35*86400, timezone.utc).isoformat(),))
            self.last_capture = time.time()
            self.last_write = datetime.now(timezone.utc).isoformat()
            self.last_error = ''
        except Exception as exc:
            self.last_error = type(exc).__name__ + ': ' + str(exc)

    def summary(self):
        return {'last_write_utc': self.last_write, 'last_error': self.last_error,
                'retention_days': 35, **storage_health(Path(self.path).parent)}


def read_evidence(state_dir, start, end):
    path = Path(state_dir) / 'runtime_audit.sqlite'
    result = {'events': [], 'bars': {}, 'errors': [], **storage_health(state_dir)}
    if not path.exists():
        result['errors'].append('runtime_audit.sqlite missing')
        return result
    lo, hi = epoch(start), epoch(end)
    try:
        with closing(sqlite3.connect(str(path), timeout=5)) as db:
            for ts, kind, symbol, raw in db.execute('SELECT ts,kind,symbol,payload FROM events ORDER BY id'):
                at = epoch(ts)
                if at is not None and lo <= at <= hi:
                    result['events'].append({'ts': ts, 'kind': kind, 'symbol': symbol, 'data': json.loads(raw)})
            for symbol, ts, feed, raw in db.execute('SELECT symbol,ts,feed,payload FROM bars ORDER BY ts'):
                at = epoch(ts)
                if at is not None and lo <= at <= hi:
                    bar = json.loads(raw)
                    bar['audit_feed'] = feed
                    result['bars'].setdefault(symbol, []).append(bar)
    except Exception as exc:
        result['errors'].append(type(exc).__name__ + ': ' + str(exc))
    return result


def recover_stock_entry(symbol, position, orders, plan):
    """Recover surviving bot lots; do not claim ownership of unrelated/manual shares."""
    flat = {str(o.get('id') or o.get('client_order_id')): o for o in flatten_orders(orders)}
    filled = [o for o in flat.values() if str(o.get('symbol', '')).upper() == symbol
              and float(o.get('filled_qty') or 0) > 0 and epoch(o.get('filled_at')) is not None]
    lots = []
    for order in sorted(filled, key=lambda o: epoch(o['filled_at'])):
        qty = float(order['filled_qty'])
        if order.get('side') == 'buy':
            lots.append([qty, order])
        elif order.get('side') == 'sell':
            while qty > 1e-8 and lots:
                take = min(qty, lots[0][0]); qty -= take; lots[0][0] -= take
                if lots[0][0] <= 1e-8:
                    lots.pop(0)
    actual = float(position.get('qty') or 0)
    if lots and abs(sum(q for q, _ in lots)-actual) < 1e-6:
        if all(str(o.get('client_order_id') or '').startswith('tbe-') for _, o in lots):
            order = lots[0][1]
            return epoch(order['filled_at']), order
        return None, None
    # A saved confirmed fill survives a truncated order-history window.
    if plan.get('entry_filled_at') and abs(float(plan.get('confirmed_qty') or 0)-actual) < 1e-6:
        return epoch(plan['entry_filled_at']), None
    return None, None


def supervise_stocks(api, positions, orders, plans, guard, control, clock, audit, now=None):
    """Run before quotes/signals. A pause stops entries, while protection continues."""
    now = time.time() if now is None else now
    result = {'actions': {}, 'unknown_positions': [], 'missing_protection': []}
    regular = bool(clock.get('is_open'))
    close = epoch(clock.get('next_close'))
    flatten = regular and close is not None and 0 <= close-now <= 60*float(control.get('stock_flatten_before_close_min', 5))
    enabled = bool(control.get('armed')) and not control.get('kill') and not control.get('test_mode')
    for pos in positions:
        symbol = str(pos.get('symbol') or '').upper()
        qty = float(pos.get('qty') or 0)
        if qty <= 0 or '/' in symbol or str(pos.get('asset_class', '')).lower() == 'crypto':
            continue
        key = 'STOCK:' + symbol
        plan = plans.get(key) or {}
        started, entry = recover_stock_entry(symbol, pos, orders, plan)
        if started is None:
            result['unknown_positions'].append(symbol)
            continue
        if entry:
            plan = dict(plan)
            if plan.get('entry_client_order_id') != entry.get('client_order_id'):
                plan = {}
            plan.update({'entry_filled_at': entry['filled_at'], 'confirmed_qty': qty,
                         'entry_client_order_id': entry.get('client_order_id'), 'order_id': entry.get('id')})
            avg = float(pos.get('avg_entry_price') or 0)
            for leg in entry.get('legs') or []:
                if avg > 0 and leg.get('stop_price'):
                    plan['stop_loss_pct'] = max(0, 1-float(leg['stop_price'])/avg)
                if avg > 0 and leg.get('limit_price'):
                    plan['take_profit_pct'] = max(0, float(leg['limit_price'])/avg-1)
            plans[key] = plan
        sells = open_sell_orders(orders, symbol)
        protected = any(str(o.get('type') or o.get('order_type')).lower() in {'stop', 'stop_limit', 'trailing_stop'} for o in sells)
        if not protected:
            result['missing_protection'].append(symbol)
        age = max(0, now-started)
        reason = ''
        if flatten:
            reason = 'CLOSE_FLATTEN'
        elif float(control.get('max_hold_sec', 1800)) > 0 and age >= float(control.get('max_hold_sec', 1800)):
            reason = 'MAX_HOLD'
        elif not protected and regular and control.get('broker_bracket_enabled', True):
            # An unprotected owned position is closed, rather than depending on indicators.
            reason = 'MISSING_PROTECTION'
        existing = guard.get_exit('STOCK', symbol)
        if enabled and regular and (reason or existing):
            action = guard.advance_exit(api, orders, market='STOCK', symbol=symbol, position_qty=qty,
                                        reason=reason or existing.get('latest_reason') or existing.get('reason'),
                                        regular_market_open=True, extended_session_open=False)
            result['actions'][symbol] = {'action': action, 'reason': reason, 'held_sec': round(age, 1)}
            state = guard.get_exit('STOCK', symbol) or {}
            if isinstance(state, dict):
                result['actions'][symbol].update({'order_id': state.get('order_id'),
                                                  'client_order_id': state.get('client_order_id')})
            if action != 'EXIT_PENDING':
                audit.event('RISK_EXIT', symbol, {**result['actions'][symbol], 'entry_filled_at': plan.get('entry_filled_at')})
    return result


class EntryConfirmation:
    """Two-stage confirmation for stock entries.

    A candidate must survive for confirm_sec *and* avoid deteriorating while it
    waits. This is deliberately lightweight: it confirms price progress and
    rejects strongly negative micro/relative/sector context without inventing a
    second independent trading strategy.
    """

    def __init__(self):
        self.candidates = {}

    def ready(
        self,
        symbol,
        strategy,
        eligible,
        now,
        confirm_sec=20,
        *,
        price=None,
        min_progress_bps=1.0,
        micro=0.0,
        relative=0.0,
        sector_ok=True,
    ):
        if not eligible:
            self.candidates.pop(symbol, None)
            return False

        try:
            px = float(price or 0.0)
        except Exception:
            px = 0.0
        try:
            micro_v = float(micro or 0.0)
        except Exception:
            micro_v = 0.0
        try:
            relative_v = float(relative or 0.0)
        except Exception:
            relative_v = 0.0

        prior = self.candidates.get(symbol)
        stale_after = max(180.0, float(confirm_sec) * 3.0)
        if not prior or prior.get("strategy") != strategy or now - float(prior.get("last_seen", now)) > stale_after:
            self.candidates[symbol] = {
                "strategy": strategy,
                "first_seen": now,
                "last_seen": now,
                "first_price": px,
            }
            return float(confirm_sec) <= 0.0

        prior["last_seen"] = now
        first_price = float(prior.get("first_price") or px or 0.0)
        age = max(0.0, now - float(prior.get("first_seen", now)))
        if age < float(confirm_sec):
            return False

        # Trend/scalp/event entries should show at least a tiny amount of price
        # follow-through. Mean reversion only requires that the candidate has
        # stopped deteriorating.
        progress_bps = 0.0
        if first_price > 0 and px > 0:
            progress_bps = (px / first_price - 1.0) * 10000.0
        required = 0.0 if str(strategy).lower() == "meanrev" else float(min_progress_bps)

        context_ok = bool(sector_ok) and micro_v >= -4.0 and relative_v >= -5.0
        if not context_ok or progress_bps < required:
            # Keep the candidate alive briefly, but move the reference forward
            # so a later genuine confirmation can still pass.
            if px > 0:
                prior["first_price"] = px
            prior["first_seen"] = now
            return False
        return True

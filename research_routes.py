"""Authenticated on-demand PAPER research. Local data only, bounded work/cache."""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_research import SYMBOLS, comparison, load_candles
from resource_policy import disk_budget


def realized_review(state_dir, now=None):
    """Separate exact bot attribution from manual/unknown matched FIFO legs.

    Uses broker-recorded activities; never fabricates fills from signals.
    A missing fee record is not proof of zero fees.
    """
    root = Path(state_dir)
    path = root / 'broker_history.sqlite'
    out = {'days': 7, 'available': False, 'bot_closed_legs': 0, 'bot_gross_usd': 0,
           'manual_or_unattributed_closed_legs': 0, 'manual_or_unattributed_gross_usd': 0,
           'recorded_usd_fee_cashflow': 0, 'non_usd_fee_rows': 0,
           'bot_net_usd': None, 'fee_status': 'not_verified',
           'note': 'Matched FIFO legs, gross before fees. Unmatched exits and missing fees are not assumed zero.'}
    if not path.exists():
        return out
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    try:
        from trade_export import _trade_ledger, _parse_ts
        with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
            payloads = db.execute('SELECT payload FROM activities LIMIT 50001').fetchall()
        if len(payloads) > 50000:
            return {**out, 'error': 'archive_sample_limit'}
        activities = [json.loads(r[0]) for r in payloads]
        fills = [a for a in activities if str(a.get('activity_type')).upper() == 'FILL']
        bot_orders = set()
        learning = root / 'learning_v3.sqlite'
        if learning.exists():
            with closing(sqlite3.connect('file:' + learning.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
                entries = db.execute("SELECT order_id, client_order_id FROM decisions WHERE event_type='ENTRY' AND action LIKE 'BUY%' ORDER BY id DESC LIMIT 10000").fetchall()
            bot_orders.update(str(r[0]) for r in entries if r[0])
            cids = {r[1] for r in entries if r[1]}
            with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
                orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders LIMIT 50000')]
            bot_orders.update(str(o['id']) for o in orders if o.get('id') and o.get('client_order_id') in cids)
        trades = [t for t in _trade_ledger(fills) if _parse_ts(t['exit_time']) and since <= _parse_ts(t['exit_time']) <= now]
        for t in trades:
            bot = t['entry_order_id'] in bot_orders
            prefix = 'bot' if bot else 'manual_or_unattributed'
            out[prefix + '_closed_legs'] += 1
            out[prefix + '_gross_usd'] += t['gross_pnl_usd']
        for a in activities:
            stamp = _parse_ts(a.get('transaction_time') or a.get('created_at') or a.get('date'))
            if str(a.get('activity_type')).upper() not in {'FEE', 'CFEE'} or not stamp or not since <= stamp <= now:
                continue
            if a.get('qty') is not None or str(a.get('currency') or 'USD').upper() != 'USD':
                out['non_usd_fee_rows'] += 1
            else:
                out['recorded_usd_fee_cashflow'] += float(a.get('net_amount') or 0)
        out['bot_gross_usd'] = round(out['bot_gross_usd'], 4)
        out['manual_or_unattributed_gross_usd'] = round(out['manual_or_unattributed_gross_usd'], 4)
        out['available'] = True
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        out['error'] = 'archive_unavailable'
    return out


def edge_diagnostics(state_dir, days=7):
    path = Path(state_dir) / 'learning_v3.sqlite'
    out = {'days': days, 'recorded_decisions': 0, 'crypto_observations': 0,
           'crypto_positive_net_ev': 0, 'stale_quotes': 0, 'stale_bars': 0,
           'render_blocked_candidates': 0, 'sample_limited': False,
           'probability_note': 'P(up) is not a validated probability of target before stop.'}
    if not path.exists():
        return out
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
            rows = db.execute('SELECT market,action,expected_value_bps,estimated_cost_bps,quote_age_sec FROM decisions WHERE ts>=? ORDER BY id DESC LIMIT 10000', (since,)).fetchall()
        out['recorded_decisions'] = len(rows)
        out['sample_limited'] = len(rows) == 10000
        crypto = [r for r in rows if r[0] == 'CRYPTO' and r[2] is not None]
        out['crypto_observations'] = len(crypto)
        out['crypto_positive_net_ev'] = sum(float(r[2]) > 0 for r in crypto)
        out['stale_quotes'] = sum(str(r[1]).startswith('QUOTE_STALE') for r in rows)
        out['stale_bars'] = sum(r[1] == 'WAIT_FRESH_BARS' for r in rows)
        out['render_blocked_candidates'] = sum(r[1] == 'RENDER_USAGE_GUARD' for r in rows)
        if crypto:
            out['best_crypto_net_ev_bps'] = round(max(float(r[2]) for r in crypto), 2)
            costs = [float(r[3]) for r in crypto if r[3] is not None]
            out['median_crypto_cost_bps'] = round(statistics.median(costs), 2) if costs else None
    except (sqlite3.Error, ValueError, TypeError):
        out['error'] = 'journal_unavailable'
    return out


def register_research(app, state_dir, read_json, same_origin):
    from flask import jsonify, request
    root = Path(state_dir)
    lock = threading.Lock()
    state = {'busy': False, 'last_started': 0, 'error': ''}
    review_cache = {'at': 0, 'data': {}}
    cache_path = root / 'paper_research_results.json'

    def is_paper():
        return os.getenv('ALPACA_PAPER', 'true').strip().lower() in {'true', '1', 'yes', 'on'}

    def read_results():
        saved = read_json(cache_path, {})
        return saved if isinstance(saved, dict) else {}

    def run():
        try:
            if not is_paper():
                raise ValueError('paper_only_required')
            candles = load_candles(root)
            control = read_json(root / 'control.json', {})
            status = read_json(root / 'status.json', {})
            symbols = status.get('symbols') or {}
            reports = {}
            for symbol in SYMBOLS:
                spread = (symbols.get(symbol) or {}).get('debug', {}).get('spread_bps')
                # A missing spread is an explicit assumption, not zero cost.
                spread = max(0, float(spread)) if spread is not None else 10
                reports[symbol] = comparison(candles.get(symbol, []),
                    fee_bps=max(0, float(control.get('crypto_taker_fee_bps', 25))),
                    spread_bps=spread,
                    slippage_bps=max(0, float(control.get('crypto_slippage_bps', 4))))
                reports[symbol]['spread_assumption_bps'] = spread
            data = {'paper': True, 'mode': 'RESEARCH_ONLY', 'auto_promotion': False,
                    'updated_at': datetime.now(timezone.utc).isoformat(), 'symbols': reports,
                    'edge_diagnostics': edge_diagnostics(root), 'storage_budget': disk_budget(root, refresh=True)}
            raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            if len(raw.encode()) > 256 * 1024:
                raise ValueError('research_result_budget')
            tmp = cache_path.with_suffix('.tmp')
            tmp.write_text(raw, encoding='utf-8')
            os.replace(tmp, cache_path)
            state['error'] = ''
        except Exception as exc:
            state['error'] = type(exc).__name__
        finally:
            with lock:
                state['busy'] = False

    @app.get('/api/research')
    def research_status():
        if not is_paper():
            return jsonify({'error': 'paper_only_required'}), 403
        if time.monotonic() - review_cache['at'] > 60:
            review_cache.update(at=time.monotonic(), data=realized_review(root))
        return jsonify({'paper': True, 'busy': state['busy'], 'error': state['error'],
                        'results': read_results(), 'realized_review': review_cache['data']})

    @app.post('/api/research/run')
    def research_run():
        if not is_paper():
            return jsonify({'error': 'paper_only_required'}), 403
        if not same_origin():
            return jsonify({'error': 'same_origin_required'}), 403
        if not request.is_json:
            return jsonify({'error': 'json_required'}), 400
        with lock:
            if state['busy']:
                return jsonify({'paper': True, 'busy': True}), 202
            if state['last_started'] and time.monotonic() - state['last_started'] < 900:
                return jsonify({'error': 'research_cooldown', 'retry_after_sec': 900}), 429
            if not disk_budget(root)['telemetry_allowed']:
                return jsonify({'error': 'storage_budget'}), 429
            state.update(busy=True, last_started=time.monotonic())
        threading.Thread(target=run, name='paper-research', daemon=True).start()
        return jsonify({'paper': True, 'busy': True}), 202

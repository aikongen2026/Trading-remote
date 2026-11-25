"""Offline, long-only strategy comparisons. No broker client or order methods.

Uses only existing candles, rejects gaps/forming candles, enters on the next
minute, charges realistic round-trip costs and treats ambiguous bars as losses.
Parameters are fixed before the chronological 70/30 development/holdout split.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ('BTC/USD', 'ETH/USD', 'SOL/USD')
MAX_BARS = 4096
STRATEGIES = ('sma_reference', 'trend_pullback', 'donchian_breakout', 'range_reversion')
_LAST_SAVE = {}


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def clean_bars(bars, now=None):
    now = time.time() if now is None else timestamp(now)
    out = {}
    for raw in bars:
        try:
            t = timestamp(raw.get('t', raw.get('timestamp')))
            vals = {k: float(raw[k]) for k in ('o', 'h', 'l', 'c', 'v')}
            if not all(math.isfinite(v) for v in vals.values()):
                continue
            if t % 60 or t + 60 > now or min(vals[k] for k in ('o', 'h', 'l', 'c')) <= 0 or vals['v'] < 0:
                continue
            if vals['l'] > min(vals['o'], vals['c']) or vals['h'] < max(vals['o'], vals['c']) or vals['l'] > vals['h']:
                continue
            out[t] = {'t': t, **vals}
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return [out[t] for t in sorted(out)][-MAX_BARS:]


def aggregate(bars, minutes):
    """Only complete UTC-aligned buckets; a gap is never forward-filled."""
    grouped = {}
    for b in bars:
        start = int(b['t'] // (minutes * 60)) * minutes * 60
        grouped.setdefault(start, []).append(b)
    out = []
    for start, rows in sorted(grouped.items()):
        if [b['t'] for b in rows] != [start + n * 60 for n in range(minutes)]:
            continue
        out.append({'t': start, 'end': start + minutes * 60, 'o': rows[0]['o'],
                    'h': max(b['h'] for b in rows), 'l': min(b['l'] for b in rows),
                    'c': rows[-1]['c'], 'v': sum(b['v'] for b in rows)})
    return out


def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    for value in values[period:]:
        result += 2 / (period + 1) * (value - result)
    return result


def atr_adx(bars, period=14):
    if len(bars) < 2 * period + 1:
        return None, None
    trs, plus, minus = [], [], []
    for a, b in zip(bars, bars[1:]):
        trs.append(max(b['h'] - b['l'], abs(b['h'] - a['c']), abs(b['l'] - a['c'])))
        up, down = b['h'] - a['h'], a['l'] - b['l']
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
    tr, p, m = sum(trs[:period]), sum(plus[:period]), sum(minus[:period])
    dx = []
    for i in range(period - 1, len(trs)):
        if i >= period:
            tr, p, m = tr - tr / period + trs[i], p - p / period + plus[i], m - m / period + minus[i]
        pd, md = (100 * p / tr, 100 * m / tr) if tr else (0, 0)
        dx.append(100 * abs(pd - md) / (pd + md) if pd + md else 0)
    adx = sum(dx[:period]) / period
    for value in dx[period:]:
        adx += (value - adx) / period
    return tr / period, adx


def setup(bars, hourly, strategy):
    if len(bars) < 32 or any(b['t'] - a['t'] != bars[-1]['end'] - bars[-1]['t'] for a, b in zip(bars[-32:], bars[-31:])):
        return None
    rows = bars[-90:]
    closes = [b['c'] for b in rows]
    last, prev = rows[-1], rows[-2]
    atr, adx = atr_adx(rows)
    if not atr or atr <= 0:
        return None
    level = ema(closes, 20)
    volume = sum(b['v'] for b in rows[-21:-1]) / 20
    volume_ok = volume > 0 and last['v'] >= volume * 1.2
    high, low = max(b['h'] for b in rows[-21:-1]), min(b['l'] for b in rows[-21:-1])
    stop, target = None, None
    if strategy == 'sma_reference':
        # Transparent comparator only, NOT a reproduction of production routing.
        if sum(closes[-8:]) / 8 <= sum(closes[-24:]) / 24 or last['c'] <= prev['c']:
            return None
        stop, target = last['c'] - 2 * atr, last['c'] + 4 * atr
    elif strategy in {'trend_pullback', 'donchian_breakout'}:
        if len(hourly) < 51 or any(b['t'] - a['t'] != 3600 for a, b in zip(hourly[-51:], hourly[-50:])):
            return None
        hc = [b['c'] for b in hourly[-90:]]
        if not (hc[-1] > ema(hc, 50) > ema(hc[:-1], 50) and last['c'] > ema(hc, 50) and adx >= 20 and volume_ok):
            return None
        if strategy == 'trend_pullback':
            if not (prev['l'] <= level + 0.25 * atr and last['c'] > prev['h'] and last['c'] > level):
                return None
            stop = min(b['l'] for b in rows[-4:]) - 0.25 * atr
            target = last['c'] + 2 * (last['c'] - stop)
        else:
            if last['c'] <= high:
                return None
            stop, target = last['c'] - 2 * atr, last['c'] + 6 * atr
    elif strategy == 'range_reversion':
        touches = sum(b['l'] <= low + 0.3 * atr for b in rows[-21:-1])
        upper_touches = sum(b['h'] >= high - 0.3 * atr for b in rows[-21:-1])
        if not (adx < 20 and touches >= 2 and upper_touches >= 2 and last['l'] <= low + 0.25 * atr and last['c'] > low and last['c'] > last['o']):
            return None
        stop, target = min(low, last['l']) - 0.25 * atr, (high + low) / 2
    if stop is None or not (0 < stop < last['c'] < target):
        return None
    return {'signal_at': last['end'], 'price': last['c'], 'stop': stop, 'target': target,
            'atr': atr, 'adx': adx, 'volume_ratio': last['v'] / volume if volume else None,
            'strategy': strategy, 'timeframe': str(int((last['end']-last['t'])/60))+'m', 'confirmation_timeframe': '1h' if strategy in {'trend_pullback', 'donchian_breakout'} else None}


def metrics(trades):
    values = [t['net_bps'] for t in trades]
    gain, loss = sum(max(x, 0) for x in values), -sum(min(x, 0) for x in values)
    equity, peak, dd = 1.0, 1.0, 0.0
    for value in values:
        equity *= max(0, 1 + value / 10000)
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak)
    return {'trades': len(values), 'win_rate_pct': round(100 * sum(x > 0 for x in values) / len(values), 2) if values else None,
            'net_expectancy_bps': round(statistics.mean(values), 3) if values else None,
            'profit_factor': round(gain / loss, 3) if loss else None,
            'profit_factor_note': 'no_losses' if values and not loss else 'no_trades' if not values else '',
            'max_drawdown_pct': round(dd * 100, 3), 'total_net_bps': round(sum(values), 3)}


def replay(bars, strategy, *, fee_bps=25, spread_bps=10, slippage_bps=4, execution='market', exit_mode='fixed', split_at=None, timeframe_minutes=5):
    five, hourly = aggregate(bars, timeframe_minutes), aggregate(bars, 60)
    hour_ends = [b['end'] for b in hourly]
    by_time = {b['t']: i for i, b in enumerate(bars)}
    trades, busy_until, missed = [], 0, 0
    for i in range(31, len(five)):
        signal_time = five[i]['end']
        if signal_time < busy_until:
            continue
        # Slice ends at the decision: no later hourly candle can enter the filter.
        n_hour = bisect.bisect_right(hour_ends, signal_time)
        candidate = setup(five[:i + 1], hourly[:n_hour], strategy)
        if not candidate or signal_time not in by_time:
            continue
        n = by_time[signal_time]
        first = bars[n]
        entry_fee = fee_bps
        entry = first['o']
        if execution == 'resting_limit':
            entry = min(first['o'], candidate['price'] * 0.9995)
            # Rest for one minute at a nonmarketable price. No optimistic
            # maker claim; fee remains taker unless confirmed by a broker fill.
            if first['l'] > entry:
                missed += 1
                continue
        stop, target = candidate['stop'], candidate['target']
        risk = entry - stop
        # Minimum 1.5:1 after cost. Do not improve EV by merely widening targets.
        costs = entry_fee + fee_bps + spread_bps + slippage_bps
        if risk <= 0 or target <= entry or (target - entry - entry * costs / 10000) / risk < 1.5:
            continue
        exit_price, exit_time, reason = None, None, None
        peak = entry
        prior_time = signal_time - 60
        for b in bars[n:n + 240]:
            if b['t'] > signal_time and b['t'] - prior_time != 60:
                reason = 'DATA_GAP'
                break
            prior_time = b['t']
            # Stops use only the previous bar's peak. Stop first when both hit.
            if b['l'] <= stop:
                exit_price, reason = min(b['o'], stop), 'STOP'
            elif exit_mode == 'fixed' and b['h'] >= target and (execution == 'market' or b['t'] > signal_time):
                exit_price, reason = max(b['o'], target), 'TARGET'
            elif b['t'] >= signal_time + 239 * 60:
                exit_price, reason = b['c'], 'TIME'
            if exit_price is not None:
                exit_time = b['t'] + 60
                break
            peak = max(peak, b['h']) if execution == 'market' or b['t'] > signal_time else peak
            if exit_mode == 'atr_trail' and peak - entry >= risk:
                stop = max(stop, peak - 2 * candidate['atr'])
        if exit_price is None:
            # An unresolved position blocks further entries; never turn a
            # missing path or a still-open position into a fictitious win.
            busy_until = bars[-1]['t'] + 60
            continue
        busy_until = exit_time
        if split_at and signal_time < split_at < exit_time:
            continue  # embargo trades crossing development/holdout boundary
        gross = (exit_price / entry - 1) * 10000
        trades.append({'entry_at': signal_time, 'exit_at': exit_time, 'entry': entry, 'exit': exit_price,
                       'gross_bps': gross, 'cost_bps': costs, 'net_bps': gross - costs, 'reason': reason,
                       'initial_risk_pct': risk / entry * 100})
    return trades, missed


def comparison(bars, *, fee_bps=25, spread_bps=10, slippage_bps=4, now=None):
    clean = clean_bars(bars, now)
    result = {'paper': True, 'mode': 'RESEARCH_ONLY', 'auto_promotion': False, 'bars': len(clean),
              'source': 'existing_closed_candles_only', 'models': [],
              'limitations': ['Historical simulation; no broker orders.',
                             'SMA reference is not the complete production strategy.',
                             'Fees, spread and slippage are assumptions; queue position is unknown.',
                             'Missing paths are excluded, not treated as profitable.',
                             'Results use unit notional; drawdown is a strategy-path statistic, not account drawdown.']}
    if len(clean) < 300:
        result['state'] = 'WAIT_HISTORY'
        return result
    split = clean[int(len(clean) * .7)]['t']
    completed_hours = len(aggregate(clean, 60))
    result['completed_hour_bars'] = completed_hours
    for strategy in STRATEGIES:
        for timeframe_minutes, execution, exit_mode in ((tf, ex, exit) for tf in (5, 15) for ex, exit in (('market', 'fixed'), ('resting_limit', 'fixed'), ('market', 'atr_trail'))):
            trades, missed = replay(clean, strategy, fee_bps=fee_bps, spread_bps=spread_bps,
                                     slippage_bps=slippage_bps, execution=execution, exit_mode=exit_mode, split_at=split, timeframe_minutes=timeframe_minutes)
            development = [t for t in trades if t['entry_at'] < split]
            holdout = [t for t in trades if t['entry_at'] >= split]
            result['models'].append({'strategy': strategy, 'execution': execution, 'exit_mode': exit_mode, 'timeframe_minutes': timeframe_minutes,
                                     'development': metrics(development), 'holdout': metrics(holdout),
                                     'data_ready': len(aggregate(clean, timeframe_minutes)) >= 32 and (strategy not in {'trend_pullback', 'donchian_breakout'} or completed_hours >= 51),
                                     'completed_hour_bars': completed_hours,
                                     'required_hour_bars': 51 if strategy in {'trend_pullback', 'donchian_breakout'} else 0,
                                     'missed_limits': missed, 'sufficient_samples': len(holdout) >= 30,
                                     'cost_bps': 2 * fee_bps + spread_bps + slippage_bps})
    result['state'] = 'EVALUATED'
    result['split_at_utc'] = datetime.fromtimestamp(split, timezone.utc).isoformat()
    return result


def persist_candles(state_dir, by_symbol, *, now=None):
    """Small bounded cache, no new data subscriptions or growing SQLite table."""
    root = Path(state_dir)
    at = time.time() if now is None else now
    if at - _LAST_SAVE.get(str(root), 0) < 300:
        return
    from resource_policy import telemetry_allowed
    if not telemetry_allowed(root):
        return
    previous = load_candles(root)
    data = {}
    for symbol in SYMBOLS:
        rows = clean_bars(previous.get(symbol, []) + by_symbol.get(symbol, []), now=at)
        if rows:
            data[symbol] = rows
    packed = gzip.compress(json.dumps(data, separators=(',', ':')).encode(), compresslevel=1, mtime=0)
    if len(packed) > 2 * 1024 * 1024:
        return
    path = root / 'paper_research_candles.json.gz'
    pending = path.with_suffix('.tmp')
    pending.write_bytes(packed)
    os.replace(pending, path)
    _LAST_SAVE[str(root)] = at


def load_candles(state_dir):
    path = Path(state_dir) / 'paper_research_candles.json.gz'
    if not path.exists():
        return {}
    try:
        with gzip.open(path, 'rt') as src:
            text = src.read(4 * 1024 * 1024 + 1)
        if len(text) > 4 * 1024 * 1024:
            return {}
        raw = json.loads(text)
        return {s: clean_bars(raw[s]) for s in SYMBOLS if raw.get(s)}
    except (OSError, ValueError, TypeError):
        return {}

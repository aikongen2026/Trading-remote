import json
import math
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paper_research import aggregate, atr_adx, clean_bars, comparison, load_candles, metrics, persist_candles, replay, setup
from research_routes import realized_review

BASE = 1704067200  # UTC hour boundary


def candles(n=400, base=BASE):
    result = []
    for i in range(n):
        price = 100 + i * .01 + math.sin(i / 9) * .15
        result.append({'t': base + i * 60, 'o': price, 'h': price + .2,
                       'l': price - .2, 'c': price + .01, 'v': 10})
    return result


class CandleTests(unittest.TestCase):
    def test_forming_future_invalid_and_nonfinite_bars_are_rejected(self):
        rows = candles(6)
        rows += [{**rows[0], 'c': float('nan')}, {**rows[1], 'l': 500}]
        good = clean_bars(rows, now=BASE + 5 * 60)
        self.assertEqual(len(good), 5)
        self.assertEqual(good[-1]['t'], BASE + 240)

    def test_aggregate_never_fills_missing_minutes(self):
        rows = candles(10)
        self.assertEqual(len(aggregate(rows, 5)), 2)
        del rows[2]
        grouped = aggregate(rows, 5)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]['t'], BASE + 300)

    def test_higher_timeframe_excludes_incomplete_bucket(self):
        self.assertEqual(len(aggregate(candles(119), 60)), 1)
        self.assertEqual(len(aggregate(candles(120), 60)), 2)

    def test_cache_is_bounded_and_roundtrips_only_market_candles(self):
        with tempfile.TemporaryDirectory() as root:
            persist_candles(root, {'BTC/USD': candles(4200)}, now=time.time())
            saved = load_candles(root)
            self.assertEqual(len(saved['BTC/USD']), 4096)
            self.assertLess((Path(root) / 'paper_research_candles.json.gz').stat().st_size, 2 * 1024 * 1024)
            self.assertEqual(set(saved), {'BTC/USD'})


class ReplayTests(unittest.TestCase):
    def test_rule_based_breakout_requires_trend_volume_and_closed_hour(self):
        five = [{'t': BASE+i*300, 'end': BASE+(i+1)*300, 'o': 100+i*.1,
                 'h': 100.2+i*.1, 'l': 99.8+i*.1, 'c': 100.1+i*.1, 'v': 20} for i in range(90)]
        five[-1].update(o=109, c=112, h=113, l=108, v=50)
        hour = [{'t': BASE+i*3600, 'end': BASE+(i+1)*3600,
                 'o': 50+i, 'h': 52+i, 'l': 49+i, 'c': 51+i, 'v': 100} for i in range(51)]
        self.assertIsNotNone(setup(five, hour, 'donchian_breakout'))
        five[-1]['v'] = 1
        self.assertIsNone(setup(five, hour, 'donchian_breakout'))
        five[-1]['v'] = 50
        self.assertIsNone(setup(five, hour[:50], 'donchian_breakout'))

    def test_adx_and_atr_distinguish_a_persistent_trend(self):
        rows = [{'t': BASE+i*300, 'o': 100+i, 'h': 102+i,
                 'l': 99+i, 'c': 101+i, 'v': 1} for i in range(60)]
        atr, adx = atr_adx(rows)
        self.assertAlmostEqual(atr, 3)
        self.assertAlmostEqual(adx, 100)

    def test_stop_wins_when_target_and_stop_hit_same_bar(self):
        bars = candles(200)
        trigger = BASE + 160 * 60
        bars[160].update(o=100, h=103, l=98, c=100)
        def signal(rows, hourly, strategy):
            if rows[-1]['end'] == trigger:
                return {'price': 100, 'stop': 99, 'target': 102, 'atr': .5}
        with patch('paper_research.setup', side_effect=signal):
            trades, _ = replay(bars, 'sma_reference', fee_bps=0, spread_bps=0, slippage_bps=0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['reason'], 'STOP')
        self.assertAlmostEqual(trades[0]['net_bps'], -100)

    def test_signal_enters_next_minute_not_previous_close(self):
        bars = candles(200)
        trigger = BASE + 160 * 60
        bars[160].update(o=101, h=103, l=100, c=102)
        def signal(rows, hourly, strategy):
            if rows[-1]['end'] == trigger:
                return {'price': 100, 'stop': 99, 'target': 105, 'atr': .5}
        bars[161].update(o=102, h=106, l=101, c=105)
        with patch('paper_research.setup', side_effect=signal):
            trades, _ = replay(bars, 'sma_reference', fee_bps=0, spread_bps=0, slippage_bps=0)
        self.assertEqual(trades[0]['entry'], 101)

    def test_two_fees_spread_and_slippage_are_charged(self):
        bars = candles(200)
        bars[160].update(o=100, h=103, l=99.5, c=102)
        def signal(rows, hourly, strategy):
            if rows[-1]['end'] == BASE + 160 * 60:
                return {'price': 100, 'stop': 99, 'target': 104, 'atr': .5}
        bars[161].update(o=102, h=105, l=101, c=104)
        with patch('paper_research.setup', side_effect=signal):
            trades, _ = replay(bars, 'sma_reference', fee_bps=25, spread_bps=3, slippage_bps=4)
        self.assertEqual(trades[0]['cost_bps'], 57)
        self.assertAlmostEqual(trades[0]['net_bps'], 343)

    def test_future_hour_candles_are_never_visible_to_signal(self):
        seen = []
        def inspect(rows, hourly, strategy):
            self.assertTrue(all(b['end'] <= rows[-1]['end'] for b in hourly))
            seen.append(len(hourly))
        with patch('paper_research.setup', side_effect=inspect):
            replay(candles(400), 'trend_pullback')
        self.assertTrue(seen)

    def test_no_orders_or_automatic_promotion_and_both_timeframes(self):
        result = comparison(candles(), now=BASE + 1000 * 60)
        self.assertTrue(result['paper'])
        self.assertFalse(result['auto_promotion'])
        self.assertEqual({m['timeframe_minutes'] for m in result['models']}, {5, 15})
        self.assertEqual(len(result['models']), 24)
        self.assertTrue(all(not m['sufficient_samples'] for m in result['models']))

    def test_no_fictitious_infinite_profit_factor_on_empty_sample(self):
        self.assertIsNone(metrics([])['profit_factor'])
        self.assertIsNone(metrics([])['net_expectancy_bps'])
        self.assertEqual(comparison([])['state'], 'WAIT_HISTORY')


class RealizedReviewTests(unittest.TestCase):
    def test_manual_position_is_not_counted_as_bot_profit(self):
        with tempfile.TemporaryDirectory() as root:
            now = datetime.now(timezone.utc)
            with closing(sqlite3.connect(Path(root) / 'broker_history.sqlite')) as db, db:
                db.execute('CREATE TABLE activities(id TEXT,payload TEXT)')
                for n, side, price in ((1, 'buy', 100), (2, 'sell', 102)):
                    raw = {'activity_type': 'FILL', 'symbol': 'AEP', 'side': side,
                           'price': price, 'qty': 2, 'transaction_time': now.isoformat(), 'order_id': str(n)}
                    db.execute('INSERT INTO activities VALUES(?,?)', (str(n), json.dumps(raw)))
            result = realized_review(root, now=now)
            self.assertTrue(result['available'])
            self.assertEqual(result['bot_gross_usd'], 0)
            self.assertEqual(result['manual_or_unattributed_gross_usd'], 4)
            self.assertIsNone(result['bot_net_usd'])


if __name__ == '__main__':
    unittest.main()

import gzip
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from resource_policy import compact_status, disk_budget, merge_stream_bars, stream_quotes_first
from trading_bot import BarsCache


class StreamTests(unittest.TestCase):
    def test_fresh_stream_quotes_do_not_make_rest_calls(self):
        rest = Mock()
        engine = SimpleNamespace(lock=threading.Lock(), live_quotes={
            'BTCUSD': {'ts': datetime.now(timezone.utc).isoformat(), 'bid': 99, 'ask': 100}})
        out = stream_quotes_first(['BTC/USD'], engine, 30, rest)
        rest.assert_not_called()
        self.assertEqual(out['BTC/USD']['bp'], 99)

    def test_only_missing_symbol_uses_rest(self):
        rest = Mock(return_value={'ETH/USD': {'bp': 50, 'ap': 51}})
        engine = SimpleNamespace(lock=threading.Lock(), live_quotes={
            'BTCUSD': {'ts': datetime.now(timezone.utc).isoformat(), 'bid': 99, 'ask': 100}})
        out = stream_quotes_first(['BTC/USD', 'ETH/USD'], engine, 30, rest)
        rest.assert_called_once_with(['ETH/USD'])
        self.assertEqual(len(out), 2)

    def test_stale_invalid_and_future_quotes_require_rest(self):
        for offset, bid, ask in ((-60, 99, 100), (60, 99, 100), (0, 101, 100)):
            rest = Mock(return_value={})
            engine = SimpleNamespace(lock=threading.Lock(), live_quotes={'BTCUSD': {
                'ts': (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(), 'bid': bid, 'ask': ask}})
            stream_quotes_first(['BTC/USD'], engine, 30, rest)
            rest.assert_called_once()

    def test_rest_error_does_not_discard_fresh_stream_quotes(self):
        engine = SimpleNamespace(lock=threading.Lock(), live_quotes={
            'BTCUSD': {'ts': datetime.now(timezone.utc).isoformat(), 'bid': 99, 'ask': 100}})
        out = stream_quotes_first(['BTC/USD', 'ETH/USD'], engine, 30, Mock(side_effect=RuntimeError()))
        self.assertEqual(set(out), {'BTC/USD'})

    def test_minute_stream_keeps_original_timestamps_and_ignores_forming_bar(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        good = {'t': (now-timedelta(minutes=1)).isoformat(), 'o': 1, 'h': 2, 'l': 1, 'c': 2, 'v': 1}
        forming = {**good, 't': now.isoformat()}
        cache = BarsCache()
        cache.replace('BTC/USD', [{**good, 't': (now-timedelta(minutes=2)).isoformat()}], 160)
        engine = SimpleNamespace(lock=threading.Lock(), stream_bar_history={'BTCUSD': [good, forming]})
        merge_stream_bars(cache, ['BTC/USD'], engine)
        self.assertEqual(cache.get_bars('BTC/USD')[-1], good)
        self.assertEqual(len(cache.get_bars('BTC/USD')), 2)

    def test_stream_fragment_cannot_suppress_initial_backfill(self):
        cache = BarsCache()
        engine = SimpleNamespace(lock=threading.Lock(), stream_bar_history={'BTCUSD': [
            {'t': (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(), 'c': 100}]})
        merge_stream_bars(cache, ['BTC/USD'], engine)
        self.assertTrue(cache.needs_refresh('BTC/USD', 600))
        self.assertEqual(cache.get_closes('BTC/USD'), [])


class BudgetTests(unittest.TestCase):
    def test_state_budget_counts_wal_and_snapshots_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'TRADINGBOT_STATE_BUDGET_MB': '64'}):
            path = Path(root) / 'learning_v3.sqlite-wal'
            with path.open('wb') as out:
                out.truncate(55 * 1024 * 1024)
            result = disk_budget(root, refresh=True)
            self.assertFalse(result['telemetry_allowed'])
            self.assertTrue(result['new_entries_blocked'])
            self.assertTrue(path.exists())

    def test_compact_diagnostics_does_not_mutate_original(self):
        original = {'intelligence': {'latest_news': ['large'], 'regime_score': 50}, 'paper': True}
        compact = compact_status(original)
        self.assertNotIn('latest_news', compact['intelligence'])
        self.assertIn('latest_news', original['intelligence'])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        import control_server
        self.server = control_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = patch.multiple(control_server, STATE_DIR=root, STATUS_PATH=root/'status.json', CONTROL_PATH=root/'control.json', AUTH_REQUIRED=False)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.client = control_server.app.test_client()
        self.server.STATUS_PATH.write_text(json.dumps({'paper': True, 'notes': 'repeated test data ' * 500,
            'render_usage': {'policy': 'OVER'}, 'intelligence': {'latest_news': ['large news payload']}}))

    def test_authenticated_status_compresses_and_supports_unchanged_response(self):
        plain = self.client.get('/api/status?compact=1')
        compressed = self.client.get('/api/status?compact=1', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertLess(len(compressed.data), len(plain.data) * .3)
        self.assertEqual(json.loads(gzip.decompress(compressed.data)), plain.json)
        self.assertTrue(plain.json['render_usage']['new_entries_blocked'])
        unchanged = self.client.get('/api/status?compact=1', headers={'If-None-Match': plain.headers['ETag']})
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.data, b'')

    def test_gzip_q_zero_is_respected(self):
        response = self.client.get('/api/status', headers={'Accept-Encoding': 'gzip;q=0'})
        self.assertNotIn('Content-Encoding', response.headers)

    def test_public_status_still_requires_login(self):
        with patch.object(self.server, 'AUTH_REQUIRED', True):
            self.assertEqual(self.client.get('/api/status').status_code, 401)
            self.assertEqual(self.client.get('/api/research').status_code, 401)

    def test_research_refuses_live_and_foreign_origins(self):
        with patch.dict(os.environ, {'ALPACA_PAPER': 'false'}):
            self.assertEqual(self.client.post('/api/research/run', headers={'Origin':'http://localhost'}).status_code, 403)
        with patch.dict(os.environ, {'ALPACA_PAPER': 'true'}):
            self.assertEqual(self.client.post('/api/research/run', headers={'Origin':'https://hostile.example'}).status_code, 403)

    def test_dashboard_names_account_change_and_keeps_paper_research_explicit(self):
        page = self.client.get('/').data.decode()
        self.assertIn('Kontoendring i dag', page)
        self.assertIn('Render trafikk', page)
        self.assertIn('Strategitester – kun PAPER', page)
        self.assertIn('/static/paper-research.js', page)


if __name__ == '__main__':
    unittest.main()

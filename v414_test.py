"""Offline guard regressions. Network access and order submission are mocked."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import corporate_backoff as cb
from render_usage_guard import RenderUsageGuard, policy_for_percent
from runtime_audit import storage_health
from saxo_sim import SaxoSimEngine, SaxoSimError
from trading_bot import AlpacaRest, AlpacaApiError


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        cb._until = cb._failures = 0
        self.sx = SaxoSimEngine(self.tmp.name)
        self.sx.enabled = True
        self.sx.access_token = 'fake-token'
        self.sx.session = Mock()

    def api(self):
        return AlpacaRest(SimpleNamespace(paper=True, trading_base='https://paper-api.alpaca.markets',
                                         data_base='https://data.alpaca.markets', key='fake', secret='fake'))

    def test_paper_lock(self):
        with self.assertRaisesRegex(RuntimeError, 'PAPER_ONLY_BLOCK'):
            AlpacaRest(SimpleNamespace(paper=False))
        api = self.api()
        api.s = Mock()
        with self.assertRaisesRegex(RuntimeError, 'PAPER_ONLY_BLOCK'):
            api._req('POST', 'https://api.alpaca.markets/v2/orders')
        api.s.request.assert_not_called()

    def test_429_no_immediate_retry(self):
        api = self.api()
        api.s = Mock()
        api.s.request.return_value = SimpleNamespace(status_code=429, headers={'Retry-After': '120'})
        for _ in range(2):
            with self.assertRaises(AlpacaApiError):
                api.account()
        self.assertEqual(api.s.request.call_count, 1)

    def test_corporate_parameters_and_shared_circuit(self):
        api = self.api()
        api.s = Mock()
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '3600'
        api.s.get.return_value = response
        with patch('corporate_backoff.time.monotonic', return_value=100):
            with self.assertRaises(requests.HTTPError):
                api.corporate_actions(['AAPL'], '2026-10-23', '2026-10-24')
            self.assertGreaterEqual(cb._until, 3700)
            with self.assertRaises(cb.CorporateCircuitOpen):
                cb.corporate_get(Mock(), 'https://data.alpaca.markets/v1/corporate-actions', {})
        self.assertNotIn('cas_region', api.s.get.call_args.kwargs['params'])
        self.assertEqual(api.s.get.call_count, 1)

    def test_corporate_probe_recovers(self):
        session = Mock()
        session.get.return_value = SimpleNamespace(raise_for_status=lambda: None, text='{}', json=lambda: {})
        cb._until = 100
        cb._failures = 2
        with patch('corporate_backoff.time.monotonic', return_value=101):
            self.assertEqual(cb.corporate_get(session, 'url', {}), {})
        self.assertEqual(cb._failures, 0)

    def test_render_boundaries(self):
        for pct, expected in [(59.99, 'NORMAL'), (60, 'WARN'), (75, 'SAVE'), (85, 'STRONG'), (95, 'HARD'), (100, 'OVER')]:
            self.assertEqual(policy_for_percent(pct), expected)
            Path(self.tmp.name, 'render_usage.json').write_text(json.dumps({'percent': pct}))
            self.assertEqual(self.sx._render_allows_norway(), pct < 60)
        self.assertEqual(self.sx.refresh_once()['state'], 'RENDER_GUARD')
        self.sx.session.request.assert_not_called()

    def test_render_meter_error_keeps_guard(self):
        Path(self.tmp.name, 'render_usage.json').write_text(json.dumps({'percent': 85, 'policy': 'STRONG'}))
        guard = RenderUsageGuard(self.tmp.name)
        guard.api_key = 'fake'
        guard.session = Mock()
        guard.session.get.side_effect = requests.Timeout()
        status = guard.snapshot()
        self.assertEqual((status['percent'], status['policy']), (85, 'STRONG'))

    def test_saxo_live_block(self):
        with patch('saxo_sim.SIM_BASE', 'https://gateway.saxobank.com/openapi'):
            with self.assertRaisesRegex(SaxoSimError, 'live endpoint refused'):
                self.sx._request('GET', '/port/v1/accounts/me')
        self.sx.session.request.assert_not_called()

    def test_saxo_status_requires_account(self):
        self.sx.account_key = 'configured'
        with patch.object(self.sx, '_request', return_value={'Data': []}):
            with self.assertRaisesRegex(SaxoSimError, 'ACCOUNT_NOT_FOUND'):
                self.sx._discover_account()
        self.assertFalse(self.sx._status()['account_connected'])

    def test_saxo_success_read_only(self):
        self.sx.symbols = ['EQNR']
        self.sx.instrument_cache = {'EQNR': {'uic': 1}}
        with patch.object(self.sx, '_request', side_effect=[{'Data': [{'AccountKey': 'sim', 'Active': True}]}, {'Data': []}]), patch.object(self.sx, '_discover_instruments'), patch.object(self.sx, '_oslo_open', return_value=False), patch.object(self.sx, '_place_market') as order:
            status = self.sx.refresh_once()
        self.assertEqual(status['state'], 'RUNNING_SIM')
        self.assertTrue(status['account_connected'])
        self.assertEqual(status['symbols_resolved'], 1)
        self.assertEqual(status['positions'], 0)
        self.assertTrue(status['last_successful_poll'])
        order.assert_not_called()

    def test_saxo_waiting_and_traffic_persistence(self):
        self.sx.access_token = ''
        self.assertEqual(self.sx.refresh_once()['state'], 'WAITING_FOR_SIM_TOKEN')
        self.sx._record_bytes(100, 200)
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.bytes_day, 300)
        self.assertEqual(restarted.bytes_month, 300)

    @unittest.skipIf(os.name == 'nt', 'Render runs on Linux')
    def test_render_disk_without_ismount(self):
        with patch.dict(os.environ, {'RENDER': 'true'}), patch('os.path.ismount', return_value=False), patch.object(Path, 'is_dir', return_value=True):
            self.assertTrue(storage_health('/var/data/tradingbot')['persistent'])
            self.assertFalse(storage_health('/var/database/tradingbot')['persistent'])
            self.assertFalse(storage_health('/tmp/tradingbot')['persistent'])


if __name__ == '__main__':
    unittest.main()

"""Offline export regression tests. No credentials, network, or trading orders."""
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import control_server
import trade_export


NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


class ReadOnlyAPI:
    cfg = SimpleNamespace(paper=True, stock_feed='iex', trading_base='https://example.invalid')

    def account(self):
        return {'equity': '999', 'currency': 'USD', 'account_number': 'private'}

    def positions(self):
        return []

    def _req(self, method, url, params=None):
        if method != 'GET':
            raise AssertionError('Exports must never mutate broker state')
        if url.endswith('/v2/orders'):
            return [
                {'id': 'buy', 'submitted_at': '2026-11-04T10:00:00Z', 'order_type': 'market'},
                {'id': 'sell', 'submitted_at': '2026-11-04T10:05:00Z', 'order_type': 'market'},
            ]
        if url.endswith('/v2/account/activities'):
            return [
                {'id': 'f1', 'order_id': 'buy', 'transaction_time': '2026-11-04T10:00:00Z',
                 'activity_type': 'FILL', 'side': 'buy', 'symbol': 'ABC', 'qty': '2', 'price': '100'},
                {'id': 'f2', 'order_id': 'sell', 'transaction_time': '2026-11-04T10:05:00Z',
                 'activity_type': 'FILL', 'side': 'sell', 'symbol': 'ABC', 'qty': '2', 'price': '99'},
            ]
        raise AssertionError(url)

    def portfolio_history(self, *args, **kwargs):
        return {'timestamp': [1793786400, 1793872800], 'equity': [1000, 998],
                'profit_loss': [0, -2], 'profit_loss_pct': [0, -.002]}

    def bars(self, *args, **kwargs):
        return {}

    def crypto_bars(self, *args, **kwargs):
        return {}


class ExportTests(unittest.TestCase):
    def test_file_export_has_complete_evidence_and_preserves_pnl(self):
        with tempfile.TemporaryDirectory() as state, patch.object(trade_export, '_now', return_value=NOW):
            Path(state, 'control.json').write_text(json.dumps({'password': 'do-not-export', 'armed': True}))
            output, manifest = trade_export.build_ai_analysis_export_file(7, state, 'test', ReadOnlyAPI())
            try:
                self.assertFalse(isinstance(output, io.BytesIO))
                with zipfile.ZipFile(output) as archive:
                    self.assertEqual(archive.namelist(), ['AI_ANALYSE.json'])
                    raw = archive.read('AI_ANALYSE.json')
                data = json.loads(raw)
                self.assertNotIn(b'do-not-export', raw)
                self.assertNotIn(b'private', raw)
                self.assertEqual(len(data['fills_raw']), 2)
                self.assertEqual(data['completed_trades'][0]['gross_pnl_usd'], -2)
                self.assertEqual(data['diagnostics']['all_matched_trades']['gross_pnl_usd'], -2)
                self.assertEqual(manifest['days'], 7)
                self.assertTrue(manifest['paper'])
            finally:
                output.close()

    def test_streaming_json_sanitizes_nested_secrets_and_unicode(self):
        data = {'text': 'æøå "\n', 'rows': [{'Authorization': 'hidden', 'n': 3}], 'tuple': (1, None)}
        encoded = ''.join(trade_export._json_chunks(data))
        self.assertEqual(json.loads(encoded), {'text': data['text'], 'rows': [{'n': 3}], 'tuple': [1, None]})

    def test_file_is_closed_when_build_fails(self):
        output = tempfile.TemporaryFile()
        with patch.object(trade_export.tempfile, 'TemporaryFile', return_value=output), \
             patch.object(trade_export, 'build_ai_analysis_export', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                trade_export.build_ai_analysis_export_file(7, '.', 'test')
        self.assertTrue(output.closed)

    def test_http_exports_are_serialized_without_blocking_other_requests(self):
        with patch.object(control_server, 'AUTH_REQUIRED', False):
            control_server._EXPORT_LOCK.acquire()
            try:
                with control_server.app.test_client() as client:
                    response = client.get('/api/export?days=7')
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.json['error'], 'export_in_progress')
                    self.assertEqual(response.headers['Retry-After'], '30')
            finally:
                control_server._EXPORT_LOCK.release()

    def test_http_failure_releases_lock(self):
        with patch.object(control_server, 'AUTH_REQUIRED', False), \
             patch.object(trade_export, 'build_ai_analysis_export_file', side_effect=RuntimeError('failed')):
            with control_server.app.test_client() as client:
                self.assertEqual(client.get('/api/export?days=7').status_code, 500)
            self.assertTrue(control_server._EXPORT_LOCK.acquire(blocking=False))
            control_server._EXPORT_LOCK.release()

    def test_http_download_streams_file_and_closes_it(self):
        output = tempfile.TemporaryFile()
        output.write(b'zip fixture')
        output.seek(0)
        with patch.object(control_server, 'AUTH_REQUIRED', False), \
             patch.object(trade_export, 'build_ai_analysis_export_file', return_value=(output, {'export_schema': 'test'})):
            with control_server.app.test_client() as client:
                response = client.get('/api/export?days=7', buffered=False)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content_length, 11)
                self.assertEqual(b''.join(response.response), b'zip fixture')
                response.close()
        self.assertTrue(output.closed)

    def test_export_still_requires_authentication(self):
        with patch.object(control_server, 'AUTH_REQUIRED', True):
            with control_server.app.test_client() as client:
                self.assertEqual(client.get('/api/export?days=7').status_code, 401)


if __name__ == '__main__':
    unittest.main()

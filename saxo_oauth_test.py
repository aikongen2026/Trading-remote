"""OAuth regressions: fake credentials, mocked HTTP, no broker orders."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from saxo_sim import SaxoSimEngine, SaxoSimError


class SaxoOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            'SAXO_SIM_ENABLED': 'true', 'SAXO_SIM_ACCESS_TOKEN': 'fake-access',
            'SAXO_SIM_REFRESH_TOKEN': 'fake-refresh', 'SAXO_SIM_APP_KEY': 'fake-app',
            'SAXO_SIM_APP_SECRET': 'fake-secret',
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.sx = SaxoSimEngine(self.tmp.name)
        self.sx.session = Mock()
        self.sx.session.post.return_value = self.response(200, {
            'access_token': 'fake-rotated-access', 'refresh_token': 'fake-rotated-refresh',
            'expires_in': 1200, 'refresh_token_expires_in': 2400,
        })

    @staticmethod
    def response(code, data):
        return SimpleNamespace(status_code=code, json=lambda: data, content=b'{}',
                               request=SimpleNamespace(body='mock-body'), headers={}, text='')

    def test_rotation_survives_restart(self):
        self.assertTrue(self.sx._refresh_access_token())
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.refresh_token, 'fake-rotated-refresh')
        self.assertEqual(restarted.access_token, 'fake-rotated-access')
        self.assertGreater(restarted.token_expires_at, 0)
        call = self.sx.session.post.call_args
        self.assertEqual(call.args[0], 'https://sim.logonvalidation.net/token')
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_refresh_without_access_bootstraps(self):
        self.sx.access_token = ''
        self.sx.session.request.return_value = self.response(200, {'Data': []})
        self.assertEqual(self.sx._request('GET', '/port/v1/accounts/me'), {'Data': []})
        self.sx.session.post.assert_called_once()

    def test_partial_refresh_config_not_ready(self):
        self.sx.access_token = ''
        self.sx.app_secret = ''
        self.assertFalse(self.sx.configured)
        status = self.sx.refresh_once()
        self.assertEqual(status['state'], 'WAITING_FOR_SIM_TOKEN')
        self.assertEqual(status['missing_oauth_fields'], ['SAXO_SIM_APP_SECRET'])
        self.sx.session.post.assert_not_called()

    def test_401_refresh_retries_once(self):
        self.sx.session.request.side_effect = [self.response(401, {}), self.response(200, {'Data': []})]
        self.sx._request('GET', '/port/v1/accounts/me')
        self.assertEqual(self.sx.session.request.call_count, 2)
        self.sx.session.post.assert_called_once()

    def test_second_401_does_not_loop(self):
        self.sx.session.request.return_value = self.response(401, {})
        with self.assertRaises(SaxoSimError):
            self.sx._request('GET', '/port/v1/accounts/me')
        self.assertEqual(self.sx.session.request.call_count, 2)
        self.sx.session.post.assert_called_once()

    def test_expiry_refreshes_before_request(self):
        self.sx.token_expires_at = 1020
        self.sx.session.request.return_value = self.response(200, {})
        with patch('saxo_sim.time.time', return_value=1000):
            self.sx._request('GET', '/port/v1/accounts/me')
        self.sx.session.post.assert_called_once()
        self.assertEqual(self.sx.token_expires_at, 2200)

    def test_rejected_refresh_stops_repeated_attempts(self):
        self.sx.session.post.return_value = self.response(400, {'error': 'invalid_grant'})
        with self.assertRaises(SaxoSimError):
            self.sx._refresh_access_token()
        self.assertEqual(self.sx.refresh_once()['state'], 'REAUTH_REQUIRED')
        self.sx.session.post.assert_called_once()

    def test_expired_refresh_requires_login(self):
        self.sx.refresh_expires_at = 1
        with self.assertRaisesRegex(SaxoSimError, 'REAUTH_REQUIRED'):
            self.sx._refresh_access_token()
        self.sx.session.post.assert_not_called()

    def test_accounting_failure_keeps_rotated_tokens(self):
        with patch.object(self.sx, '_record_bytes', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.sx._refresh_access_token()
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.refresh_token, 'fake-rotated-refresh')

    def test_failed_persistence_blocks_broker_and_recovers(self):
        with patch.object(self.sx, '_persist_tokens', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.sx._refresh_access_token()
            self.assertFalse(self.sx._status()['token_persisted'])
            with self.assertRaises(OSError):
                self.sx._request('GET', '/port/v1/accounts/me')
            self.sx.session.request.assert_not_called()
        self.sx.session.request.return_value = self.response(200, {})
        self.sx._request('GET', '/port/v1/accounts/me')
        self.assertTrue(self.sx._status()['token_persisted'])
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.refresh_token, 'fake-rotated-refresh')

    def test_replace_failure_keeps_previous_file(self):
        self.sx._persist_tokens()
        old = self.sx.token_state_path.read_bytes()
        with patch('saxo_sim.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.sx._refresh_access_token()
        self.assertEqual(self.sx.token_state_path.read_bytes(), old)
        self.assertFalse(list(Path(self.tmp.name).glob('.saxo-tokens-*')))

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_private_before_and_after_replace(self):
        real_replace = os.replace
        modes = []
        def capture(src, dst):
            if Path(dst) == self.sx.token_state_path:
                modes.append(stat.S_IMODE(Path(src).stat().st_mode))
            return real_replace(src, dst)
        old_umask = os.umask(0o022)
        try:
            with patch('saxo_sim.os.replace', side_effect=capture):
                self.sx._refresh_access_token()
        finally:
            os.umask(old_umask)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.sx.token_state_path.stat().st_mode), 0o600)
        self.assertFalse(list(Path(self.tmp.name).glob('.saxo-tokens-*')))

    def test_corrupt_store_never_reuses_old_environment(self):
        self.sx.token_state_path.write_text('{broken')
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertFalse(restarted.configured)
        self.assertEqual(restarted.refresh_once()['state'], 'REAUTH_REQUIRED')
        self.assertFalse(restarted._status()['token_persisted'])

    def test_persisted_pair_is_not_mixed_with_environment(self):
        self.sx.token_state_path.write_text(json.dumps({'environment': 'SIM', 'access_token': 'fake-stored'}))
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.access_token, 'fake-stored')
        self.assertEqual(restarted.refresh_token, '')

    def test_disabled_never_calls_broker(self):
        self.sx.enabled = False
        self.assertEqual(self.sx.refresh_once()['state'], 'DISABLED')
        self.sx.session.request.assert_not_called()
        self.sx.session.post.assert_not_called()

    def test_live_endpoint_refused(self):
        with patch('saxo_sim.SIM_BASE', 'https://gateway.saxobank.com/openapi'):
            with self.assertRaisesRegex(SaxoSimError, 'live endpoint refused'):
                self.sx._request('GET', '/port/v1/accounts/me')
        self.sx.session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()

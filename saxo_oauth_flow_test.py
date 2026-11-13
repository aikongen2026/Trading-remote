"""SIM browser OAuth tests: no real credentials, network or trading orders."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import Mock, patch

from flask import Flask
import control_server
from saxo_oauth import COOKIE, SIM_REDIRECT_URI, register_saxo_oauth
from saxo_sim import SaxoSimEngine

BASE = 'https://trading-remote.onrender.com'


class SaxoOAuthFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            'SAXO_SIM_ENABLED': 'true', 'SAXO_SIM_APP_KEY': 'fake-app-key',
            'SAXO_SIM_APP_SECRET': 'fake-app-secret',
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        auth = patch.object(control_server, 'AUTH_REQUIRED', True)
        auth.start()
        self.addCleanup(auth.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.engine = SaxoSimEngine(self.tmp.name)
        self.engine.session = Mock()
        self.engine.session.post.return_value = SimpleNamespace(
            status_code=200, content=b'fake',
            json=lambda: {'access_token': 'fake-access', 'refresh_token': 'fake-refresh',
                          'expires_in': 1200, 'refresh_token_expires_in': 2400},
        )
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='fake-test-session-key',
                               SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Strict')
        self.app.before_request(control_server.require_login)
        self.app.add_url_rule('/login', 'login', lambda: 'login')
        register_saxo_oauth(self.app, self.engine)
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=BASE) as sess:
            sess['auth'] = True

    def start(self):
        response = self.client.get('/saxo/oauth', base_url=BASE)
        csrf = re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)
        response = self.client.post('/saxo/oauth/start', data={'csrf': csrf}, base_url=BASE)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.location).query)
        return response, query['state'][0]

    def callback(self, state, **extra):
        values = {'state': state, 'code': 'fake-auth-code', **extra}
        return self.client.get('/saxo/oauth/callback?' + urlencode(values), base_url=BASE)

    def test_setup_and_start_require_dashboard_login(self):
        other = self.app.test_client()
        self.assertEqual(other.get('/saxo/oauth', base_url=BASE).status_code, 302)
        self.assertEqual(other.post('/saxo/oauth/start', base_url=BASE).status_code, 302)
        self.assertEqual(other.get('/api/status', base_url=BASE).status_code, 401)
        self.engine.session.post.assert_not_called()

    def test_missing_config_shows_names_only(self):
        self.engine.app_secret = ''
        response = self.client.get('/saxo/oauth', base_url=BASE)
        self.assertIn('SAXO_SIM_APP_SECRET', response.text)
        self.assertNotIn('fake-app-key', response.text)
        self.assertNotIn('Logg inn hos Saxo SIM', response.text)

    def test_start_rejects_csrf(self):
        self.client.get('/saxo/oauth', base_url=BASE)
        response = self.client.post('/saxo/oauth/start', data={'csrf': 'wrong'}, base_url=BASE)
        self.assertEqual(response.status_code, 403)
        self.engine.session.post.assert_not_called()

    def test_start_always_uses_sim_and_registered_redirect(self):
        response, state = self.start()
        target = urlparse(response.location)
        self.assertEqual(target.scheme + '://' + target.netloc + target.path,
                         'https://sim.logonvalidation.net/authorize')
        query = parse_qs(target.query)
        self.assertEqual(query['redirect_uri'], [SIM_REDIRECT_URI])
        self.assertEqual(query['response_type'], ['code'])
        cookies = '\n'.join(response.headers.getlist('Set-Cookie'))
        self.assertIn(COOKIE, cookies)
        self.assertIn('SameSite=Lax', cookies)
        self.assertIn('HttpOnly', cookies)
        self.assertIn('Secure', cookies)
        self.assertEqual(self.app.config['SESSION_COOKIE_SAMESITE'], 'Strict')
        self.assertNotIn('fake-app-secret', response.location)

    def test_success_without_strict_dashboard_cookie_persists_tokens(self):
        _, state = self.start()
        self.client.delete_cookie('session', domain='trading-remote.onrender.com')
        self.engine.auth_error = 'SAXO_SIM_REAUTH_REQUIRED'
        self.engine.token_store_error = 'SAXO_SIM_TOKEN_STORE_INVALID'
        response = self.callback(state)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.endswith('outcome=success'))
        self.assertNotIn('fake-auth-code', response.location)
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        data = json.loads(self.engine.token_state_path.read_text())
        self.assertEqual(data['environment'], 'SIM')
        restarted = SaxoSimEngine(self.tmp.name)
        self.assertEqual(restarted.refresh_token, 'fake-refresh')
        self.assertGreater(restarted.refresh_expires_at, 0)
        self.assertIsNone(self.engine.auth_error)
        self.assertIsNone(self.engine.token_store_error)
        self.assertFalse(self.engine.account_connected)
        call = self.engine.session.post.call_args
        self.assertEqual(call.args[0], 'https://sim.logonvalidation.net/token')
        self.assertEqual(call.kwargs['data']['grant_type'], 'authorization_code')
        self.assertEqual(call.kwargs['data']['redirect_uri'], SIM_REDIRECT_URI)
        self.assertFalse(call.kwargs['allow_redirects'])
        self.engine.session.request.assert_not_called()
        result = self.client.get(response.location, base_url=BASE)
        self.assertNotIn('fake-access', result.text)
        self.assertNotIn('fake-refresh', result.text)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')

    def test_callback_rejects_missing_cookie(self):
        _, state = self.start()
        other = self.app.test_client()
        response = other.get('/saxo/oauth/callback?' + urlencode({'state': state, 'code': 'fake'}), base_url=BASE)
        self.assertTrue(response.location.endswith('outcome=invalid'))
        self.engine.session.post.assert_not_called()

    def test_callback_rejects_wrong_state(self):
        self.start()
        self.assertTrue(self.callback('wrong').location.endswith('outcome=invalid'))
        self.engine.session.post.assert_not_called()

    def test_callback_rejects_duplicate_state_parameters(self):
        _, state = self.start()
        response = self.client.get('/saxo/oauth/callback?' + urlencode([
            ('state', state), ('state', state), ('code', 'fake')]), base_url=BASE)
        self.assertTrue(response.location.endswith('outcome=invalid'))
        self.engine.session.post.assert_not_called()

    def test_callback_rejects_wrong_browser_cookie(self):
        _, state = self.start()
        self.client.set_cookie(COOKIE, 'wrong-browser', domain='trading-remote.onrender.com', secure=True)
        self.assertTrue(self.callback(state).location.endswith('outcome=invalid'))
        self.engine.session.post.assert_not_called()

    def test_expired_flow_does_not_exchange(self):
        with patch('saxo_oauth.time.monotonic', return_value=100):
            _, state = self.start()
        with patch('saxo_oauth.time.monotonic', return_value=701):
            self.assertTrue(self.callback(state).location.endswith('outcome=invalid'))
        self.engine.session.post.assert_not_called()

    def test_callback_is_one_time(self):
        _, state = self.start()
        nonce = self.client.get_cookie(COOKIE, domain='trading-remote.onrender.com').value
        self.assertTrue(self.callback(state).location.endswith('outcome=success'))
        self.client.set_cookie(COOKIE, nonce, domain='trading-remote.onrender.com', secure=True)
        self.assertTrue(self.callback(state).location.endswith('outcome=invalid'))
        self.engine.session.post.assert_called_once()

    def test_declined_flow_does_not_exchange(self):
        _, state = self.start()
        response = self.callback(state, error='access_denied')
        self.assertTrue(response.location.endswith('outcome=declined'))
        self.engine.session.post.assert_not_called()

    def test_traffic_guard_does_not_exchange(self):
        _, state = self.start()
        with patch.object(self.engine, '_render_allows_norway', return_value=False):
            response = self.callback(state)
        self.assertTrue(response.location.endswith('outcome=guard'))
        self.engine.session.post.assert_not_called()

    def test_exchange_failure_is_redacted(self):
        _, state = self.start()
        self.engine.session.post.side_effect = RuntimeError('fake-app-secret fake-auth-code')
        response = self.callback(state)
        self.assertTrue(response.location.endswith('outcome=failed'))
        result = self.client.get(response.location, base_url=BASE)
        self.assertNotIn('fake-app-secret', result.text)
        self.assertNotIn('fake-auth-code', result.text)
        self.assertFalse(self.engine.token_state_path.exists())

    def test_invalid_token_response_does_not_replace_existing_tokens(self):
        _, state = self.start()
        self.engine.session.post.return_value.json = lambda: {'access_token': 'fake-new'}
        response = self.callback(state)
        self.assertTrue(response.location.endswith('outcome=failed'))
        self.assertFalse(self.engine.token_state_path.exists())

    def test_disabled_engine_does_not_exchange(self):
        _, state = self.start()
        self.engine.enabled = False
        self.assertTrue(self.callback(state).location.endswith('outcome=config'))
        self.engine.session.post.assert_not_called()

    def test_persistence_failure_does_not_report_success(self):
        _, state = self.start()
        with patch.object(self.engine, '_persist_tokens', side_effect=OSError('disk full')):
            response = self.callback(state)
        self.assertTrue(response.location.endswith('outcome=failed'))
        self.assertTrue(self.engine._tokens_dirty)
        self.assertFalse(self.engine._status()['token_persisted'])
        self.engine.session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()

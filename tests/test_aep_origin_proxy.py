"""AEP confirmation Origin guard behind Render's HTTPS terminating proxy.

Only request origin validation is changed: one-time session nonce, explicit
confirmation and PAPER-only broker safeguards remain enforced separately.
"""
import unittest
from unittest.mock import patch
import control_server


class OriginChecks(unittest.TestCase):
    def setUp(self):
        self.client = control_server.app.test_client()

    def verify(self, origin, host="trading-remote.onrender.com", fetch_site="same-origin", public=True, expected=True):
        headers={"Host":host,"Sec-Fetch-Site":fetch_site}
        if origin is not None:
            headers["Origin"]=origin
        with patch.object(control_server,"PUBLIC_MODE", public):
            with control_server.app.test_request_context(
                "/api/aep-protection/confirm", method="POST", headers=headers,
                environ_base={"wsgi.url_scheme":"http"}
            ):
                self.assertEqual(control_server._aep_same_origin_request(), expected)

    def test_https_origin_accepted_when_proxy_reports_http(self):
        self.verify("https://trading-remote.onrender.com", expected=True)

    def test_normalized_https_port_accepted(self):
        self.verify("https://trading-remote.onrender.com:443", expected=True)

    def test_local_http_allowed_only_when_not_public(self):
        self.verify("http://localhost:5050", host="localhost:5050", public=False, expected=False)
        self.verify("http://localhost", host="localhost", public=False, expected=True)
        self.verify("http://trading-remote.onrender.com", public=True, expected=False)

    def test_foreign_and_fake_origins_blocked(self):
        for origin in (
            "https://attacker.example", "https://trading-remote.onrender.com.attacker.example",
            "https://trading-remote.onrender.com:444", "null", "file://",
            "https://trading-remote.onrender.com@attacker.example",
            "https://trading-remote.onrender.com/path",
            "javascript://trading-remote.onrender.com",
        ):
            with self.subTest(origin=origin):
                self.verify(origin, expected=False)

    def test_fetch_metadata_rejects_cross_site(self):
        self.verify("https://trading-remote.onrender.com", fetch_site="cross-site", expected=False)
        self.verify("https://trading-remote.onrender.com", fetch_site="same-site", expected=False)

    def test_legacy_webview_origin_absent_still_needs_nonce(self):
        self.verify(None, expected=True)
        with patch.object(control_server, "PUBLIC_MODE", True):
            client=control_server.app.test_client()
            with client.session_transaction() as sess:
                sess["auth"]=True
            response=client.post(
                "/api/aep-protection/confirm",
                json={"nonce":"missing","stop_price":"120.17","confirm":"BEKREFT AEP PAPER STOP"},
                headers={"Host":"trading-remote.onrender.com"}
            )
            self.assertEqual(response.status_code,409)


if __name__=="__main__":
    unittest.main()

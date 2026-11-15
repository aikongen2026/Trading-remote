"""Version and Saxo SIM no-reauth regression checks."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app_version import runtime_version
from saxo_sim import SaxoSimEngine
from saxo_oauth import register_saxo_oauth
from flask import Flask


class ReleaseAndSaxoTests(unittest.TestCase):
    def test_each_deploy_has_distinct_version(self):
        self.assertNotEqual(runtime_version("0123456789abcdef"),
                            runtime_version("fedcba9876543210"))
        self.assertTrue(runtime_version("0123456789abcdef").startswith("4.16.0+"))

    def test_saxo_connected_oauth_does_not_redirect_or_rotate_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "SAXO_SIM_ENABLED": "true", "SAXO_SIM_APP_KEY": "fake-key",
                "SAXO_SIM_APP_SECRET": "fake-secret"
            }):
                eng = SaxoSimEngine(tmp)
                eng.refresh_token = "dummy-refresh"
                eng.access_token = "dummy-access"
                eng._persist_tokens()
                eng.account_connected = True
                app = Flask(__name__)
                app.secret_key = "test-key"
                register_saxo_oauth(app, eng)
                with app.test_client() as client:
                    response = client.get("/saxo/oauth", base_url="https://trading-remote.onrender.com")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("allerede tilkoblet", response.get_data(as_text=True))
                    self.assertNotIn("sim.logonvalidation.net/authorize", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

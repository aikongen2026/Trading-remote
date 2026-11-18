"""AEP protection requires explicit approval, fresh PAPER data, safe one-shot submission."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import control_server
from paper_aep_protection import (
    AepProtectionError, fresh_aep_unprotected, submit_approved_aep_stop,
    validate_stop_price,
)
from trading_bot import AlpacaRest


class FakePaper:
    cfg = SimpleNamespace(paper=True, trading_base="https://paper-api.alpaca.markets")

    def __init__(self, qty=2, stop_orders=None, current_price=126.00):
        self.qty = qty
        self.stop_orders = stop_orders if stop_orders is not None else []
        self.price = current_price
        self.sent = []
        self.raise_on_submit = None
        self.get_orders_count = 0

    def positions(self):
        return [{"symbol": "AEP", "qty": str(self.qty),
                 "asset_class": "us_equity", "current_price": str(self.price)}]

    def get_orders(self, *, status="open", limit=500, nested=True):
        self.get_orders_count += 1
        assert status == "open" and nested is True
        return list(self.stop_orders)

    def submit_protective_stop(self, symbol, qty, stop_price, *, client_order_id):
        self.sent.append((symbol, qty, stop_price, client_order_id))
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        return {"id": "paper-stop-id", "status": "accepted"}


class AepStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.broker = FakePaper()

    def preview(self):
        from time import time
        row = fresh_aep_unprotected(self.broker)
        return {**row, "expires": time() + 120}

    def test_preview_is_read_only_and_five_percent_below_price(self):
        result = fresh_aep_unprotected(self.broker)
        self.assertEqual((result["qty"], result["suggested_stop_price"]), (2, 119.70))
        self.assertEqual(self.broker.sent, [])

    def test_reject_live_endpoint_even_when_account_has_paper_flag(self):
        self.broker.cfg.trading_base = "https://api.alpaca.markets"
        with self.assertRaises(AepProtectionError):
            fresh_aep_unprotected(self.broker)
        self.assertEqual(self.broker.sent, [])

    def test_reject_existing_open_sell_order(self):
        self.broker.stop_orders = [{"symbol": "AEP", "side": "sell",
                                    "qty": "2", "status": "new", "type": "limit"}]
        with self.assertRaises(AepProtectionError):
            fresh_aep_unprotected(self.broker)
        self.assertEqual(self.broker.sent, [])

    def test_price_bounds_and_decimal_precision(self):
        self.assertEqual(validate_stop_price("119.70", 126), "119.70")
        for x in ["127", "125.50", "93.50", "abc", "119.701", "0", "-1"]:
            with self.subTest(x=x):
                with self.assertRaises(AepProtectionError):
                    validate_stop_price(x, 126)

    def test_one_stop_submission_cannot_be_replayed(self):
        preview = self.preview()
        result = submit_approved_aep_stop(
            self.broker, self.path, preview=preview, stop_price="119.70"
        )
        self.assertEqual(result["paper"], True)
        self.assertEqual(len(self.broker.sent), 1)
        self.assertEqual(self.broker.sent[0][:3], ("AEP", 2, 119.70))
        with self.assertRaises(AepProtectionError):
            submit_approved_aep_stop(
                self.broker, self.path, preview=preview, stop_price="119.70"
            )
        self.assertEqual(len(self.broker.sent), 1)

    def test_timeout_never_resubmits(self):
        import requests
        self.broker.raise_on_submit = requests.Timeout()
        preview = self.preview()
        with self.assertRaises(AepProtectionError):
            submit_approved_aep_stop(
                self.broker, self.path, preview=preview, stop_price="119.70"
            )
        self.assertEqual(len(self.broker.sent), 1)
        self.broker.raise_on_submit = None
        with self.assertRaises(AepProtectionError):
            submit_approved_aep_stop(
                self.broker, self.path, preview=preview, stop_price="119.70"
            )
        self.assertEqual(len(self.broker.sent), 1)

    def test_changed_position_or_price_blocks(self):
        preview = self.preview()
        self.broker.qty = 3
        with self.assertRaises(AepProtectionError):
            submit_approved_aep_stop(
                self.broker, self.path, preview=preview, stop_price="119.70"
            )
        self.assertEqual(self.broker.sent, [])

    def test_expired_preview_blocks(self):
        preview = self.preview()
        preview["expires"] = 0
        with self.assertRaises(AepProtectionError):
            submit_approved_aep_stop(
                self.broker, self.path, preview=preview, stop_price="119.70"
            )

    def test_api_requires_authenticated_confirmation_and_origin(self):
        with patch.multiple(control_server, STATE_DIR=self.path,
                            STATUS_PATH=self.path / "status.json"):
            with patch("trading_bot.load_cfg", return_value=object()):
                with patch("trading_bot.AlpacaRest", return_value=self.broker):
                    client = control_server.app.test_client()
                    unauth = client.post("/api/aep-protection/preview", json={})
                    if control_server.AUTH_REQUIRED:
                        self.assertEqual(unauth.status_code, 401)
                    with client.session_transaction() as sess:
                        sess["auth"] = True
                    preview_response = client.post("/api/aep-protection/preview", json={})
                    self.assertEqual(preview_response.status_code, 200)
                    nonce = preview_response.json["preview_nonce"]
                    wrong = client.post("/api/aep-protection/confirm", json={
                        "preview_nonce": nonce, "stop_price": "119.70",
                        "confirmation": "NEI"
                    })
                    self.assertEqual(wrong.status_code, 409)
                    self.assertEqual(self.broker.sent, [])

    def test_paper_broker_method_never_calls_live(self):
        cfg = SimpleNamespace(
            paper=True, trading_base="https://paper-api.alpaca.markets",
            key="x", secret="y"
        )
        api = AlpacaRest(cfg)
        with patch.object(api, "_req", return_value={"id": "id"}) as req:
            api.submit_protective_stop("AEP", 2, 119.70, client_order_id="tbp-aep-abc123")
        self.assertEqual(req.call_args.args[0], "POST")
        self.assertEqual(req.call_args.args[1], "https://paper-api.alpaca.markets/v2/orders")
        self.assertEqual(req.call_args.kwargs["json_body"]["side"], "sell")
        self.assertEqual(req.call_args.kwargs["json_body"]["type"], "stop")
        self.assertEqual(req.call_args.kwargs["json_body"]["time_in_force"], "gtc")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import trading_bot

HERE = Path(__file__).resolve().parent


def main() -> int:
    control = json.loads((HERE / "control.json").read_text(encoding="utf-8"))
    assert control["strategy_mode"] in {"scalp", "hype", "trend", "cross"}
    assert control["market_mode"] == "auto"
    assert control["armed"] is False
    assert control["paused"] is True
    assert "BTC/USD" in control["crypto_symbols"]
    assert control["test_buy"]["go"] is False
    assert control["test_sell_all"]["go"] is False

    up = [100.0 + i * 0.08 for i in range(40)]
    down = list(reversed(up))

    sig_up, dbg_up = trading_bot.scalp_signal(up, up[-1] * 1.0004, 6, 18, 4, 0.0006, 0.00015, 0.00015)
    sig_down, dbg_down = trading_bot.scalp_signal(down, down[-1] * 0.9996, 6, 18, 4, 0.0006, 0.00015, 0.00015)

    assert sig_up == 1, (sig_up, dbg_up)
    assert sig_down == -1, (sig_down, dbg_down)

    # Crypto cost gate: 25 bps taker each way means 50 bps fees before spread/slippage.
    assert trading_bot.crypto_estimated_roundtrip_cost_bps(3.0, 25.0) == 53.0
    assert trading_bot.crypto_estimated_roundtrip_cost_bps(35.0, 25.0) == 85.0
    confirms = trading_bot.crypto_long_confirmations(
        up, up[-1] * 1.0004, 6, 18, 4, 0.0006, 0.00015, 0.00015, 2
    )
    assert confirms >= 2, confirms

    # Regression test for the original multi-symbol bars bug:
    # first page contains only NVDA, second page contains AAPL.
    api = object.__new__(trading_bot.AlpacaRest)
    api.cfg = SimpleNamespace(data_base="https://example.invalid", stock_feed="iex")
    pages = [
        {
            "bars": {"NVDA": [{"t": "2026-01-01T10:00:00Z", "c": 100}, {"t": "2026-01-01T10:01:00Z", "c": 101}]},
            "next_page_token": "page2",
        },
        {
            "bars": {"AAPL": [{"t": "2026-01-01T10:00:00Z", "c": 200}, {"t": "2026-01-01T10:01:00Z", "c": 201}]},
            "next_page_token": None,
        },
    ]

    def fake_req(*args, **kwargs):
        return pages.pop(0)

    api._req = fake_req
    got = api.bars(["AAPL", "NVDA"], "1Min", 2, "start", "end")
    assert len(got["AAPL"]) == 2
    assert len(got["NVDA"]) == 2



    # Crypto pagination uses the same total-page-limit semantics as stocks.
    capi = object.__new__(trading_bot.AlpacaRest)
    capi.cfg = SimpleNamespace(data_base="https://example.invalid", stock_feed="iex", trading_base="https://paper.invalid")
    crypto_pages = [
        {"bars": {"ETH/USD": [{"t":"2026-01-01T10:00:00Z","c":3000}, {"t":"2026-01-01T10:01:00Z","c":3001}]}, "next_page_token":"p2"},
        {"bars": {"BTC/USD": [{"t":"2026-01-01T10:00:00Z","c":60000}, {"t":"2026-01-01T10:01:00Z","c":60010}]}, "next_page_token":None},
    ]
    capi._req = lambda *args, **kwargs: crypto_pages.pop(0)
    cgot = capi.crypto_bars(["BTC/USD", "ETH/USD"], "1Min", 2, "start", "end")
    assert len(cgot["BTC/USD"]) == 2
    assert len(cgot["ETH/USD"]) == 2

    # Crypto notional orders must use GTC and notional, not stock DAY qty semantics.
    captured = {}
    def capture_req(method, url, **kwargs):
        captured.update(kwargs.get("json_body") or {})
        return {"id":"test"}
    capi._req = capture_req
    capi.submit_notional_order("BTC/USD", "buy", 100.0)
    assert captured["symbol"] == "BTC/USD"
    assert captured["time_in_force"] == "gtc"
    assert captured["notional"] == "100.00"

    # Session regression: Sunday 21:00 ET is overnight; Sunday daytime is closed.
    from datetime import datetime, timezone, timedelta
    et = timezone(timedelta(hours=-4))
    assert trading_bot.stock_session_for_et(datetime(2026,10,4,21,0,tzinfo=et), True) == "OVERNIGHT"
    assert trading_bot.stock_session_for_et(datetime(2026,10,4,10,0,tzinfo=et), True) == "CLOSED"

    # Windows regression: bot must not crash if system tzdata is missing.
    original_zoneinfo = trading_bot.ZoneInfo
    try:
        def missing_zone(_key):
            raise trading_bot.ZoneInfoNotFoundError("test missing tzdata")
        trading_bot.ZoneInfo = missing_zone
        et = trading_bot.eastern_now()
        assert et.utcoffset() is not None
        assert int(et.utcoffset().total_seconds() / 3600) in (-4, -5)
    finally:
        trading_bot.ZoneInfo = original_zoneinfo

    # Remote/PWA package checks. Runtime route tests run when Flask/Waitress are installed.
    assert (HERE / "static" / "manifest.webmanifest").exists()
    assert (HERE / "static" / "sw.js").exists()
    assert (HERE / "static" / "icon-192.png").exists()
    assert (HERE / "static" / "icon-512.png").exists()
    try:
        import control_server
        client = control_server.app.test_client()
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/service-worker.js").status_code == 200
        assert client.get("/healthz").status_code in (200, 503)
        import cloud_app  # noqa: F401 - import itself is the regression test
        remote_runtime = True
    except ModuleNotFoundError as e:
        if e.name not in {"flask", "waitress"}:
            raise
        remote_runtime = False

    print("OK: control.json safe-start")
    print("OK: scalp BUY signal")
    print("OK: scalp SELL signal")
    print("OK: crypto cost-aware entry math")
    print("OK: crypto 2-bar confirmation")
    print("OK: multi-symbol bars pagination")
    print("OK: crypto bars pagination")
    print("OK: crypto notional GTC order")
    print("OK: US overnight session detection")
    print("OK: Windows timezone fallback")
    print("OK: remote dashboard + PWA assets")
    print("OK: cloud runtime routes" if remote_runtime else "SKIP: cloud runtime routes (install requirements first)")
    print("OK: imports and syntax")
    print("SELF TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

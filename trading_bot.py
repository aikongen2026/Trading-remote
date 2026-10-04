#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trading_bot.py (IEX + Quotes) - IMPROVED DROP-IN

Mål:
- Beholde samme filmodell: control.json + status.json
- Beholde Alpaca REST via requests
- Beholde eksisterende strategi-modi: cross / trend / hype
- Beholde popup ved faktisk trading
- Gjøre loopen raskere og mindre tung:
  * parallell henting av data
  * cache av bars mellom looper
  * mindre unødvendig clock/bars-belastning
  * tryggere request-retry og timeout
"""

from __future__ import annotations

import json
import os
import time
import math
import traceback
import threading
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote as url_quote

import requests

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    tk = None
    messagebox = None


HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = (os.getenv("TRADINGBOT_STATE_DIR") or HERE).strip() or HERE
os.makedirs(STATE_DIR, exist_ok=True)
CONTROL_PATH = os.path.join(STATE_DIR, "control.json")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
BOT_INSTANCE_PORT = int(os.getenv("TRADINGBOT_INSTANCE_PORT", "5051"))


def acquire_single_instance_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", BOT_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError as e:
        sock.close()
        raise RuntimeError(
            f"Another trading_bot instance appears to be running (local port {BOT_INSTANCE_PORT} is busy)."
        ) from e


DEFAULT_CONTROL: Dict[str, Any] = {
    "paused": True,
    "kill": False,
    "armed": False,
    "extended_hours": False,
    "test_mode": False,
    "strategy_mode": "scalp",
    "poll_sec": 1,
    "fast_sma": 6,
    "slow_sma": 18,
    "timeframe": "1Min",
    "bars_limit": 160,
    "stock_qty": 1,
    "cooldown_sec": 45,
    "max_spread_usd": 0.08,
    "max_spread_bps": 8.0,
    "extended_limit_buffer_bps": 5.0,
    "quote_stale_sec": 20,
    "skip_if_open_order": True,
    "hype_mom_n": 4,
    "min_mom_pct": 0.0006,
    "min_move_pct": 0.00015,
    "min_abs_slope": 0.02,
    "min_slope_pct": 0.00015,
    "take_profit_pct": 0.0040,
    "stop_loss_pct": 0.0030,
    "trailing_activate_pct": 0.0020,
    "trailing_stop_pct": 0.0025,
    "max_hold_sec": 1800,
    "max_positions": 5,
    "daily_loss_limit_pct": 0.02,
    "refresh_bars_every_sec": 10,
    "quotes_batch_size": 200,
    "bars_batch_size": 200,
    "market_mode": "auto",
    "crypto_strategy_mode": "scalp",
    "crypto_notional_usd": 100.0,
    "crypto_cooldown_sec": 30,
    "crypto_max_spread_bps": 25.0,
    "crypto_take_profit_pct": 0.0100,
    "crypto_stop_loss_pct": 0.0060,
    "crypto_trailing_activate_pct": 0.0060,
    "crypto_trailing_stop_pct": 0.0040,
    "crypto_max_hold_sec": 2700,
    "crypto_max_positions": 3,
    "crypto_taker_fee_bps": 25.0,
    "crypto_min_net_edge_bps": 20.0,
    "crypto_entry_confirm_bars": 2,
    "crypto_symbols": [
        "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "LINK/USD", "AVAX/USD"
    ],
    "symbols": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "AMD",
        "NFLX", "INTC", "QCOM", "TXN", "AMAT", "MU", "IBM", "ORCL", "ADBE", "CRM",
        "CSCO", "INTU", "PYPL", "NOW", "UBER", "ABNB", "BKNG", "SNOW", "PLTR", "CRWD",
        "PANW", "ZS", "SNPS", "CDNS", "JPM", "BAC", "WFC", "C", "GS", "MS",
        "V", "MA", "AXP", "KO", "PEP", "COST", "WMT", "HD", "UNH", "LLY"
    ],
    "test_buy": {"symbol": "AAPL", "qty": 1, "go": False},
    "test_sell_all": {"symbol": "AAPL", "go": False},
}


def ensure_control_file() -> Dict[str, Any]:
    control = safe_read_json(CONTROL_PATH, {})
    if not isinstance(control, dict):
        control = {}

    symbols = control.get("symbols")
    symbols_ok = isinstance(symbols, list) and len([s for s in symbols if str(s).strip()]) > 0

    if not os.path.exists(CONTROL_PATH) or not symbols_ok:
        merged = dict(DEFAULT_CONTROL)
        if isinstance(control, dict):
            merged.update(control)
        merged["symbols"] = list(DEFAULT_CONTROL["symbols"])
        safe_write_json(CONTROL_PATH, merged)
        return merged

    return control



# ----------------------------
# Utilities
# ----------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def safe_read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def to_bool_env(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def popup_trade(message: str) -> None:
    def _show() -> None:
        if tk is None or messagebox is None:
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            messagebox.showinfo("Trading Bot", message, parent=root)
            root.destroy()
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()


def parse_any_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def timeframe_to_seconds(timeframe: str) -> int:
    tf = (timeframe or "").strip().lower()
    mapping = {
        "1min": 60,
        "5min": 300,
        "15min": 900,
        "30min": 1800,
        "1hour": 3600,
        "1day": 86400,
    }
    return mapping.get(tf, 60)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> int:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    delta = (weekday - first.weekday()) % 7
    return 1 + delta + (n - 1) * 7


def eastern_now() -> datetime:
    """Return current US Eastern time, even on Windows without system tzdata."""
    now = utc_now()
    try:
        return now.astimezone(ZoneInfo("America/New_York"))
    except (ZoneInfoNotFoundError, ModuleNotFoundError):
        # Fallback implements the current US DST rule:
        # second Sunday in March 02:00 EST -> first Sunday in November 02:00 EDT.
        year = now.year
        march_day = _nth_weekday_of_month(year, 3, 6, 2)  # Sunday=6
        nov_day = _nth_weekday_of_month(year, 11, 6, 1)
        dst_start_utc = datetime(year, 3, march_day, 7, 0, tzinfo=timezone.utc)
        dst_end_utc = datetime(year, 11, nov_day, 6, 0, tzinfo=timezone.utc)
        is_dst = dst_start_utc <= now < dst_end_utc
        offset = timedelta(hours=-4 if is_dst else -5)
        name = "EDT" if is_dst else "EST"
        return now.astimezone(timezone(offset, name=name))


def stock_session_for_et(et: datetime, allow_extended: bool) -> str:
    """Return REGULAR/PREMARKET/AFTERHOURS/OVERNIGHT/CLOSED for US equities.

    Holiday eligibility is still enforced by Alpaca when an order is submitted.
    """
    wd = et.weekday()  # Mon=0 ... Sun=6
    minute = et.hour * 60 + et.minute
    if wd <= 4 and (9 * 60 + 30) <= minute < 16 * 60:
        return "REGULAR"
    if not allow_extended:
        return "CLOSED"
    if wd <= 4 and 4 * 60 <= minute < (9 * 60 + 30):
        return "PREMARKET"
    if wd <= 4 and 16 * 60 <= minute < 20 * 60:
        return "AFTERHOURS"
    overnight = (wd == 6 and minute >= 20 * 60) or (wd in (0, 1, 2, 3) and (minute < 4 * 60 or minute >= 20 * 60)) or (wd == 4 and minute < 4 * 60)
    if overnight:
        return "OVERNIGHT"
    return "CLOSED"


def current_stock_session(regular_market_open: bool, allow_extended: bool) -> str:
    if regular_market_open:
        return "REGULAR"
    session = stock_session_for_et(eastern_now(), allow_extended)
    # Alpaca clock is authoritative during regular hours (holidays/halts).
    if session == "REGULAR":
        return "CLOSED"
    return session


def is_supported_extended_session_now() -> bool:
    return current_stock_session(False, True) in {"PREMARKET", "AFTERHOURS", "OVERNIGHT"}


# ----------------------------
# .env loader (no deps)
# ----------------------------
def _strip_quotes(v: str) -> str:
    v = (v or "").strip()
    v = v.lstrip("\ufeff").strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v


def load_dotenv_local(dotenv_path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(dotenv_path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = _strip_quotes(v)
                if k:
                    values[k] = v
                    # Keep explicit server/cloud environment variables authoritative.
                    # Local .env only fills values that are not already set.
                    os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return values


LOCAL_ENV = load_dotenv_local(os.path.join(HERE, ".env"))


def _env_value(*names: str, default: str = "") -> str:
    # Explicit process environment wins (Render/VPS/Docker secrets).
    for name in names:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return str(v)
    for name in names:
        v = LOCAL_ENV.get(name)
        if v is not None and str(v).strip() != "":
            return str(v)
    return default


@dataclass
class BotCfg:
    key: str
    secret: str
    paper: bool
    trading_base: str
    data_base: str
    stock_feed: str


def load_cfg() -> BotCfg:
    key = _strip_quotes(_env_value("ALPACA_KEY", "APCA_API_KEY_ID", "ALPACA_KEY_ID"))
    secret = _strip_quotes(_env_value("ALPACA_SECRET", "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"))
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        raise RuntimeError("Mangler Alpaca keys. Sett APCA_API_KEY_ID og APCA_API_SECRET_KEY i .env.")

    paper = to_bool_env(_env_value("ALPACA_PAPER", default="true"), True)
    trading_base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"

    data_base = (_env_value("ALPACA_DATA_BASE", default="https://data.alpaca.markets") or "").strip().rstrip("/")
    if not data_base:
        data_base = "https://data.alpaca.markets"

    stock_feed = (_env_value("ALPACA_DATA_FEED", default="iex") or "iex").strip().lower()

    return BotCfg(
        key=key,
        secret=secret,
        paper=paper,
        trading_base=trading_base,
        data_base=data_base,
        stock_feed=stock_feed,
    )


class AlpacaAuthError(RuntimeError):
    pass


class AlpacaRest:
    def __init__(self, cfg: BotCfg) -> None:
        self.cfg = cfg
        self.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.s.headers.update(
            {
                "APCA-API-KEY-ID": cfg.key,
                "APCA-API-SECRET-KEY": cfg.secret,
                "Content-Type": "application/json",
            }
        )

    def _req(
        self,
        method: str,
        url: str,
        *,
        params: Dict[str, Any] | None = None,
        json_body: Any | None = None,
        timeout: Tuple[int, int] = (4, 20),
    ) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, params=params, json=json_body, timeout=timeout)
                if r.status_code == 429 and attempt < 2:
                    retry_after = r.headers.get("Retry-After")
                    delay = 0.75 * (attempt + 1)
                    if retry_after:
                        try:
                            delay = max(delay, min(10.0, float(retry_after)))
                        except Exception:
                            pass
                    time.sleep(delay)
                    continue
                if r.status_code in (401, 403):
                    raise AlpacaAuthError(
                        f"Alpaca authentication failed ({r.status_code}). "
                        "Check PAPER/LIVE API keys with SETUP_ALPACA_KEYS.bat."
                    )
                if r.status_code >= 400:
                    try:
                        msg = r.json()
                    except Exception:
                        msg = r.text
                    raise RuntimeError(f"{method} {url} -> {r.status_code}: {msg}")
                if r.text.strip() == "":
                    return None
                return r.json()
            except Exception as e:
                last_error = e
                if attempt == 2:
                    raise
                time.sleep(0.20 * (attempt + 1))
        raise RuntimeError(f"Request failed unexpectedly: {last_error}")

    def account(self) -> Dict[str, Any]:
        return self._req("GET", f"{self.cfg.trading_base}/v2/account")

    def clock(self) -> Dict[str, Any]:
        return self._req("GET", f"{self.cfg.trading_base}/v2/clock")

    def positions(self) -> List[Dict[str, Any]]:
        return self._req("GET", f"{self.cfg.trading_base}/v2/positions")

    def get_orders(self, limit: int = 30, status: str = "all") -> List[Dict[str, Any]]:
        return self._req(
            "GET",
            f"{self.cfg.trading_base}/v2/orders",
            params={"status": status, "limit": int(limit), "direction": "desc"},
        )

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "day",
        *,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        extended_hours: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": tif,
            "qty": str(qty),
        }
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price is required for limit orders")
            body["limit_price"] = f"{float(limit_price):.4f}"
        if extended_hours:
            body["extended_hours"] = True
        return self._req("POST", f"{self.cfg.trading_base}/v2/orders", json_body=body)

    def close_position(self, symbol: str) -> Any:
        encoded = url_quote(symbol, safe="")
        return self._req("DELETE", f"{self.cfg.trading_base}/v2/positions/{encoded}")

    def quotes_latest(self, symbols: List[str], feed: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        if not symbols:
            return {}
        data = self._req(
            "GET",
            f"{self.cfg.data_base}/v2/stocks/quotes/latest",
            params={"symbols": ",".join(symbols), "feed": feed or self.cfg.stock_feed},
        )
        quotes = (data or {}).get("quotes") or {}
        return quotes if isinstance(quotes, dict) else {}

    def latest_bars(self, symbols: List[str], feed: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        if not symbols:
            return {}
        data = self._req(
            "GET",
            f"{self.cfg.data_base}/v2/stocks/bars/latest",
            params={"symbols": ",".join(symbols), "feed": feed or self.cfg.stock_feed},
        )
        bars = (data or {}).get("bars") or {}
        return bars if isinstance(bars, dict) else {}

    def bars(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        start_iso: str,
        end_iso: str,
        feed: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not symbols:
            return {}

        wanted = [s.upper() for s in symbols]
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in wanted}
        page_token: Optional[str] = None
        # The multi-symbol endpoint applies limit to the whole page, not each symbol.
        # Use a large page and follow next_page_token until each requested symbol has
        # enough bars or the API has no more pages.
        page_limit = min(10000, max(1000, int(limit) * max(1, len(wanted))))

        for _ in range(30):
            params: Dict[str, Any] = {
                "symbols": ",".join(wanted),
                "timeframe": timeframe,
                "limit": page_limit,
                "feed": feed or self.cfg.stock_feed,
                "adjustment": "raw",
                "start": start_iso,
                "end": end_iso,
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token

            data = self._req("GET", f"{self.cfg.data_base}/v2/stocks/bars", params=params) or {}
            page_bars = data.get("bars") or {}
            if isinstance(page_bars, dict):
                for sym, arr in page_bars.items():
                    key = str(sym).upper()
                    if key in out and isinstance(arr, list):
                        out[key].extend(arr)

            if all(len(out.get(sym, [])) >= int(limit) for sym in wanted):
                break

            page_token = data.get("next_page_token")
            if not page_token:
                break

        for sym in wanted:
            arr = out.get(sym, [])
            arr = sorted(arr, key=lambda x: str(x.get("t") or x.get("timestamp") or ""))
            if limit > 0 and len(arr) > limit:
                arr = arr[-int(limit):]
            out[sym] = arr
        return out

    def submit_notional_order(
        self,
        symbol: str,
        side: str,
        notional: float,
        *,
        tif: str = "gtc",
        order_type: str = "market",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": tif,
            "notional": f"{float(notional):.2f}",
        }
        return self._req("POST", f"{self.cfg.trading_base}/v2/orders", json_body=body)

    def crypto_quotes_latest(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if not symbols:
            return {}
        data = self._req(
            "GET",
            f"{self.cfg.data_base}/v1beta3/crypto/us/latest/quotes",
            params={"symbols": ",".join(symbols)},
        )
        quotes = (data or {}).get("quotes") or {}
        return quotes if isinstance(quotes, dict) else {}

    def crypto_latest_bars(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if not symbols:
            return {}
        data = self._req(
            "GET",
            f"{self.cfg.data_base}/v1beta3/crypto/us/latest/bars",
            params={"symbols": ",".join(symbols)},
        )
        bars = (data or {}).get("bars") or {}
        return bars if isinstance(bars, dict) else {}

    def crypto_bars(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        start_iso: str,
        end_iso: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not symbols:
            return {}
        wanted = [s.upper() for s in symbols]
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in wanted}
        page_token: Optional[str] = None
        page_limit = min(10000, max(1000, int(limit) * max(1, len(wanted))))
        for _ in range(30):
            params: Dict[str, Any] = {
                "symbols": ",".join(wanted),
                "timeframe": timeframe,
                "limit": page_limit,
                "start": start_iso,
                "end": end_iso,
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._req("GET", f"{self.cfg.data_base}/v1beta3/crypto/us/bars", params=params) or {}
            page_bars = data.get("bars") or {}
            if isinstance(page_bars, dict):
                for sym, arr in page_bars.items():
                    key = str(sym).upper()
                    if key in out and isinstance(arr, list):
                        out[key].extend(arr)
            if all(len(out.get(sym, [])) >= int(limit) for sym in wanted):
                break
            page_token = data.get("next_page_token")
            if not page_token:
                break
        for sym in wanted:
            arr = sorted(out.get(sym, []), key=lambda x: str(x.get("t") or x.get("timestamp") or ""))
            if limit > 0 and len(arr) > limit:
                arr = arr[-int(limit):]
            out[sym] = arr
        return out


# ----------------------------
# Indicators
# ----------------------------
def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def crossover_signal(closes: List[float], fast: int, slow: int) -> int:
    if len(closes) < slow + 2:
        return 0
    f1 = sma(closes[:-1], fast)
    s1 = sma(closes[:-1], slow)
    f2 = sma(closes, fast)
    s2 = sma(closes, slow)
    if f1 is None or s1 is None or f2 is None or s2 is None:
        return 0
    if f1 <= s1 and f2 > s2:
        return 1
    if f1 >= s1 and f2 < s2:
        return -1
    return 0


def trend_state(closes: List[float], fast: int, slow: int) -> Optional[int]:
    if len(closes) < slow:
        return None
    f = sma(closes, fast)
    s = sma(closes, slow)
    if f is None or s is None:
        return None
    if f > s:
        return 1
    if f < s:
        return -1
    return 0


def pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return (b - a) / a


def slope_of_sma(closes: List[float], n: int, lookback: int = 3) -> Optional[float]:
    if len(closes) < n + lookback:
        return None
    now = sma(closes, n)
    prev = sma(closes[:-lookback], n)
    if now is None or prev is None:
        return None
    return now - prev


def momentum_score(closes: List[float], mom_n: int = 6) -> Optional[float]:
    if len(closes) < mom_n + 1:
        return None
    a = closes[-(mom_n + 1)]
    b = closes[-1]
    return pct_change(a, b)


def slope_pct_of_sma(closes: List[float], n: int, lookback: int = 3) -> Optional[float]:
    if len(closes) < n + lookback:
        return None
    now = sma(closes, n)
    prev = sma(closes[:-lookback], n)
    if now is None or prev is None or prev == 0:
        return None
    return pct_change(prev, now)


def scalp_signal(
    closes: List[float],
    live_price: Optional[float],
    fast: int,
    slow: int,
    mom_n: int,
    min_mom_pct: float,
    min_move_pct: float,
    min_slope_pct: float,
) -> Tuple[int, Dict[str, Any]]:
    debug: Dict[str, Any] = {}
    if len(closes) < max(slow + 2, mom_n + 2):
        return 0, debug

    f = sma(closes, fast)
    sl = sma(closes, slow)
    mom = momentum_score(closes, mom_n)
    slope_pct = slope_pct_of_sma(closes, fast, lookback=3)
    last_move = pct_change(closes[-2], closes[-1]) if len(closes) >= 2 else 0.0
    micro = pct_change(closes[-1], float(live_price)) if live_price and closes[-1] else 0.0

    debug.update({
        "fast_sma": None if f is None else round(f, 6),
        "slow_sma": None if sl is None else round(sl, 6),
        "mom": None if mom is None else round(mom, 6),
        "slope_pct": None if slope_pct is None else round(slope_pct, 6),
        "last_move_pct": round(last_move, 6),
        "micro_move_pct": round(micro, 6),
    })

    if f is None or sl is None or mom is None or slope_pct is None:
        return 0, debug

    long_votes = 0
    short_votes = 0
    long_votes += 1 if f > sl else 0
    short_votes += 1 if f < sl else 0
    long_votes += 1 if mom >= abs(min_mom_pct) else 0
    short_votes += 1 if mom <= -abs(min_mom_pct) else 0
    long_votes += 1 if slope_pct >= abs(min_slope_pct) else 0
    short_votes += 1 if slope_pct <= -abs(min_slope_pct) else 0
    long_votes += 1 if (last_move >= abs(min_move_pct) or micro >= abs(min_move_pct)) else 0
    short_votes += 1 if (last_move <= -abs(min_move_pct) or micro <= -abs(min_move_pct)) else 0

    debug["long_votes"] = long_votes
    debug["short_votes"] = short_votes

    # Require trend alignment plus at least two supporting momentum votes.
    if f > sl and long_votes >= 3:
        return 1, debug
    if f < sl and short_votes >= 3:
        return -1, debug
    return 0, debug


def crypto_estimated_roundtrip_cost_bps(spread_bps: float, taker_fee_bps: float) -> float:
    """Conservative market-order round-trip estimate: buy+sell taker fees plus one full spread."""
    return max(0.0, float(spread_bps)) + 2.0 * max(0.0, float(taker_fee_bps))


def crypto_long_confirmations(
    closes: List[float],
    live_price: Optional[float],
    fast: int,
    slow: int,
    mom_n: int,
    min_mom_pct: float,
    min_move_pct: float,
    min_slope_pct: float,
    required: int,
) -> int:
    """Count consecutive bullish scalp states across distinct 1-minute bar snapshots."""
    need = max(1, int(required))
    count = 0
    for offset in range(need):
        end = len(closes) - offset
        if end <= 0:
            break
        subset = closes[:end]
        lp = live_price if offset == 0 else subset[-1]
        sig, _ = scalp_signal(
            subset, lp, fast, slow, mom_n,
            min_mom_pct, min_move_pct, min_slope_pct,
        )
        if sig <= 0:
            break
        count += 1
    return count


# ----------------------------
# Helpers
# ----------------------------
def _dedupe_symbols(syms: List[Any]) -> List[str]:
    deduped: List[str] = []
    seen = set()
    for raw in syms:
        s = str(raw).strip().upper()
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def symbol_universe(control: Dict[str, Any]) -> List[str]:
    syms = control.get("symbols") or []
    return _dedupe_symbols(list(syms))


def crypto_universe(control: Dict[str, Any]) -> List[str]:
    syms = control.get("crypto_symbols") or []
    return _dedupe_symbols(list(syms))


def symbol_key(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace("/", "")


def position_for_symbol(positions_map: Dict[str, Dict[str, Any]], sym: str) -> Optional[Dict[str, Any]]:
    exact = positions_map.get(sym.upper())
    if exact:
        return exact
    target = symbol_key(sym)
    for psym, pos in positions_map.items():
        if symbol_key(psym) == target:
            return pos
    return None


def positions_to_map(positions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in positions or []:
        sym = str(p.get("symbol", "")).upper()
        if sym:
            out[sym] = p
    return out


def calc_day_pnl_usd(acct: Dict[str, Any]) -> float:
    try:
        eq = float(acct.get("equity", 0) or 0)
        le = float(acct.get("last_equity", 0) or 0)
        return eq - le
    except Exception:
        return 0.0


def json_safe_order(o: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(o)
    if "id" in out:
        out["id"] = str(out["id"])
    return out


def maybe_handle_test_orders(api: AlpacaRest, control: Dict[str, Any], notes: List[str]) -> None:
    tb = control.get("test_buy") or {}
    if isinstance(tb, dict) and tb.get("go"):
        sym = str(tb.get("symbol", "")).strip().upper()
        qty = float(tb.get("qty", 1) or 1)
        try:
            api.submit_order(sym, "buy", qty, tif="gtc" if "/" in sym else "day")
            popup_trade(f"TEST BUY {sym} x{qty}")
            notes.append(f"TEST BUY ok: {sym} x{qty}")
        except Exception as e:
            notes.append(f"TEST BUY FEIL: {sym} -> {e}")
        tb["go"] = False
        control["test_buy"] = tb

    ts = control.get("test_sell_all") or {}
    if isinstance(ts, dict) and ts.get("go"):
        sym = str(ts.get("symbol", "")).strip().upper()
        try:
            api.close_position(sym)
            popup_trade(f"TEST SELL ALL {sym}")
            notes.append(f"TEST SELL ALL ok: {sym}")
        except Exception as e:
            notes.append(f"TEST SELL ALL FEIL: {sym} -> {e}")
        ts["go"] = False
        control["test_sell_all"] = ts


def chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i : i + n] for i in range(0, len(lst), n)]


def has_open_order_for_symbol(orders: List[Dict[str, Any]], sym: str) -> bool:
    sym = sym.upper()
    open_statuses = {"new", "accepted", "pending_new", "partially_filled", "held", "replaced"}
    for o in orders or []:
        try:
            if symbol_key(str(o.get("symbol", ""))) != symbol_key(sym):
                continue
            st = str(o.get("status", "")).lower()
            if st in open_statuses:
                return True
        except Exception:
            continue
    return False


# ----------------------------
# Data cache
# ----------------------------
class BarsCache:
    def __init__(self) -> None:
        self.data: Dict[str, List[Dict[str, Any]]] = {}
        self.last_refresh_ts: Dict[str, float] = {}

    def get_closes(self, symbol: str) -> List[float]:
        bars = self.data.get(symbol.upper()) or []
        closes: List[float] = []
        for b in bars:
            c = b.get("c")
            if c is None:
                continue
            try:
                closes.append(float(c))
            except Exception:
                continue
        return closes

    def needs_refresh(self, symbol: str, min_interval_sec: int) -> bool:
        last_ts = self.last_refresh_ts.get(symbol.upper(), 0.0)
        return (time.time() - last_ts) >= max(1, min_interval_sec)

    def merge(self, symbol: str, incoming: List[Dict[str, Any]], limit: int) -> None:
        sym = symbol.upper()
        current = self.data.get(sym) or []
        merged = current + list(incoming or [])
        by_ts: Dict[str, Dict[str, Any]] = {}
        for b in merged:
            ts = str(b.get("t") or b.get("timestamp") or "")
            if ts:
                by_ts[ts] = b
        ordered = sorted(by_ts.values(), key=lambda x: str(x.get("t") or x.get("timestamp") or ""))
        if limit > 0 and len(ordered) > limit:
            ordered = ordered[-limit:]
        self.data[sym] = ordered
        self.last_refresh_ts[sym] = time.time()

    def replace(self, symbol: str, incoming: List[Dict[str, Any]], limit: int) -> None:
        sym = symbol.upper()
        ordered = sorted(list(incoming or []), key=lambda x: str(x.get("t") or x.get("timestamp") or ""))
        if limit > 0 and len(ordered) > limit:
            ordered = ordered[-limit:]
        self.data[sym] = ordered
        self.last_refresh_ts[sym] = time.time()


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    instance_socket = acquire_single_instance_socket()
    cfg = load_cfg()
    api = AlpacaRest(cfg)

    print(f"{utc_now_iso()} | INFO | Starter bot. PAPER={cfg.paper} TRADING_URL={cfg.trading_base} FEED={cfg.stock_feed}")
    print(f"{utc_now_iso()} | INFO | HERE={HERE}")
    print(f"{utc_now_iso()} | INFO | CONTROL_PATH={CONTROL_PATH}")

    ensure_control_file()
    try:
        acct = api.account()
        print(f"{utc_now_iso()} | INFO | Account OK: status={acct.get('status')} equity={acct.get('equity')}")
    except AlpacaAuthError as e:
        control = safe_read_json(CONTROL_PATH, {})
        strategy = str(control.get("strategy_mode", "scalp")) if isinstance(control, dict) else "scalp"
        msg = str(e)
        print(f"{utc_now_iso()} | ERROR | {msg}")
        safe_write_json(
            STATUS_PATH,
            {
                "paper": cfg.paper,
                "auth_ok": False,
                "engine_state": "AUTH_ERROR",
                "armed": False,
                "paused": True,
                "kill": False,
                "extended_hours": False,
                "test_mode": False,
                "strategy_mode": strategy,
                "last_update": utc_now_iso(),
                "day_pnl_usd": 0,
                "notes": msg,
                "symbols": {},
                "orders": [],
            },
        )
        return
    except Exception as e:
        acct = {}
        print(f"{utc_now_iso()} | WARNING | Initial account check failed: {e}")


    if not os.path.exists(STATUS_PATH):
        safe_write_json(
            STATUS_PATH,
            {
                "paper": cfg.paper,
                "auth_ok": True,
                "engine_state": "STARTING",
                "armed": False,
                "paused": True,
                "kill": False,
                "extended_hours": False,
                "test_mode": False,
                "strategy_mode": "scalp",
                "last_update": utc_now_iso(),
                "day_pnl_usd": 0,
                "notes": "",
                "symbols": {},
                "orders": [],
            },
        )

    last_trade_ts: Dict[str, float] = {}
    position_peak_price: Dict[str, float] = {}
    position_seen_ts: Dict[str, float] = {}
    bars_cache = BarsCache()
    crypto_bars_cache = BarsCache()

    cached_clock: Dict[str, Any] = {"value": None, "ts": 0.0}
    cached_account: Dict[str, Any] = {"value": acct if isinstance(acct, dict) else {}, "ts": time.time() if acct else 0.0}
    cached_positions: Dict[str, Any] = {"value": [], "ts": 0.0}
    cached_orders: Dict[str, Any] = {"value": [], "ts": 0.0}

    while True:
        loop_start = time.time()
        notes: List[str] = []

        try:
            control = ensure_control_file()
            if not isinstance(control, dict):
                control = {}

            paused = bool(control.get("paused", False))
            kill = bool(control.get("kill", False))
            armed = bool(control.get("armed", False))
            extended = bool(control.get("extended_hours", False))
            test_mode = bool(control.get("test_mode", True))
            market_mode = str(control.get("market_mode", "auto")).strip().lower()
            if market_mode not in ("auto", "stocks", "crypto"):
                market_mode = "auto"
            crypto_strategy_mode = str(control.get("crypto_strategy_mode", "scalp")).strip().lower()
            if crypto_strategy_mode not in ("scalp", "trend", "cross"):
                crypto_strategy_mode = "scalp"

            strategy_mode = str(control.get("strategy_mode", "cross")).strip().lower()
            if strategy_mode not in ("cross", "trend", "hype", "scalp"):
                strategy_mode = "scalp"

            fast_sma = int(control.get("fast_sma", 10))
            slow_sma = int(control.get("slow_sma", 30))
            fast_sma = max(2, fast_sma)
            slow_sma = max(fast_sma + 1, slow_sma)

            poll_sec = int(control.get("poll_sec", 5))
            poll_sec = max(1, poll_sec)

            stock_qty = float(control.get("stock_qty", 1) or 1)

            cooldown_sec = int(control.get("cooldown_sec", 15))
            cooldown_sec = max(1, cooldown_sec)

            timeframe = str(control.get("timeframe", "1Min")).strip() or "1Min"
            limit = int(control.get("bars_limit", max(120, slow_sma + 10)))

            max_spread_usd = float(control.get("max_spread_usd", 0.10) or 0.0)
            quote_stale_sec = int(control.get("quote_stale_sec", 20))
            skip_if_open_order = bool(control.get("skip_if_open_order", True))

            hype_mom_n = int(control.get("hype_mom_n", 6))
            min_mom_pct = float(control.get("min_mom_pct", 0.002) or 0.0)
            min_move_pct = float(control.get("min_move_pct", 0.001) or 0.0)
            min_abs_slope = float(control.get("min_abs_slope", 0.02) or 0.0)
            min_slope_pct = float(control.get("min_slope_pct", 0.00015) or 0.0)

            take_profit_pct = max(0.0, float(control.get("take_profit_pct", 0.0040) or 0.0))
            stop_loss_pct = max(0.0, float(control.get("stop_loss_pct", 0.0030) or 0.0))
            trailing_activate_pct = max(0.0, float(control.get("trailing_activate_pct", 0.0020) or 0.0))
            trailing_stop_pct = max(0.0, float(control.get("trailing_stop_pct", 0.0025) or 0.0))
            max_hold_sec = max(0, int(control.get("max_hold_sec", 1800) or 0))
            max_positions = max(1, int(control.get("max_positions", 5) or 5))
            max_spread_bps = max(0.0, float(control.get("max_spread_bps", 8.0) or 0.0))
            extended_limit_buffer_bps = max(0.0, float(control.get("extended_limit_buffer_bps", 5.0) or 0.0))

            crypto_notional_usd = max(1.0, float(control.get("crypto_notional_usd", 100.0) or 100.0))
            crypto_cooldown_sec = max(1, int(control.get("crypto_cooldown_sec", 30) or 30))
            crypto_max_spread_bps = max(0.0, float(control.get("crypto_max_spread_bps", 25.0) or 0.0))
            crypto_take_profit_pct = max(0.0, float(control.get("crypto_take_profit_pct", 0.0100) or 0.0))
            crypto_stop_loss_pct = max(0.0, float(control.get("crypto_stop_loss_pct", 0.0060) or 0.0))
            crypto_trailing_activate_pct = max(0.0, float(control.get("crypto_trailing_activate_pct", 0.0060) or 0.0))
            crypto_trailing_stop_pct = max(0.0, float(control.get("crypto_trailing_stop_pct", 0.0040) or 0.0))
            crypto_max_hold_sec = max(0, int(control.get("crypto_max_hold_sec", 2700) or 0))
            crypto_max_positions = max(1, int(control.get("crypto_max_positions", 3) or 3))
            crypto_taker_fee_bps = max(0.0, float(control.get("crypto_taker_fee_bps", 25.0) or 0.0))
            crypto_min_net_edge_bps = max(0.0, float(control.get("crypto_min_net_edge_bps", 20.0) or 0.0))
            crypto_entry_confirm_bars = max(1, int(control.get("crypto_entry_confirm_bars", 2) or 1))

            daily_loss_limit_usd = float(control.get("daily_loss_limit_usd", 0.0) or 0.0)
            daily_loss_limit_pct = max(0.0, float(control.get("daily_loss_limit_pct", 0.02) or 0.0))

            quotes_batch_size = int(control.get("quotes_batch_size", 200) or 200)
            quotes_batch_size = max(1, min(500, quotes_batch_size))

            bars_batch_size = int(control.get("bars_batch_size", 200) or 200)
            bars_batch_size = max(1, min(500, bars_batch_size))

            # Ytelsesforbedring:
            # bars trenger ikke refetches hvert sekund siden 1Min bars ikke endrer seg meningsfullt like ofte.
            refresh_bars_every_sec = int(control.get("refresh_bars_every_sec", max(10, min(60, timeframe_to_seconds(timeframe) // 2))))
            refresh_bars_every_sec = max(1, refresh_bars_every_sec)

            stocks = symbol_universe(control)
            crypto_symbols = crypto_universe(control)
            trade_stocks = market_mode in ("auto", "stocks")
            trade_crypto = market_mode in ("auto", "crypto")

            if armed and not kill:
                try:
                    maybe_handle_test_orders(api, control, notes)
                except Exception as e:
                    notes.append(f"Test-order handler feilet: {e}")

            try:
                safe_write_json(CONTROL_PATH, control)
            except Exception:
                pass

            # Konto/posisjoner/ordrer caches. Markedsdata kan fortsatt skannes hvert sekund,
            # men vi bruker ikke opp REST-kall på uforandret kontostatus.
            day_pnl = 0.0
            positions_map: Dict[str, Dict[str, Any]] = {}
            orders: List[Dict[str, Any]] = []
            stock_ok = False
            now_api = time.time()

            future_map: Dict[Any, str] = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                if (now_api - cached_account["ts"]) >= 5.0:
                    future_map[pool.submit(api.account)] = "account"
                if (now_api - cached_positions["ts"]) >= 2.0:
                    future_map[pool.submit(api.positions)] = "positions"
                if (now_api - cached_orders["ts"]) >= 2.0:
                    future_map[pool.submit(api.get_orders, 30, "all")] = "orders"
                if (now_api - cached_clock["ts"]) >= 10.0:
                    future_map[pool.submit(api.clock)] = "clock"

                results: Dict[str, Any] = {}
                for fut in as_completed(future_map):
                    name = future_map[fut]
                    try:
                        results[name] = fut.result()
                    except Exception as e:
                        notes.append(f"{name.title()} feilet: {e}")

            if isinstance(results.get("account"), dict):
                cached_account["value"] = results["account"]
                cached_account["ts"] = time.time()
            if isinstance(results.get("positions"), list):
                cached_positions["value"] = results["positions"]
                cached_positions["ts"] = time.time()
            if isinstance(results.get("orders"), list):
                cached_orders["value"] = results["orders"]
                cached_orders["ts"] = time.time()
            if isinstance(results.get("clock"), dict):
                cached_clock["value"] = results["clock"]
                cached_clock["ts"] = time.time()

            acct = cached_account.get("value") or {}
            if acct:
                day_pnl = calc_day_pnl_usd(acct)

            pos_raw = cached_positions.get("value") or []
            positions_map = positions_to_map(pos_raw if isinstance(pos_raw, list) else [])

            orders_raw = cached_orders.get("value") or []
            if isinstance(orders_raw, list):
                orders = [json_safe_order(o) for o in orders_raw]

            clk = cached_clock.get("value") or {}
            regular_market_open = bool(clk.get("is_open", False))
            allow_extended = bool(extended or market_mode == "auto")
            stock_session = current_stock_session(regular_market_open, allow_extended)
            extended_session_open = stock_session in {"PREMARKET", "AFTERHOURS", "OVERNIGHT"}
            stock_ok = trade_stocks and stock_session != "CLOSED"
            stock_live_feed = "overnight" if stock_session == "OVERNIGHT" else cfg.stock_feed

            risk_blocked = False
            effective_daily_loss_limit = abs(daily_loss_limit_usd) if daily_loss_limit_usd > 0 else 0.0
            if effective_daily_loss_limit <= 0 and daily_loss_limit_pct > 0 and acct:
                try:
                    last_equity = float(acct.get("last_equity", 0) or 0)
                    if last_equity > 0:
                        effective_daily_loss_limit = last_equity * daily_loss_limit_pct
                except Exception:
                    effective_daily_loss_limit = 0.0
            if effective_daily_loss_limit > 0 and day_pnl <= -effective_daily_loss_limit:
                notes.append(
                    f"DAILY LOSS LIMIT hit ({day_pnl:.2f} <= -{effective_daily_loss_limit:.2f}) -> trading blocked"
                )
                risk_blocked = True

            symbols_state: Dict[str, Any] = {}
            if trade_stocks and not stocks:
                notes.append("Ingen aksjesymboler i control.json['symbols'].")
            elif trade_stocks and not stock_ok:
                for sym in stocks:
                    pos = position_for_symbol(positions_map, sym)
                    symbols_state[sym] = {
                        "market": "STOCK", "price": None, "bid": None, "ask": None, "spread": None,
                        "quote_time": None, "signal": None,
                        "pos_qty": float(pos.get("qty", 0) or 0) if pos else 0,
                        "avg_entry": float(pos.get("avg_entry_price", 0) or 0) if pos else None,
                        "action": "WAIT_MARKET", "error": "", "debug": {"session": stock_session},
                    }
            elif trade_stocks:
                # quotes
                quotes_by_sym: Dict[str, Dict[str, Any]] = {}
                quote_parts = chunk(stocks, quotes_batch_size)
                if quote_parts:
                    with ThreadPoolExecutor(max_workers=min(8, len(quote_parts))) as pool:
                        futs = [pool.submit(api.quotes_latest, part, stock_live_feed) for part in quote_parts]
                        for fut in as_completed(futs):
                            try:
                                quotes_by_sym.update(fut.result() or {})
                            except Exception as e:
                                notes.append(f"Quotes feilet: {e}")

                # Bars: backfill history only for cold symbols. Once warm, use the
                # lightweight latest-bar endpoint instead of repeatedly downloading days of history.
                refresh_syms = [s for s in stocks if bars_cache.needs_refresh(s, refresh_bars_every_sec)]
                cold_syms = [s for s in refresh_syms if not bars_cache.get_closes(s)]
                warm_syms = [s for s in refresh_syms if bars_cache.get_closes(s)]

                if cold_syms:
                    end_dt = utc_now()
                    start_dt = end_dt - timedelta(days=3)
                    start_iso = start_dt.isoformat()
                    end_iso = end_dt.isoformat()
                    bar_parts = chunk(cold_syms, bars_batch_size)
                    with ThreadPoolExecutor(max_workers=min(4, len(bar_parts))) as pool:
                        futs = [pool.submit(api.bars, part, timeframe, limit, start_iso, end_iso, cfg.stock_feed) for part in bar_parts]
                        for fut in as_completed(futs):
                            try:
                                result = fut.result() or {}
                                for rsym, arr in result.items():
                                    if isinstance(arr, list):
                                        bars_cache.replace(rsym.upper(), arr, limit)
                            except Exception as e:
                                notes.append(f"Bars backfill failed: {e}")

                if warm_syms and timeframe.strip().lower() in ("1min", "1t"):
                    latest_parts = chunk(warm_syms, bars_batch_size)
                    with ThreadPoolExecutor(max_workers=min(4, len(latest_parts))) as pool:
                        futs = [pool.submit(api.latest_bars, part, stock_live_feed) for part in latest_parts]
                        for fut in as_completed(futs):
                            try:
                                result = fut.result() or {}
                                for rsym, bar in result.items():
                                    if isinstance(bar, dict):
                                        bars_cache.merge(rsym.upper(), [bar], limit)
                            except Exception as e:
                                notes.append(f"Latest bars failed: {e}")
                elif warm_syms:
                    end_dt = utc_now()
                    start_dt = end_dt - timedelta(days=3)
                    for part in chunk(warm_syms, bars_batch_size):
                        try:
                            result = api.bars(part, timeframe, limit, start_dt.isoformat(), end_dt.isoformat(), cfg.stock_feed)
                            for rsym, arr in (result or {}).items():
                                if isinstance(arr, list):
                                    bars_cache.replace(rsym.upper(), arr, limit)
                        except Exception as e:
                            notes.append(f"Bars refresh failed: {e}")

                now_ts = time.time()
                for sym in stocks:
                    st: Dict[str, Any] = {
                        "market": "STOCK",
                        "price": None,
                        "bid": None,
                        "ask": None,
                        "spread": None,
                        "quote_time": None,
                        "signal": None,
                        "pos_qty": 0,
                        "avg_entry": None,
                        "action": "NONE",
                        "error": "",
                        "debug": {},
                    }

                    try:
                        pos = position_for_symbol(positions_map, sym)
                        if pos:
                            try:
                                st["pos_qty"] = float(pos.get("qty", 0) or 0)
                                st["avg_entry"] = float(pos.get("avg_entry_price", 0) or 0)
                            except Exception:
                                pass

                        q = quotes_by_sym.get(sym.upper())
                        quote_age_ok = True
                        if isinstance(q, dict):
                            try:
                                bp = q.get("bp")
                                ap = q.get("ap")
                                qt = q.get("t") or q.get("timestamp")

                                if bp is not None and float(bp) > 0:
                                    st["bid"] = float(bp)
                                if ap is not None and float(ap) > 0:
                                    st["ask"] = float(ap)

                                if st["bid"] is not None and st["ask"] is not None and float(st["ask"]) >= float(st["bid"]):
                                    st["spread"] = float(st["ask"]) - float(st["bid"])
                                    st["price"] = (float(st["bid"]) + float(st["ask"])) / 2.0
                                    if st["price"] and float(st["price"]) > 0:
                                        st["debug"]["spread_bps"] = round(
                                            10000.0 * float(st["spread"]) / float(st["price"]), 3
                                        )

                                if qt is not None:
                                    st["quote_time"] = str(qt)

                                if qt is not None and quote_stale_sec > 0:
                                    qdt = parse_any_ts(qt)
                                    if qdt is not None:
                                        age = (utc_now() - qdt).total_seconds()
                                        st["debug"]["quote_age_sec"] = round(age, 3)
                                        if age > quote_stale_sec:
                                            quote_age_ok = False
                            except Exception:
                                pass

                        closes = bars_cache.get_closes(sym.upper())
                        if closes and st["price"] is None:
                            st["price"] = closes[-1]

                        if not closes:
                            st["error"] = "No bars"
                            st["action"] = "WAIT_DATA"
                            symbols_state[sym] = st
                            continue

                        sig = 0
                        if strategy_mode == "cross":
                            sig = crossover_signal(closes, fast_sma, slow_sma)
                        elif strategy_mode == "trend":
                            tsig = trend_state(closes, fast_sma, slow_sma)
                            sig = int(tsig or 0)
                        elif strategy_mode == "scalp":
                            sig, dbg = scalp_signal(
                                closes,
                                st.get("price"),
                                fast_sma,
                                slow_sma,
                                hype_mom_n,
                                min_mom_pct,
                                min_move_pct,
                                min_slope_pct,
                            )
                            st["debug"].update(dbg)
                        else:
                            tsig = trend_state(closes, fast_sma, slow_sma)
                            mom = momentum_score(closes, hype_mom_n)
                            slp = slope_of_sma(closes, fast_sma, lookback=3)

                            st["debug"]["mom"] = None if mom is None else round(mom, 6)
                            st["debug"]["slope_fast_sma"] = None if slp is None else round(slp, 6)

                            base = int(tsig or 0)
                            last_move = pct_change(closes[-2], closes[-1]) if len(closes) >= 2 else 0.0
                            st["debug"]["last_move_pct"] = round(last_move, 6)

                            if (
                                base > 0
                                and mom is not None and mom >= abs(min_mom_pct)
                                and slp is not None and slp >= abs(min_abs_slope)
                                and last_move >= abs(min_move_pct)
                            ):
                                sig = 1
                            elif (
                                base < 0
                                and mom is not None and mom <= -abs(min_mom_pct)
                                and slp is not None and slp <= -abs(min_abs_slope)
                                and last_move <= -abs(min_move_pct)
                            ):
                                sig = -1

                        st["signal"] = sig

                        if not armed or paused or kill:
                            st["action"] = "HOLD"
                            symbols_state[sym] = st
                            continue

                        if not stock_ok:
                            st["action"] = "WAIT_MARKET"
                            symbols_state[sym] = st
                            continue

                        if test_mode:
                            st["action"] = "TEST_MODE"
                            symbols_state[sym] = st
                            continue

                        if isinstance(q, dict) and not quote_age_ok:
                            st["action"] = "QUOTE_STALE"
                            symbols_state[sym] = st
                            continue

                        qty = stock_qty

                        # Position management gets priority over entry-only filters.
                        # A pending order/cooldown/spread filter must never suppress a protective exit.
                        # This makes the bot recycle capital more often while keeping a bounded loss.
                        if st["pos_qty"] > 0 and st.get("avg_entry") and st.get("price"):
                            avg_entry = float(st["avg_entry"])
                            live_price = float(st["price"])
                            pnl_pct = pct_change(avg_entry, live_price)
                            st["debug"]["position_pnl_pct"] = round(pnl_pct, 6)

                            if sym not in position_seen_ts:
                                position_seen_ts[sym] = now_ts
                            peak = max(position_peak_price.get(sym, live_price), live_price)
                            position_peak_price[sym] = peak
                            held_sec = max(0.0, now_ts - position_seen_ts.get(sym, now_ts))
                            st["debug"]["held_sec"] = int(held_sec)
                            st["debug"]["peak_price"] = round(peak, 4)

                            exit_reason = ""
                            if take_profit_pct > 0 and pnl_pct >= take_profit_pct:
                                exit_reason = "TAKE_PROFIT"
                            elif stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
                                exit_reason = "STOP_LOSS"
                            elif (
                                trailing_stop_pct > 0
                                and trailing_activate_pct > 0
                                and peak >= avg_entry * (1.0 + trailing_activate_pct)
                                and live_price <= peak * (1.0 - trailing_stop_pct)
                            ):
                                exit_reason = "TRAILING_STOP"
                            elif max_hold_sec > 0 and held_sec >= max_hold_sec:
                                exit_reason = "MAX_HOLD"
                            elif sig < 0:
                                exit_reason = "SIGNAL_FLIP"

                            if exit_reason:
                                if regular_market_open:
                                    api.close_position(sym)
                                else:
                                    if not extended_session_open or not st.get("bid"):
                                        st["action"] = "WAIT_EXTENDED_QUOTE"
                                        symbols_state[sym] = st
                                        continue
                                    sell_limit = max(0.01, float(st["bid"]) * (1.0 - extended_limit_buffer_bps / 10000.0))
                                    api.submit_order(
                                        sym,
                                        "sell",
                                        float(st["pos_qty"]),
                                        tif="day",
                                        order_type="limit",
                                        limit_price=sell_limit,
                                        extended_hours=True,
                                    )
                                popup_trade(f"SELL ALL {sym} ({exit_reason})")
                                last_trade_ts[sym] = now_ts
                                st["action"] = f"SELL_ALL:{exit_reason}"
                                notes.append(f"AUTO SELL {sym} reason={exit_reason} pnl={pnl_pct:.4%}")
                                position_peak_price.pop(sym, None)
                                position_seen_ts.pop(sym, None)
                                symbols_state[sym] = st
                                continue
                        else:
                            position_peak_price.pop(sym, None)
                            position_seen_ts.pop(sym, None)

                        open_position_count = sum(
                            1 for p in (pos_raw if isinstance(pos_raw, list) else [])
                            if float(p.get("qty", 0) or 0) > 0
                            and str(p.get("asset_class", "")).lower() != "crypto"
                            and str(p.get("exchange", "")).upper() != "CRYPTO"
                            and "/" not in str(p.get("symbol", ""))
                        )

                        if sig > 0 and st["pos_qty"] <= 0:
                            if risk_blocked:
                                st["action"] = "RISK_BLOCKED"
                            elif skip_if_open_order and has_open_order_for_symbol(orders, sym):
                                st["action"] = "OPEN_ORDER"
                            elif now_ts - last_trade_ts.get(sym, 0) < cooldown_sec:
                                st["action"] = "COOLDOWN"
                            elif max_spread_usd > 0 and st["spread"] is not None and float(st["spread"]) > max_spread_usd:
                                st["action"] = "SPREAD_TOO_WIDE"
                            elif max_spread_bps > 0 and float(st["debug"].get("spread_bps", 0.0) or 0.0) > max_spread_bps:
                                st["action"] = "SPREAD_BPS_TOO_WIDE"
                            elif open_position_count >= max_positions:
                                st["action"] = "MAX_POSITIONS"
                            else:
                                if regular_market_open:
                                    api.submit_order(sym, "buy", qty, tif="day")
                                else:
                                    if not extended_session_open or not st.get("ask"):
                                        st["action"] = "WAIT_EXTENDED_QUOTE"
                                        symbols_state[sym] = st
                                        continue
                                    buy_limit = float(st["ask"]) * (1.0 + extended_limit_buffer_bps / 10000.0)
                                    api.submit_order(
                                        sym,
                                        "buy",
                                        qty,
                                        tif="day",
                                        order_type="limit",
                                        limit_price=buy_limit,
                                        extended_hours=True,
                                    )
                                popup_trade(f"BUY {sym} x{qty}")
                                last_trade_ts[sym] = now_ts
                                st["action"] = f"BUY {qty}"
                                notes.append(f"AUTO {strategy_mode.upper()} BUY {sym} x{qty}")
                                # Prevent multiple symbols in the same loop from ignoring the cap.
                                positions_map[sym] = {"symbol": sym, "qty": str(qty), "avg_entry_price": str(st.get("price") or 0)}
                        else:
                            st["action"] = "HOLD"

                    except Exception as e:
                        st["error"] = str(e)

                    symbols_state[sym] = st

            # CRYPTO 24/7 -----------------------------------------------------
            if trade_crypto:
                if not crypto_symbols:
                    notes.append("Ingen crypto_symbols i control.json.")
                else:
                    crypto_quotes: Dict[str, Dict[str, Any]] = {}
                    for part in chunk(crypto_symbols, min(100, quotes_batch_size)):
                        try:
                            crypto_quotes.update(api.crypto_quotes_latest(part) or {})
                        except Exception as e:
                            notes.append(f"Crypto quotes feilet: {e}")

                    refresh_crypto = [s for s in crypto_symbols if crypto_bars_cache.needs_refresh(s, refresh_bars_every_sec)]
                    cold_crypto = [s for s in refresh_crypto if not crypto_bars_cache.get_closes(s)]
                    warm_crypto = [s for s in refresh_crypto if crypto_bars_cache.get_closes(s)]
                    if cold_crypto:
                        end_dt = utc_now()
                        start_dt = end_dt - timedelta(days=2)
                        for part in chunk(cold_crypto, min(50, bars_batch_size)):
                            try:
                                result = api.crypto_bars(part, timeframe, limit, start_dt.isoformat(), end_dt.isoformat())
                                for rsym, arr in (result or {}).items():
                                    if isinstance(arr, list):
                                        crypto_bars_cache.replace(rsym.upper(), arr, limit)
                            except Exception as e:
                                notes.append(f"Crypto bars backfill feilet: {e}")
                    if warm_crypto and timeframe.strip().lower() in ("1min", "1t"):
                        for part in chunk(warm_crypto, min(100, bars_batch_size)):
                            try:
                                result = api.crypto_latest_bars(part) or {}
                                for rsym, bar in result.items():
                                    if isinstance(bar, dict):
                                        crypto_bars_cache.merge(rsym.upper(), [bar], limit)
                            except Exception as e:
                                notes.append(f"Crypto latest bars feilet: {e}")
                    elif warm_crypto:
                        end_dt = utc_now()
                        start_dt = end_dt - timedelta(days=2)
                        for part in chunk(warm_crypto, min(50, bars_batch_size)):
                            try:
                                result = api.crypto_bars(part, timeframe, limit, start_dt.isoformat(), end_dt.isoformat())
                                for rsym, arr in (result or {}).items():
                                    if isinstance(arr, list):
                                        crypto_bars_cache.replace(rsym.upper(), arr, limit)
                            except Exception as e:
                                notes.append(f"Crypto bars refresh feilet: {e}")

                    now_crypto_ts = time.time()
                    crypto_position_count = sum(
                        1 for p in (pos_raw if isinstance(pos_raw, list) else [])
                        if float(p.get("qty", 0) or 0) > 0
                        and (str(p.get("asset_class", "")).lower() == "crypto"
                             or str(p.get("exchange", "")).upper() == "CRYPTO"
                             or symbol_key(str(p.get("symbol", ""))) in {symbol_key(x) for x in crypto_symbols})
                    )

                    for sym in crypto_symbols:
                        st: Dict[str, Any] = {
                            "market": "CRYPTO", "price": None, "bid": None, "ask": None, "spread": None,
                            "quote_time": None, "signal": None, "pos_qty": 0, "avg_entry": None,
                            "action": "NONE", "error": "", "debug": {"session": "24/7"},
                        }
                        try:
                            pos = position_for_symbol(positions_map, sym)
                            if pos:
                                st["pos_qty"] = float(pos.get("qty", 0) or 0)
                                st["avg_entry"] = float(pos.get("avg_entry_price", 0) or 0)

                            q = crypto_quotes.get(sym) or crypto_quotes.get(sym.upper())
                            quote_age_ok = True
                            if isinstance(q, dict):
                                bp, ap = q.get("bp"), q.get("ap")
                                qt = q.get("t") or q.get("timestamp")
                                if bp is not None and float(bp) > 0:
                                    st["bid"] = float(bp)
                                if ap is not None and float(ap) > 0:
                                    st["ask"] = float(ap)
                                if st["bid"] is not None and st["ask"] is not None and float(st["ask"]) >= float(st["bid"]):
                                    st["spread"] = float(st["ask"]) - float(st["bid"])
                                    st["price"] = (float(st["bid"]) + float(st["ask"])) / 2.0
                                    if st["price"] > 0:
                                        st["debug"]["spread_bps"] = round(10000.0 * float(st["spread"]) / float(st["price"]), 3)
                                if qt is not None:
                                    st["quote_time"] = str(qt)
                                    qdt = parse_any_ts(qt)
                                    if qdt is not None and quote_stale_sec > 0:
                                        age = (utc_now() - qdt).total_seconds()
                                        st["debug"]["quote_age_sec"] = round(age, 3)
                                        if age > max(quote_stale_sec, 30):
                                            quote_age_ok = False

                            closes = crypto_bars_cache.get_closes(sym)
                            if closes and st["price"] is None:
                                st["price"] = closes[-1]
                            if not closes:
                                st["error"] = "No crypto bars"
                                st["action"] = "WAIT_DATA"
                                symbols_state[sym] = st
                                continue

                            if crypto_strategy_mode == "cross":
                                sig = crossover_signal(closes, fast_sma, slow_sma)
                            elif crypto_strategy_mode == "trend":
                                sig = int(trend_state(closes, fast_sma, slow_sma) or 0)
                            else:
                                sig, dbg = scalp_signal(
                                    closes, st.get("price"), fast_sma, slow_sma, hype_mom_n,
                                    min_mom_pct, min_move_pct, min_slope_pct,
                                )
                                st["debug"].update(dbg)
                            st["signal"] = sig

                            if not armed or paused or kill:
                                st["action"] = "HOLD"
                                symbols_state[sym] = st
                                continue
                            if test_mode:
                                st["action"] = "TEST_MODE"
                                symbols_state[sym] = st
                                continue

                            # IMPORTANT: stale quotes may block NEW entries, but must never suppress
                            # protective exits for an existing crypto position.
                            quote_is_stale = bool(isinstance(q, dict) and not quote_age_ok)

                            if st["pos_qty"] > 0 and st.get("avg_entry") and st.get("price"):
                                avg_entry = float(st["avg_entry"])
                                live_price = float(st["price"])
                                pnl_pct = pct_change(avg_entry, live_price)
                                st["debug"]["position_pnl_pct"] = round(pnl_pct, 6)
                                k = f"CRYPTO:{sym}"
                                if k not in position_seen_ts:
                                    position_seen_ts[k] = now_crypto_ts
                                peak = max(position_peak_price.get(k, live_price), live_price)
                                position_peak_price[k] = peak
                                held_sec = max(0.0, now_crypto_ts - position_seen_ts.get(k, now_crypto_ts))
                                st["debug"]["held_sec"] = int(held_sec)
                                exit_reason = ""
                                if crypto_take_profit_pct > 0 and pnl_pct >= crypto_take_profit_pct:
                                    exit_reason = "TAKE_PROFIT"
                                elif crypto_stop_loss_pct > 0 and pnl_pct <= -crypto_stop_loss_pct:
                                    exit_reason = "STOP_LOSS"
                                elif (crypto_trailing_stop_pct > 0 and crypto_trailing_activate_pct > 0
                                      and peak >= avg_entry * (1.0 + crypto_trailing_activate_pct)
                                      and live_price <= peak * (1.0 - crypto_trailing_stop_pct)):
                                    exit_reason = "TRAILING_STOP"
                                elif crypto_max_hold_sec > 0 and held_sec >= crypto_max_hold_sec:
                                    exit_reason = "MAX_HOLD"
                                elif sig < 0:
                                    exit_reason = "SIGNAL_FLIP"
                                if exit_reason:
                                    api.submit_order(sym, "sell", float(st["pos_qty"]), tif="gtc", order_type="market")
                                    popup_trade(f"CRYPTO SELL {sym} ({exit_reason})")
                                    last_trade_ts[k] = now_crypto_ts
                                    st["action"] = f"SELL_ALL:{exit_reason}"
                                    notes.append(f"CRYPTO SELL {sym} reason={exit_reason} pnl={pnl_pct:.4%}")
                                    position_peak_price.pop(k, None)
                                    position_seen_ts.pop(k, None)
                                    symbols_state[sym] = st
                                    continue

                                # Keep holding if no protective exit fired. A stale quote is shown
                                # explicitly, but it no longer disables the stop logic above.
                                if quote_is_stale:
                                    st["action"] = "QUOTE_STALE_HOLD"
                                    symbols_state[sym] = st
                                    continue
                            else:
                                position_peak_price.pop(f"CRYPTO:{sym}", None)
                                position_seen_ts.pop(f"CRYPTO:{sym}", None)

                            k = f"CRYPTO:{sym}"
                            if sig > 0 and st["pos_qty"] <= 0:
                                spread_bps = float(st["debug"].get("spread_bps", 0.0) or 0.0)
                                gross_target_bps = crypto_take_profit_pct * 10000.0
                                estimated_rt_cost_bps = crypto_estimated_roundtrip_cost_bps(
                                    spread_bps, crypto_taker_fee_bps
                                )
                                estimated_net_edge_bps = gross_target_bps - estimated_rt_cost_bps
                                st["debug"]["gross_target_bps"] = round(gross_target_bps, 2)
                                st["debug"]["estimated_rt_cost_bps"] = round(estimated_rt_cost_bps, 2)
                                st["debug"]["estimated_net_edge_bps"] = round(estimated_net_edge_bps, 2)

                                confirmations = 1
                                if crypto_strategy_mode == "scalp":
                                    confirmations = crypto_long_confirmations(
                                        closes, st.get("price"), fast_sma, slow_sma, hype_mom_n,
                                        min_mom_pct, min_move_pct, min_slope_pct,
                                        crypto_entry_confirm_bars,
                                    )
                                st["debug"]["entry_confirmations"] = confirmations
                                st["debug"]["entry_confirmations_required"] = crypto_entry_confirm_bars

                                if quote_is_stale:
                                    st["action"] = "QUOTE_STALE"
                                elif risk_blocked:
                                    st["action"] = "RISK_BLOCKED"
                                elif skip_if_open_order and has_open_order_for_symbol(orders, sym):
                                    st["action"] = "OPEN_ORDER"
                                elif now_crypto_ts - last_trade_ts.get(k, 0) < crypto_cooldown_sec:
                                    st["action"] = "COOLDOWN"
                                elif crypto_strategy_mode == "scalp" and confirmations < crypto_entry_confirm_bars:
                                    st["action"] = "WAIT_CONFIRM"
                                elif crypto_max_spread_bps > 0 and spread_bps > crypto_max_spread_bps:
                                    st["action"] = "SPREAD_BPS_TOO_WIDE"
                                elif estimated_net_edge_bps < crypto_min_net_edge_bps:
                                    st["action"] = "COST_TOO_HIGH"
                                elif crypto_position_count >= crypto_max_positions:
                                    st["action"] = "MAX_CRYPTO_POSITIONS"
                                else:
                                    api.submit_notional_order(sym, "buy", crypto_notional_usd, tif="gtc", order_type="market")
                                    popup_trade(f"CRYPTO BUY {sym} ${crypto_notional_usd:.0f}")
                                    last_trade_ts[k] = now_crypto_ts
                                    st["action"] = f"BUY ${crypto_notional_usd:.0f}"
                                    notes.append(
                                        f"CRYPTO {crypto_strategy_mode.upper()} BUY {sym} ${crypto_notional_usd:.2f} "
                                        f"est_cost={estimated_rt_cost_bps:.1f}bps est_net={estimated_net_edge_bps:.1f}bps"
                                    )
                                    crypto_position_count += 1
                            else:
                                st["action"] = "QUOTE_STALE" if quote_is_stale else "HOLD"
                        except Exception as e:
                            st["error"] = str(e)
                        symbols_state[sym] = st

            out = {
                "paper": cfg.paper,
                "auth_ok": True,
                "engine_state": "RUNNING",
                "armed": armed,
                "paused": paused,
                "kill": kill,
                "extended_hours": extended,
                "auto_extended": bool(market_mode == "auto"),
                "market_mode": market_mode,
                "stock_session": stock_session,
                "crypto_24_7": bool(trade_crypto),
                "test_mode": test_mode,
                "strategy_mode": strategy_mode,
                "crypto_strategy_mode": crypto_strategy_mode,
                "last_update": utc_now_iso(),
                "day_pnl_usd": round(day_pnl, 2),
                "risk_blocked": risk_blocked,
                "daily_loss_limit_usd_effective": round(effective_daily_loss_limit, 2),
                "notes": " | ".join([n for n in notes if n]).strip(),
                "symbols": symbols_state,
                "orders": orders,
            }
            safe_write_json(STATUS_PATH, out)

            print(
                f"{utc_now_iso()} | INFO | Heartbeat: market={market_mode} stock_session={stock_session} mode={strategy_mode} symbols={len(symbols_state)} "
                f"armed={armed} paused={paused} TEST={test_mode} orders={len(orders)} "
                f"poll={poll_sec}s bars_refresh={refresh_bars_every_sec}s notes={out['notes']}"
            )

        except Exception as e:
            err = f"{utc_now_iso()} | ERROR | {e}"
            print(err)
            traceback.print_exc()
            try:
                safe_write_json(
                    STATUS_PATH,
                    {
                        "paper": cfg.paper if "cfg" in locals() else True,
                        "auth_ok": True,
                        "engine_state": "ERROR",
                        "armed": bool(locals().get("armed", False)),
                        "paused": bool(locals().get("paused", False)),
                        "kill": bool(locals().get("kill", False)),
                        "extended_hours": bool(locals().get("extended", False)),
                        "test_mode": bool(locals().get("test_mode", False)),
                        "strategy_mode": str(locals().get("strategy_mode", "scalp")),
                        "last_update": utc_now_iso(),
                        "day_pnl_usd": 0,
                        "notes": err,
                        "symbols": {},
                        "orders": [],
                    },
                )
            except Exception:
                pass

        control = safe_read_json(CONTROL_PATH, {})
        poll_sec = 2
        try:
            if isinstance(control, dict):
                poll_sec = max(1, int(control.get("poll_sec", 2)))
        except Exception:
            poll_sec = 2

        elapsed = time.time() - loop_start
        sleep_for = max(0.0, poll_sec - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"{utc_now_iso()} | INFO | Avslutter (Ctrl+C).")
    except Exception as e:
        print(f"{utc_now_iso()} | ERROR | {e}")
        traceback.print_exc()
        raise

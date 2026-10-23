#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Alpaca Edge V4.9 sidecar for TradingBot.

Adds broker-native observability and low-risk decision guards without replacing
TradingBot's core execution engine:
- order group_id/group_role preservation for bracket/OCO reconciliation
- realtime Alpaca News websocket modifier
- corporate-action entry veto
- centralized REST request budget / 429 telemetry
- v3 market clock/calendar cross-checks
- persistent event recorder for decisions, news, trade updates and session health
- overnight/BOATS shadow data collection only (never submits overnight orders)

The module is installed by cloud_app.py before trading_bot.main() starts.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover - core bot still runs without news stream
    websocket = None

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def sf(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def symbol_key(v: Any) -> str:
    return str(v or "").upper().replace("/", "").replace("-", "").strip()


def parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        return None


class ApiBudget:
    """Soft client-side budget below Alpaca's account REST ceiling.

    This counts logical REST calls made by TradingBot and sidecar collectors.
    The bot's own _req retry/backoff remains authoritative for actual network
    errors; this layer prevents avoidable bursts and records 429 pressure.
    """

    def __init__(self, soft_limit_per_min: int = 175) -> None:
        self.soft_limit = max(30, int(soft_limit_per_min))
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()
        self.total = 0
        self.rate_limited = 0
        self.last_429_utc: Optional[str] = None
        self.last_url = ""

    def _trim(self, now: float) -> None:
        while self.calls and now - self.calls[0] >= 60.0:
            self.calls.popleft()

    def before(self, url: str = "") -> None:
        while True:
            wait = 0.0
            now = time.time()
            with self.lock:
                self._trim(now)
                if len(self.calls) < self.soft_limit:
                    self.calls.append(now)
                    self.total += 1
                    self.last_url = str(url or "")[-180:]
                    return
                wait = max(0.05, 60.0 - (now - self.calls[0]) + 0.05)
            time.sleep(min(wait, 2.0))

    def note_429(self) -> None:
        with self.lock:
            self.rate_limited += 1
            self.last_429_utc = iso_now()

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            self._trim(time.time())
            return {
                "calls_last_60s": len(self.calls),
                "soft_limit_per_min": self.soft_limit,
                "total_calls": self.total,
                "http_429_count": self.rate_limited,
                "last_429_utc": self.last_429_utc,
            }


class EventRecorder:
    """Compact persistent audit database designed for later AI analysis."""

    def __init__(self, state_dir: str) -> None:
        self.path = str(Path(state_dir) / "alpaca_edge_v49.sqlite")
        self.lock = threading.RLock()
        self.last_error = ""
        self.last_write_utc: Optional[str] = None
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    symbol TEXT,
                    payload TEXT NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_edge_events_kind_ts ON events(kind,ts)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_edge_events_symbol_ts ON events(symbol,ts)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS news(
                    article_id TEXT PRIMARY KEY,
                    created_at TEXT,
                    received_at TEXT,
                    symbols TEXT,
                    source TEXT,
                    headline TEXT,
                    score REAL,
                    payload TEXT
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS overnight_shadow(
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL,
                    minute_open REAL,
                    minute_close REAL,
                    prev_close REAL,
                    signal REAL,
                    feed TEXT,
                    payload TEXT,
                    PRIMARY KEY(ts,symbol)
                )"""
            )

    def event(self, kind: str, symbol: str = "", payload: Optional[Dict[str, Any]] = None) -> None:
        try:
            raw = json.dumps(payload or {}, ensure_ascii=False, default=str, separators=(",", ":"))
            with self.lock, closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    "INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)",
                    (iso_now(), str(kind), str(symbol or ""), raw),
                )
                cutoff = (utc_now() - timedelta(days=45)).isoformat()
                db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.last_write_utc = iso_now()
            self.last_error = ""
        except Exception as exc:
            self.last_error = type(exc).__name__

    def news_item(self, item: Dict[str, Any], score: float) -> None:
        try:
            article_id = str(item.get("id") or item.get("url") or f"news-{time.time_ns()}")
            syms = [symbol_key(x) for x in (item.get("symbols") or []) if symbol_key(x)]
            payload = json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":"))
            with self.lock, closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    """INSERT OR REPLACE INTO news
                    (article_id,created_at,received_at,symbols,source,headline,score,payload)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        article_id,
                        str(item.get("created_at") or ""),
                        iso_now(),
                        ",".join(syms),
                        str(item.get("source") or ""),
                        str(item.get("headline") or "")[:1000],
                        float(score),
                        payload,
                    ),
                )
            self.last_write_utc = iso_now()
        except Exception as exc:
            self.last_error = type(exc).__name__

    def overnight(self, symbol: str, payload: Dict[str, Any], feed: str) -> None:
        try:
            lt = payload.get("latestTrade") or payload.get("latest_trade") or {}
            mb = payload.get("minuteBar") or payload.get("minute_bar") or {}
            pb = payload.get("prevDailyBar") or payload.get("prev_daily_bar") or {}
            price = sf(lt.get("p") or lt.get("price") or mb.get("c"))
            opn = sf(mb.get("o") or mb.get("open"))
            close = sf(mb.get("c") or mb.get("close") or price)
            prev = sf(pb.get("c") or pb.get("close"))
            signal = 0.0
            if opn > 0 and close > 0:
                signal = (close / opn - 1.0) * 10000.0
            ts = str(mb.get("t") or lt.get("t") or iso_now())
            raw = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            with self.lock, closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    """INSERT OR REPLACE INTO overnight_shadow
                    (ts,symbol,price,minute_open,minute_close,prev_close,signal,feed,payload)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (ts, symbol_key(symbol), price, opn, close, prev, signal, feed, raw),
                )
            self.last_write_utc = iso_now()
        except Exception as exc:
            self.last_error = type(exc).__name__

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db:
                for kind, n in db.execute("SELECT kind,COUNT(*) FROM events GROUP BY kind"):
                    counts[str(kind)] = int(n)
                news_n = int(db.execute("SELECT COUNT(*) FROM news").fetchone()[0])
                overnight_n = int(db.execute("SELECT COUNT(*) FROM overnight_shadow").fetchone()[0])
        except Exception:
            news_n = 0
            overnight_n = 0
        return {
            "path": self.path,
            "last_write_utc": self.last_write_utc,
            "last_error": self.last_error,
            "event_counts": counts,
            "news_rows": news_n,
            "overnight_rows": overnight_n,
        }


POSITIVE_TERMS = {
    "beats estimates": 5.0, "raises guidance": 6.0, "raised guidance": 6.0,
    "record revenue": 4.0, "record profit": 4.0, "approval": 4.0,
    "approved": 4.0, "upgrade": 3.0, "upgraded": 3.0,
    "price target raised": 2.5, "buyback": 3.0, "contract award": 3.0,
    "wins contract": 3.0, "strong demand": 3.0, "margin expansion": 2.5,
}
NEGATIVE_TERMS = {
    "misses estimates": -5.0, "cuts guidance": -6.0, "cut guidance": -6.0,
    "profit warning": -6.0, "downgrade": -3.0, "downgraded": -3.0,
    "price target cut": -2.5, "recall": -4.0, "investigation": -4.0,
    "sec investigation": -5.0, "fraud": -7.0, "bankruptcy": -10.0,
    "default": -7.0, "data breach": -4.0, "cyberattack": -5.0,
    "share offering": -4.0, "dilution": -4.0, "weak demand": -3.0,
}


def headline_score(item: Dict[str, Any]) -> float:
    text = (str(item.get("headline") or "") + " " + str(item.get("summary") or "")).lower()
    score = 0.0
    for term, weight in POSITIVE_TERMS.items():
        if term in text:
            score += weight
    for term, weight in NEGATIVE_TERMS.items():
        if term in text:
            score += weight
    return clamp(score, -12.0, 12.0)


class LiveNews:
    def __init__(self, key: str, secret: str, recorder: Optional[EventRecorder]) -> None:
        self.key = key
        self.secret = secret
        self.recorder = recorder
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.by_symbol: Dict[str, Dict[str, Any]] = {}
        self.health: Dict[str, Any] = {
            "state": "STARTING", "last_event_utc": None, "last_error": "", "articles": 0
        }

    @staticmethod
    def _messages(raw: Any) -> List[Dict[str, Any]]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            return [raw]
        return [x for x in (raw or []) if isinstance(x, dict)]

    def modifier(self, symbol: str) -> float:
        key = symbol_key(symbol)
        with self.lock:
            row = dict(self.by_symbol.get(key) or {})
        if not row:
            return 0.0
        age = max(0.0, time.time() - sf(row.get("epoch"), time.time()))
        # Realtime event boost decays quickly; REST/news history still handles the long tail.
        decay = math.exp(-math.log(2.0) * age / (20.0 * 60.0))
        return clamp(sf(row.get("score")) * decay * 0.65, -7.0, 7.0)

    def _consume(self, msg: Dict[str, Any]) -> None:
        if msg.get("T") != "n":
            return
        score = headline_score(msg)
        syms = [symbol_key(x) for x in (msg.get("symbols") or []) if symbol_key(x)]
        now = time.time()
        with self.lock:
            for sym in syms:
                prior = self.by_symbol.get(sym)
                # Keep the strongest fresh event; opposite newer headlines can replace it.
                if not prior or now - sf(prior.get("epoch")) > 120 or abs(score) >= abs(sf(prior.get("score"))):
                    self.by_symbol[sym] = {
                        "score": score,
                        "epoch": now,
                        "headline": str(msg.get("headline") or "")[:240],
                        "id": msg.get("id"),
                    }
        self.health.update(state="CONNECTED", last_event_utc=iso_now(), last_error="")
        self.health["articles"] = int(self.health.get("articles", 0)) + 1
        if self.recorder:
            self.recorder.news_item(msg, score)

    def run(self) -> None:
        if websocket is None:
            self.health.update(state="DISABLED", last_error="websocket-client missing")
            return
        url = "wss://stream.data.alpaca.markets/v1beta1/news"
        while not self.stop.is_set():
            ws = None
            try:
                self.health["state"] = "CONNECTING"
                ws = websocket.create_connection(url, timeout=15)
                ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                auth = self._messages(ws.recv())
                if not any(
                    (m.get("T") == "success" and str(m.get("msg", "")).lower() == "authenticated")
                    or (m.get("stream") == "authorization" and (m.get("data") or {}).get("status") == "authorized")
                    for m in auth
                ):
                    raise PermissionError("news websocket authentication not acknowledged")
                ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                self.health.update(state="CONNECTED", last_error="")
                ws.settimeout(30)
                while not self.stop.is_set():
                    try:
                        for msg in self._messages(ws.recv()):
                            self._consume(msg)
                    except Exception as exc:
                        # A timeout is a keepalive opportunity; other errors reconnect.
                        if websocket and isinstance(exc, websocket.WebSocketTimeoutException):
                            try:
                                ws.ping()
                                continue
                            except Exception:
                                pass
                        raise
            except Exception as exc:
                self.health.update(state="RECONNECTING", last_error=type(exc).__name__)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
            self.stop.wait(8)

    def start(self) -> None:
        threading.Thread(target=self.run, name="alpaca-news-v49", daemon=True).start()


class CorporateActionGuard:
    SEVERE_TYPES = {
        "reverse_splits", "forward_splits", "unit_splits", "spin_offs",
        "cash_mergers", "stock_mergers", "stock_and_cash_mergers",
        "redemptions", "name_changes", "worthless_removals", "rights_distributions",
        "reorganizations", "partial_calls",
    }
    DIVIDEND_TYPES = {"cash_dividends", "stock_dividends", "capital_gains_distributions"}

    def __init__(self, session: requests.Session, budget: ApiBudget, state_dir: str, recorder: Optional[EventRecorder]) -> None:
        self.s = session
        self.budget = budget
        self.state_dir = state_dir
        self.recorder = recorder
        self.lock = threading.RLock()
        self.blocked: Dict[str, Dict[str, Any]] = {}
        self.health: Dict[str, Any] = {"state": "STARTING", "last_refresh_utc": None, "blocked": 0, "last_error": ""}
        self.stop = threading.Event()

    def _symbols(self) -> List[str]:
        try:
            raw = json.loads(Path(self.state_dir, "control.json").read_text(encoding="utf-8"))
            return [str(x).upper() for x in (raw.get("symbols") or []) if x and "/" not in str(x)]
        except Exception:
            return []

    @staticmethod
    def _event_symbol(row: Dict[str, Any]) -> str:
        return symbol_key(row.get("symbol") or row.get("new_symbol") or row.get("old_symbol"))

    @staticmethod
    def _event_date(row: Dict[str, Any]) -> Optional[datetime]:
        for k in ("ex_date", "process_date", "record_date", "payable_date", "effective_date"):
            v = row.get(k)
            if not v:
                continue
            try:
                return datetime.fromisoformat(str(v)[:10]).replace(tzinfo=UTC)
            except Exception:
                pass
        return None

    def refresh(self) -> None:
        symbols = self._symbols()
        if not symbols:
            self.health.update(state="NO_SYMBOLS", last_error="")
            return
        today = utc_now().date()
        params_base = {
            "start": (today - timedelta(days=1)).isoformat(),
            "end": (today + timedelta(days=3)).isoformat(),
            "data_quality": "all",
        }
        blocked: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(symbols), 45):
            params = dict(params_base)
            params["symbols"] = ",".join(symbols[i:i+45])
            self.budget.before("corporate-actions")
            from corporate_backoff import corporate_get
            data = corporate_get(self.s, "https://data.alpaca.markets/v1/corporate-actions", params)
            if not isinstance(data, dict):
                continue
            for bucket, arr in data.items():
                if not isinstance(arr, list):
                    continue
                for row in arr:
                    if not isinstance(row, dict):
                        continue
                    sym = self._event_symbol(row)
                    if not sym:
                        continue
                    dt = self._event_date(row)
                    days = 99
                    if dt:
                        days = (dt.date() - today).days
                    severe = bucket in self.SEVERE_TYPES
                    dividend_day = bucket in self.DIVIDEND_TYPES and -0 <= days <= 0
                    if severe and -1 <= days <= 3 or dividend_day:
                        blocked[sym] = {
                            "type": bucket,
                            "date": dt.date().isoformat() if dt else None,
                            "days": days,
                            "reason": "corporate_action_guard",
                        }
                        if self.recorder:
                            self.recorder.event("CORPORATE_ACTION_BLOCK", sym, {"bucket": bucket, "row": row})
        with self.lock:
            self.blocked = blocked
        self.health.update(state="OK", last_refresh_utc=iso_now(), blocked=len(blocked), last_error="")

    def is_blocked(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.blocked.get(symbol_key(symbol))
            return dict(row) if row else None

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                self.health.update(state="ERROR", last_error=type(exc).__name__)
            self.stop.wait(30 * 60)

    def start(self) -> None:
        threading.Thread(target=self.run, name="alpaca-corp-action-v49", daemon=True).start()


class SessionAuthority:
    """Parallel Alpaca v3 clock/calendar authority and audit cross-check."""

    def __init__(self, session: requests.Session, budget: ApiBudget, recorder: Optional[EventRecorder], trading_base: str) -> None:
        self.s = session
        self.budget = budget
        self.recorder = recorder
        self.base = trading_base.rstrip("/")
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.health: Dict[str, Any] = {
            "state": "STARTING", "last_clock_utc": None, "last_calendar_utc": None,
            "last_error": "", "clock": None, "calendar_today": None,
        }
        self._last_calendar_fetch = 0.0

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.budget.before(path)
        r = self.s.get(self.base + path, params=params, timeout=(4, 15))
        if r.status_code == 429:
            self.budget.note_429()
        r.raise_for_status()
        return r.json() if r.text.strip() else None

    def refresh(self) -> None:
        clock = self._get("/v3/clock", {"markets": "NYSE,BOATS"})
        today = utc_now().date().isoformat()
        with self.lock:
            self.health["clock"] = clock
            self.health["last_clock_utc"] = iso_now()
            self.health["state"] = "OK"
            self.health["last_error"] = ""
        if self.recorder:
            self.recorder.event("SESSION_CLOCK_V3", "", {"clock": clock})
        if time.time() - self._last_calendar_fetch >= 15 * 60:
            cal = self._get(f"/v3/calendar/NYSE", {"start": today, "end": today, "timezone": "UTC"})
            with self.lock:
                self.health["calendar_today"] = cal
                self.health["last_calendar_utc"] = iso_now()
            self._last_calendar_fetch = time.time()
            if self.recorder:
                self.recorder.event("SESSION_CALENDAR_V3", "", {"calendar": cal})

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                with self.lock:
                    self.health.update(state="ERROR", last_error=type(exc).__name__)
            self.stop.wait(60)

    def start(self) -> None:
        threading.Thread(target=self.run, name="alpaca-session-v49", daemon=True).start()


class OvernightShadow:
    """Collect overnight market evidence; never submits an order."""

    def __init__(self, session: requests.Session, budget: ApiBudget, state_dir: str, recorder: Optional[EventRecorder]) -> None:
        self.s = session
        self.budget = budget
        self.state_dir = state_dir
        self.recorder = recorder
        self.stop = threading.Event()
        self.health: Dict[str, Any] = {"state": "IDLE", "last_refresh_utc": None, "last_error": "", "feed": "overnight", "symbols": 0}

    def _symbols(self) -> List[str]:
        try:
            raw = json.loads(Path(self.state_dir, "control.json").read_text(encoding="utf-8"))
            syms = [str(x).upper() for x in (raw.get("symbols") or []) if x and "/" not in str(x)]
            # Shadow collection intentionally stays small and cheap.
            return syms[:24]
        except Exception:
            return []

    @staticmethod
    def _overnight_window() -> bool:
        now = datetime.now(NY)
        if now.weekday() == 5:  # Saturday
            return False
        # Sunday is eligible only after 20:00 ET. Mon-Fri before 04:00 or after 20:00.
        if now.weekday() == 6:
            return now.hour >= 20
        if now.weekday() == 4 and now.hour >= 20:  # Friday evening closed for next trade day
            return False
        return now.hour < 4 or now.hour >= 20

    def refresh(self) -> None:
        if not self._overnight_window():
            self.health.update(state="IDLE", last_error="")
            return
        symbols = self._symbols()
        if not symbols:
            self.health.update(state="NO_SYMBOLS")
            return
        self.budget.before("overnight-snapshots")
        r = self.s.get(
            "https://data.alpaca.markets/v2/stocks/snapshots",
            params={"symbols": ",".join(symbols), "feed": "overnight"},
            timeout=(4, 20),
        )
        if r.status_code == 429:
            self.budget.note_429()
        if r.status_code in (401, 403):
            self.health.update(state="UNAVAILABLE", last_error=f"HTTP_{r.status_code}")
            return
        r.raise_for_status()
        data = r.json() if r.text.strip() else {}
        snaps = data.get("snapshots") if isinstance(data, dict) and isinstance(data.get("snapshots"), dict) else data
        n = 0
        if isinstance(snaps, dict):
            for sym, payload in snaps.items():
                if isinstance(payload, dict):
                    n += 1
                    if self.recorder:
                        self.recorder.overnight(sym, payload, "overnight")
        self.health.update(state="SHADOW_ONLY", last_refresh_utc=iso_now(), last_error="", symbols=n)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                self.health.update(state="ERROR", last_error=type(exc).__name__)
            self.stop.wait(60)

    def start(self) -> None:
        threading.Thread(target=self.run, name="alpaca-overnight-shadow-v49", daemon=True).start()


class AlpacaEdgeV49:
    def __init__(self, trading_bot_module: Any) -> None:
        self.tb = trading_bot_module
        self.state_dir = str(getattr(trading_bot_module, "STATE_DIR", Path(__file__).resolve().parent))
        self.key = str(os.getenv("ALPACA_KEY") or os.getenv("APCA_API_KEY_ID") or "").strip()
        self.secret = str(os.getenv("ALPACA_SECRET") or os.getenv("APCA_API_SECRET_KEY") or "").strip()
        self.paper = str(os.getenv("ALPACA_PAPER", "true")).lower() in {"1", "true", "yes", "y", "on"}
        self.trading_base = "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"
        self.budget = ApiBudget(int(os.getenv("ALPACA_REST_SOFT_LIMIT", "175")))
        self.recorder: Optional[EventRecorder] = None
        self.news: Optional[LiveNews] = None
        self.corporate: Optional[CorporateActionGuard] = None
        self.session: Optional[SessionAuthority] = None
        self.overnight: Optional[OvernightShadow] = None
        self.http = requests.Session()
        if self.key and self.secret:
            self.http.headers.update({
                "APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret,
                "Content-Type": "application/json",
            })
        self.started = False
        self._patches_installed = False
        self.intelligence_instance = None

    def install_patches(self) -> None:
        if self._patches_installed:
            return
        self._patches_installed = True

        # Preserve Alpaca's new bracket grouping fields in broker history.
        try:
            import broker_history
            broker_history.ORDER_FIELDS.update({"group_id", "group_role"})
            orig_ingest = broker_history.BrokerHistory.ingest

            supervisor = self

            def ingest_with_edge(obj: Any, message: Any) -> Any:
                result = orig_ingest(obj, message)
                try:
                    msgs = message
                    if isinstance(msgs, bytes):
                        msgs = msgs.decode("utf-8", errors="replace")
                    if isinstance(msgs, str):
                        msgs = json.loads(msgs)
                    if isinstance(msgs, dict):
                        msgs = [msgs]
                    for item in msgs or []:
                        if not isinstance(item, dict) or item.get("stream") != "trade_updates":
                            continue
                        data = item.get("data") or {}
                        order = data.get("order") or {}
                        if supervisor.recorder:
                            supervisor.recorder.event(
                                "TRADE_UPDATE",
                                symbol_key(order.get("symbol")),
                                {
                                    "event": data.get("event"),
                                    "timestamp": data.get("timestamp"),
                                    "execution_id": data.get("execution_id"),
                                    "price": data.get("price"),
                                    "qty": data.get("qty"),
                                    "position_qty": data.get("position_qty"),
                                    "order": broker_history.clean_order(order),
                                },
                            )
                except Exception:
                    pass
                return result

            broker_history.BrokerHistory.ingest = ingest_with_edge
        except Exception:
            pass

        # Include the new evidence DB in the existing persistent checkpoint mechanism.
        try:
            import state_storage
            files = tuple(getattr(state_storage, "FILES", ()))
            if "alpaca_edge_v49.sqlite" not in files:
                state_storage.FILES = files + ("alpaca_edge_v49.sqlite",)
        except Exception:
            pass

        # Budget all logical Trading API/data REST calls through a shared soft ceiling.
        try:
            orig_req = self.tb.AlpacaRest._req
            supervisor = self

            def budgeted_req(api_self: Any, method: str, url: str, **kwargs: Any) -> Any:
                # Local circuit refusals consume no API quota and must not delay
                # position protection or inflate the real HTTP 429 counter.
                if time.monotonic() < api_self._cooldowns.get(url, 0):
                    return orig_req(api_self, method, url, **kwargs)
                supervisor.budget.before(url)
                try:
                    return orig_req(api_self, method, url, **kwargs)
                except Exception as exc:
                    status = getattr(exc, "status_code", None)
                    if status == 429 or "429" in str(exc):
                        supervisor.budget.note_429()
                    raise

            self.tb.AlpacaRest._req = budgeted_req

            orig_clock = self.tb.AlpacaRest.clock

            def authoritative_clock(api_self: Any) -> Dict[str, Any]:
                # Legacy v2 clock remains the exact shape consumed by TradingBot.
                # V3 NYSE/BOATS clock+calendar are continuously cross-checked by
                # SessionAuthority and included in the audit database.
                row = orig_clock(api_self)
                if isinstance(row, dict):
                    row = dict(row)
                    row["v49_session_authority"] = True
                return row

            self.tb.AlpacaRest.clock = authoritative_clock
        except Exception:
            pass

        # Realtime news becomes a bounded additive signal; corporate actions are hard entry vetoes.
        try:
            import intelligence_engine
            supervisor = self
            orig_init = intelligence_engine.IntelligenceEngine.__init__
            orig_news_score = intelligence_engine.IntelligenceEngine.news_score
            orig_score_symbol = intelligence_engine.IntelligenceEngine.score_symbol
            orig_record = intelligence_engine.IntelligenceEngine.record_decision

            def init_with_edge(obj: Any, *args: Any, **kwargs: Any) -> None:
                orig_init(obj, *args, **kwargs)
                supervisor.intelligence_instance = obj

            def news_score_with_stream(obj: Any, symbol: str) -> float:
                base = sf(orig_news_score(obj, symbol))
                realtime = supervisor.news.modifier(symbol) if supervisor.news else 0.0
                return clamp(base + realtime, -30.0, 30.0)

            def score_symbol_with_guard(obj: Any, symbol: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
                out = orig_score_symbol(obj, symbol, *args, **kwargs)
                block = supervisor.corporate.is_blocked(symbol) if supervisor.corporate else None
                if block and isinstance(out, dict):
                    out = dict(out)
                    out["entry_allowed"] = False
                    out["risk_veto"] = True
                    out["corporate_action_guard"] = block
                    out["reason"] = str(out.get("reason") or "") + f" corporate_action={block.get('type')}"
                return out

            def record_with_edge(obj: Any, symbol: str, market: str, price: Any, signal: int, action: str, intelligence: Dict[str, Any], *args: Any, **kwargs: Any) -> Any:
                result = orig_record(obj, symbol, market, price, signal, action, intelligence, *args, **kwargs)
                if supervisor.recorder:
                    try:
                        supervisor.recorder.event(
                            "DECISION",
                            symbol_key(symbol),
                            {
                                "market": market,
                                "price": price,
                                "signal": signal,
                                "action": action,
                                "score": intelligence.get("score"),
                                "technical": intelligence.get("technical"),
                                "mtf": intelligence.get("mtf"),
                                "relative": intelligence.get("relative"),
                                "micro": intelligence.get("micro"),
                                "news": intelligence.get("news"),
                                "regime": intelligence.get("regime"),
                                "global_risk": intelligence.get("global_risk"),
                                "probability_up": intelligence.get("probability_up"),
                                "probability_tp_before_sl": intelligence.get("probability_tp_before_sl"),
                                "expected_value_bps": intelligence.get("expected_value_bps"),
                                "take_profit_pct": intelligence.get("take_profit_pct"),
                                "stop_loss_pct": intelligence.get("stop_loss_pct"),
                                "client_order_id": intelligence.get("client_order_id"),
                                "entry_client_order_id": intelligence.get("entry_client_order_id"),
                                "group_id": intelligence.get("group_id"),
                                "group_role": intelligence.get("group_role"),
                                "order_id": intelligence.get("order_id"),
                                "event_type": intelligence.get("event_type"),
                                "exit_reason": intelligence.get("exit_reason"),
                                "context": intelligence.get("context_json"),
                            },
                        )
                    except Exception:
                        pass
                return result

            intelligence_engine.IntelligenceEngine.__init__ = init_with_edge
            intelligence_engine.IntelligenceEngine.news_score = news_score_with_stream
            intelligence_engine.IntelligenceEngine.score_symbol = score_symbol_with_guard
            intelligence_engine.IntelligenceEngine.record_decision = record_with_edge
        except Exception:
            pass

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.recorder = EventRecorder(self.state_dir)
        if not (self.key and self.secret):
            self.recorder.event("EDGE_START", "", {"status": "NO_KEYS"})
            return
        self.news = LiveNews(self.key, self.secret, self.recorder)
        self.corporate = CorporateActionGuard(self.http, self.budget, self.state_dir, self.recorder)
        self.session = SessionAuthority(self.http, self.budget, self.recorder, self.trading_base)
        self.overnight = OvernightShadow(self.http, self.budget, self.state_dir, self.recorder)
        self.news.start()
        self.corporate.start()
        self.session.start()
        self.overnight.start()
        self.recorder.event("EDGE_START", "", {"version": "4.10.0-smart-edge", "paper": self.paper})
        threading.Thread(target=self._status_loop, name="alpaca-edge-status-v49", daemon=True).start()

    def health(self) -> Dict[str, Any]:
        return {
            "version": "4.10.0-smart-edge",
            "started": self.started,
            "api_budget": self.budget.summary(),
            "news_stream": dict(self.news.health) if self.news else {"state": "NOT_STARTED"},
            "corporate_actions": dict(self.corporate.health) if self.corporate else {"state": "NOT_STARTED"},
            "session_authority": dict(self.session.health) if self.session else {"state": "NOT_STARTED"},
            "overnight_shadow": dict(self.overnight.health) if self.overnight else {"state": "NOT_STARTED"},
            "recorder": self.recorder.summary() if self.recorder else {"state": "NOT_STARTED"},
            "group_fields": ["group_id", "group_role"],
            "sip_auto": str(os.getenv("ALPACA_DATA_FEED", "auto")),
        }

    def _status_loop(self) -> None:
        path = Path(self.state_dir) / "status.json"
        while self.started:
            try:
                if path.exists():
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        raw["alpaca_edge_v49"] = self.health()
                        tmp = path.with_suffix(".v49.tmp")
                        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                        os.replace(tmp, path)
            except Exception:
                pass
            time.sleep(5)


def install_v49(trading_bot_module: Any) -> AlpacaEdgeV49:
    edge = AlpacaEdgeV49(trading_bot_module)
    edge.install_patches()
    return edge

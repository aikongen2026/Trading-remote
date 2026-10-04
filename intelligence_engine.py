#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradingBot Intelligence V3.

Purpose:
- Combine technical context with real-time Alpaca news, world-event monitoring (GDELT),
  official macro feeds (Fed/BLS/EIA), cross-market regime and relative strength.
- Produce bounded, explainable per-symbol confidence scores.
- Never submit orders directly. The deterministic trading/risk engine remains the final gate.

No additional API keys are required beyond the existing Alpaca key/secret.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

try:
    import websocket  # websocket-client
except Exception:  # optional; REST polling remains available
    websocket = None

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def parse_ts(v: Any) -> Optional[datetime]:
    """Parse ISO-8601 plus RFC-2822 dates used by RSS feeds."""
    if not v:
        return None
    s = str(v).strip()
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        pass
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except Exception:
            pass
    return None


def pct(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b - a) / a


def normalize_symbol(s: str) -> str:
    return str(s or "").upper().replace("/", "").replace("-", "").strip()


# ---- Market groups / impact graph -------------------------------------------------
TECH = {
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
    "NFLX", "INTC", "QCOM", "TXN", "AMAT", "MU", "IBM", "ORCL", "ADBE", "CRM", "CSCO",
    "INTU", "NOW", "SNOW", "PLTR", "CRWD", "PANW", "ZS", "SNPS", "CDNS",
}
ENERGY = {"XOM", "CVX", "COP", "OXY", "SLB", "HAL", "MPC", "VLO", "XLE", "USO"}
DEFENSE = {"LMT", "NOC", "RTX", "GD", "LHX"}
AIRLINES = {"AAL", "UAL", "DAL", "LUV"}
BANKS = {"JPM", "BAC", "WFC", "C", "GS", "MS", "V", "MA", "AXP"}
CONSUMER = {"KO", "PEP", "COST", "WMT", "HD", "ABNB", "BKNG", "UBER"}
HEALTH = {"UNH", "LLY"}
CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "LINKUSD", "AVAXUSD"}

BENCHMARK_BY_GROUP = {
    "TECH": "QQQ",
    "ENERGY": "XLE",
    "BANKS": "SPY",
    "CONSUMER": "SPY",
    "HEALTH": "SPY",
    "DEFENSE": "SPY",
    "AIRLINES": "SPY",
    "DEFAULT": "SPY",
}


def group_for_symbol(symbol: str) -> str:
    s = normalize_symbol(symbol)
    if s in TECH:
        return "TECH"
    if s in ENERGY:
        return "ENERGY"
    if s in DEFENSE:
        return "DEFENSE"
    if s in AIRLINES:
        return "AIRLINES"
    if s in BANKS:
        return "BANKS"
    if s in CONSUMER:
        return "CONSUMER"
    if s in HEALTH:
        return "HEALTH"
    if s in CRYPTO:
        return "CRYPTO"
    return "DEFAULT"


# Conservative lexical layer. Scores are hypotheses, not direct orders.
POSITIVE_TERMS: Dict[str, float] = {
    "beats estimates": 15, "beat estimates": 15, "above estimates": 10, "tops estimates": 12,
    "raises guidance": 18, "raised guidance": 18, "guidance raised": 18, "raises outlook": 16,
    "record revenue": 12, "record profit": 12, "record sales": 10, "strong demand": 10,
    "accelerating growth": 10, "margin expansion": 8, "free cash flow rises": 8,
    "upgrade": 9, "upgraded": 9, "price target raised": 7, "initiated with buy": 10,
    "buyback": 8, "share repurchase": 8, "dividend increase": 7, "special dividend": 6,
    "approval": 10, "approved": 10, "fda approval": 18, "contract award": 9, "wins contract": 9,
    "backlog growth": 7, "partnership": 6, "strategic partnership": 7, "acquisition": 5,
    "rate cut": 9, "dovish": 8, "inflation cools": 9, "inflation slows": 8,
    "ceasefire": 7, "peace deal": 8, "settlement reached": 5, "debt reduced": 6,
}
NEGATIVE_TERMS: Dict[str, float] = {
    "misses estimates": -15, "missed estimates": -15, "below estimates": -10,
    "cuts guidance": -18, "cut guidance": -18, "guidance cut": -18, "lowers outlook": -16,
    "profit warning": -18, "revenue warning": -15, "margin pressure": -8, "weak demand": -10,
    "downgrade": -9, "downgraded": -9, "price target cut": -7, "initiated with sell": -10,
    "recall": -12, "investigation": -10, "probe": -9, "lawsuit": -8, "fraud": -18,
    "accounting issue": -16, "restatement": -14, "sec investigation": -15,
    "bankruptcy": -28, "default": -20, "delisting": -18, "going concern": -18,
    "data breach": -12, "cyberattack": -14, "cyber attack": -14, "ransomware": -14,
    "share offering": -10, "secondary offering": -9, "dilution": -12, "convertible offering": -8,
    "sanction": -10, "tariff": -8, "embargo": -12, "rate hike": -10, "hawkish": -8,
    "inflation accelerates": -10, "inflation rises": -8, "recession": -12, "layoffs": -6, "layoff": -6,
    "factory shutdown": -10, "production halt": -12, "supply disruption": -6,
}

GEOPOLITICAL_HIGH = {
    "war", "missile", "airstrike", "air strike", "invasion", "attack", "explosion", "drone strike",
    "nuclear", "mobilization", "mobilisation", "blockade", "strait of hormuz", "red sea",
    "iran", "israel", "russia", "ukraine", "taiwan", "china military", "north korea",
}
ENERGY_TERMS = {"opec", "oil", "crude", "pipeline", "refinery", "petroleum", "natural gas", "lng", "hormuz"}
MACRO_TERMS = {"federal reserve", "fed", "fomc", "cpi", "inflation", "payroll", "unemployment", "jobs report", "ppi", "interest rate", "rate decision"}

# GDELT is broad; give modest extra weight to established primary/financial sources.
SOURCE_QUALITY = {
    "reuters.com": 1.35, "apnews.com": 1.25, "bloomberg.com": 1.30,
    "ft.com": 1.25, "wsj.com": 1.25, "cnbc.com": 1.15,
    "bbc.com": 1.10, "economist.com": 1.15, "marketwatch.com": 1.10,
}


@dataclass
class NewsItem:
    uid: str
    ts: str
    source: str
    headline: str
    summary: str = ""
    url: str = ""
    symbols: List[str] = None
    base_score: float = 0.0
    risk: float = 0.0
    category: str = "general"
    importance: float = 1.0

    def __post_init__(self) -> None:
        if self.symbols is None:
            self.symbols = []


class SignalJournal:
    """Lightweight local/persistent SQLite journal for later validation."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._enabled = True
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path, timeout=3) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT,
                    price REAL,
                    technical REAL,
                    mtf REAL,
                    relative REAL,
                    news REAL,
                    regime REAL,
                    global_risk REAL,
                    composite REAL,
                    signal INTEGER,
                    action TEXT,
                    reason TEXT,
                    outcome_5m REAL,
                    outcome_15m REAL,
                    outcome_60m REAL
                    )"""
                )
                db.execute("CREATE INDEX IF NOT EXISTS idx_decisions_sym_ts ON decisions(symbol, ts)")
                cols = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
                for col in ("outcome_5m", "outcome_15m", "outcome_60m"):
                    if col not in cols:
                        db.execute(f"ALTER TABLE decisions ADD COLUMN {col} REAL")
                db.commit()
        except Exception:
            self._enabled = False

    def log(self, row: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            with self._lock, sqlite3.connect(self.path, timeout=3) as db:
                db.execute(
                    """INSERT INTO decisions(ts,symbol,market,price,technical,mtf,relative,news,regime,global_risk,composite,signal,action,reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row.get("ts") or utc_now().isoformat(), row.get("symbol"), row.get("market"),
                        row.get("price"), row.get("technical"), row.get("mtf"), row.get("relative"),
                        row.get("news"), row.get("regime"), row.get("global_risk"), row.get("composite"),
                        row.get("signal"), row.get("action"), row.get("reason"),
                    ),
                )
                db.commit()
        except Exception:
            pass

    def update_outcomes(self, symbol: str, current_price: float) -> None:
        if not self._enabled or current_price <= 0:
            return
        now = utc_now()
        try:
            with self._lock, sqlite3.connect(self.path, timeout=3) as db:
                rows = db.execute(
                    "SELECT id, ts, price, outcome_5m, outcome_15m, outcome_60m FROM decisions WHERE symbol=? AND price>0 ORDER BY id DESC LIMIT 250",
                    (symbol,),
                ).fetchall()
                for rid, ts, entry, o5, o15, o60 in rows:
                    d = parse_ts(ts)
                    if d is None or not entry:
                        continue
                    age = (now - d).total_seconds()
                    updates = []
                    vals = []
                    ret = pct(float(entry), current_price)
                    if o5 is None and 300 <= age < 1200:
                        updates.append("outcome_5m=?"); vals.append(ret)
                    if o15 is None and 900 <= age < 3600:
                        updates.append("outcome_15m=?"); vals.append(ret)
                    if o60 is None and age >= 3600:
                        updates.append("outcome_60m=?"); vals.append(ret)
                    if updates:
                        vals.append(rid)
                        db.execute(f"UPDATE decisions SET {','.join(updates)} WHERE id=?", vals)
                db.commit()
        except Exception:
            pass


class IntelligenceEngine:
    def __init__(self, api: Any, cfg: Any, state_dir: str, stock_symbols: Iterable[str], crypto_symbols: Iterable[str]) -> None:
        self.api = api
        self.cfg = cfg
        self.state_dir = state_dir
        self.stock_symbols = [str(s).upper() for s in stock_symbols]
        self.crypto_symbols = [str(s).upper() for s in crypto_symbols]
        self.lock = threading.RLock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "TradingBot-Intelligence-V3/1.0"})

        self.items: Deque[NewsItem] = deque(maxlen=600)
        self.seen: Dict[str, float] = {}
        self.symbol_news_score: Dict[str, float] = defaultdict(float)
        self.symbol_news_count: Dict[str, int] = defaultdict(int)
        self.global_risk = 0.0
        self.global_sentiment = 0.0
        self.regime_score = 50.0
        self.regime_label = "NEUTRAL"
        self.regime_returns: Dict[str, float] = {}
        self.last_headline = ""
        self.last_source = ""
        self.last_update = ""
        self.source_health: Dict[str, str] = {
            "alpaca_news": "STARTING",
            "stock_stream": "STARTING",
            "crypto_stream": "STARTING",
            "gdelt": "STARTING",
            "fed": "STARTING",
            "bls": "STARTING",
            "eia": "STARTING",
            "sec": "STARTING",
            "market_regime": "STARTING",
            "bls_calendar": "STARTING",
        }
        self.last_run = defaultdict(float)
        self._refresh_lock = threading.Lock()
        self.rest_news_interval = 45.0
        self.gdelt_interval = 15.0 * 60.0  # GDELT 2.0 core data updates every 15 minutes.
        self.official_interval = 4.0 * 60.0
        self.regime_interval = 60.0
        self.sec_interval = 180.0
        self.persist_interval = 30.0
        self.calendar_interval = 6.0 * 3600.0
        self.decay_half_life_min = 45.0
        self.macro_events: List[Tuple[datetime, str]] = []
        self.event_blackout = False
        self.next_macro_event = ""
        self.next_macro_event_ts = ""
        # Live market streams are *confirmation* data. The risk/execution engine
        # remains deterministic and has REST fallback. Basic Alpaca accounts
        # have a limited stock stream symbol count, so subscribe to a liquid core.
        preferred = [
            "SPY","QQQ","AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO",
            "AMD","NFLX","QCOM","AMAT","MU","PLTR","ORCL","CRM","UBER","JPM",
            "BAC","WFC","GS","V","MA","COST","WMT","XOM","CVX","XLE"
        ]
        stock_set = {str(x).upper() for x in self.stock_symbols}
        self.stock_stream_symbols = [x for x in preferred if x in stock_set][:30]
        self.live_quotes: Dict[str, Dict[str, Any]] = {}
        self.live_bars: Dict[str, Dict[str, Any]] = {}
        self.journal = SignalJournal(os.path.join(state_dir, "learning_v3.sqlite"))
        self.sec_company_names: Dict[str, str] = {}
        self.sec_map_loaded_ts = 0.0
        self._news_thread: Optional[threading.Thread] = None
        self._stock_thread: Optional[threading.Thread] = None
        self._crypto_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._load_state()
        self._start_news_stream()
        self._start_market_streams()

    def close(self) -> None:
        self._stop.set()

    # -------------------- persistence --------------------
    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, "intelligence_v3.json")

    def _load_state(self) -> None:
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            self.global_risk = safe_float(data.get("global_risk"), 0.0)
            self.global_sentiment = safe_float(data.get("global_sentiment"), 0.0)
            self.regime_score = safe_float(data.get("regime_score"), 50.0)
            self.regime_label = str(data.get("regime_label") or "NEUTRAL")
            self.last_headline = str(data.get("last_headline") or "")
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            Path(self.state_dir).mkdir(parents=True, exist_ok=True)
            tmp = self.state_path + ".tmp"
            payload = self.summary()
            Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    # -------------------- news classification --------------------
    def _classify_text(self, headline: str, summary: str = "", source: str = "") -> Tuple[float, float, str, float]:
        text = html.unescape((headline + " " + summary).lower())
        score = 0.0
        for term, weight in POSITIVE_TERMS.items():
            if term in text:
                score += weight
        for term, weight in NEGATIVE_TERMS.items():
            if term in text:
                score += weight

        category = "general"
        risk = 0.0
        importance = 1.0
        if any(t in text for t in GEOPOLITICAL_HIGH):
            category = "geopolitics"
            risk = 75.0
            importance = 1.45
            # escalation is broadly risk-off unless later mapped to a beneficiary sector
            score -= 8.0
        if any(t in text for t in ENERGY_TERMS):
            category = "energy" if category == "general" else category + "+energy"
            importance = max(importance, 1.25)
        if any(t in text for t in MACRO_TERMS):
            category = "macro" if category == "general" else category + "+macro"
            risk = max(risk, 45.0)
            importance = max(importance, 1.35)
        if "earnings" in text or "guidance" in text or "quarter" in text:
            category = "earnings" if category == "general" else category
            importance = max(importance, 1.30)
        if any(x in text for x in ("form 8-k", "form 10-q", "form 10-k", "8-k", "10-q", "10-k")):
            category = "filing" if category == "general" else category
            risk = max(risk, 20.0)
            importance = max(importance, 1.35)
        if any(x in text for x in ("breaking", "urgent", "unexpected", "emergency", "halted", "suspended")):
            risk = max(risk, 60.0)
            importance *= 1.15
        src = source.lower()
        if src in {"federal reserve", "fed", "bls", "eia", "sec"}:
            importance *= 1.20
        for domain, mult in SOURCE_QUALITY.items():
            if domain in src:
                importance *= mult
                break
        return clamp(score, -60, 60), clamp(risk, 0, 100), category, clamp(importance, 0.5, 2.0)

    def _uid(self, source: str, headline: str, url: str = "", raw_id: Any = None) -> str:
        if raw_id is not None:
            return f"{source}:{raw_id}"
        raw = f"{source}|{headline}|{url}".encode("utf-8", errors="ignore")
        return hashlib.sha1(raw).hexdigest()

    def ingest(self, source: str, headline: str, summary: str = "", url: str = "", symbols: Optional[List[str]] = None,
               ts: Optional[str] = None, raw_id: Any = None) -> bool:
        headline = re.sub(r"\s+", " ", html.unescape(headline or "")).strip()
        summary = re.sub(r"\s+", " ", html.unescape(summary or "")).strip()
        if not headline:
            return False
        parsed = parse_ts(ts) if ts else None
        if parsed is not None and (utc_now() - parsed).total_seconds() > 48 * 3600:
            return False
        uid = self._uid(source, headline, url, raw_id)
        now = time.time()
        with self.lock:
            if uid in self.seen:
                return False
            self.seen[uid] = now
            # trim dedupe table
            if len(self.seen) > 4000:
                cutoff = now - 24 * 3600
                self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}

            base, risk, category, importance = self._classify_text(headline, summary, source)
            item = NewsItem(
                uid=uid,
                ts=ts or utc_now().isoformat(), source=source, headline=headline, summary=summary,
                url=url, symbols=[normalize_symbol(s) for s in (symbols or []) if s],
                base_score=base, risk=risk, category=category, importance=importance,
            )
            self.items.appendleft(item)
            self.last_headline = headline[:240]
            self.last_source = source
            self.last_update = utc_now().isoformat()
        self._recompute_news_scores()
        return True

    def _event_symbol_adjustment(self, symbol: str, text: str) -> float:
        s = normalize_symbol(symbol)
        g = group_for_symbol(s)
        t = text.lower()
        adj = 0.0
        geo = any(k in t for k in GEOPOLITICAL_HIGH)
        energy = any(k in t for k in ENERGY_TERMS)
        if geo and energy:
            if g == "ENERGY":
                adj += 15.0
            if g == "AIRLINES":
                adj -= 16.0
            if g == "DEFENSE":
                adj += 10.0
            if g in {"TECH", "CONSUMER"}:
                adj -= 5.0
        elif geo:
            if g == "DEFENSE":
                adj += 8.0
            if g == "AIRLINES":
                adj -= 5.0
            if g in {"TECH", "CONSUMER"}:
                adj -= 3.0
            if g == "ENERGY" and any(x in t for x in ("russia", "iran", "sanction", "embargo", "hormuz")):
                adj += 7.0
        if any(x in t for x in ("tariff", "trade war", "export controls", "export restriction")):
            if g in {"TECH", "CONSUMER"}:
                adj -= 7.0
        if any(x in t for x in ("production cut", "output cut", "supply disruption", "pipeline shutdown", "tanker attack")):
            if g == "ENERGY": adj += 10.0
            if g == "AIRLINES": adj -= 6.0
        if any(x in t for x in ("ceasefire", "peace deal", "truce")):
            if g == "ENERGY": adj -= 4.0
            if g == "DEFENSE": adj -= 3.0
            if g == "AIRLINES": adj += 5.0
        if "rate cut" in t or "dovish" in t:
            if g == "TECH":
                adj += 8.0
            if g == "BANKS":
                adj -= 3.0
        if "rate hike" in t or "hawkish" in t or "inflation" in t and "higher" in t:
            if g == "TECH":
                adj -= 8.0
            if g == "BANKS":
                adj += 2.0
        if any(x in t for x in ("chip export", "semiconductor restriction", "ai chip ban", "export controls")):
            if s in TECH:
                adj -= 12.0
        return adj

    def _recompute_news_scores(self) -> None:
        now = utc_now()
        sym_score: Dict[str, float] = defaultdict(float)
        sym_count: Dict[str, int] = defaultdict(int)
        global_weighted = 0.0
        global_w = 0.0
        risk_peak = 0.0
        with self.lock:
            items = list(self.items)
        for item in items:
            ts = parse_ts(item.ts) or now
            age_min = max(0.0, (now - ts).total_seconds() / 60.0)
            decay = math.exp(-math.log(2) * age_min / max(5.0, self.decay_half_life_min))
            w = decay * item.importance
            if w < 0.03:
                continue
            val = item.base_score * w
            global_weighted += val
            global_w += w
            risk_peak = max(risk_peak, item.risk * min(1.0, w))
            text = f"{item.headline} {item.summary}"
            targets = item.symbols
            if targets:
                for sym in targets:
                    sym_score[sym] += val + self._event_symbol_adjustment(sym, text) * w
                    sym_count[sym] += 1
            else:
                # World events are mapped to sector baskets rather than blindly to every ticker.
                for sym in self.stock_symbols + self.crypto_symbols:
                    a = self._event_symbol_adjustment(sym, text)
                    if a:
                        ns = normalize_symbol(sym)
                        sym_score[ns] += a * w
                        sym_count[ns] += 1
        with self.lock:
            self.symbol_news_score = defaultdict(float, {k: clamp(v, -40, 40) for k, v in sym_score.items()})
            self.symbol_news_count = defaultdict(int, sym_count)
            self.global_sentiment = clamp(global_weighted / max(1.0, global_w), -30, 30)
            self.global_risk = clamp(max(risk_peak, abs(self.global_sentiment) * 1.2), 0, 100)

    # -------------------- Alpaca news stream + REST fallback --------------------
    def _start_news_stream(self) -> None:
        if websocket is None:
            self.source_health["alpaca_news"] = "REST_FALLBACK"
            return
        if self._news_thread and self._news_thread.is_alive():
            return
        self._news_thread = threading.Thread(target=self._news_stream_loop, name="alpaca-news-v3", daemon=True)
        self._news_thread.start()

    def _news_stream_loop(self) -> None:
        url = "wss://stream.data.alpaca.markets/v1beta1/news"
        while not self._stop.is_set():
            ws = None
            try:
                headers = [
                    f"APCA-API-KEY-ID: {self.cfg.key}",
                    f"APCA-API-SECRET-KEY: {self.cfg.secret}",
                ]
                ws = websocket.create_connection(url, header=headers, timeout=20)
                self.source_health["alpaca_news"] = "CONNECTED"
                # consume connected/authenticated messages, then subscribe all news
                for _ in range(3):
                    try:
                        raw = ws.recv()
                        if raw:
                            msgs = json.loads(raw)
                            if isinstance(msgs, list) and any(m.get("msg") == "authenticated" for m in msgs if isinstance(m, dict)):
                                break
                    except Exception:
                        break
                ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                ws.settimeout(30)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception:
                        # keepalive timeout -> reconnect cleanly
                        break
                    if not raw:
                        continue
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict):
                        msgs = [msgs]
                    for m in msgs if isinstance(msgs, list) else []:
                        if not isinstance(m, dict) or m.get("T") != "n":
                            continue
                        self.ingest(
                            "alpaca", str(m.get("headline") or ""), str(m.get("summary") or ""),
                            str(m.get("url") or ""), list(m.get("symbols") or []),
                            str(m.get("created_at") or m.get("updated_at") or utc_now().isoformat()), m.get("id"),
                        )
                self.source_health["alpaca_news"] = "RECONNECTING"
            except Exception as exc:
                self.source_health["alpaca_news"] = f"STREAM_ERR:{type(exc).__name__}"
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            self._stop.wait(10)

    def _poll_alpaca_news(self) -> None:
        try:
            data = self.api._req(
                "GET", f"{self.cfg.data_base}/v1beta1/news",
                params={"sort": "desc", "limit": 50, "include_content": "false"},
                timeout=(4, 15),
            ) or {}
            arr = data.get("news") or []
            n = 0
            for m in arr if isinstance(arr, list) else []:
                if not isinstance(m, dict):
                    continue
                if self.ingest(
                    "alpaca", str(m.get("headline") or ""), str(m.get("summary") or ""),
                    str(m.get("url") or ""), list(m.get("symbols") or []),
                    str(m.get("created_at") or m.get("updated_at") or utc_now().isoformat()), m.get("id"),
                ):
                    n += 1
            if self.source_health.get("alpaca_news", "").startswith("STREAM_ERR") or websocket is None:
                self.source_health["alpaca_news"] = f"REST_OK:{n}"
        except Exception as exc:
            if not str(self.source_health.get("alpaca_news", "")).startswith("CONNECTED"):
                self.source_health["alpaca_news"] = f"ERR:{type(exc).__name__}"

    # -------------------- live market confirmation streams --------------------
    def _start_market_streams(self) -> None:
        if websocket is None:
            self.source_health["stock_stream"] = "REST_ONLY"
            self.source_health["crypto_stream"] = "REST_ONLY"
            return
        if self.stock_stream_symbols:
            self._stock_thread = threading.Thread(target=self._stock_stream_loop, name="stock-stream-v3", daemon=True)
            self._stock_thread.start()
        if self.crypto_symbols:
            self._crypto_thread = threading.Thread(target=self._crypto_stream_loop, name="crypto-stream-v3", daemon=True)
            self._crypto_thread.start()

    def _consume_stream_message(self, m: Dict[str, Any], market: str) -> None:
        typ = m.get("T")
        sym = str(m.get("S") or "").upper()
        if not sym:
            return
        key = normalize_symbol(sym)
        if typ == "q":
            with self.lock:
                self.live_quotes[key] = {
                    "bid": safe_float(m.get("bp")), "ask": safe_float(m.get("ap")),
                    "bid_size": safe_float(m.get("bs")), "ask_size": safe_float(m.get("as")),
                    "ts": str(m.get("t") or utc_now().isoformat()), "market": market,
                }
        elif typ in {"b", "u"}:  # minute bar / updated bar
            with self.lock:
                self.live_bars[key] = {
                    "open": safe_float(m.get("o")), "high": safe_float(m.get("h")),
                    "low": safe_float(m.get("l")), "close": safe_float(m.get("c")),
                    "volume": safe_float(m.get("v")), "ts": str(m.get("t") or utc_now().isoformat()),
                    "market": market,
                }

    def _stock_stream_loop(self) -> None:
        feed = str(getattr(self.cfg, "stock_feed", "iex") or "iex").lower()
        if feed not in {"iex", "sip", "delayed_sip"}:
            feed = "iex"
        url = f"wss://stream.data.alpaca.markets/v2/{feed}"
        while not self._stop.is_set():
            ws = None
            try:
                headers = [f"APCA-API-KEY-ID: {self.cfg.key}", f"APCA-API-SECRET-KEY: {self.cfg.secret}"]
                ws = websocket.create_connection(url, header=headers, timeout=20)
                # Consume connected/authenticated messages from handshake auth.
                for _ in range(3):
                    try:
                        raw = ws.recv()
                        msgs = json.loads(raw) if raw else []
                        if isinstance(msgs, list) and any(isinstance(x, dict) and x.get("msg") == "authenticated" for x in msgs):
                            break
                    except Exception:
                        break
                ws.send(json.dumps({"action":"subscribe","quotes":self.stock_stream_symbols,"bars":self.stock_stream_symbols}))
                ws.settimeout(30)
                self.source_health["stock_stream"] = f"CONNECTED:{len(self.stock_stream_symbols)}"
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception:
                        break
                    if not raw:
                        continue
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict): msgs = [msgs]
                    for m in msgs if isinstance(msgs, list) else []:
                        if isinstance(m, dict): self._consume_stream_message(m, "STOCK")
                self.source_health["stock_stream"] = "RECONNECTING"
            except Exception as exc:
                self.source_health["stock_stream"] = f"ERR:{type(exc).__name__}"
            finally:
                try:
                    if ws: ws.close()
                except Exception: pass
            self._stop.wait(10)

    def _crypto_stream_loop(self) -> None:
        url = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
        symbols = list(self.crypto_symbols)
        while not self._stop.is_set():
            ws = None
            try:
                headers = [f"APCA-API-KEY-ID: {self.cfg.key}", f"APCA-API-SECRET-KEY: {self.cfg.secret}"]
                ws = websocket.create_connection(url, header=headers, timeout=20)
                for _ in range(3):
                    try:
                        raw = ws.recv()
                        msgs = json.loads(raw) if raw else []
                        if isinstance(msgs, list) and any(isinstance(x, dict) and x.get("msg") == "authenticated" for x in msgs):
                            break
                    except Exception:
                        break
                ws.send(json.dumps({"action":"subscribe","quotes":symbols,"bars":symbols}))
                ws.settimeout(30)
                self.source_health["crypto_stream"] = f"CONNECTED:{len(symbols)}"
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception:
                        break
                    if not raw: continue
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict): msgs = [msgs]
                    for m in msgs if isinstance(msgs, list) else []:
                        if isinstance(m, dict): self._consume_stream_message(m, "CRYPTO")
                self.source_health["crypto_stream"] = "RECONNECTING"
            except Exception as exc:
                self.source_health["crypto_stream"] = f"ERR:{type(exc).__name__}"
            finally:
                try:
                    if ws: ws.close()
                except Exception: pass
            self._stop.wait(10)

    def microstructure_score(self, symbol: str, market: str) -> Tuple[float, Dict[str, Any]]:
        """Small confirmation score from live bid/ask imbalance + current bar.

        Bounded intentionally: market microstructure may confirm a trade but can
        never override risk controls or a strongly negative macro/news picture.
        """
        key = normalize_symbol(symbol)
        with self.lock:
            q = dict(self.live_quotes.get(key) or {})
            b = dict(self.live_bars.get(key) or {})
        if not q:
            return 0.0, {"live": False}
        ts = parse_ts(q.get("ts"))
        age = (utc_now() - ts).total_seconds() if ts else 9999.0
        if age > 20:
            return 0.0, {"live": False, "age_sec": round(age, 1)}
        bid, ask = safe_float(q.get("bid")), safe_float(q.get("ask"))
        if bid <= 0 or ask <= 0 or ask < bid:
            return 0.0, {"live": False}
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / max(mid, 1e-12) * 10000.0
        bs, ass = safe_float(q.get("bid_size")), safe_float(q.get("ask_size"))
        imbalance = (bs - ass) / max(bs + ass, 1e-12) if (bs + ass) > 0 else 0.0
        score = clamp(imbalance * 5.0, -5.0, 5.0)
        close = safe_float(b.get("close"))
        opn = safe_float(b.get("open"))
        if close > 0 and opn > 0:
            score += clamp(pct(opn, close) * 800.0, -3.0, 3.0)
        # Penalize bad execution conditions instead of interpreting them as alpha.
        max_spread = 12.0 if market.upper() == "STOCK" else 30.0
        if spread_bps > max_spread:
            score -= clamp((spread_bps - max_spread) / 8.0, 0.0, 5.0)
        return clamp(score, -7.0, 7.0), {
            "live": True, "age_sec": round(age, 2), "spread_bps": round(spread_bps, 2),
            "imbalance": round(imbalance, 3),
        }

    # -------------------- GDELT world radar --------------------
    def _poll_gdelt(self) -> None:
        query = '(war OR missile OR airstrike OR invasion OR sanctions OR OPEC OR oil OR pipeline OR "Federal Reserve" OR inflation OR tariff OR Taiwan OR "Red Sea" OR "Strait of Hormuz")'
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 75,
            "timespan": "15min",
            "sort": "datedesc",
        }
        try:
            r = self.session.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=(5, 20))
            r.raise_for_status()
            data = r.json()
            arr = data.get("articles") or []
            n = 0
            for a in arr if isinstance(arr, list) else []:
                if not isinstance(a, dict):
                    continue
                title = str(a.get("title") or "")
                domain = str(a.get("domain") or "").lower().strip()
                src = f"gdelt:{domain}" if domain else "gdelt"
                context = " ".join(str(a.get(k) or "") for k in ("sourcecountry", "language")).strip()
                if self.ingest(
                    src, title, context, str(a.get("url") or ""), [],
                    str(a.get("seendate") or a.get("date") or utc_now().isoformat()), None,
                ):
                    n += 1
            self.source_health["gdelt"] = f"OK:{n}"
        except Exception as exc:
            self.source_health["gdelt"] = f"ERR:{type(exc).__name__}"

    # -------------------- official sources --------------------
    def _poll_rss(self, source: str, url: str) -> int:
        r = self.session.get(url, timeout=(5, 18))
        r.raise_for_status()
        root = ET.fromstring(r.content)
        count = 0
        # RSS and Atom compatible enough for title/link/date extraction
        for node in list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry")):
            title = node.findtext("title") or node.findtext("{http://www.w3.org/2005/Atom}title") or ""
            desc = node.findtext("description") or node.findtext("summary") or node.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            link = node.findtext("link") or ""
            if not link:
                ln = node.find("{http://www.w3.org/2005/Atom}link")
                if ln is not None:
                    link = ln.attrib.get("href", "")
            dt = node.findtext("pubDate") or node.findtext("updated") or node.findtext("{http://www.w3.org/2005/Atom}updated") or utc_now().isoformat()
            if self.ingest(source, re.sub("<[^>]+>", " ", title), re.sub("<[^>]+>", " ", desc), link, [], dt):
                count += 1
        return count

    def _poll_official(self) -> None:
        # Federal Reserve monetary-policy RSS; BLS latest releases; EIA What's New RSS.
        # EIA What's New is preferable to editorial articles because it announces data products
        # (including energy releases) as they are published.
        sources = [
            ("fed", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
            ("bls", "https://www.bls.gov/feed/bls_latest.rss"),
            ("eia", "https://www.eia.gov/about/new/WNtest3.php"),
        ]
        for key, url in sources:
            try:
                n = self._poll_rss(key, url)
                self.source_health[key] = f"OK:{n}"
            except Exception as exc:
                self.source_health[key] = f"ERR:{type(exc).__name__}"

    # -------------------- scheduled macro-event radar --------------------
    def _poll_bls_calendar(self) -> None:
        """Read the official BLS calendar. No API key required.

        We only use the schedule as a *risk veto* around releases. The bot never
        guesses the number before publication.
        """
        url = "https://www.bls.gov/schedule/news_release/bls.ics"
        try:
            r = self.session.get(url, timeout=(5, 20))
            r.raise_for_status()
            text = r.text.replace("\r\n ", "").replace("\n ", "")
            events: List[Tuple[datetime, str]] = []
            current: Dict[str, str] = {}
            for raw in text.splitlines():
                line = raw.strip()
                if line == "BEGIN:VEVENT":
                    current = {}
                elif line == "END:VEVENT":
                    dt_raw = current.get("dtstart", "")
                    summary = current.get("summary", "BLS release")
                    m = re.search(r"(\d{8})T(\d{6})", dt_raw)
                    if m:
                        try:
                            naive = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                            if "TZID=America/New_York" in dt_raw or "TZID=US/Eastern" in dt_raw:
                                d = naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
                            elif dt_raw.endswith("Z"):
                                d = naive.replace(tzinfo=UTC)
                            else:
                                d = naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
                            if d >= utc_now() - timedelta(days=1):
                                events.append((d, summary))
                        except Exception:
                            pass
                    current = {}
                elif line.startswith("DTSTART"):
                    current["dtstart"] = line
                elif line.startswith("SUMMARY:"):
                    current["summary"] = line.split(":", 1)[1].replace("\\,", ",")
            events.sort(key=lambda x: x[0])
            self.macro_events = events[:80]
            self.source_health["bls_calendar"] = f"OK:{len(self.macro_events)}"
        except Exception as exc:
            self.source_health["bls_calendar"] = f"ERR:{type(exc).__name__}"

    def _update_event_blackout(self) -> None:
        """Block new entries near major scheduled BLS releases.

        Window: 12 minutes before through 3 minutes after. Existing protective
        exits keep working; only new entries are vetoed.
        """
        now = utc_now()
        self.event_blackout = False
        self.next_macro_event = ""
        self.next_macro_event_ts = ""
        important = ("consumer price", "employment situation", "producer price", "job openings", "employment cost")
        for d, name in self.macro_events:
            low = name.lower()
            if not any(k in low for k in important):
                continue
            if d >= now - timedelta(minutes=3):
                self.next_macro_event = name
                self.next_macro_event_ts = d.isoformat()
                if d - timedelta(minutes=12) <= now <= d + timedelta(minutes=3):
                    self.event_blackout = True
                    self.global_risk = max(self.global_risk, 72.0)
                break

    # -------------------- SEC current filings --------------------
    def _load_sec_company_map(self) -> None:
        if self.sec_company_names and (time.time() - self.sec_map_loaded_ts) < 12 * 3600:
            return
        headers = {"User-Agent": os.getenv("SEC_USER_AGENT", "TradingBot-Intelligence-V3 github.com/aikongen2026/Trading-remote")}
        r = self.session.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=(5, 20))
        r.raise_for_status()
        data = r.json()
        wanted = {normalize_symbol(x) for x in self.stock_symbols}
        out: Dict[str, str] = {}
        for row in data.values() if isinstance(data, dict) else []:
            if not isinstance(row, dict):
                continue
            t = normalize_symbol(row.get("ticker", ""))
            if t in wanted:
                out[t] = str(row.get("title") or "").strip().lower()
        self.sec_company_names = out
        self.sec_map_loaded_ts = time.time()

    def _poll_sec(self) -> None:
        try:
            self._load_sec_company_map()
            headers = {"User-Agent": os.getenv("SEC_USER_AGENT", "TradingBot-Intelligence-V3 github.com/aikongen2026/Trading-remote")}
            count = 0
            for form in ("8-k", "10-q", "10-k"):
                url = "https://www.sec.gov/cgi-bin/browse-edgar"
                params = {"action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include", "start": 0, "count": 100, "output": "atom"}
                r = self.session.get(url, params=params, headers=headers, timeout=(5, 20))
                r.raise_for_status()
                root = ET.fromstring(r.content)
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for e in root.findall(".//a:entry", ns):
                    title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
                    summary = (e.findtext("a:summary", default="", namespaces=ns) or "").strip()
                    updated = e.findtext("a:updated", default=utc_now().isoformat(), namespaces=ns)
                    link = ""
                    ln = e.find("a:link", ns)
                    if ln is not None:
                        link = ln.attrib.get("href", "")
                    low = title.lower()
                    syms: List[str] = []
                    for ticker, company in self.sec_company_names.items():
                        if company and company[:20] in low:
                            syms.append(ticker)
                    if not syms:
                        continue
                    if self.ingest("sec", f"Form {form.upper()} - {title}", summary, link, syms, updated):
                        count += 1
            self.source_health["sec"] = f"OK:{count}"
        except Exception as exc:
            self.source_health["sec"] = f"ERR:{type(exc).__name__}"

    # -------------------- cross-market regime --------------------
    def _market_regime(self) -> None:
        proxies = ["SPY", "QQQ", "IWM", "XLE", "GLD", "TLT", "HYG", "USO", "UUP", "VIXY"]
        try:
            end = utc_now()
            start = end - timedelta(days=3)
            bars = self.api.bars(proxies, "5Min", 50, start.isoformat(), end.isoformat(), self.cfg.stock_feed)
            ret: Dict[str, float] = {}
            for sym in proxies:
                arr = bars.get(sym) or []
                vals = [safe_float(x.get("c")) for x in arr if safe_float(x.get("c")) > 0]
                if len(vals) >= 2:
                    base = vals[-min(13, len(vals))]
                    ret[sym] = pct(base, vals[-1])
            score = 50.0
            score += clamp(ret.get("SPY", 0) * 1600, -10, 10)
            score += clamp(ret.get("QQQ", 0) * 1200, -8, 8)
            score += clamp(ret.get("IWM", 0) * 900, -5, 5)
            score += clamp(ret.get("HYG", 0) * 1500, -5, 5)
            score -= clamp(ret.get("VIXY", 0) * 500, -6, 6)
            score -= clamp(ret.get("UUP", 0) * 350, -3, 3)
            # gold + bonds rising while equities fall often coincides with defensive/risk-off flow
            if ret.get("SPY", 0) < 0 and ret.get("GLD", 0) > 0:
                score -= 4
            if ret.get("SPY", 0) < 0 and ret.get("TLT", 0) > 0:
                score -= 3
            # abrupt oil move increases event risk; direction handled per sector separately
            oil_move = max(abs(ret.get("USO", 0)), abs(ret.get("XLE", 0)))
            if oil_move > 0.012:
                self.global_risk = max(self.global_risk, clamp(oil_move * 2600, 20, 75))
            self.regime_score = clamp(score, 0, 100)
            if self.regime_score >= 62:
                self.regime_label = "RISK_ON"
            elif self.regime_score <= 38:
                self.regime_label = "RISK_OFF"
            else:
                self.regime_label = "NEUTRAL"
            self.regime_returns = ret
            self.source_health["market_regime"] = "OK"
        except Exception as exc:
            self.source_health["market_regime"] = f"ERR:{type(exc).__name__}"

    # -------------------- scheduling / scoring --------------------
    def refresh_if_due(self) -> None:
        """Kick due network refreshes in a background worker; never block trading decisions."""
        now = time.time()
        due = (
            now - self.last_run["alpaca_rest"] >= self.rest_news_interval
            or now - self.last_run["gdelt"] >= self.gdelt_interval
            or now - self.last_run["official"] >= self.official_interval
            or now - self.last_run["calendar"] >= self.calendar_interval
            or now - self.last_run["sec"] >= self.sec_interval
            or now - self.last_run["regime"] >= self.regime_interval
            or now - self.last_run["persist"] >= self.persist_interval
        )
        if not due or not self._refresh_lock.acquire(blocking=False):
            return
        def worker() -> None:
            try:
                self._refresh_due_blocking()
            finally:
                self._refresh_lock.release()
        threading.Thread(target=worker, name="intel-refresh-v3", daemon=True).start()

    def _refresh_due_blocking(self) -> None:
        now = time.time()
        if now - self.last_run["alpaca_rest"] >= self.rest_news_interval:
            self.last_run["alpaca_rest"] = now
            self._poll_alpaca_news()
        if now - self.last_run["gdelt"] >= self.gdelt_interval:
            self.last_run["gdelt"] = now
            self._poll_gdelt()
        if now - self.last_run["official"] >= self.official_interval:
            self.last_run["official"] = now
            self._poll_official()
        if now - self.last_run["calendar"] >= self.calendar_interval:
            self.last_run["calendar"] = now
            self._poll_bls_calendar()
        self._update_event_blackout()
        if now - self.last_run["sec"] >= self.sec_interval:
            self.last_run["sec"] = now
            self._poll_sec()
        if now - self.last_run["regime"] >= self.regime_interval:
            self.last_run["regime"] = now
            self._market_regime()
        self._recompute_news_scores()
        if now - self.last_run["persist"] >= self.persist_interval:
            self.last_run["persist"] = now
            self._persist()

    def news_score(self, symbol: str) -> float:
        return clamp(self.symbol_news_score.get(normalize_symbol(symbol), 0.0), -40, 40)

    def relative_strength(self, symbol: str, closes: List[float], lookback: int = 15) -> float:
        if len(closes) < 2:
            return 0.0
        n = min(max(2, lookback), len(closes))
        sym_ret = pct(closes[-n], closes[-1])
        grp = group_for_symbol(symbol)
        benchmark = BENCHMARK_BY_GROUP.get(grp, "SPY")
        bench_ret = self.regime_returns.get(benchmark, self.regime_returns.get("SPY", 0.0))
        diff = sym_ret - bench_ret
        return clamp(diff * 1600.0, -12, 12)

    def multi_timeframe_score(self, closes: List[float]) -> float:
        if len(closes) < 8:
            return 0.0
        score = 0.0
        for n, weight in ((5, 3.0), (15, 5.0), (60, 7.0)):
            if len(closes) >= n + 1:
                r = pct(closes[-n - 1], closes[-1])
                score += clamp(r * (450 / max(1, math.sqrt(n))), -weight, weight)
        # trend alignment between short/medium averages
        if len(closes) >= 30:
            ma8 = sum(closes[-8:]) / 8
            ma20 = sum(closes[-20:]) / 20
            ma30 = sum(closes[-30:]) / 30
            if ma8 > ma20 > ma30:
                score += 4
            elif ma8 < ma20 < ma30:
                score -= 4
        return clamp(score, -15, 15)

    def technical_strength(self, signal: int, debug: Dict[str, Any]) -> float:
        out = 0.0
        if signal > 0:
            out += 12
        elif signal < 0:
            out -= 12
        mom = safe_float(debug.get("mom"), 0.0)
        slope = safe_float(debug.get("slope_pct"), safe_float(debug.get("slope_fast_sma"), 0.0))
        last = safe_float(debug.get("last_move_pct"), 0.0)
        out += clamp(mom * 1800, -8, 8)
        out += clamp(slope * 3200, -6, 6)
        out += clamp(last * 1800, -5, 5)
        return clamp(out, -25, 25)

    def score_symbol(self, symbol: str, market: str, closes: List[float], signal: int,
                     debug: Dict[str, Any], profile: str = "active") -> Dict[str, Any]:
        # refresh_if_due() is called once per engine loop, not once per symbol.
        # This keeps 60+ symbol scans cheap while retaining the same current state.
        tech = self.technical_strength(signal, debug)
        mtf = self.multi_timeframe_score(closes)
        rel = self.relative_strength(symbol, closes) if market.upper() == "STOCK" else 0.0
        micro, micro_debug = self.microstructure_score(symbol, market)
        news = self.news_score(symbol)
        regime_component = (self.regime_score - 50.0) * 0.20 if market.upper() == "STOCK" else 0.0
        if market.upper() == "CRYPTO":
            # crypto is more sensitive to risk appetite, but cap the effect.
            regime_component = (self.regime_score - 50.0) * 0.10
        risk_penalty = max(0.0, self.global_risk - 55.0) * 0.22
        g = group_for_symbol(symbol)
        if g in {"ENERGY", "DEFENSE"} and self.global_risk > 65:
            risk_penalty *= 0.35
        composite = 50.0 + tech + mtf + rel + micro + news * 0.65 + regime_component - risk_penalty
        composite = clamp(composite, 0, 100)
        entry_threshold = 60.0 if profile == "active" else 67.0
        exit_threshold = 38.0 if profile == "active" else 42.0
        hard_risk_veto = bool(self.global_risk >= 88.0 and g not in {"ENERGY", "DEFENSE"})
        confidence = abs(composite - 50.0) * 2.0
        reason = (
            f"score={composite:.1f} tech={tech:+.1f} mtf={mtf:+.1f} rel={rel:+.1f} micro={micro:+.1f} "
            f"news={news:+.1f} regime={self.regime_score:.1f} risk={self.global_risk:.1f} "
            f"event_blackout={self.event_blackout} risk_veto={hard_risk_veto}"
        )
        return {
            "score": round(composite, 1),
            "confidence": round(clamp(confidence, 0, 100), 1),
            "entry_allowed": bool(signal > 0 and composite >= entry_threshold and not self.event_blackout and not hard_risk_veto),
            "exit_bias": bool(composite <= exit_threshold),
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
            "event_blackout": bool(self.event_blackout),
            "risk_veto": hard_risk_veto,
            "technical": round(tech, 2),
            "mtf": round(mtf, 2),
            "relative": round(rel, 2),
            "micro": round(micro, 2),
            "micro_debug": micro_debug,
            "news": round(news, 2),
            "regime": round(self.regime_score, 2),
            "global_risk": round(self.global_risk, 2),
            "reason": reason,
        }

    def update_learning_outcomes(self, symbol: str, price: Any) -> None:
        self.journal.update_outcomes(symbol, safe_float(price, 0.0))

    def record_decision(self, symbol: str, market: str, price: Any, signal: int, action: str, intelligence: Dict[str, Any]) -> None:
        # Reduce journal spam: only actions/signals worth analysing are stored.
        if not action or action in {"NONE", "HOLD"} and signal == 0:
            return
        self.journal.log({
            "ts": utc_now().isoformat(), "symbol": symbol, "market": market,
            "price": safe_float(price, 0.0), "technical": intelligence.get("technical"),
            "mtf": intelligence.get("mtf"), "relative": intelligence.get("relative"),
            "news": intelligence.get("news"), "regime": intelligence.get("regime"),
            "global_risk": intelligence.get("global_risk"), "composite": intelligence.get("score"),
            "signal": signal, "action": action, "reason": intelligence.get("reason"),
        })

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            latest = [asdict(x) for x in list(self.items)[:8]]
            return {
                "enabled": True,
                "version": "3.0",
                "regime_score": round(self.regime_score, 1),
                "regime_label": self.regime_label,
                "global_risk": round(self.global_risk, 1),
                "global_sentiment": round(self.global_sentiment, 1),
                "last_headline": self.last_headline,
                "last_source": self.last_source,
                "last_update": self.last_update,
                "source_health": dict(self.source_health),
                "event_blackout": bool(self.event_blackout),
                "next_macro_event": self.next_macro_event,
                "next_macro_event_ts": self.next_macro_event_ts,
                "latest_news": latest,
            }

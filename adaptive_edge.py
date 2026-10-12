#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adaptive Edge V4.5 helpers.

The live champion remains deterministic.  Challengers are shadow-only and can
never submit orders.  This module provides:
- adaptive strategy routing (trend / mean-reversion / event / scalp)
- dynamic daily/intraday universe ranking
- sector confirmation for long entries
- champion/challenger shadow journaling and 15-minute outcome measurement
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

UTC = timezone.utc


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def sf(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def norm(s: str) -> str:
    return str(s or "").upper().replace("/", "").replace("-", "").strip()


def pct(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b - a) / a


class AdaptiveRouter:
    """Route each symbol to the strategy style that best fits current context.

    The router only chooses an existing deterministic signal family.  It does
    not place orders and it does not bypass the Intelligence/EV/risk gates.
    """

    def __init__(self, intelligence: Any) -> None:
        self.intelligence = intelligence

    def route(self, symbol: str, base_strategy: str = "scalp") -> Tuple[str, Dict[str, Any]]:
        base = str(base_strategy or "scalp").lower()
        if base not in {"scalp", "trend", "cross", "hype"}:
            base = "scalp"
        i = self.intelligence
        risk = sf(getattr(i, "global_risk", 0.0))
        regime = sf(getattr(i, "regime_score", 50.0), 50.0)
        news = sf(i.news_score(symbol) if hasattr(i, "news_score") else 0.0)
        factor = sf(getattr(i, "daily_factor_score", {}).get(norm(symbol), 0.0))
        try:
            from intelligence_engine import group_for_symbol
            group = group_for_symbol(symbol)
        except Exception:
            group = "DEFAULT"
        gm = sf(getattr(i, "group_momentum", {}).get(group, 0.0))

        mode = base
        reason = "base"
        # Fresh symbol-specific event information gets its own momentum/event route.
        if abs(news) >= 8.0 and risk < 88.0:
            mode, reason = "event", "symbol_news"
        # Neutral markets are where a bounded mean-reversion setup is most useful.
        elif 43.0 <= regime <= 57.0 and risk < 80.0:
            mode, reason = "meanrev", "neutral_regime"
        # Strong positive market/sector state routes long candidates to trend logic.
        elif regime >= 58.0 and (factor > 1.5 or gm > 0.004) and risk < 85.0:
            mode, reason = "trend", "risk_on_trend"
        elif regime <= 40.0:
            mode, reason = "scalp", "risk_off_defensive"

        return mode, {
            "mode": mode, "reason": reason, "regime": round(regime, 2),
            "global_risk": round(risk, 2), "news": round(news, 2),
            "factor": round(factor, 2), "group_momentum": round(gm, 5),
        }


def dynamic_universe(
    symbols: Iterable[str],
    closes_by_symbol: Dict[str, List[float]],
    quotes_by_symbol: Dict[str, Dict[str, Any]],
    intelligence: Any,
    *,
    top_n: int = 18,
    positions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[str], Dict[str, float]]:
    """Rank symbols by *opportunity*, not predicted direction.

    This reduces needless scanning/trading in sleepy names while always keeping
    open positions in the active set. Direction is still decided later by the
    strategy + Intelligence + EV gates.
    """
    now = datetime.now(UTC)
    scores: Dict[str, float] = {}
    for raw in symbols:
        sym = str(raw).upper()
        vals = [sf(x) for x in (closes_by_symbol.get(sym) or []) if sf(x) > 0]
        ret15 = pct(vals[-min(16, len(vals))], vals[-1]) if len(vals) >= 4 else 0.0
        # realized 1m variability; useful for finding names that actually move
        rs = [pct(a, b) for a, b in zip(vals[-21:-1], vals[-20:]) if a > 0] if len(vals) >= 6 else []
        rv = math.sqrt(sum(r*r for r in rs) / max(1, len(rs))) if rs else 0.0
        news = abs(sf(intelligence.news_score(sym) if hasattr(intelligence, "news_score") else 0.0))
        factor = sf(getattr(intelligence, "daily_factor_score", {}).get(norm(sym), 0.0))
        try:
            from intelligence_engine import group_for_symbol
            group = group_for_symbol(sym)
        except Exception:
            group = "DEFAULT"
        gm = abs(sf(getattr(intelligence, "group_momentum", {}).get(group, 0.0)))

        spread_bps = 50.0
        age = 999.0
        q = quotes_by_symbol.get(sym) or {}
        try:
            bid, ask = sf(q.get("bp")), sf(q.get("ap"))
            if bid > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
                spread_bps = (ask - bid) / max(mid, 1e-9) * 10000.0
            ts = str(q.get("t") or q.get("timestamp") or "")
            if ts:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=UTC)
                age = max(0.0, (now - d.astimezone(UTC)).total_seconds())
        except Exception:
            pass

        score = 45.0
        score += clamp(abs(ret15) * 1800.0, 0.0, 18.0)
        score += clamp(rv * 3200.0, 0.0, 12.0)
        score += clamp(news * 0.65, 0.0, 12.0)
        score += clamp(abs(factor) * 0.50, 0.0, 8.0)
        score += clamp(gm * 450.0, 0.0, 7.0)
        score -= clamp(max(0.0, spread_bps - 8.0) / 5.0, 0.0, 18.0)
        if age > 20.0:
            score -= 20.0
        scores[sym] = round(clamp(score, 0.0, 100.0), 2)

    risk = sf(getattr(intelligence, "global_risk", 0.0))
    n = max(5, int(top_n))
    if risk >= 80:
        n = min(n, 8)
    elif risk >= 70:
        n = min(n, 12)
    ordered = sorted(scores, key=lambda s: scores[s], reverse=True)
    active = ordered[:n]
    posmap = positions or {}
    for sym in symbols:
        if str(sym).upper() in posmap and str(sym).upper() not in active:
            active.append(str(sym).upper())
    return active, scores


def sector_confirmation(intelligence: Any, symbol: str) -> Dict[str, Any]:
    """Small explicit sector/breadth confirmation for a long entry."""
    try:
        from intelligence_engine import BENCHMARK_BY_GROUP, group_for_symbol
        group = group_for_symbol(symbol)
        bench = BENCHMARK_BY_GROUP.get(group, "SPY")
    except Exception:
        group, bench = "DEFAULT", "SPY"
    gm = sf(getattr(intelligence, "group_momentum", {}).get(group, 0.0))
    br = sf(getattr(intelligence, "regime_returns", {}).get(bench, 0.0))
    score = clamp(gm * 260.0 + br * 300.0, -8.0, 8.0)
    # Only strongly opposing breadth is a veto; ordinary noise is a score input.
    ok = bool(score > -4.5)
    return {
        "ok": ok, "score": round(score, 2), "group": group,
        "benchmark": bench, "group_momentum": round(gm, 5),
        "benchmark_return": round(br, 5),
    }


class ShadowLab:
    """Champion/challenger shadow laboratory.

    The lab never submits orders. It records when each model *would* enter and
    later measures the 15-minute return. This supports out-of-sample comparison
    before a challenger is ever promoted to the live/paper champion.
    """

    MODELS = ("champion_gate", "strict_edge", "ofi_confirm", "router_focus")

    def __init__(self, state_dir: str) -> None:
        self.path = os.path.join(state_dir, "learning_v3.sqlite")
        self.lock = threading.Lock()
        self.last_log: Dict[Tuple[str, str], float] = {}
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _ensure(self) -> None:
        try:
            with sqlite3.connect(self.path, timeout=3) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS shadow_signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, symbol TEXT NOT NULL, model TEXT NOT NULL,
                    price REAL, score REAL, probability_up REAL, ev_bps REAL,
                    micro REAL, sector_score REAL, router_mode TEXT,
                    outcome_15m REAL, bot_version TEXT
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_model_ts ON shadow_signals(model, ts)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol_ts ON shadow_signals(symbol, ts)")
                db.commit()
        except Exception:
            pass

    def observe(
        self, *, symbol: str, price: float, signal: int, intel: Dict[str, Any],
        plan: Optional[Dict[str, Any]], router_mode: str, sector: Dict[str, Any],
        champion_allowed: bool, bot_version: str, min_interval_sec: int = 60,
    ) -> None:
        if price <= 0 or signal <= 0:
            return
        p = plan or {}
        score = sf(intel.get("score"), 0.0)
        prob = sf(p.get("probability_up"), sf(intel.get("probability_up"), 0.0))
        ev = sf(p.get("expected_value_bps"), sf(intel.get("expected_value_bps"), -999.0))
        micro = sf(intel.get("micro"), 0.0)
        sec = sf(sector.get("score"), 0.0)
        sec_ok = bool(sector.get("ok", True))
        flags = {
            "champion_gate": bool(champion_allowed),
            "strict_edge": bool(sec_ok and score >= 70.0 and prob >= 0.58 and ev >= 10.0),
            "ofi_confirm": bool(sec_ok and score >= 62.0 and prob >= 0.55 and ev >= 5.0 and micro >= 1.5),
            "router_focus": bool(sec_ok and router_mode in {"trend", "event", "meanrev"} and score >= 62.0 and ev >= 5.0),
        }
        now = time.time()
        ts = datetime.now(UTC).isoformat()
        try:
            with self.lock, sqlite3.connect(self.path, timeout=3) as db:
                for model, yes in flags.items():
                    if not yes:
                        continue
                    key = (norm(symbol), model)
                    if now - self.last_log.get(key, 0.0) < max(15, int(min_interval_sec)):
                        continue
                    self.last_log[key] = now
                    db.execute(
                        """INSERT INTO shadow_signals(ts,symbol,model,price,score,probability_up,ev_bps,micro,sector_score,router_mode,outcome_15m,bot_version)
                           VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                        (ts, norm(symbol), model, price, score, prob, ev, micro, sec, router_mode, bot_version),
                    )
                db.commit()
        except Exception:
            pass

    def update_outcomes(self, symbol: str, current_price: float) -> None:
        if current_price <= 0:
            return
        now = datetime.now(UTC)
        try:
            with self.lock, sqlite3.connect(self.path, timeout=3) as db:
                rows = db.execute(
                    "SELECT id,ts,price FROM shadow_signals WHERE symbol=? AND outcome_15m IS NULL ORDER BY id DESC LIMIT 250",
                    (norm(symbol),),
                ).fetchall()
                for rid, ts, entry in rows:
                    try:
                        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=UTC)
                        age = (now - d.astimezone(UTC)).total_seconds()
                    except Exception:
                        continue
                    if age >= 900 and sf(entry) > 0:
                        db.execute("UPDATE shadow_signals SET outcome_15m=? WHERE id=?", (pct(sf(entry), current_price), rid))
                db.commit()
        except Exception:
            pass

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"enabled": True, "models": {}}
        try:
            with self.lock, sqlite3.connect(self.path, timeout=3) as db:
                for model in self.MODELS:
                    rows = db.execute(
                        "SELECT outcome_15m FROM shadow_signals WHERE model=? AND outcome_15m IS NOT NULL ORDER BY id DESC LIMIT 1000",
                        (model,),
                    ).fetchall()
                    vals = [sf(r[0]) for r in rows]
                    n = len(vals)
                    wins = sum(1 for x in vals if x > 0)
                    out["models"][model] = {
                        "samples": n,
                        "win_rate_pct": round(wins / n * 100.0, 1) if n else None,
                        "avg_return_bps": round(sum(vals) / n * 10000.0, 2) if n else None,
                    }
        except Exception:
            pass
        return out

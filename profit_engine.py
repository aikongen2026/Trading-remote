#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Research-backed profit/risk layer for TradingBot V4.

This module does NOT place orders. It converts the existing technical +
intelligence score into a cost-aware trade plan with:
- volatility-adaptive take-profit / stop-loss levels
- Bayesian calibration from the bot's own historical paper outcomes
- expected-value (EV) gating after spread/fees/slippage
- risk-based position sizing with hard notional caps

The goal is to reduce low-quality turnover, not to guarantee profits.
"""
from __future__ import annotations

import math
import os
import sqlite3
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def normalize_symbol(s: str) -> str:
    return str(s or "").upper().replace("/", "").replace("-", "").strip()


def pct(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b - a) / a


def _log_returns(closes: Iterable[float]) -> List[float]:
    vals = [safe_float(x) for x in closes if safe_float(x) > 0]
    out: List[float] = []
    for a, b in zip(vals[:-1], vals[1:]):
        if a > 0 and b > 0:
            try:
                out.append(math.log(b / a))
            except Exception:
                pass
    return out


def ewma_sigma_1bar(closes: Iterable[float], lookback: int = 40, lam: float = 0.94) -> float:
    """EWMA one-bar volatility in decimal return units.

    A small robust floor prevents zero-volatility plans on sparse data.
    """
    rs = _log_returns(closes)
    if not rs:
        return 0.0
    rs = rs[-max(5, int(lookback)):]
    # Recent observations receive more weight.
    weights = []
    w = 1.0
    for _ in range(len(rs)):
        weights.append(w)
        w *= lam
    weights = list(reversed(weights))
    sw = sum(weights) or 1.0
    mean = sum(r * wt for r, wt in zip(rs, weights)) / sw
    var = sum(wt * (r - mean) ** 2 for r, wt in zip(rs, weights)) / sw
    return max(0.0, math.sqrt(max(0.0, var)))


def robust_sigma_1bar(closes: Iterable[float], lookback: int = 40) -> float:
    """Blend EWMA and median-absolute-deviation volatility.

    The blend is deliberately simple and stable on a small Render instance.
    """
    rs = _log_returns(closes)
    if len(rs) < 3:
        return ewma_sigma_1bar(closes, lookback)
    rs = rs[-max(5, int(lookback)):]
    med = statistics.median(rs)
    mad = statistics.median([abs(x - med) for x in rs])
    sigma_mad = 1.4826 * mad
    sigma_ewma = ewma_sigma_1bar(closes, lookback)
    if sigma_mad <= 0:
        return sigma_ewma
    if sigma_ewma <= 0:
        return sigma_mad
    return 0.65 * sigma_ewma + 0.35 * sigma_mad


def horizon_minutes_for_strategy(strategy: str, market: str) -> int:
    s = str(strategy or "scalp").lower()
    m = str(market or "STOCK").upper()
    if s == "hype":
        return 12 if m == "STOCK" else 18
    if s == "trend":
        return 45 if m == "STOCK" else 60
    if s == "cross":
        return 30 if m == "STOCK" else 45
    return 15 if m == "STOCK" else 20


@dataclass
class TradePlan:
    allowed: bool
    reason: str
    market: str
    symbol: str
    score: float
    probability_up: float
    calibration_samples: float
    expected_move_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    trailing_activate_pct: float
    trailing_stop_pct: float
    estimated_cost_bps: float
    expected_value_bps: float
    reward_risk: float
    horizon_min: int
    qty: float = 0.0
    notional_usd: float = 0.0
    risk_dollars: float = 0.0
    volatility_scale: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["probability_up_pct"] = round(self.probability_up * 100.0, 2)
        return d


class OutcomeCalibrator:
    """Bayesian probability calibration from the bot's own paper outcomes.

    We intentionally shrink heavily toward a conservative prior until enough
    out-of-sample-like observations have accumulated. This avoids letting a
    handful of lucky trades dominate position sizing.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _query(self, market: str, outcome_col: str, limit: int = 1200) -> List[Tuple[Any, ...]]:
        if not os.path.exists(self.db_path):
            return []
        if outcome_col not in {"outcome_5m", "outcome_15m", "outcome_60m"}:
            outcome_col = "outcome_15m"
        try:
            with sqlite3.connect(self.db_path, timeout=2) as db:
                return db.execute(
                    f"""SELECT ts,symbol,composite,{outcome_col},action
                        FROM decisions
                        WHERE market=? AND {outcome_col} IS NOT NULL
                          AND signal>0 AND action LIKE 'BUY%'
                        ORDER BY id DESC LIMIT ?""",
                    (market, int(limit)),
                ).fetchall()
        except Exception:
            return []

    def estimate(
        self,
        market: str,
        symbol: str,
        score: float,
        horizon_min: int,
        *,
        profile: str = "active",
    ) -> Tuple[float, float, Dict[str, Any]]:
        market = str(market or "STOCK").upper()
        symbol = normalize_symbol(symbol)
        score = clamp(float(score), 0.0, 100.0)
        # Intelligence score is used only as a weak prior. It is not treated as
        # a calibrated probability until the journal contains enough evidence.
        p0 = clamp(0.50 + max(0.0, score - 50.0) * (0.0042 if profile == "active" else 0.0038), 0.50, 0.69)
        outcome_col = "outcome_5m" if horizon_min <= 8 else "outcome_15m" if horizon_min <= 30 else "outcome_60m"
        rows = self._query(market, outcome_col)
        if not rows:
            return p0, 0.0, {"prior": p0, "empirical": None, "outcome_col": outcome_col}

        now = datetime.now(timezone.utc)
        succ = 0.0
        total = 0.0
        weighted_ret = 0.0
        weighted_abs = 0.0
        same_symbol_weight = 0.0
        for ts, rsym, comp, outcome, _action in rows:
            c = safe_float(comp, 50.0)
            r = safe_float(outcome, 0.0)
            # Similar score states get more weight; the exact same symbol gets a
            # modest boost without overfitting to one ticker.
            sim = math.exp(-abs(c - score) / 12.0)
            if normalize_symbol(rsym) == symbol:
                sim *= 1.35
                same_symbol_weight += sim
            # Recency half-life ~45 days, but never zero-weight older evidence.
            age_days = 0.0
            try:
                d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - d.astimezone(timezone.utc)).total_seconds() / 86400.0)
            except Exception:
                pass
            rec = 0.25 + 0.75 * math.exp(-math.log(2.0) * age_days / 45.0)
            w = sim * rec
            if w <= 0:
                continue
            total += w
            if r > 0:
                succ += w
            weighted_ret += w * r
            weighted_abs += w * abs(r)

        if total <= 0:
            return p0, 0.0, {"prior": p0, "empirical": None, "outcome_col": outcome_col}
        p_emp = succ / total
        # Strong shrinkage while sample is small. Balanced mode is even more conservative.
        prior_strength = 45.0 if profile == "active" else 60.0
        p_post = (prior_strength * p0 + total * p_emp) / (prior_strength + total)
        p_post = clamp(p_post, 0.46, 0.76)
        avg_ret = weighted_ret / total
        avg_abs = weighted_abs / total
        return p_post, total, {
            "prior": round(p0, 4),
            "empirical": round(p_emp, 4),
            "avg_return": round(avg_ret, 6),
            "avg_abs_return": round(avg_abs, 6),
            "same_symbol_weight": round(same_symbol_weight, 2),
            "outcome_col": outcome_col,
        }


class ProfitEngine:
    def __init__(self, state_dir: str) -> None:
        # Reuse the V3 journal so an upgrade preserves existing learning history.
        self.calibrator = OutcomeCalibrator(os.path.join(state_dir, "learning_v3.sqlite"))

    def plan(
        self,
        *,
        market: str,
        symbol: str,
        price: float,
        closes: List[float],
        spread_bps: float,
        intelligence: Dict[str, Any],
        strategy: str,
        profile: str,
        account_equity: float,
        base_stock_qty: float = 1.0,
        base_crypto_notional: float = 100.0,
        crypto_taker_fee_bps: float = 25.0,
        stock_slippage_bps: float = 2.5,
        crypto_slippage_bps: float = 4.0,
        max_stock_notional_usd: float = 5000.0,
        max_crypto_notional_usd: float = 750.0,
        stock_max_position_pct_equity: float = 0.02,
        crypto_max_position_pct_equity: float = 0.0075,
        stock_risk_pct_equity: float = 0.0005,
        crypto_risk_pct_equity: float = 0.00035,
    ) -> TradePlan:
        market = str(market or "STOCK").upper()
        profile = str(profile or "active").lower()
        strategy = str(strategy or "scalp").lower()
        symbol = str(symbol or "").upper()
        score = safe_float(intelligence.get("score"), 50.0)
        horizon = horizon_minutes_for_strategy(strategy, market)

        sigma1 = robust_sigma_1bar(closes, lookback=45)
        expected_move = sigma1 * math.sqrt(max(1, horizon))
        # Sparse feeds sometimes show artificially tiny variance. Use a small
        # floor that still leaves transaction-cost gating in control.
        if market == "CRYPTO":
            expected_move = clamp(expected_move, 0.0035, 0.045)
            tp = clamp(expected_move * 1.00, 0.0080, 0.0300)
            sl = clamp(expected_move * 0.62, 0.0045, 0.0150)
            cost_bps = max(0.0, spread_bps) + 2.0 * max(0.0, crypto_taker_fee_bps) + max(0.0, crypto_slippage_bps)
            min_ev_bps = 18.0 if profile == "active" else 28.0
            min_prob = 0.545 if profile == "active" else 0.565
            min_rr = 1.35
        else:
            expected_move = clamp(expected_move, 0.0015, 0.025)
            tp = clamp(expected_move * 0.90, 0.0030, 0.0150)
            sl = clamp(expected_move * 0.56, 0.0020, 0.0080)
            cost_bps = max(0.0, spread_bps) + max(0.0, stock_slippage_bps)
            min_ev_bps = 4.0 if profile == "active" else 7.0
            min_prob = 0.535 if profile == "active" else 0.555
            min_rr = 1.35

        # Make the target comfortably larger than estimated round-trip friction.
        min_tp_from_cost = (cost_bps + min_ev_bps + 8.0) / 10000.0
        tp = max(tp, min_tp_from_cost)
        rr = tp / max(sl, 1e-9)

        p_up, samples, cal = self.calibrator.estimate(market, symbol, score, horizon, profile=profile)
        # Intelligence risk and market regime adjust probability slightly, not explosively.
        risk = safe_float(intelligence.get("global_risk"), 0.0)
        regime = safe_float(intelligence.get("regime"), 50.0)
        news = safe_float(intelligence.get("news"), 0.0)
        relative = safe_float(intelligence.get("relative"), 0.0)
        long_factor = safe_float(intelligence.get("long_factor"), 0.0)
        crash_risk = bool(intelligence.get("momentum_crash_risk"))

        p_up += clamp(news / 500.0, -0.035, 0.035)
        p_up += clamp(relative / 500.0, -0.025, 0.025)
        p_up += clamp(long_factor / 600.0, -0.025, 0.025)
        if market == "STOCK":
            p_up += clamp((regime - 50.0) / 1200.0, -0.025, 0.025)
        p_up -= clamp(max(0.0, risk - 65.0) / 1000.0, 0.0, 0.035)
        if crash_risk:
            p_up -= 0.035
        p_up = clamp(p_up, 0.40, 0.78)

        ev_bps = p_up * (tp * 10000.0) - (1.0 - p_up) * (sl * 10000.0) - cost_bps
        rr = tp / max(sl, 1e-9)

        # Volatility-managed sizing: wider stop / higher volatility -> smaller position.
        equity = max(0.0, safe_float(account_equity, 0.0))
        if market == "CRYPTO":
            risk_pct = crypto_risk_pct_equity * (0.80 if profile == "balanced" else 1.0)
            risk_dollars = equity * risk_pct if equity > 0 else base_crypto_notional * sl
            risk_sized = risk_dollars / max(sl, 1e-6)
            cap = min(max_crypto_notional_usd, equity * crypto_max_position_pct_equity) if equity > 0 else max_crypto_notional_usd
            strength_mult = clamp(0.75 + (score - 55.0) / 50.0 + max(0.0, ev_bps) / 250.0, 0.65, 1.50)
            notional = min(cap, max(base_crypto_notional * 0.65, min(risk_sized, base_crypto_notional * strength_mult)))
            qty = 0.0
        else:
            risk_pct = stock_risk_pct_equity * (0.80 if profile == "balanced" else 1.0)
            risk_dollars = equity * risk_pct if equity > 0 else max(1.0, price * base_stock_qty * sl)
            max_qty_risk = risk_dollars / max(price * sl, 1e-9) if price > 0 else 0.0
            cap_notional = min(max_stock_notional_usd, equity * stock_max_position_pct_equity) if equity > 0 else max_stock_notional_usd
            max_qty_cap = cap_notional / price if price > 0 else 0.0
            strength_mult = 1.0
            if score >= 72 and ev_bps >= 10:
                strength_mult = 2.0
            if score >= 82 and ev_bps >= 18:
                strength_mult = 3.0
            desired = max(base_stock_qty, base_stock_qty * strength_mult)
            qty = math.floor(max(0.0, min(desired, max_qty_risk, max_qty_cap)))
            notional = qty * price

        # Position size is cut further during forecastable momentum-crash states.
        vol_scale = 0.55 if crash_risk else (0.75 if risk >= 78 else 1.0)
        if market == "CRYPTO":
            notional *= vol_scale
        else:
            qty = math.floor(qty * vol_scale)
            notional = qty * price

        allowed = True
        reasons: List[str] = []
        if safe_float(intelligence.get("score"), 0.0) < safe_float(intelligence.get("entry_threshold"), 60.0):
            allowed = False; reasons.append("intel")
        if bool(intelligence.get("event_blackout")) or bool(intelligence.get("risk_veto")):
            allowed = False; reasons.append("risk_veto")
        if p_up < min_prob:
            allowed = False; reasons.append("probability")
        if ev_bps < min_ev_bps:
            allowed = False; reasons.append("negative_ev")
        if rr < min_rr:
            allowed = False; reasons.append("reward_risk")
        if market == "STOCK" and qty < 1:
            allowed = False; reasons.append("size")
        if market == "CRYPTO" and notional < max(10.0, base_crypto_notional * 0.50):
            allowed = False; reasons.append("size")

        trail_activate = clamp(tp * 0.55, sl * 0.65, tp * 0.85)
        trail_stop = clamp(sl * 0.70, 0.0015 if market == "STOCK" else 0.0035, sl)

        reason = "OK" if allowed else "WAIT_" + "+".join(reasons)
        plan = TradePlan(
            allowed=allowed,
            reason=reason,
            market=market,
            symbol=symbol,
            score=round(score, 2),
            probability_up=round(p_up, 6),
            calibration_samples=round(samples, 2),
            expected_move_pct=round(expected_move, 6),
            take_profit_pct=round(tp, 6),
            stop_loss_pct=round(sl, 6),
            trailing_activate_pct=round(trail_activate, 6),
            trailing_stop_pct=round(trail_stop, 6),
            estimated_cost_bps=round(cost_bps, 2),
            expected_value_bps=round(ev_bps, 2),
            reward_risk=round(rr, 3),
            horizon_min=horizon,
            qty=float(qty),
            notional_usd=round(notional, 2),
            risk_dollars=round(risk_dollars, 2),
            volatility_scale=round(vol_scale, 3),
        )
        # Extra calibration detail is useful in debug/status but not required by dataclass.
        d = plan.to_dict()
        d["calibration"] = cal
        return plan

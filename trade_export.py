#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a self-contained TradingBot analysis ZIP for 1-30 days.

The export is intentionally read-only. It never submits/cancels orders and it
never writes API keys/passwords to the archive. Data comes from Alpaca account
history plus the bot's own decision journal/state so the ZIP can be reviewed
later to diagnose execution quality, strategy behaviour, fees and P/L.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import trading_bot

SENSITIVE_KEYS = {
    "alpaca_key", "alpaca_secret", "apca_api_key_id", "apca_api_secret_key",
    "dashboard_password", "dashboard_secret_key", "password", "secret",
    "api_key", "api_secret", "authorization",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _sanitize(obj: Any) -> Any:
    """Remove secrets recursively while keeping analytical identifiers."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k)
            low = key.lower()
            if low in SENSITIVE_KEYS or any(x in low for x in ("api_secret", "secret_key", "api-key-secret")):
                continue
            out[key] = _sanitize(v)
        return out
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    return obj


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(_sanitize(obj), ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(_sanitize(v), ensure_ascii=False, separators=(",", ":"), default=str)
    if v is None:
        return ""
    return v


def _rows_to_csv(rows: Iterable[Dict[str, Any]]) -> str:
    rows = [dict(r) for r in rows if isinstance(r, dict)]
    if not rows:
        return ""
    preferred = [
        "transaction_time", "date", "activity_type", "type", "symbol", "side", "qty", "price", "net_amount",
        "order_id", "id", "submitted_at", "filled_at", "canceled_at", "status", "filled_qty", "filled_avg_price",
        "client_order_id", "order_class", "time_in_force", "notional", "commission", "fee", "description",
    ]
    keys = []
    seen = set()
    all_keys = set()
    for r in rows:
        all_keys.update(str(k) for k in r.keys())
    for k in preferred + sorted(all_keys):
        if k in all_keys and k not in seen:
            keys.append(k); seen.add(k)
    sio = io.StringIO(newline="")
    w = csv.DictWriter(sio, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _cell(r.get(k)) for k in keys})
    return sio.getvalue()


def _fetch_orders(api: Any, since: datetime, max_records: int = 50000) -> List[Dict[str, Any]]:
    """Page backwards using before_order_id; stop once the selected window is passed."""
    out: List[Dict[str, Any]] = []
    seen = set()
    before_id: str | None = None
    for _ in range(120):
        params: Dict[str, Any] = {"status": "all", "limit": 500, "direction": "desc", "nested": "true"}
        if before_id:
            params["before_order_id"] = before_id
        page = api._req("GET", f"{api.cfg.trading_base}/v2/orders", params=params)
        if not isinstance(page, list) or not page:
            break
        oldest_dt = None
        last_id = None
        for row in page:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("id") or "")
            last_id = oid or last_id
            dt = _parse_ts(row.get("submitted_at") or row.get("created_at"))
            if dt and (oldest_dt is None or dt < oldest_dt):
                oldest_dt = dt
            if dt is not None and dt < since:
                continue
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(row)
            if len(out) >= max_records:
                return out
        if len(page) < 500 or (oldest_dt is not None and oldest_dt < since):
            break
        next_id = str(page[-1].get("id") or "") if isinstance(page[-1], dict) else ""
        if not next_id or next_id == before_id:
            break
        before_id = next_id
    return out


def _fetch_activities(api: Any, since: datetime, until: datetime, max_records: int = 50000) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    token: str | None = None
    for _ in range(600):
        params: Dict[str, Any] = {
            "after": _iso(since), "until": _iso(until), "direction": "desc", "page_size": 100,
        }
        if token:
            params["page_token"] = token
        page = api._req("GET", f"{api.cfg.trading_base}/v2/account/activities", params=params)
        if not isinstance(page, list) or not page:
            break
        for row in page:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            out.append(row)
            if len(out) >= max_records:
                return out
        if len(page) < 100:
            break
        last = page[-1] if isinstance(page[-1], dict) else {}
        next_token = str(last.get("id") or "")
        if not next_token or next_token == token:
            break
        token = next_token
    return out


def _portfolio_rows(history: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(history, dict):
        return []
    ts = history.get("timestamp") or []
    eq = history.get("equity") or []
    pl = history.get("profit_loss") or []
    pp = history.get("profit_loss_pct") or []
    rows: List[Dict[str, Any]] = []
    for i, raw_ts in enumerate(ts):
        dt = _parse_ts(raw_ts)
        rows.append({
            "timestamp": _iso(dt) if dt else raw_ts,
            "equity": eq[i] if i < len(eq) else None,
            "profit_loss": pl[i] if i < len(pl) else None,
            "profit_loss_pct": pp[i] if i < len(pp) else None,
            "base_value": history.get("base_value"),
            "timeframe": history.get("timeframe"),
        })
    return rows


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _learning_rows(state_dir: Path, since: datetime) -> List[Dict[str, Any]]:
    path = state_dir / "learning_v3.sqlite"
    if not path.exists():
        return []
    try:
        with sqlite3.connect(str(path), timeout=5) as db:
            db.row_factory = sqlite3.Row
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "decisions" not in names:
                return []
            rows = db.execute("SELECT * FROM decisions WHERE ts >= ? ORDER BY ts ASC", (_iso(since),)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _summary(days: int, orders: List[Dict[str, Any]], activities: List[Dict[str, Any]], decisions: List[Dict[str, Any]], portfolio_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    order_status = Counter(str(x.get("status") or "unknown") for x in orders)
    activity_types = Counter(str(x.get("activity_type") or x.get("type") or "unknown") for x in activities)
    fills = [x for x in activities if str(x.get("activity_type") or x.get("type") or "").upper() == "FILL"]
    buy_fills = sum(1 for x in fills if str(x.get("side") or "").lower() == "buy")
    sell_fills = sum(1 for x in fills if str(x.get("side") or "").lower() == "sell")
    canceled_zero = sum(1 for x in orders if str(x.get("status") or "").lower() == "canceled" and float(x.get("filled_qty") or 0) == 0)
    action_counts = Counter(str(x.get("action") or "") for x in decisions if x.get("action") is not None)
    start_eq = end_eq = None
    for r in portfolio_rows:
        try:
            val = float(r.get("equity"))
        except Exception:
            continue
        if start_eq is None:
            start_eq = val
        end_eq = val
    return {
        "selected_days": days,
        "orders": len(orders),
        "order_status_counts": dict(order_status),
        "activities": len(activities),
        "activity_type_counts": dict(activity_types),
        "fill_events": len(fills),
        "buy_fill_events": buy_fills,
        "sell_fill_events": sell_fills,
        "canceled_orders_with_zero_fill": canceled_zero,
        "bot_decisions": len(decisions),
        "decision_action_counts": dict(action_counts),
        "portfolio_equity_start": start_eq,
        "portfolio_equity_end": end_eq,
        "portfolio_equity_change": (end_eq - start_eq) if start_eq is not None and end_eq is not None else None,
        "important_note": "Equity change includes open-position mark-to-market and account cash movements. Use fills/activities/fees for execution-level analysis.",
    }


def build_analysis_export(days: int, state_dir: str | Path, bot_version: str, api: Any | None = None) -> Tuple[bytes, Dict[str, Any]]:
    days = max(1, min(30, int(days)))
    now = _now()
    since = now - timedelta(days=days)
    state = Path(state_dir)
    errors: List[str] = []

    if api is None:
        cfg = trading_bot.load_cfg()
        api = trading_bot.AlpacaRest(cfg)

    try:
        account_raw = api.account()
    except Exception as exc:
        account_raw = {}; errors.append(f"account: {exc}")
    try:
        positions = api.positions()
        if not isinstance(positions, list): positions = []
    except Exception as exc:
        positions = []; errors.append(f"positions: {exc}")
    try:
        orders = _fetch_orders(api, since)
    except Exception as exc:
        orders = []; errors.append(f"orders: {exc}")
    try:
        activities = _fetch_activities(api, since, now)
    except Exception as exc:
        activities = []; errors.append(f"activities: {exc}")
    try:
        history = api.portfolio_history(f"{days}D", "1H", continuous=True)
        if not isinstance(history, dict): history = {}
    except Exception as exc:
        history = {}; errors.append(f"portfolio_history: {exc}")

    portfolio_rows = _portfolio_rows(history)
    decisions = _learning_rows(state, since)
    status = _read_json(state / "status.json", {})
    control = _read_json(state / "control.json", {})
    execution_state = _read_json(state / "execution_state_v4_2.json", {})
    trade_plans = _read_json(state / "trade_plans_v4.json", {})

    # Account fields useful for performance analysis, without account number/id.
    keep_account = {
        k: v for k, v in (account_raw or {}).items()
        if k in {
            "status", "currency", "cash", "portfolio_value", "equity", "last_equity", "long_market_value",
            "short_market_value", "buying_power", "regt_buying_power", "daytrading_buying_power", "initial_margin",
            "maintenance_margin", "pattern_day_trader", "daytrade_count", "trading_blocked", "transfers_blocked",
            "account_blocked", "created_at", "multiplier", "crypto_status",
        }
    }

    fills = [x for x in activities if str(x.get("activity_type") or x.get("type") or "").upper() == "FILL"]
    fee_types = {"FEE", "CFEE", "REGFEE", "TAF", "SEC", "ACATC"}
    fees = [x for x in activities if str(x.get("activity_type") or x.get("type") or "").upper() in fee_types or "fee" in str(x.get("description") or "").lower()]
    summary = _summary(days, orders, activities, decisions, portfolio_rows)

    manifest = {
        "export_schema": "tradingbot-analysis-v1",
        "bot_version": bot_version,
        "generated_at_utc": _iso(now),
        "range_start_utc": _iso(since),
        "range_end_utc": _iso(now),
        "days": days,
        "paper": bool(getattr(getattr(api, "cfg", None), "paper", status.get("paper", True))),
        "contains_secrets": False,
        "errors": errors,
    }

    readme = f"""TRADINGBOT ANALYSE-EKSPORT\n===========================\n\nPeriode: siste {days} dag(er)\nFra: {_iso(since)}\nTil: {_iso(now)}\nBotversjon: {bot_version}\n\nHENSIKT\n-------\nDenne ZIP-en er laget for detaljert analyse av hva boten faktisk gjorde: ordre, fills, gebyrer, kontoutvikling, botens egne beslutningssignaler, execution-guard og trade plans.\n\nFILER\n-----\n00_manifest.json              Versjon/periode/feil under eksport\n01_summary.json               Hurtigstatistikk og kontrolltall\n02_account_snapshot.json      Sanitert kontosnapshot (ingen API-noekler/passord)\n03_positions_snapshot.csv     Aapne posisjoner ved eksporttidspunkt\n03_positions_snapshot.json    Samme som JSON\n04_orders.csv/json            Alpaca-ordre i valgt periode, inkl. canceled/filled/bracket-data\n05_activities.csv/json        Alle kontoaktiviteter i valgt periode\n06_fills.csv                  Kun faktiske fill-hendelser; order_id binder fill til ordre\n07_fees.csv                   Gebyr-/fee-aktiviteter som kan identifiseres\n08_portfolio_history.csv/json Kontoequity/P&L gjennom perioden\n09_bot_decisions.csv          Botens journal: score, signal, EV, sannsynlighet, outcomes m.m.\n10_status_snapshot.json       Dashboard/status ved eksport\n11_control_snapshot.json      Kontrollinnstillinger (sanitert)\n12_execution_state.json       Ordresikkerhet / paagaaende exit-tilstand\n13_trade_plans.json           Lagrede TP/SL/edge-planer\n\nANALYSE\n-------\nLast opp hele ZIP-en i ChatGPT og be om f.eks.:\n\"Analyser denne TradingBot-eksporten. Beregn netto resultat, vinn/tap, profit factor, holdetid, kostnader, slippage, raske reverseringer, gjentatte ordre og hvilke signal-/EV-/score-kombinasjoner som fungerer best. Kom med konkrete kodeendringer.\"\n\nVIKTIG\n------\nPortfolio equity inkluderer urealisert P/L og kontobevegelser. Fills og aktiviteter er derfor med slik at realisert execution-resultat og gebyrer kan analyseres separat. Kryptogebyrer kan vaere i krypto eller USD og skal ikke blandes som samme enhet.\n"""

    files: Dict[str, bytes | str] = {
        "README_ANALYSE.txt": readme,
        "00_manifest.json": _json_bytes(manifest),
        "01_summary.json": _json_bytes(summary),
        "02_account_snapshot.json": _json_bytes(keep_account),
        "03_positions_snapshot.json": _json_bytes(positions),
        "03_positions_snapshot.csv": _rows_to_csv(positions),
        "04_orders.json": _json_bytes(orders),
        "04_orders.csv": _rows_to_csv(orders),
        "05_activities.json": _json_bytes(activities),
        "05_activities.csv": _rows_to_csv(activities),
        "06_fills.csv": _rows_to_csv(fills),
        "07_fees.csv": _rows_to_csv(fees),
        "08_portfolio_history.json": _json_bytes(history),
        "08_portfolio_history.csv": _rows_to_csv(portfolio_rows),
        "09_bot_decisions.csv": _rows_to_csv(decisions),
        "10_status_snapshot.json": _json_bytes(status),
        "11_control_snapshot.json": _json_bytes(control),
        "12_execution_state.json": _json_bytes(execution_state),
        "13_trade_plans.json": _json_bytes(trade_plans),
    }
    if errors:
        files["EXPORT_ERRORS.txt"] = "\n".join(errors) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, payload in files.items():
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            zf.writestr(name, payload)
    return buf.getvalue(), manifest

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
import re
import sqlite3
from contextlib import closing
from runtime_audit import read_evidence, storage_health
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Optional

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
    """Page orders by timestamp window, independent of undocumented order-id cursors."""
    out: List[Dict[str, Any]] = []
    seen = set()
    cursor_until = _now()
    for _ in range(240):
        params: Dict[str, Any] = {
            "status": "all", "limit": 500, "direction": "desc", "nested": "true",
            "after": _iso(since), "until": _iso(cursor_until),
        }
        page = api._req("GET", f"{api.cfg.trading_base}/v2/orders", params=params)
        if not isinstance(page, list) or not page:
            break
        oldest = None
        added = 0
        for row in page:
            if not isinstance(row, dict):
                continue
            dt = _parse_ts(row.get("submitted_at") or row.get("created_at"))
            if dt is None or dt < since:
                continue
            oid = str(row.get("id") or "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(row); added += 1
            if oldest is None or dt < oldest:
                oldest = dt
            if len(out) >= max_records:
                return sorted(out, key=lambda x: str(x.get("submitted_at") or x.get("created_at") or ""), reverse=True)
        if len(page) < 500 or oldest is None:
            break
        next_until = oldest - timedelta(microseconds=1)
        if next_until >= cursor_until:
            break
        cursor_until = next_until
        if not added and cursor_until <= since:
            break
    return sorted(out, key=lambda x: str(x.get("submitted_at") or x.get("created_at") or ""), reverse=True)


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
            dt = _parse_ts(row.get("transaction_time") or row.get("date") or row.get("created_at"))
            if dt is not None and not (since <= dt <= until):
                continue
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
        with closing(sqlite3.connect(str(path), timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "decisions" not in names:
                return []
            rows = db.execute("SELECT * FROM decisions WHERE ts >= ? ORDER BY ts ASC", (_iso(since),)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []



def _trade_ledger(fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Simple FIFO long-only ledger for fills whose entry and exit are both in-window."""
    from collections import defaultdict, deque
    lots = defaultdict(deque)
    rows: List[Dict[str, Any]] = []
    ordered = sorted(fills, key=lambda x: str(x.get("transaction_time") or x.get("date") or ""))
    for f in ordered:
        sym = str(f.get("symbol") or "").upper()
        side = str(f.get("side") or "").lower()
        try:
            qty = float(f.get("qty") or 0); price = float(f.get("price") or 0)
        except Exception:
            continue
        if not sym or qty <= 0 or price <= 0:
            continue
        ts = str(f.get("transaction_time") or f.get("date") or "")
        if side == "buy":
            lots[sym].append([qty, price, ts, str(f.get("order_id") or "")])
        elif side == "sell":
            remain = qty
            while remain > 1e-12 and lots[sym]:
                lot = lots[sym][0]
                take = min(remain, float(lot[0]))
                pnl = (price - float(lot[1])) * take
                entry_dt = _parse_ts(lot[2]); exit_dt = _parse_ts(ts)
                hold = (exit_dt-entry_dt).total_seconds() if entry_dt and exit_dt else None
                rows.append({
                    "symbol": sym, "qty": take, "entry_time": lot[2], "entry_price": lot[1],
                    "exit_time": ts, "exit_price": price, "hold_sec": hold, "gross_pnl_usd": round(pnl, 6),
                    "gross_return_pct": round((price/float(lot[1])-1)*100, 6) if lot[1] else None,
                    "entry_order_id": lot[3], "exit_order_id": str(f.get("order_id") or ""),
                })
                lot[0] = float(lot[0]) - take; remain -= take
                if lot[0] <= 1e-12:
                    lots[sym].popleft()
    return rows

def realized_winning_trades(api: Any, days: int, max_items: int = 50) -> Dict[str, Any]:
    """Return actual profitable FIFO round-trip legs closed in the selected window.

    We fetch a wider history than the selected window so a position opened before
    the window but closed inside it can still be matched.  Values are gross P/L
    from actual Alpaca FILL prices; account/crypto fees are intentionally not
    guessed or allocated to individual trades.
    """
    days = max(1, min(7, int(days)))
    now = _now()
    window_start = now - timedelta(days=days)
    # Bot trades are short-lived, but 30d gives enough context for positions that
    # were opened before the selected 1/3/7-day result window.
    history_start = now - timedelta(days=30)
    activities = _fetch_activities(api, history_start, now)
    fills = [x for x in activities if str(x.get("activity_type") or x.get("type") or "").upper() == "FILL"]
    ledger = _trade_ledger(fills)
    closed: List[Dict[str, Any]] = []
    for row in ledger:
        exit_dt = _parse_ts(row.get("exit_time"))
        if not exit_dt or exit_dt < window_start or exit_dt > now:
            continue
        pnl = float(row.get("gross_pnl_usd") or 0.0)
        if pnl <= 0:
            continue
        out = dict(row)
        out["gross_pnl_usd"] = round(pnl, 6)
        closed.append(out)
    closed.sort(key=lambda x: str(x.get("exit_time") or ""), reverse=True)
    return {
        "days": days,
        "generated_at": _iso(now),
        "winner_count": len(closed),
        "gross_profit_usd": round(sum(float(x.get("gross_pnl_usd") or 0) for x in closed), 6),
        "trades": closed[:max(1, min(200, int(max_items)))],
        "shown": min(len(closed), max(1, min(200, int(max_items)))),
        "note": "Realisert brutto P/L fra faktiske Alpaca fills. Gebyrer er ikke fordelt per trade, og portfolio-resultatet over inkluderer ogsaa aapne posisjoner.",
    }


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
    trade_ledger = _trade_ledger(fills)
    summary = _summary(days, orders, activities, decisions, portfolio_rows)
    summary["matched_round_trip_legs"] = len(trade_ledger)
    summary["matched_round_trip_gross_pnl_usd"] = round(sum(float(x.get("gross_pnl_usd") or 0) for x in trade_ledger), 6)

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
        "09b_trade_ledger.csv": _rows_to_csv(trade_ledger),
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


# ---------------------------------------------------------------------------
# V4.4.2 single-file AI audit export
# ---------------------------------------------------------------------------
def _csv_payload_rows(payload: bytes) -> List[Dict[str, Any]]:
    try:
        txt = payload.decode("utf-8-sig", errors="replace")
        return list(csv.DictReader(io.StringIO(txt)))
    except Exception:
        return []

def _parse_dt_any(value: Any) -> Optional[datetime]:
    try:
        if not value:
            return None
        v = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def _flatten_order_map(orders: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    def rec(o: Dict[str, Any], parent_id: str = "") -> None:
        if not isinstance(o, dict):
            return
        row = dict(o)
        row["parent_order_id"] = parent_id or None
        oid = str(row.get("id") or "")
        if oid:
            out[oid] = row
        for leg in row.get("legs") or []:
            if isinstance(leg, dict):
                rec(leg, oid)
    for o in orders:
        if isinstance(o, dict):
            rec(o)
    return out

def _client_version_tag(cid: Any) -> Optional[str]:
    m = re.search(r"-(?:s|c)(v\d+)-", str(cid or ""), flags=re.I)
    return m.group(1).lower() if m else None

def _decision_window(decisions: List[Dict[str, Any]], symbol: str, when: Optional[datetime], seconds: int = 180) -> Optional[Dict[str, Any]]:
    if when is None:
        return None
    best = None
    best_abs = None
    for d in decisions:
        if str(d.get("symbol") or "").upper() != str(symbol or "").upper():
            continue
        dt = _parse_dt_any(d.get("ts"))
        if dt is None:
            continue
        delta = abs((dt - when).total_seconds())
        if delta <= seconds and (best_abs is None or delta < best_abs):
            best, best_abs = d, delta
    if best is None:
        return None
    x = dict(best)
    x["distance_sec"] = round(float(best_abs or 0.0), 3)
    return x

def _metric_block(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [_num(r.get("gross_pnl_usd")) for r in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    pf = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (999.0 if gross_profit > 0 else None)
    holds = sorted(_num(r.get("hold_sec")) for r in rows if _num(r.get("hold_sec")) >= 0)
    med_hold = holds[len(holds)//2] if holds else None
    return {
        "trades": len(rows), "winners": len(wins), "losers": len(losses),
        "win_rate_pct": round((len(wins)/len(rows)*100.0), 2) if rows else None,
        "gross_pnl_usd": round(sum(pnls), 6), "gross_profit_usd": round(gross_profit, 6),
        "gross_loss_usd": round(gross_loss, 6), "profit_factor": round(pf, 4) if pf is not None else None,
        "avg_win_usd": round(sum(wins)/len(wins), 6) if wins else None,
        "avg_loss_usd": round(sum(losses)/len(losses), 6) if losses else None,
        "expectancy_usd_per_trade": round(sum(pnls)/len(rows), 6) if rows else None,
        "median_hold_sec": round(med_hold, 3) if med_hold is not None else None,
    }

def _trade_diagnostics(trades: List[Dict[str, Any]], positions: List[Dict[str, Any]], portfolio_rows: List[Dict[str, Any]], decisions: List[Dict[str, Any]], status: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    by_exit: Dict[str, List[Dict[str, Any]]] = {}
    buckets = [(0,30,"<30s"),(30,60,"30-60s"),(60,120,"1-2m"),(120,300,"2-5m"),(300,600,"5-10m"),(600,1800,"10-30m"),(1800,10**12,">30m")]
    by_hold: Dict[str, List[Dict[str, Any]]] = {name: [] for _,_,name in buckets}
    for t in trades:
        by_symbol.setdefault(str(t.get("symbol") or "?"), []).append(t)
        by_exit.setdefault(str(t.get("exit_type") or "unknown"), []).append(t)
        h = _num(t.get("hold_sec"), -1)
        for lo, hi, name in buckets:
            if lo <= h < hi:
                by_hold[name].append(t); break
    stop_rows = by_exit.get("stop", [])
    all_losses = abs(sum(min(0.0, _num(t.get("gross_pnl_usd"))) for t in trades))
    stop_losses = abs(sum(min(0.0, _num(t.get("gross_pnl_usd"))) for t in stop_rows))
    dts = [_parse_dt_any(d.get("ts")) for d in decisions]
    dts = [d for d in dts if d is not None]
    jstart, jend = (min(dts), max(dts)) if dts else (None, None)
    period_start = _parse_dt_any(manifest.get("range_start_utc")); period_end = _parse_dt_any(manifest.get("range_end_utc"))
    coverage_sec = max(0.0, (jend-jstart).total_seconds()) if jstart and jend else 0.0
    period_sec = max(1.0, (period_end-period_start).total_seconds()) if period_start and period_end else 1.0
    post_rows = []
    if jstart:
        post_rows = [t for t in trades if (_parse_dt_any(t.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= jstart]
    open_unrealized = sum(_num(p.get("unrealized_pl")) for p in positions)
    open_intraday = sum(_num(p.get("unrealized_intraday_pl")) for p in positions)
    top_losses = sorted(trades, key=lambda x:_num(x.get("gross_pnl_usd")))[:10]
    top_winners = sorted(trades, key=lambda x:_num(x.get("gross_pnl_usd")), reverse=True)[:10]
    cr = status.get("cloud_runtime") if isinstance(status.get("cloud_runtime"), dict) else {}
    return {
        "all_matched_trades": _metric_block(trades),
        "post_decision_journal_start": _metric_block(post_rows),
        "by_symbol": {k:_metric_block(v) for k,v in sorted(by_symbol.items())},
        "by_exit_type": {k:_metric_block(v) for k,v in sorted(by_exit.items())},
        "by_hold_bucket": {k:_metric_block(v) for k,v in by_hold.items()},
        "stop_exit_loss_share_pct": round(stop_losses/all_losses*100.0, 2) if all_losses else 0.0,
        "top_losses": top_losses, "top_winners": top_winners,
        "open_positions_unrealized_pnl_usd": round(open_unrealized, 6),
        "open_positions_intraday_pnl_usd": round(open_intraday, 6),
        "decision_journal": {
            "rows": len(decisions), "start_utc": _iso(jstart) if jstart else None, "end_utc": _iso(jend) if jend else None,
            "coverage_seconds": round(coverage_sec, 1), "selected_period_seconds": round(period_sec, 1),
            "coverage_ratio_pct": round(min(100.0, coverage_sec/period_sec*100.0), 2),
            "warning": "Detailed decision history may reset on Render Free/redeploy; raw Alpaca orders/fills remain authoritative." if not cr.get("always_on_expected") else None,
        },
        "cloud_runtime": cr,
        "source_health": ((status.get("intelligence") or {}).get("source_health") if isinstance(status.get("intelligence"), dict) else {}),
        "global_risk": ((status.get("intelligence") or {}).get("global_risk") if isinstance(status.get("intelligence"), dict) else None),
        "market_regime": ((status.get("intelligence") or {}).get("regime_label") if isinstance(status.get("intelligence"), dict) else None),
    }

def _bar_dt(bar: Dict[str, Any]) -> Optional[datetime]:
    return _parse_dt_any(bar.get("t") or bar.get("timestamp"))

def _bar_close_at_or_after(bars: List[Dict[str, Any]], target: Optional[datetime], max_delay_sec: int = 180) -> Optional[float]:
    if target is None:
        return None
    best = None
    best_delta = None
    for b in bars:
        dt = _bar_dt(b)
        if dt is None or dt < target:
            continue
        delta = (dt-target).total_seconds()
        if delta > max_delay_sec:
            break
        if best_delta is None or delta < best_delta:
            best_delta = delta; best = _num(b.get("c"), 0.0)
    return best if best and best > 0 else None

def _trade_path_stats(trade: Dict[str, Any], bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    et = _parse_dt_any(trade.get("entry_time")); xt = _parse_dt_any(trade.get("exit_time"))
    ep = _num(trade.get("entry_price"), 0.0); xp = _num(trade.get("exit_price"), 0.0)
    if not et or not xt or ep <= 0:
        return {}
    hold = [b for b in bars if (_bar_dt(b) is not None and et <= _bar_dt(b) <= xt)]
    highs = [_num(b.get("h"), 0.0) for b in hold if _num(b.get("h"), 0.0) > 0]
    lows = [_num(b.get("l"), 0.0) for b in hold if _num(b.get("l"), 0.0) > 0]
    out: Dict[str, Any] = {
        "bars_during_hold": len(hold),
        "mfe_pct": round((max(highs)/ep-1.0)*100.0, 4) if highs else None,
        "mae_pct": round((min(lows)/ep-1.0)*100.0, 4) if lows else None,
    }
    for mins in (5,15,60):
        p = _bar_close_at_or_after(bars, et + timedelta(minutes=mins), max_delay_sec=240)
        out[f"entry_outcome_{mins}m_pct"] = round((p/ep-1.0)*100.0, 4) if p else None
        if xp > 0:
            q = _bar_close_at_or_after(bars, xt + timedelta(minutes=mins), max_delay_sec=240)
            out[f"post_exit_{mins}m_pct"] = round((q/xp-1.0)*100.0, 4) if q else None
    return out

def _fetch_trade_bars(api: Any, trades: List[Dict[str, Any]], manifest: Dict[str, Any], days: int) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    errors: List[str] = []
    syms = sorted({str(t.get("symbol") or "").upper() for t in trades if t.get("symbol")})
    stock = [s for s in syms if "/" not in s]
    crypto = [s for s in syms if "/" in s]
    out: Dict[str, List[Dict[str, Any]]] = {}
    start = str(manifest.get("range_start_utc") or "")
    end = str(manifest.get("range_end_utc") or "")
    limit = max(1200, int(days)*1440 + 1440)
    feed = getattr(getattr(api, "cfg", None), "stock_feed", "iex")
    if feed not in {"iex", "sip", "delayed_sip"}:
        try:
            feed = api.resolve_stock_feed()
        except Exception:
            feed = "iex"
    def chunks(xs: List[str], n: int = 10):
        for i in range(0, len(xs), n): yield xs[i:i+n]
    for chunk in chunks(stock):
        try:
            got = api.bars(chunk, "1Min", limit, start, end, feed)
            if isinstance(got, dict): out.update(got)
        except Exception as exc:
            errors.append(f"stock_bars:{','.join(chunk)}:{type(exc).__name__}:HTTP {getattr(exc, 'status_code', 'unknown')}:feed={feed}")
    for chunk in chunks(crypto):
        try:
            got = api.crypto_bars(chunk, "1Min", limit, start, end)
            if isinstance(got, dict): out.update(got)
        except Exception as exc:
            errors.append(f"crypto_bars:{','.join(chunk)}:{type(exc).__name__}")
    return out, errors

def _shadow_rows(state_dir: str | Path, start_utc: Any, end_utc: Any) -> List[Dict[str, Any]]:
    path = Path(state_dir) / "learning_v3.sqlite"
    if not path.exists():
        return []
    start = str(start_utc or "")
    end = str(end_utc or "")
    try:
        import sqlite3
        with closing(sqlite3.connect(path, timeout=3)) as db, db:
            cols = [x[1] for x in db.execute("PRAGMA table_info(shadow_signals)").fetchall()]
            if not cols:
                return []
            rows = db.execute(
                "SELECT * FROM shadow_signals WHERE ts>=? AND ts<=? ORDER BY id", (start, end)
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


def build_ai_analysis_export(days: int, state_dir: str | Path, bot_version: str, api: Any | None = None) -> Tuple[bytes, Dict[str, Any]]:
    """Build ONE uploadable ZIP containing ONE comprehensive AI_ANALYSE.json.

    The payload deliberately contains both precomputed diagnostics and the raw
    sanitized evidence needed to independently recalculate them. The user can
    upload the ZIP as-is; there is no need to unpack 15+ files.
    """
    if api is None:
        cfg = trading_bot.load_cfg()
        api = trading_bot.AlpacaRest(cfg)
    legacy_blob, manifest = build_analysis_export(days, state_dir, bot_version, api=api)
    extracted: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(legacy_blob), "r") as zf:
        for name in zf.namelist():
            extracted[name] = zf.read(name)
    def j(name: str, default: Any) -> Any:
        try:
            return json.loads(extracted.get(name, b"").decode("utf-8"))
        except Exception:
            return default
    summary = j("01_summary.json", {})
    account = j("02_account_snapshot.json", {})
    positions = j("03_positions_snapshot.json", [])
    orders = j("04_orders.json", [])
    activities = j("05_activities.json", [])
    history = j("08_portfolio_history.json", {})
    status = j("10_status_snapshot.json", {})
    control = j("11_control_snapshot.json", {})
    execution = j("12_execution_state.json", {})
    plans = j("13_trade_plans.json", {})
    decisions = _csv_payload_rows(extracted.get("09_bot_decisions.csv", b""))
    fills = _csv_payload_rows(extracted.get("06_fills.csv", b""))
    fees = _csv_payload_rows(extracted.get("07_fees.csv", b""))
    portfolio_rows = _csv_payload_rows(extracted.get("08_portfolio_history.csv", b""))
    ledger = _trade_ledger([dict(x) for x in activities if str(x.get("activity_type") or x.get("type") or "").upper()=="FILL"])
    omap = _flatten_order_map(orders if isinstance(orders, list) else [])
    enriched: List[Dict[str, Any]] = []
    for t in ledger:
        row = dict(t)
        eo = omap.get(str(row.get("entry_order_id") or ""), {})
        xo = omap.get(str(row.get("exit_order_id") or ""), {})
        et = _parse_dt_any(row.get("entry_time")); xt = _parse_dt_any(row.get("exit_time"))
        entry_dec = _decision_window(decisions, str(row.get("symbol") or ""), et)
        exit_dec = _decision_window(decisions, str(row.get("symbol") or ""), xt)
        entry_exact = next((d for d in decisions if d.get("event_type") == "ENTRY" and
                            ((eo.get("client_order_id") and d.get("client_order_id") == eo.get("client_order_id")) or
                             (eo.get("id") and d.get("order_id") == eo.get("id")))), None)
        exit_exact = next((d for d in decisions if d.get("event_type") == "EXIT_REQUEST" and
                           ((xo.get("client_order_id") and d.get("client_order_id") == xo.get("client_order_id")) or
                            (xo.get("id") and d.get("order_id") == xo.get("id")))), None)
        entry_dec = entry_exact or entry_dec
        exit_dec = exit_exact or exit_dec
        legs = eo.get("legs") or []
        tp_leg = next((x for x in legs if str(x.get("order_type") or x.get("type") or "").lower()=="limit" and str(x.get("side") or "").lower()=="sell"), {}) if isinstance(legs,list) else {}
        sl_leg = next((x for x in legs if str(x.get("order_type") or x.get("type") or "").lower()=="stop" and str(x.get("side") or "").lower()=="sell"), {}) if isinstance(legs,list) else {}
        exit_type = str(xo.get("order_type") or xo.get("type") or "unknown").lower()
        inferred = "BROKER_STOP" if exit_type=="stop" else ("BROKER_TAKE_PROFIT" if exit_type=="limit" else "BOT_MARKET_EXIT")
        if exit_exact and exit_exact.get("exit_reason"):
            inferred = str(exit_exact.get("exit_reason"))
        edp = _num((entry_dec or {}).get("price"), 0.0)
        xdp = _num((exit_dec or {}).get("price"), 0.0)
        ep = _num(row.get("entry_price"), 0.0); xp = _num(row.get("exit_price"), 0.0)
        row.update({
            "entry_order_type": eo.get("order_type") or eo.get("type"),
            "entry_order_class": eo.get("order_class"),
            "entry_client_order_id": eo.get("client_order_id"),
            "entry_version_tag": _client_version_tag(eo.get("client_order_id")),
            "planned_take_profit_price": tp_leg.get("limit_price"),
            "planned_stop_price": sl_leg.get("stop_price"),
            "exit_order_type": xo.get("order_type") or xo.get("type"),
            "exit_type": exit_type,
            "exit_reason_inferred": inferred,
            "exit_order_class": xo.get("order_class"),
            "exit_client_order_id": xo.get("client_order_id"),
            "exit_version_tag": _client_version_tag(xo.get("client_order_id")),
            "entry_decision_nearest": entry_dec,
            "exit_decision_nearest": exit_dec,
            "entry_decision_exact_match": entry_exact is not None,
            "exit_decision_exact_match": exit_exact is not None,
            "entry_slippage_vs_decision_bps": round((ep/edp-1.0)*10000.0, 3) if ep>0 and edp>0 else None,
            "exit_slippage_vs_decision_bps": round((xp/xdp-1.0)*10000.0, 3) if xp>0 and xdp>0 else None,
        })
        enriched.append(row)
    trade_bars, bar_errors = _fetch_trade_bars(api, enriched, manifest, days)
    evidence = read_evidence(state_dir, manifest.get("range_start_utc"), manifest.get("range_end_utc"))
    path_sources = {}
    for symbol in {str(t.get("symbol") or "").upper() for t in enriched}:
        historical = trade_bars.get(symbol) or []
        local = evidence["bars"].get(symbol) or []
        combined = {str(b.get("t") or b.get("timestamp")): b for b in local}
        combined.update({str(b.get("t") or b.get("timestamp")): b for b in historical})
        trade_bars[symbol] = sorted(combined.values(), key=lambda b: str(b.get("t") or b.get("timestamp")))
        path_sources[symbol] = {"historical_bars": len(historical), "local_bars": len(local),
                                "combined_bars": len(combined)}
    for row in enriched:
        row["market_path"] = _trade_path_stats(row, trade_bars.get(str(row.get("symbol") or "").upper(), []))
        risk_event = next((e for e in reversed(evidence["events"]) if e.get("kind") == "RISK_EXIT" and
                           ((row.get("exit_order_id") and e["data"].get("order_id") == row["exit_order_id"]) or
                            (row.get("exit_client_order_id") and e["data"].get("client_order_id") == row["exit_client_order_id"]))), None)
        row["exit_risk_event_exact"] = risk_event
        if risk_event and risk_event["data"].get("reason"):
            row["exit_reason_inferred"] = risk_event["data"]["reason"]
    diagnostics = _trade_diagnostics(enriched, positions if isinstance(positions,list) else [], portfolio_rows, decisions, status, manifest)
    diagnostics["market_path_errors"] = bar_errors
    diagnostics["path_data_coverage"] = path_sources
    diagnostics["storage_health"] = storage_health(state_dir)
    diagnostics["by_entry_version"] = {
        version: _metric_block([t for t in enriched if (t.get("entry_version_tag") or "untagged") == version])
        for version in sorted({t.get("entry_version_tag") or "untagged" for t in enriched})}
    shadow_rows = _shadow_rows(state_dir, manifest.get("range_start_utc"), manifest.get("range_end_utc"))
    shadow_summary: Dict[str, Any] = {}
    for model in sorted({str(r.get("model") or "") for r in shadow_rows if r.get("model")}):
        vals = [_num(r.get("outcome_15m")) for r in shadow_rows if str(r.get("model") or "") == model and r.get("outcome_15m") is not None]
        if vals:
            shadow_summary[model] = {
                "samples": len(vals),
                "win_rate_pct": round(sum(1 for x in vals if x > 0) / len(vals) * 100.0, 2),
                "avg_return_bps": round(sum(vals) / len(vals) * 10000.0, 3),
            }
        else:
            shadow_summary[model] = {"samples": 0, "win_rate_pct": None, "avg_return_bps": None}
    diagnostics["champion_challenger"] = shadow_summary
    coverage_warning = diagnostics.get("decision_journal", {}).get("warning")
    payload = {
        "format": "TradingBot-AI-Audit-v3",
        "meta": manifest,
        "executive_summary": summary,
        "diagnostics": diagnostics,
        "analysis_limitations": [x for x in [
            coverage_warning,
            "Portfolio equity includes unrealized P/L and cash/account movements; matched fills are used for execution P/L.",
            "Fees are included only when Alpaca exposes them as account activities; gross trade P/L is kept separate.",
            "A nearest decision is contextual evidence, not guaranteed to be the exact entry/exit trigger unless event_type/client_order_id matches.",
            ("Historical 1-minute path data had fetch errors: " + "; ".join(bar_errors)) if bar_errors else None
        ] if x],
        "completed_trades": enriched,
        "open_positions": positions,
        "account_snapshot": account,
        "portfolio_history_raw": history,
        "portfolio_history_rows": portfolio_rows,
        "orders_raw": orders,
        "fills_raw": fills,
        "activities_raw": activities,
        "fees_raw": fees,
        "bot_decisions_raw": decisions,
        "shadow_challenger_raw": shadow_rows,
        "status_snapshot": status,
        "control_snapshot": control,
        "execution_state": execution,
        "trade_plans": plans,
        "runtime_events": evidence["events"],
        "local_market_bars": evidence["bars"],
        "audit_read_errors": evidence["errors"],
        "market_path_note": "MFE/MAE use 1-minute bars, not tick paths; missing data remains null. Local fallback may have gaps.",
    }
    ai_manifest = dict(manifest)
    ai_manifest["export_schema"] = "tradingbot-ai-audit-v3"
    ai_manifest["single_upload_file"] = True
    ai_manifest["inside_zip"] = ["AI_ANALYSE.json"]
    ai_manifest["build_id"] = status.get("build_id")
    ai_manifest["server_started_at_utc"] = status.get("server_started_at_utc")
    payload["meta"] = ai_manifest
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as zf:
        zf.writestr("AI_ANALYSE.json", json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    return out.getvalue(), ai_manifest

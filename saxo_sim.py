#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Saxo SIM / Oslo Bors side engine for TradingBot V4.13.

Safety invariants:
- SIM ONLY. The REST base is hard-coded to Saxo's simulation gateway.
- Never reads/accepts a LIVE base URL.
- Disabled unless SAXO_SIM_ENABLED=true and SIM credentials are present.
- Uses the main TradingBot armed/paused/kill controls.
- Pauses new Norway traffic at Render WARN (60% by default).
- Has its own daily/monthly response-byte budget as a second cost guard.

This module is deliberately independent from Alpaca execution. It writes its
status to saxo_sim_status.json so the dashboard can surface it without coupling
the main US/crypto engine to Saxo availability.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests


SIM_BASE = "https://gateway.saxobank.com/sim/openapi"
SIM_TOKEN_URL = "https://sim.logonvalidation.net/token"
OSLO_TZ = ZoneInfo("Europe/Oslo")
DEFAULT_SYMBOLS = [
    "EQNR", "DNB", "KOG", "AKRBP", "NHY", "MOWI",
    "YAR", "TEL", "ORK", "TOM", "SALM", "SUBC",
]


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = default
    return max(low, min(high, value))


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = default
    return max(low, min(high, value))


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / float(n)


def _pct(a: float, b: float) -> float:
    return (b / a - 1.0) if a else 0.0


class SaxoSimError(RuntimeError):
    pass


class SaxoSimEngine:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "saxo_sim_status.json"
        self.usage_path = self.state_dir / "render_usage.json"
        self.control_path = self.state_dir / "control.json"
        self.cache_path = self.state_dir / "saxo_sim_instruments.json"
        self.trade_log_path = self.state_dir / "saxo_sim_trades.jsonl"

        self.enabled = _bool_env("SAXO_SIM_ENABLED", False)
        self.access_token = str(os.getenv("SAXO_SIM_ACCESS_TOKEN") or os.getenv("SAXO_SIM_TOKEN") or "").strip()
        self.refresh_token = str(os.getenv("SAXO_SIM_REFRESH_TOKEN") or "").strip()
        self.app_key = str(os.getenv("SAXO_SIM_APP_KEY") or "").strip()
        self.app_secret = str(os.getenv("SAXO_SIM_APP_SECRET") or "").strip()
        self.account_key = str(os.getenv("SAXO_SIM_ACCOUNT_KEY") or "").strip()

        self.poll_sec = _int_env("SAXO_SIM_POLL_SEC", 120, 60, 900)
        self.chart_refresh_sec = _int_env("SAXO_SIM_CHART_REFRESH_SEC", 600, 300, 3600)
        self.daily_budget_mb = _float_env("SAXO_SIM_DAILY_BUDGET_MB", 12.0, 1.0, 100.0)
        self.monthly_budget_mb = _float_env("SAXO_SIM_MONTHLY_BUDGET_MB", 250.0, 25.0, 1000.0)
        self.max_positions = _int_env("SAXO_SIM_MAX_POSITIONS", 3, 1, 10)
        self.qty = _int_env("SAXO_SIM_STOCK_QTY", 1, 1, 100)
        self.take_profit_pct = _float_env("SAXO_SIM_TAKE_PROFIT_PCT", 0.008, 0.002, 0.05)
        self.stop_loss_pct = _float_env("SAXO_SIM_STOP_LOSS_PCT", 0.005, 0.002, 0.05)
        self.min_momentum_pct = _float_env("SAXO_SIM_MIN_MOMENTUM_PCT", 0.003, 0.0005, 0.03)

        raw_symbols = str(os.getenv("SAXO_SIM_SYMBOLS") or "").strip()
        self.symbols = [
            x.strip().upper() for x in raw_symbols.replace(";", ",").split(",") if x.strip()
        ] or list(DEFAULT_SYMBOLS)
        self.symbols = list(dict.fromkeys(self.symbols))[:20]

        self.session = requests.Session()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.instrument_cache: Dict[str, Dict[str, Any]] = {}
        self.chart_cache: Dict[str, Dict[str, Any]] = {}
        self.last_chart_refresh: Dict[str, float] = {}
        self.positions: Dict[int, Dict[str, Any]] = {}
        self.bytes_day = 0
        self.bytes_month = 0
        self.day_key = ""
        self.month_key = ""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and (self.access_token or self.refresh_token))

    def _budget_rollover(self) -> None:
        now = datetime.now(OSLO_TZ)
        dkey = now.strftime("%Y-%m-%d")
        mkey = now.strftime("%Y-%m")
        if self.day_key != dkey:
            self.day_key = dkey
            self.bytes_day = 0
        if self.month_key != mkey:
            self.month_key = mkey
            self.bytes_month = 0

    def _budget_ok(self) -> bool:
        self._budget_rollover()
        return (
            self.bytes_day < self.daily_budget_mb * 1024 * 1024
            and self.bytes_month < self.monthly_budget_mb * 1024 * 1024
        )

    def _render_allows_norway(self) -> bool:
        usage = _read_json(self.usage_path, {})
        try:
            pct = float((usage or {}).get("percent"))
        except Exception:
            return True
        # Norway sidecar pauses already at WARN. The main engine can continue.
        return pct < 60.0

    def _record_bytes(self, request_bytes: int, response_bytes: int) -> None:
        self._budget_rollover()
        amount = max(0, request_bytes) + max(0, response_bytes)
        self.bytes_day += amount
        self.bytes_month += amount

    def _refresh_access_token(self) -> bool:
        if not (self.refresh_token and self.app_key and self.app_secret):
            return False
        raw = f"{self.app_key}:{self.app_secret}".encode("utf-8")
        auth = base64.b64encode(raw).decode("ascii")
        r = self.session.post(
            SIM_TOKEN_URL,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            timeout=20,
        )
        self._record_bytes(len(str(r.request.body or "")), len(r.content or b""))
        if r.status_code >= 400:
            return False
        data = r.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            return False
        self.access_token = token
        new_refresh = str(data.get("refresh_token") or "").strip()
        if new_refresh:
            self.refresh_token = new_refresh
        return True

    def _request(self, method: str, path: str, *, params: Dict[str, Any] | None = None,
                 body: Dict[str, Any] | None = None, retry_auth: bool = True) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = SIM_BASE + path
        if "/sim/openapi" not in url or "live" in url.lower():
            raise SaxoSimError("SAXO_SIM_ONLY: live endpoint refused")
        if not self._budget_ok():
            raise SaxoSimError("SAXO_TRAFFIC_BUDGET")
        if not self._render_allows_norway():
            raise SaxoSimError("RENDER_WARN_PAUSE")
        if not self.access_token and not self._refresh_access_token():
            raise SaxoSimError("SAXO_SIM_TOKEN_MISSING")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        r = self.session.request(method, url, params=params, json=body, headers=headers, timeout=25)
        req_size = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) if body is not None else 0
        self._record_bytes(req_size, len(r.content or b""))
        if r.status_code == 401 and retry_auth and self._refresh_access_token():
            return self._request(method, path, params=params, body=body, retry_auth=False)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = json.dumps(r.json(), ensure_ascii=False)[:500]
            except Exception:
                detail = (r.text or "")[:500]
            raise SaxoSimError(f"Saxo SIM {r.status_code}: {detail}")
        return r.json() if r.content else {}

    def _discover_account(self) -> None:
        if self.account_key:
            return
        data = self._request("GET", "/port/v1/accounts/me", params={"$top": 50})
        rows = data.get("Data") if isinstance(data, dict) else None
        for row in rows or []:
            if bool(row.get("Active", True)) and row.get("AccountKey"):
                self.account_key = str(row["AccountKey"])
                break
        if not self.account_key:
            raise SaxoSimError("No active Saxo SIM account found")

    @staticmethod
    def _is_norway_stock(row: Dict[str, Any], wanted: str) -> bool:
        if str(row.get("AssetType") or "").lower() != "stock":
            return False
        symbol = str(row.get("Symbol") or "").upper()
        currency = str(row.get("CurrencyCode") or "").upper()
        country = str(row.get("ExchangeCountry") or "").upper()
        exch = (str(row.get("ExchangeName") or "") + " " + str(row.get("ExchangeId") or "")).upper()
        wanted = wanted.upper()
        symbol_match = symbol == wanted or symbol.startswith(wanted + ":") or symbol.startswith(wanted + " ")
        norway = currency == "NOK" and (country in {"NO", "NORWAY"} or "OSLO" in exch or "XOSL" in symbol)
        return symbol_match and norway and row.get("Identifier") is not None

    def _discover_instruments(self) -> None:
        cached = _read_json(self.cache_path, {})
        if isinstance(cached, dict):
            self.instrument_cache = {
                str(k).upper(): v for k, v in cached.items() if isinstance(v, dict)
            }
        missing = [s for s in self.symbols if s not in self.instrument_cache]
        for sym in missing:
            data = self._request(
                "GET", "/ref/v1/instruments",
                params={"$top": 20, "AssetTypes": "Stock", "Keywords": sym, "IncludeNonTradable": "false"},
            )
            rows = data.get("Data") if isinstance(data, dict) else None
            selected = next((r for r in (rows or []) if self._is_norway_stock(r, sym)), None)
            if selected:
                self.instrument_cache[sym] = {
                    "symbol": sym,
                    "saxo_symbol": selected.get("Symbol"),
                    "description": selected.get("Description"),
                    "uic": int(selected.get("Identifier")),
                    "asset_type": "Stock",
                    "currency": selected.get("CurrencyCode") or "NOK",
                    "exchange_id": selected.get("ExchangeId") or "",
                    "exchange_name": selected.get("ExchangeName") or "",
                }
        _write_json(self.cache_path, self.instrument_cache)

    def _prices(self) -> Dict[int, Dict[str, Any]]:
        uics = [str(x.get("uic")) for x in self.instrument_cache.values() if x.get("uic")]
        if not uics:
            return {}
        params: Dict[str, Any] = {
            "AssetType": "Stock",
            "Uics": ",".join(uics),
            "FieldGroups": "Quote,PriceInfo",
        }
        if self.account_key:
            params["AccountKey"] = self.account_key
        data = self._request("GET", "/trade/v1/infoprices/list", params=params)
        rows = data.get("Data") if isinstance(data, dict) else None
        out: Dict[int, Dict[str, Any]] = {}
        for row in rows or []:
            try:
                out[int(row.get("Uic"))] = row
            except Exception:
                pass
        return out

    def _chart(self, uic: int) -> List[float]:
        key = str(uic)
        now = time.time()
        cached = self.chart_cache.get(key, {})
        if cached and now - self.last_chart_refresh.get(key, 0.0) < self.chart_refresh_sec:
            return list(cached.get("closes") or [])
        params: Dict[str, Any] = {
            "AssetType": "Stock",
            "Uic": int(uic),
            "Horizon": 5,
            "Count": 48,
            "ChartSampleFieldSet": "LastTraded",
            "FieldGroups": "Data,ChartInfo",
        }
        if self.account_key:
            params["AccountKey"] = self.account_key
        data = self._request("GET", "/chart/v3/charts", params=params)
        rows = data.get("Data") if isinstance(data, dict) else None
        closes = []
        for row in rows or []:
            try:
                value = float(row.get("Close"))
                if value > 0:
                    closes.append(value)
            except Exception:
                pass
        if closes:
            self.chart_cache[key] = {"closes": closes}
            self.last_chart_refresh[key] = now
        return closes

    def _load_positions(self) -> Dict[int, Dict[str, Any]]:
        data = self._request("GET", "/port/v1/netpositions/me", params={"$top": 200})
        rows = data.get("Data") if isinstance(data, dict) else None
        found: Dict[int, Dict[str, Any]] = {}
        watched_uics = {int(x["uic"]) for x in self.instrument_cache.values() if x.get("uic")}
        for row in rows or []:
            base = row.get("NetPositionBase") if isinstance(row, dict) else None
            view = row.get("NetPositionView") if isinstance(row, dict) else None
            if not isinstance(base, dict):
                continue
            try:
                uic = int(base.get("Uic"))
                amount = float(base.get("Amount") or 0)
            except Exception:
                continue
            if uic not in watched_uics or amount == 0:
                continue
            found[uic] = {
                "uic": uic,
                "amount": amount,
                "avg": float((view or {}).get("AverageOpenPrice") or 0),
                "current": float((view or {}).get("CurrentPrice") or 0),
                "pnl": float((view or {}).get("ProfitLossOnTrade") or 0),
                "net_position_id": row.get("NetPositionId"),
            }
        self.positions = found
        return found

    def _place_market(self, uic: int, side: str, amount: float) -> Dict[str, Any]:
        if not self.account_key:
            raise SaxoSimError("No Saxo SIM AccountKey")
        if side not in {"Buy", "Sell"}:
            raise SaxoSimError("Invalid side")
        body = {
            "AccountKey": self.account_key,
            "Amount": float(abs(amount)),
            "AssetType": "Stock",
            "BuySell": side,
            "OrderType": "Market",
            "ManualOrder": False,
            "Uic": int(uic),
            "OrderDuration": {"DurationType": "DayOrder"},
        }
        result = self._request("POST", "/trade/v2/orders", body=body)
        event = {
            "ts": _now_iso(), "paper": True, "broker": "SAXO_SIM",
            "uic": int(uic), "side": side, "amount": float(abs(amount)),
            "response": result,
        }
        with self.trade_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return result

    @staticmethod
    def _oslo_open() -> bool:
        now = datetime.now(OSLO_TZ)
        if now.weekday() >= 5:
            return False
        # Standard Euronext Oslo continuous session. Holiday/half-day closures
        # still fail closed because Saxo will not provide a tradable market.
        return dtime(9, 0) <= now.time() < dtime(16, 20)

    def _control_allows_orders(self) -> bool:
        control = _read_json(self.control_path, {})
        if not isinstance(control, dict):
            return False
        return bool(control.get("armed")) and not bool(control.get("paused")) and not bool(control.get("kill"))

    def _status(self, **extra: Any) -> Dict[str, Any]:
        base = {
            "enabled": self.enabled,
            "configured": self.configured,
            "paper": True,
            "environment": "SIM",
            "base_url": SIM_BASE,
            "symbols_requested": len(self.symbols),
            "symbols_resolved": len(self.instrument_cache),
            "poll_sec": self.poll_sec,
            "chart_refresh_sec": self.chart_refresh_sec,
            "daily_budget_mb": self.daily_budget_mb,
            "monthly_budget_mb": self.monthly_budget_mb,
            "traffic_day_mb": round(self.bytes_day / (1024 * 1024), 4),
            "traffic_month_mb": round(self.bytes_month / (1024 * 1024), 4),
            "render_gate": "OPEN" if self._render_allows_norway() else "PAUSED_AT_60_PERCENT",
            "oslo_session": "OPEN" if self._oslo_open() else "CLOSED",
            "last_update": _now_iso(),
        }
        base.update(extra)
        return base

    def refresh_once(self) -> Dict[str, Any]:
        self._budget_rollover()
        if not self.enabled:
            state = self._status(state="DISABLED", note="Set SAXO_SIM_ENABLED=true to enable Norway SIM.")
            _write_json(self.state_path, state)
            return state
        if not self.configured:
            state = self._status(state="WAITING_FOR_SIM_TOKEN", note="Saxo SIM token/refresh credentials missing.")
            _write_json(self.state_path, state)
            return state
        if not self._render_allows_norway():
            state = self._status(state="RENDER_GUARD", note="Norway SIM paused at Render >=60%.")
            _write_json(self.state_path, state)
            return state
        if not self._budget_ok():
            state = self._status(state="TRAFFIC_BUDGET", note="Norway module byte budget reached.")
            _write_json(self.state_path, state)
            return state

        self._discover_account()
        self._discover_instruments()
        prices = self._prices() if self._oslo_open() else {}
        positions = self._load_positions()
        rows: Dict[str, Any] = {}

        for symbol, inst in self.instrument_cache.items():
            uic = int(inst["uic"])
            qrow = prices.get(uic, {})
            quote = qrow.get("Quote") if isinstance(qrow, dict) else {}
            delayed = int((quote or {}).get("DelayedByMinutes") or 0) if isinstance(quote, dict) else 0
            price = None
            if isinstance(quote, dict):
                for key in ("Mid", "Ask", "Bid"):
                    try:
                        v = float(quote.get(key))
                        if v > 0:
                            price = v
                            break
                    except Exception:
                        pass
            closes: List[float] = []
            signal = 0
            momentum = 0.0
            if self._oslo_open():
                closes = self._chart(uic)
                fast = _sma(closes, 6)
                slow = _sma(closes, 18)
                if len(closes) >= 7:
                    momentum = _pct(closes[-7], closes[-1])
                if fast and slow and closes:
                    signal = 1 if fast > slow and closes[-1] > fast and momentum >= self.min_momentum_pct else 0

            pos = positions.get(uic)
            action = "HOLD"
            if pos and price:
                avg = float(pos.get("avg") or 0)
                pnl_pct = _pct(avg, price) if avg > 0 else 0.0
                if self._oslo_open() and self._control_allows_orders() and (
                    pnl_pct >= self.take_profit_pct or pnl_pct <= -self.stop_loss_pct or signal <= 0
                ):
                    try:
                        self._place_market(uic, "Sell" if float(pos.get("amount") or 0) > 0 else "Buy", abs(float(pos.get("amount") or 0)))
                        action = "SIM_EXIT"
                    except Exception as exc:
                        action = f"EXIT_ERROR:{type(exc).__name__}"
            elif (
                signal > 0 and self._oslo_open() and self._control_allows_orders()
                and len(positions) < self.max_positions and delayed <= 15
            ):
                try:
                    self._place_market(uic, "Buy", self.qty)
                    positions[uic] = {"uic": uic, "amount": float(self.qty), "avg": price or 0}
                    action = "SIM_BUY"
                except Exception as exc:
                    action = f"BUY_ERROR:{type(exc).__name__}"
            elif delayed > 15:
                action = "WAIT_DELAYED_DATA"
            elif not self._oslo_open():
                action = "WAIT_OSLO"

            rows[symbol] = {
                **inst,
                "price": price,
                "delayed_min": delayed,
                "signal": signal,
                "momentum_pct": round(momentum * 100.0, 3),
                "position": pos,
                "action": action,
            }

        state = self._status(
            state="RUNNING_SIM",
            account_connected=bool(self.account_key),
            positions=len(positions),
            symbols=rows,
            note="Saxo simulation only. LIVE endpoints are hard-blocked.",
        )
        _write_json(self.state_path, state)
        return state

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.refresh_once()
            except Exception as exc:
                _write_json(self.state_path, self._status(
                    state="ERROR",
                    error=f"{type(exc).__name__}: {str(exc)[:400]}",
                    note="Saxo SIM side engine failed closed; Alpaca PAPER engine is unaffected.",
                ))
            self.stop_event.wait(self.poll_sec)

    def start(self) -> None:
        threading.Thread(target=self.run, name="saxo-sim-norway", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()

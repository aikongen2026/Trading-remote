#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent order/entry coordinator for TradingBot.

Goals:
- exactly one active exit intent per market+symbol
- persist intent before sending so timeouts/restarts do not create duplicate sells
- reconcile by order id / client_order_id before retrying
- never treat a timeout as proof that an order failed
- require a genuinely renewed entry signal after a completed/attempted trade

This module does not decide *when* to trade. It only coordinates execution safely.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held", "replaced", "pending_replace"}
SUCCESS_STATUSES = {"filled"}
FAIL_STATUSES = {"canceled", "expired", "rejected", "suspended", "stopped", "calculated"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def symbol_key(sym: str) -> str:
    return str(sym or "").upper().replace("/", "").replace("-", "").strip()


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def flatten_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(o: Dict[str, Any]) -> None:
        out.append(o)
        for leg in o.get("legs") or []:
            if isinstance(leg, dict):
                walk(leg)

    for order in orders or []:
        if isinstance(order, dict):
            walk(order)
    return out


def find_order(orders: List[Dict[str, Any]], *, order_id: str = "", client_order_id: str = "") -> Optional[Dict[str, Any]]:
    for o in flatten_orders(orders):
        if order_id and str(o.get("id") or "") == order_id:
            return o
        if client_order_id and str(o.get("client_order_id") or "") == client_order_id:
            return o
    return None


def open_sell_orders(orders: List[Dict[str, Any]], sym: str) -> List[Dict[str, Any]]:
    want = symbol_key(sym)
    out: List[Dict[str, Any]] = []
    for o in flatten_orders(orders):
        if symbol_key(str(o.get("symbol") or "")) != want:
            continue
        if str(o.get("side") or "").lower() != "sell":
            continue
        if str(o.get("status") or "").lower() in OPEN_STATUSES:
            out.append(o)
    return out


def make_client_order_id(prefix: str, market: str, symbol: str) -> str:
    m = "c" if str(market).upper() == "CRYPTO" else "s"
    clean = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())[:10] or "SYM"
    # Alpaca client_order_id is intentionally kept compact.
    return f"{prefix}{m}-{clean}-{uuid.uuid4().hex[:16]}"[:48]


class ExecutionGuard:
    def __init__(self, state_dir: str) -> None:
        self.path = os.path.join(state_dir, "execution_state_v4_2.json")
        raw = _read_json(self.path)
        self.state: Dict[str, Any] = {
            "version": 1,
            "exits": raw.get("exits") if isinstance(raw.get("exits"), dict) else {},
            "entry_latches": raw.get("entry_latches") if isinstance(raw.get("entry_latches"), dict) else {},
        }
        self._save()

    @staticmethod
    def key(market: str, symbol: str) -> str:
        return f"{str(market).upper()}:{str(symbol).upper()}"

    def _save(self) -> None:
        self.state["updated_at"] = utc_now_iso()
        _atomic_write_json(self.path, self.state)

    # ----- entry latch -----
    def entry_blocked(self, market: str, symbol: str) -> bool:
        row = self.state["entry_latches"].get(self.key(market, symbol))
        return bool(isinstance(row, dict) and row.get("blocked"))

    def block_entry(self, market: str, symbol: str, reason: str = "entry_submitted", client_order_id: str = "") -> None:
        k = self.key(market, symbol)
        cur = self.state["entry_latches"].get(k)
        if isinstance(cur, dict) and cur.get("blocked") and client_order_id and cur.get("client_order_id") == client_order_id:
            return
        self.state["entry_latches"][k] = {
            "blocked": True,
            "reason": reason,
            "client_order_id": client_order_id,
            "blocked_at": utc_now_iso(),
        }
        self._save()

    def rearm_entry(self, market: str, symbol: str, reason: str = "signal_reset") -> None:
        k = self.key(market, symbol)
        if k in self.state["entry_latches"]:
            self.state["entry_latches"].pop(k, None)
            self._save()

    # ----- exit intent -----
    def get_exit(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        row = self.state["exits"].get(self.key(market, symbol))
        return row if isinstance(row, dict) else None

    def _set_exit(self, market: str, symbol: str, row: Dict[str, Any]) -> Dict[str, Any]:
        row["updated_at"] = utc_now_iso()
        self.state["exits"][self.key(market, symbol)] = row
        self._save()
        return row

    def clear_exit(self, market: str, symbol: str, note: str = "completed") -> None:
        k = self.key(market, symbol)
        if k in self.state["exits"]:
            self.state["exits"].pop(k, None)
            self._save()

    def start_exit(self, market: str, symbol: str, qty: float, reason: str) -> Dict[str, Any]:
        existing = self.get_exit(market, symbol)
        if existing:
            # Keep the first reason as audit trail; update current requested qty.
            existing["qty"] = float(qty)
            existing["latest_reason"] = reason
            return self._set_exit(market, symbol, existing)
        row = {
            "market": str(market).upper(),
            "symbol": str(symbol).upper(),
            "qty": float(qty),
            "reason": reason,
            "latest_reason": reason,
            "phase": "cancel_protection",
            "attempts": 0,
            "not_found_checks": 0,
            "created_at": utc_now_iso(),
            "created_epoch": time.time(),
            "cancel_requested_epoch": 0.0,
            "client_order_id": "",
            "order_id": "",
            "last_status": "",
            "last_error": "",
        }
        return self._set_exit(market, symbol, row)

    def _adopt_existing_exit(self, market: str, symbol: str, row: Dict[str, Any], order: Dict[str, Any]) -> Dict[str, Any]:
        row["phase"] = "submitted"
        row["order_id"] = str(order.get("id") or "")
        row["client_order_id"] = str(order.get("client_order_id") or "")
        row["last_status"] = str(order.get("status") or "").lower()
        row["adopted_existing"] = True
        return self._set_exit(market, symbol, row)

    def _reconcile_submitted(self, api: Any, orders: List[Dict[str, Any]], row: Dict[str, Any]) -> str:
        market = row["market"]
        symbol = row["symbol"]
        order = find_order(orders, order_id=str(row.get("order_id") or ""), client_order_id=str(row.get("client_order_id") or ""))
        if order is None and row.get("client_order_id"):
            last_lookup = float(row.get("last_lookup_epoch", 0.0) or 0.0)
            if time.time() - last_lookup >= 2.0:
                row["last_lookup_epoch"] = time.time()
                self._set_exit(market, symbol, row)
                try:
                    order = api.get_order_by_client_order_id(str(row.get("client_order_id")))
                except Exception as exc:
                    row["last_error"] = f"reconcile lookup: {exc}"
                    self._set_exit(market, symbol, row)
                    return "EXIT_RECONCILING"

        if isinstance(order, dict):
            row["order_id"] = str(order.get("id") or row.get("order_id") or "")
            row["last_status"] = str(order.get("status") or "").lower()
            row["filled_qty"] = str(order.get("filled_qty") or "0")
            row["not_found_checks"] = 0
            self._set_exit(market, symbol, row)
            st = row["last_status"]
            if st in OPEN_STATUSES:
                return "EXIT_PENDING"
            if st in SUCCESS_STATUSES:
                return "EXIT_FILLED_WAIT_POSITION"
            if st in FAIL_STATUSES:
                if int(row.get("attempts", 0) or 0) < 2:
                    row["phase"] = "retry_ready"
                    self._set_exit(market, symbol, row)
                    return "EXIT_RETRY_READY"
                row["phase"] = "reconcile_required"
                self._set_exit(market, symbol, row)
                return "EXIT_RECONCILE_REQUIRED"
            return "EXIT_RECONCILING"

        # A timeout after submit is *unknown*, not failed. Wait, lookup twice, then retry at most once.
        age = time.time() - float(row.get("submit_epoch", row.get("created_epoch", time.time())) or time.time())
        if age >= 5.0:
            row["not_found_checks"] = int(row.get("not_found_checks", 0) or 0) + 1
            self._set_exit(market, symbol, row)
            if row["not_found_checks"] >= 2:
                if int(row.get("attempts", 0) or 0) < 2:
                    row["phase"] = "retry_ready"
                    self._set_exit(market, symbol, row)
                    return "EXIT_RETRY_READY"
                row["phase"] = "reconcile_required"
                self._set_exit(market, symbol, row)
                return "EXIT_RECONCILE_REQUIRED"
        return "EXIT_RECONCILING"

    def advance_exit(
        self,
        api: Any,
        orders: List[Dict[str, Any]],
        *,
        market: str,
        symbol: str,
        position_qty: float,
        reason: str,
        regular_market_open: bool = False,
        extended_session_open: bool = False,
        bid: Optional[float] = None,
        extended_limit_buffer_bps: float = 5.0,
    ) -> str:
        market = str(market).upper()
        symbol = str(symbol).upper()
        qty = max(0.0, float(position_qty or 0.0))
        if qty <= 0:
            self.clear_exit(market, symbol, "position_closed")
            return "EXIT_DONE"

        row = self.get_exit(market, symbol) or self.start_exit(market, symbol, qty, reason)
        row["qty"] = qty
        row["latest_reason"] = reason
        self._set_exit(market, symbol, row)

        phase = str(row.get("phase") or "cancel_protection")
        if phase in {"submitted", "submitting"}:
            return self._reconcile_submitted(api, orders, row)
        if phase == "reconcile_required":
            return "EXIT_RECONCILE_REQUIRED"

        sells = open_sell_orders(orders, symbol)
        # If an existing bot exit is already live, adopt it instead of competing with it.
        for o in sells:
            cid = str(o.get("client_order_id") or "")
            otype = str(o.get("type") or "").lower()
            if cid.startswith("tbx-") or otype == "market":
                self._adopt_existing_exit(market, symbol, row, o)
                return "EXIT_PENDING"

        if phase == "cancel_protection":
            # Bracket/OCO protection is cancelled once before an intelligent/manual close.
            # Repeated loops do not keep cancelling/resubmitting.
            protection = [o for o in sells if not str(o.get("client_order_id") or "").startswith("tbx-")]
            if protection:
                last_cancel = float(row.get("cancel_requested_epoch", 0.0) or 0.0)
                if time.time() - last_cancel >= 3.0:
                    cancelled: List[str] = []
                    for o in protection:
                        oid = str(o.get("id") or "")
                        if not oid:
                            continue
                        try:
                            api.cancel_order(oid)
                            cancelled.append(oid)
                        except Exception as exc:
                            row["last_error"] = f"cancel protection {oid}: {exc}"
                    row["cancel_requested_epoch"] = time.time()
                    row["cancelled_protection_ids"] = cancelled
                    self._set_exit(market, symbol, row)
                return "EXIT_CANCEL_PROTECTION"
            row["phase"] = "retry_ready"
            self._set_exit(market, symbol, row)
            phase = "retry_ready"

        if phase in {"retry_ready", "waiting_market"}:
            if market == "STOCK" and not regular_market_open:
                if not extended_session_open or not bid or float(bid) <= 0:
                    row["phase"] = "waiting_market"
                    self._set_exit(market, symbol, row)
                    return "EXIT_WAIT_MARKET"

            cid = make_client_order_id("tbx-", market, symbol)
            row["client_order_id"] = cid
            row["order_id"] = ""
            row["last_status"] = "submitting"
            row["phase"] = "submitting"
            row["attempts"] = int(row.get("attempts", 0) or 0) + 1
            row["submit_epoch"] = time.time()
            row["not_found_checks"] = 0
            self._set_exit(market, symbol, row)  # persist BEFORE network submit

            try:
                if market == "CRYPTO":
                    resp = api.submit_order(symbol, "sell", qty, tif="gtc", order_type="market", client_order_id=cid)
                elif regular_market_open:
                    resp = api.submit_order(symbol, "sell", qty, tif="day", order_type="market", client_order_id=cid)
                else:
                    limit_price = max(0.01, float(bid) * (1.0 - float(extended_limit_buffer_bps) / 10000.0))
                    resp = api.submit_order(
                        symbol, "sell", qty, tif="day", order_type="limit", limit_price=limit_price,
                        extended_hours=True, client_order_id=cid,
                    )
                if isinstance(resp, dict):
                    row["order_id"] = str(resp.get("id") or "")
                    row["last_status"] = str(resp.get("status") or "accepted").lower()
                row["phase"] = "submitted"
                row["last_error"] = ""
                self._set_exit(market, symbol, row)
                return "EXIT_SUBMITTED"
            except Exception as exc:
                # Keep phase=submitting. Reconcile by client_order_id next loop.
                row["last_error"] = f"submit unknown: {exc}"
                self._set_exit(market, symbol, row)
                return "EXIT_SUBMIT_UNKNOWN"

        return "EXIT_RECONCILING"

    def summary(self) -> Dict[str, Any]:
        exits = self.state.get("exits") if isinstance(self.state.get("exits"), dict) else {}
        blocked = self.state.get("entry_latches") if isinstance(self.state.get("entry_latches"), dict) else {}
        reconcile = [k for k, v in exits.items() if isinstance(v, dict) and v.get("phase") == "reconcile_required"]
        return {
            "enabled": True,
            "active_exits": len(exits),
            "blocked_entries": sum(1 for v in blocked.values() if isinstance(v, dict) and v.get("blocked")),
            "reconcile_required": len(reconcile),
            "reconcile_symbols": reconcile[:8],
            "state_file": os.path.basename(self.path),
        }

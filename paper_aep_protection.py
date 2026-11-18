"""Explicit, authenticated AEP-only protection for Alpaca PAPER holdings.

Nothing here runs in the autonomous trading loop. A human must request a
fresh broker preview and then approve the exact stop price in the dashboard.
Never adopts a manual holding as a bot-owned trade.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from execution_guard import open_sell_orders

_LOCK = threading.Lock()
_INTENT_FILE = "aep_paper_stop_intent.json"
_STOP_MIN_FRACTION = 0.75  # price must not be over 25% below last broker mark
_STOP_MAX_FRACTION = 0.995  # minimum 0.5% gap from last broker mark


class AepProtectionError(Exception):
    def __init__(self, message: str, code: int = 409):
        self.message = message
        self.code = code
        super().__init__(message)


def _check_paper(api) -> None:
    cfg = getattr(api, "cfg", None)
    if (cfg is None or getattr(cfg, "paper", None) is not True
            or str(getattr(cfg, "trading_base", "")).rstrip("/")
            != "https://paper-api.alpaca.markets"):
        raise AepProtectionError("Kun Alpaca PAPER er tillatt", 403)


def _number(value, label: str) -> float:
    try:
        x = float(value)
    except (ValueError, TypeError, OverflowError):
        raise AepProtectionError(f"Ugyldig {label} fra megler", 503) from None
    if not math.isfinite(x) or x <= 0:
        raise AepProtectionError(f"Mangler gyldig {label} fra megler", 503)
    return x


def fresh_aep_unprotected(api) -> dict:
    """Refuse to proceed unless live PAPER position and ALL open orders agree."""
    _check_paper(api)
    positions = api.positions()
    orders = api.get_orders(status="open", limit=500, nested=True)
    if not isinstance(positions, list) or not isinstance(orders, list) or len(orders) >= 500:
        raise AepProtectionError("Kan ikke bekrefte fullstendige meglerdata", 503)
    row = next((p for p in positions if isinstance(p, dict)
                and str(p.get("symbol") or "").upper() == "AEP"), None)
    if row is None:
        raise AepProtectionError("Ingen åpen AEP-posisjon i PAPER-kontoen")
    if str(row.get("asset_class") or "us_equity").lower() != "us_equity":
        raise AepProtectionError("Uventet aktivatype hos Alpaca")
    qty = _number(row.get("qty"), "AEP-antall")
    if qty != int(qty):
        raise AepProtectionError("Kun hele AEP-aksjer støttes for GTC stop")
    if open_sell_orders(orders, "AEP"):
        raise AepProtectionError("AEP har allerede en åpen salgsordre. Ingen ny stopordre lagt inn.")
    mark = _number(row.get("current_price"), "AEP-kurs")
    suggested = float((Decimal(str(mark)) * Decimal("0.95")).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN))
    return {
        "symbol": "AEP",
        "qty": int(qty),
        "current_price": round(mark, 4),
        "suggested_stop_price": suggested,
        "time_in_force": "gtc",
        "market": "PAPER",
    }


def validate_stop_price(raw, mark: float) -> str:
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise AepProtectionError("Oppgi en gyldig stoppkurs") from None
    if not price.is_finite() or price <= 0 or price.as_tuple().exponent < -2:
        raise AepProtectionError("Stoppkurs må være positiv med maks to desimaler")
    value = float(price)
    if not float(mark) * _STOP_MIN_FRACTION <= value <= float(mark) * _STOP_MAX_FRACTION:
        raise AepProtectionError("Stoppkurs må ligge 0,5–25 % under siste meglerkurs")
    return f"{price:.2f}"


def _read_intent(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {"state": "unknown"}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        return {"state": "unknown"}


def _write_intent(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def submit_approved_aep_stop(api, state_dir, *, preview: dict, stop_price) -> dict:
    """At most one transmission until reconciled; never auto-repeat on timeout."""
    _check_paper(api)
    path = Path(state_dir) / _INTENT_FILE
    with _LOCK:
        earlier = _read_intent(path)
        if earlier and earlier.get("state") != "rejected":
            raise AepProtectionError(
                "Tidligere beskyttelsesforsøk må kontrolleres i Alpaca PAPER før nytt forsøk."
            )
        now = time.time()
        if not isinstance(preview, dict) or now > float(preview.get("expires", 0)):
            raise AepProtectionError("Forhåndsvisningen er utløpt. Hent ny AEP-status.")
        fresh = fresh_aep_unprotected(api)
        if int(preview.get("qty", -1)) != fresh["qty"]:
            raise AepProtectionError("AEP-antallet har endret seg. Hent ny forhåndsvisning.")
        original = _number(preview.get("current_price"), "forhåndsvist AEP-kurs")
        if abs(fresh["current_price"] - original) / original > 0.02:
            raise AepProtectionError("AEP-kursen har endret seg. Hent ny forhåndsvisning.")
        stop = validate_stop_price(stop_price, fresh["current_price"])
        cid = "tbp-aep-" + secrets.token_hex(12)
        intent = {
            "symbol": "AEP", "state": "pending",
            "qty": fresh["qty"], "stop_price": stop,
            "client_order_id": cid,
            "created_at": now,
        }
        # Persist before sending. If network times out, never guess that no
        # order reached Alpaca and never blindly submit a duplicate.
        _write_intent(path, intent)
        try:
            result = api.submit_protective_stop(
                "AEP", fresh["qty"], float(stop), client_order_id=cid
            )
        except Exception as error:
            if getattr(error, "status_code", None) in (400, 422):
                intent["state"] = "rejected"
                _write_intent(path, intent)
                raise AepProtectionError(
                    "Alpaca avviste stopordren. Ingen ny ordre forsøkes automatisk."
                ) from None
            # Keep the pending intent for manual reconciliation after timeouts.
            raise AepProtectionError(
                "Ordrestatus er uavklart. Kontroller Alpaca PAPER før du prøver igjen.",
                503,
            ) from None
        if not isinstance(result, dict) or not result.get("id"):
            raise AepProtectionError(
                "Uavklart ordresvar. Kontroller Alpaca PAPER før du prøver igjen.",
                503,
            )
        intent["state"] = "submitted"
        intent["order_id"] = str(result["id"])
        _write_intent(path, intent)
        return {
            "ok": True,
            "symbol": "AEP", "qty": fresh["qty"],
            "stop_price": stop,
            "order_id": str(result["id"]),
            "status": str(result.get("status") or "submitted"),
            "paper": True,
        }

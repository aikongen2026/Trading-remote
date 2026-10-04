from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Dict, Tuple

import requests

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


def strip_quotes(v: str) -> str:
    v = (v or "").strip().lstrip("\ufeff").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def read_env(path: Path = ENV_PATH) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k:
            out[k] = strip_quotes(v)
    return out


def as_bool(v: str, default: bool = True) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ja", "j"}


def get_credentials(env: Dict[str, str]) -> Tuple[str, str, bool]:
    # Local .env is the source of truth for this one-click app.
    key = strip_quotes(env.get("ALPACA_KEY") or env.get("APCA_API_KEY_ID") or env.get("ALPACA_KEY_ID") or "")
    secret = strip_quotes(env.get("ALPACA_SECRET") or env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY") or "")
    paper = as_bool(env.get("ALPACA_PAPER", "true"), True)
    return key, secret, paper


def validate(key: str, secret: str, paper: bool = True, timeout: int = 12) -> Tuple[bool, str]:
    if not key or not secret:
        return False, "API-nokler mangler."
    base = PAPER_URL if paper else LIVE_URL
    try:
        r = requests.get(
            base + "/v2/account",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        return False, f"Nettverksfeil mot Alpaca: {e}"

    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            data = {}
        status = data.get("status", "OK") if isinstance(data, dict) else "OK"
        return True, f"Alpaca-konto godkjent ({status})."
    if r.status_code in (401, 403):
        kind = "PAPER" if paper else "LIVE"
        return False, f"Alpaca avviste {kind}-noklene ({r.status_code}). Bruk noklene fra samme {kind}-konto."
    text = (r.text or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) > 180:
        text = text[:180] + "..."
    return False, f"Alpaca svarte HTTP {r.status_code}: {text or 'ukjent feil'}"


def write_env(key: str, secret: str, paper: bool = True) -> None:
    old = read_env()
    old["ALPACA_KEY"] = key.strip()
    old["ALPACA_SECRET"] = secret.strip()
    old["ALPACA_PAPER"] = "true" if paper else "false"
    old["ALPACA_DATA_FEED"] = old.get("ALPACA_DATA_FEED") or "iex"

    preferred = ["ALPACA_KEY", "ALPACA_SECRET", "ALPACA_PAPER", "ALPACA_DATA_FEED", "ALPACA_DATA_BASE"]
    lines = ["# Tradingbot lokal konfigurasjon - IKKE DEL DENNE FILEN"]
    for k in preferred:
        if k in old:
            lines.append(f"{k}={old[k]}")
    for k, v in old.items():
        if k not in preferred and k not in {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY"}:
            lines.append(f"{k}={v}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)


def check() -> int:
    env = read_env()
    key, secret, paper = get_credentials(env)
    ok, msg = validate(key, secret, paper)
    print(msg)
    return 0 if ok else 2


def configure() -> int:
    print("\n=== ALPACA PAPER API OPPSETT ===")
    print("Lim inn PAPER API Key ID og Secret Key fra Alpaca.")
    print("Secret vises ikke mens du skriver. Ctrl+C avbryter.\n")
    for attempt in range(1, 4):
        key = input("PAPER API Key ID: ").strip()
        secret = getpass.getpass("PAPER Secret Key: ").strip()
        ok, msg = validate(key, secret, True)
        print(msg)
        if ok:
            write_env(key, secret, True)
            print(f"Lagret lokalt i {ENV_PATH.name}. Du kan starte boten med START_TRADING_BOT.bat.")
            return 0
        if attempt < 3:
            print("Prov igjen.\n")
    print("Kunne ikke godkjenne noklene etter 3 forsok.")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--configure", action="store_true")
    args = ap.parse_args()
    if args.configure:
        return configure()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())

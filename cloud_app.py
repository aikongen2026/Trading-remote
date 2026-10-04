#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud launcher: runs trading engine + secure web dashboard in one service."""
from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path


def _strip_env_value(v: str) -> str:
    v = (v or "").strip().lstrip("\ufeff").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _load_bootstrap_env() -> None:
    """Load local/Render secret files before importing dashboard/engine modules.

    Supported files (first existing values win only when the process environment
    does not already define the key):
      /etc/secrets/tradingbot.env   <- recommended on Render
      /etc/secrets/alpaca_keys.env
      ./tradingbot.env              <- convenient local/VPS file
      ./.env                         <- legacy local file
    """
    here = Path(__file__).resolve().parent
    paths = [
        Path('/etc/secrets/tradingbot.env'),
        Path('/etc/secrets/alpaca_keys.env'),
        here / 'tradingbot.env',
        here / 'ALPACA_KEYS.env',
        here / '.env',
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = _strip_env_value(value)
                if key and value:
                    os.environ.setdefault(key, value)
            print(f'Loaded config file: {path}')
        except Exception as exc:
            print(f'WARNING: could not read config file {path}: {exc}')


_load_bootstrap_env()

from waitress import serve

import trading_bot
from control_server import app, patch_control, safe_read_json, safe_write_json, STATUS_PATH

ENGINE_THREAD: threading.Thread | None = None
ENGINE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def set_engine_status(state: str, note: str = "") -> None:
    st = safe_read_json(STATUS_PATH, {})
    if not isinstance(st, dict):
        st = {}
    st["engine_state"] = state
    if note:
        st["notes"] = note
    st["last_update"] = trading_bot.utc_now_iso()
    safe_write_json(STATUS_PATH, st)


def engine_target() -> None:
    try:
        trading_bot.main()
        st = safe_read_json(STATUS_PATH, {})
        if isinstance(st, dict) and st.get("engine_state") == "AUTH_ERROR":
            return
        set_engine_status("STOPPED", "Trading engine avsluttet; watchdog vurderer restart.")
    except Exception as e:
        msg = str(e)
        if "Mangler Alpaca keys" in msg or "Alpaca keys" in msg and "mangler" in msg.lower():
            # Missing configuration is not a crash. Stop the watchdog restart loop
            # and show one clear setup message in the dashboard.
            set_engine_status(
                "AUTH_ERROR",
                "Mangler Alpaca-noekler. Paa Render: Environment -> Secret Files -> "
                "legg til tradingbot.env med ALPACA_KEY og ALPACA_SECRET, saa redeploy.",
            )
            return
        traceback.print_exc()
        set_engine_status("CRASHED", f"Engine crash: {e}")


def start_engine() -> None:
    global ENGINE_THREAD
    with ENGINE_LOCK:
        if ENGINE_THREAD and ENGINE_THREAD.is_alive():
            return
        ENGINE_THREAD = threading.Thread(target=engine_target, name="trading-engine", daemon=True)
        ENGINE_THREAD.start()


def watchdog() -> None:
    autorestart = env_bool("ENGINE_AUTORESTART", True)
    while not STOP_EVENT.wait(8):
        if not autorestart:
            continue
        st = safe_read_json(STATUS_PATH, {})
        if isinstance(st, dict) and st.get("engine_state") == "AUTH_ERROR":
            # Bad keys require env/redeploy; don't hammer Alpaca endlessly.
            continue
        t = ENGINE_THREAD
        if t is None or not t.is_alive():
            set_engine_status("RESTARTING", "Watchdog restarter trading engine.")
            time.sleep(2)
            start_engine()


def initialise_state() -> None:
    trading_bot.ensure_control_file()
    safe_start = env_bool("CLOUD_SAFE_START", True)
    marker = Path(trading_bot.STATE_DIR) / ".cloud_initialized"
    if safe_start and not marker.exists():
        # First cloud boot is intentionally safe. With a persistent disk, later restarts
        # preserve the user's START/STOP state instead of unexpectedly disarming.
        patch_control({"armed": False, "paused": True, "kill": False})
        try:
            marker.write_text("initialized\n", encoding="utf-8")
        except Exception:
            pass
    if not Path(STATUS_PATH).exists():
        set_engine_status("STARTING", "Cloud service starter.")


def main() -> None:
    initialise_state()
    start_engine()
    threading.Thread(target=watchdog, name="engine-watchdog", daemon=True).start()
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "10000"))
    print(f"Tradingbot remote dashboard listening on {host}:{port}")
    serve(app, host=host, port=port, threads=8, channel_timeout=30)


if __name__ == "__main__":
    main()

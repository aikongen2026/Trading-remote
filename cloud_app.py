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
    """Load local/Render secret files before importing dashboard/engine modules."""
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
from alpaca_v49 import install_v49
from control_server import app, patch_control, safe_read_json, safe_write_json, STATUS_PATH
from render_usage_guard import RenderUsageGuard
from saxo_sim import SaxoSimEngine
from saxo_oauth import register_saxo_oauth
from onedrive_backup import OneDriveBackup
from onedrive_oauth import register_onedrive_oauth
from onedrive_rclone import RcloneBackup
from google_drive_rclone import GoogleDriveBackup, GoogleDriveBackupError
from onedrive_backup import OneDriveBackupError
from flask import jsonify, request
import hmac

# Install monkey-patches before the engine constructs AlpacaRest / IntelligenceEngine.
# The sidecar itself starts only after persistent state has been restored.
ALPACA_EDGE = install_v49(trading_bot)
RENDER_USAGE_GUARD = RenderUsageGuard(trading_bot.STATE_DIR)
SAXO_SIM = SaxoSimEngine(trading_bot.STATE_DIR)
register_saxo_oauth(app, SAXO_SIM)
ONEDRIVE_BACKUP = OneDriveBackup(trading_bot.STATE_DIR)
register_onedrive_oauth(app, ONEDRIVE_BACKUP)
RCLONE_BACKUP = RcloneBackup(trading_bot.STATE_DIR)
GOOGLE_BACKUP = GoogleDriveBackup(trading_bot.STATE_DIR)
_PAIR_FAILURES = []
_PAIR_LOCK = threading.Lock()

@app.post("/api/onedrive/pair")
def onedrive_pair():
    # Strictly isolated setup endpoint; no browser session, no trading actions.
    # The HTTPS request must carry the existing dashboard password in a header.
    if not request.is_secure:
        return jsonify({"ok": False, "error": "https_required"}), 403
    now = time.monotonic()
    with _PAIR_LOCK:
        _PAIR_FAILURES[:] = [x for x in _PAIR_FAILURES if now - x < 3600]
        if len(_PAIR_FAILURES) >= 10:
            return jsonify({"ok": False, "error": "rate_limited"}), 429
        supplied = request.headers.get("X-Setup-Password", "")
        expected = os.getenv("DASHBOARD_PASSWORD", "").strip()
        if not expected or not hmac.compare_digest(supplied, expected):
            _PAIR_FAILURES.append(now)
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not env_bool("ALPACA_PAPER", True):
        return jsonify({"ok": False, "error": "paper_only_required"}), 403
    if not request.is_json:
        return jsonify({"ok": False, "error": "json_required"}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    try:
        RCLONE_BACKUP.connect(payload["token"])
    except Exception as exc:
        # Avoid echoing OAuth secrets, Graph payloads or tokens to the client.
        return jsonify({"ok": False, "error": str(exc) if isinstance(exc, OneDriveBackupError)
                        else "setup_failed"}), 400
    return jsonify({"ok": True, "status": RCLONE_BACKUP.status()})

@app.post("/api/google-drive/pair")
def google_drive_pair():
    # Browser session is not used here: explicit dashboard password and HTTPS
    # are required, and this route cannot change trading control state.
    if not request.is_secure:
        return jsonify({"ok": False, "error": "https_required"}), 403
    now = time.monotonic()
    with _PAIR_LOCK:
        _PAIR_FAILURES[:] = [x for x in _PAIR_FAILURES if now - x < 3600]
        if len(_PAIR_FAILURES) >= 10:
            return jsonify({"ok": False, "error": "rate_limited"}), 429
        supplied = request.headers.get("X-Setup-Password", "")
        expected = os.getenv("DASHBOARD_PASSWORD", "").strip()
        if not expected or not hmac.compare_digest(supplied, expected):
            _PAIR_FAILURES.append(now)
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not env_bool("ALPACA_PAPER", True):
        return jsonify({"ok": False, "error": "paper_only_required"}), 403
    if not request.is_json:
        return jsonify({"ok": False, "error": "json_required"}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), dict):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    try:
        GOOGLE_BACKUP.connect(
            payload["token"], payload.get("client_id"), payload.get("client_secret")
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) if isinstance(exc, GoogleDriveBackupError)
                        else "setup_failed"}), 400
    return jsonify({"ok": True, "status": GOOGLE_BACKUP.status()})

@app.get("/api/google-drive/status")
def google_drive_status():
    return jsonify(GOOGLE_BACKUP.status())


@app.get("/api/onedrive/status")
def onedrive_status():
    return jsonify(RCLONE_BACKUP.status())


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
    st["alpaca_edge_v49"] = ALPACA_EDGE.health()
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
        patch_control({"armed": False, "paused": True, "kill": False})
        try:
            marker.write_text("initialized\n", encoding="utf-8")
        except Exception:
            pass
    if not Path(STATUS_PATH).exists():
        set_engine_status("STARTING", "Cloud service starter.")


def main() -> None:
    from state_storage import StateBackup

    # Restore all evidence databases before Edge V4.9 opens its SQLite recorder.
    backup = StateBackup(trading_bot.STATE_DIR)
    backup.restore()
    initialise_state()
    ALPACA_EDGE.start()
    RENDER_USAGE_GUARD.start()
    SAXO_SIM.start()
    start_engine()
    backup.start()
    ONEDRIVE_BACKUP.start()
    RCLONE_BACKUP.start()
    GOOGLE_BACKUP.start()
    threading.Thread(target=watchdog, name="engine-watchdog", daemon=True).start()
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "10000"))
    print(f"Tradingbot remote dashboard listening on {host}:{port}")
    serve(app, host=host, port=port, threads=8, channel_timeout=30)


if __name__ == "__main__":
    main()


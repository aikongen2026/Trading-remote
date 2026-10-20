#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Secure responsive dashboard / PWA for the trading bot.

Local mode:
  python control_server.py
  http://127.0.0.1:5050

Cloud mode is started by cloud_app.py. In public/cloud mode, set
DASHBOARD_PASSWORD and DASHBOARD_SECRET_KEY as environment secrets.
"""
from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from state_storage import resolve_state_dir

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = Path(__file__).resolve().parent
STATE_DIR = resolve_state_dir(HERE)
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTROL_PATH = STATE_DIR / "control.json"
STATUS_PATH = STATE_DIR / "status.json"
VERSION_PATH = HERE / "VERSION.txt"
BOT_VERSION = VERSION_PATH.read_text(encoding="utf-8", errors="ignore").strip() if VERSION_PATH.exists() else "4.5.0-adaptive-edge"
BUILD_ID = os.getenv("RENDER_GIT_COMMIT", "local")[:12]
STARTED_AT = datetime.now(timezone.utc).isoformat()

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

PUBLIC_MODE = str(os.getenv("PUBLIC_MODE") or os.getenv("RENDER") or "").lower() in {"1", "true", "yes", "on"}
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
AUTH_REQUIRED = PUBLIC_MODE or bool(DASHBOARD_PASSWORD)
if AUTH_REQUIRED and not DASHBOARD_PASSWORD:
    # Fail closed. A public dashboard without a password is not allowed.
    DASHBOARD_PASSWORD = secrets.token_urlsafe(32)
    print("WARNING: DASHBOARD_PASSWORD missing in public mode; generated temporary password for this boot:")
    print(DASHBOARD_PASSWORD)

app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_urlsafe(48)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=PUBLIC_MODE or str(os.getenv("COOKIE_SECURE", "")).lower() in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=64 * 1024,
)

_LOGIN_ATTEMPTS: Dict[str, list[float]] = {}
_PROFIT_TRADES_CACHE: Dict[int, tuple[float, Dict[str, Any]]] = {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_read_json(path: os.PathLike[str] | str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_write_json(path: os.PathLike[str] | str, data: Any) -> None:
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def patch_control(patch: Dict[str, Any]) -> Dict[str, Any]:
    control = safe_read_json(CONTROL_PATH, {})
    if not isinstance(control, dict):
        control = {}
    control.update(patch)
    safe_write_json(CONTROL_PATH, control)
    return control


def is_authed() -> bool:
    return (not AUTH_REQUIRED) or bool(session.get("auth"))


def login_rate_limited(ip: str) -> bool:
    now = time.time()
    arr = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < 600]
    _LOGIN_ATTEMPTS[ip] = arr
    return len(arr) >= 8


def record_login_failure(ip: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


@app.before_request
def require_login():
    p = request.path
    public = p in {"/login", "/healthz", "/manifest.webmanifest", "/service-worker.js"} or p.startswith("/static/")
    if public or is_authed():
        return None
    if p.startswith("/api/"):
        return jsonify({"ok": False, "error": "login_required"}), 401
    return redirect(url_for("login", next=p))


LOGIN_HTML = r"""
<!doctype html><html lang="no"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b1220"><title>Trading Bot – innlogging</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#08101d;color:#eef4ff;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;padding:20px}
.card{width:min(420px,100%);background:#0f1a33;border:1px solid #24365a;border-radius:18px;padding:24px;box-shadow:0 20px 50px #0008}h1{font-size:24px;margin:0 0 8px}.muted{color:#9fb0cf;font-size:14px;margin-bottom:20px}input{width:100%;padding:14px 15px;border-radius:12px;border:1px solid #30466f;background:#081327;color:#fff;font-size:16px;margin:8px 0 14px}button{width:100%;padding:14px;border:0;border-radius:12px;background:#0f6f4e;color:#fff;font-weight:800;font-size:16px}.err{background:#4a1420;border:1px solid #8c2f49;padding:10px;border-radius:10px;margin-bottom:12px}.logo{width:54px;height:54px;border-radius:14px;background:#12264c;display:grid;place-items:center;font-size:28px;margin-bottom:14px}

    /* ===== NEON BOOST V2 - ONLY VISUAL ===== */
    :root{
      --line:#1697ff;
      --line2:#33d6ff;
      --line3:#7af7ff;
      --blue:#1f8fff;
      --cyan:#3de3ff;
      --cyan2:#a6fbff;
      --shadow:0 24px 70px rgba(0,0,0,.52);
      --glow:0 0 0 1px rgba(51,185,255,.26),0 0 24px rgba(0,153,255,.22);
      --glow-strong:0 0 0 1px rgba(92,220,255,.42),0 0 22px rgba(0,163,255,.32),0 0 56px rgba(0,108,255,.20);
    }
    body{
      background:
        radial-gradient(1200px 580px at 50% -80px,rgba(0,140,255,.32),transparent 62%),
        radial-gradient(760px 420px at 98% 10%,rgba(0,120,255,.16),transparent 58%),
        radial-gradient(880px 500px at 0% 55%,rgba(0,220,255,.10),transparent 60%),
        linear-gradient(180deg,#020912 0%,#020b16 42%,#01060d 100%);
    }
    body:before{
      opacity:.42;
      background-image:
        linear-gradient(rgba(66,160,220,.10) 1px,transparent 1px),
        linear-gradient(90deg,rgba(66,160,220,.08) 1px,transparent 1px);
      background-size:40px 40px;
    }
    body:after{
      width:980px;
      height:430px;
      top:-235px;
      border:1px solid rgba(73,211,255,.20);
      box-shadow:
        0 0 110px rgba(0,140,255,.22),
        inset 0 0 100px rgba(25,170,255,.08);
      background:
        repeating-radial-gradient(circle at 50% 100%,rgba(53,191,255,.12) 0 1px,transparent 1px 26px),
        repeating-linear-gradient(90deg,transparent 0 39px,rgba(55,178,255,.07) 40px 41px);
    }
    .topbar{
      border:1px solid rgba(42,157,255,.42);
      background:
        linear-gradient(180deg,rgba(2,15,30,.98),rgba(3,19,38,.90)),
        radial-gradient(760px 160px at 50% 0%,rgba(0,135,255,.18),transparent 72%);
      box-shadow:
        0 20px 48px rgba(0,0,0,.32),
        inset 0 -1px 0 rgba(80,220,255,.16),
        0 0 28px rgba(0,120,255,.12);
    }
    .topbar:before{
      inset:auto 20% 0 14%;
      height:72px;
      opacity:.55;
      background:
        linear-gradient(
          165deg,
          transparent 0 10%,
          rgba(66,220,255,.95) 11% 12%,
          transparent 13% 20%,
          rgba(40,154,255,.88) 21% 22%,
          transparent 23% 34%,
          rgba(102,235,255,.88) 35% 36%,
          transparent 37% 48%,
          rgba(60,165,255,.72) 49% 50%,
          transparent 51% 62%,
          rgba(86,231,255,.86) 63% 64%,
          transparent 65%
        );
      filter:drop-shadow(0 0 10px rgba(56,206,255,.65));
    }
    .wrap::before{
      content:"";
      position:absolute;
      top:58px;
      left:140px;
      right:140px;
      height:180px;
      pointer-events:none;
      opacity:.15;
      background:
        radial-gradient(circle at 50% 10%,rgba(90,220,255,.65),transparent 42%),
        linear-gradient(90deg,transparent 0 6%,rgba(74,214,255,.18) 7%,transparent 8% 17%,rgba(28,146,255,.16) 18%,transparent 19% 29%,rgba(68,228,255,.14) 30%,transparent 31%);
      clip-path:ellipse(50% 100% at 50% 0%);
      filter:blur(4px);
    }
    .brandMark{
      border:1px solid #37beff;
      background:linear-gradient(145deg,rgba(16,108,170,.92),rgba(5,29,55,.98));
      box-shadow:
        inset 0 0 30px rgba(80,236,255,.18),
        0 0 0 1px rgba(65,220,255,.18),
        0 0 24px rgba(0,165,255,.24),
        0 0 42px rgba(0,98,255,.14);
      color:#a5f6ff;
      text-shadow:0 0 20px rgba(100,231,255,.92);
    }
    h1{
      text-shadow:
        0 0 16px rgba(73,182,255,.18),
        0 0 28px rgba(0,140,255,.14);
    }
    .revBadge,
    .runChip.on,
    .systemBox,
    .btn.active,
    .filterBtn.active{
      box-shadow:
        0 0 0 1px rgba(79,216,255,.20),
        0 0 18px rgba(41,188,255,.20),
        0 0 34px rgba(0,110,255,.10);
    }
    .card,
    .sideCard,
    .kpiCard,
    .tableCard,
    .ordersDetails{
      border-color:rgba(35,145,214,.92) !important;
      box-shadow:
        0 18px 40px rgba(0,0,0,.28),
        0 0 0 1px rgba(22,133,212,.08),
        0 0 26px rgba(0,132,255,.12),
        inset 0 1px 0 rgba(255,255,255,.04),
        inset 0 0 38px rgba(0,146,255,.05);
    }
    .card:after,
    .sideCard:before{
      box-shadow:
        inset 0 0 0 1px rgba(81,221,255,.08),
        inset 0 0 24px rgba(51,164,255,.04);
    }
    .card:hover,
    .sideCard:hover,
    .kpiCard:hover{
      border-color:#39cbff !important;
      box-shadow:
        0 20px 44px rgba(0,0,0,.32),
        0 0 0 1px rgba(86,224,255,.14),
        0 0 30px rgba(40,196,255,.22),
        0 0 54px rgba(0,114,255,.12),
        inset 0 0 30px rgba(55,180,255,.05);
    }
    .cardHead,
    .tableHead{position:relative}
    .cardHead::after,
    .tableHead::after{
      content:"";
      position:absolute;
      left:0;
      right:0;
      bottom:-8px;
      height:1px;
      background:linear-gradient(90deg,transparent,rgba(65,218,255,.7),transparent);
      box-shadow:0 0 10px rgba(53,204,255,.4);
      opacity:.7;
    }
    .kpiCard{
      background:
        linear-gradient(145deg,rgba(7,37,68,.97),rgba(3,18,35,.94)),
        radial-gradient(220px 80px at 100% 0%,rgba(45,187,255,.18),transparent 70%);
    }
    .kpiCard:before{
      content:"";
      position:absolute;
      inset:0;
      pointer-events:none;
      background:linear-gradient(90deg,transparent 0 75%,rgba(98,233,255,.08) 100%);
      opacity:.7;
    }
    .kpiValue{
      text-shadow:
        0 0 14px rgba(79,221,255,.10),
        0 0 26px rgba(0,123,255,.08);
    }
    .kpiValue.pos{
      text-shadow:
        0 0 14px rgba(61,242,193,.22),
        0 0 28px rgba(0,170,255,.08);
    }
    .btn{
      border-color:#2d78aa;
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.05),
        0 5px 18px rgba(0,0,0,.14),
        0 0 0 1px rgba(28,114,178,.06);
    }
    .btn:hover{
      border-color:#48d7ff;
      box-shadow:
        0 10px 26px rgba(0,0,0,.24),
        0 0 16px rgba(58,210,255,.18),
        0 0 30px rgba(0,122,255,.10);
    }
    .heroControls .start{
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.09),
        0 0 20px rgba(6,225,150,.20),
        0 0 34px rgba(0,145,255,.08);
    }
    .heroControls .stop{
      box-shadow:
        0 0 18px rgba(244,174,33,.14),
        0 0 30px rgba(0,120,255,.05);
    }
    .heroControls .danger{
      box-shadow:
        0 0 20px rgba(255,44,91,.16),
        0 0 34px rgba(0,125,255,.06),
        inset 0 1px 0 rgba(255,255,255,.05);
    }
    .banner.good{
      box-shadow:
        0 0 18px rgba(24,220,162,.10),
        0 0 30px rgba(0,130,255,.08),
        inset 0 0 18px rgba(41,255,194,.04);
    }
    .banner.warn{
      box-shadow:
        0 0 18px rgba(255,189,61,.08),
        0 0 26px rgba(0,120,255,.05);
    }
    .banner.bad{
      box-shadow:
        0 0 18px rgba(255,72,110,.10),
        0 0 28px rgba(0,100,255,.04);
    }
    table{border-top:1px solid rgba(58,196,255,.10)}
    th{
      background:linear-gradient(180deg,#06253d,#04192b);
      box-shadow:inset 0 -1px 0 rgba(54,196,255,.10);
    }
    tbody tr:hover{
      background:linear-gradient(90deg,rgba(10,88,132,.42),rgba(4,36,61,.22));
      box-shadow:
        inset 3px 0 0 rgba(58,214,255,.46),
        0 0 16px rgba(0,120,255,.08);
    }
    .marketBadge.stock{box-shadow:0 0 12px rgba(52,196,255,.08)}
    .marketBadge.crypto{box-shadow:0 0 12px rgba(255,197,50,.06)}
    input, select{
      border-color:#2e759f;
      box-shadow:
        inset 0 0 14px rgba(0,98,164,.05),
        0 0 0 1px rgba(15,96,148,.03);
    }
    input:focus, select:focus{
      box-shadow:
        0 0 0 2px rgba(39,223,255,.14),
        0 0 16px rgba(39,223,255,.14),
        0 0 28px rgba(0,114,255,.08);
    }

    /* ===== FORSLAG 4 MATCH V3 - VISUAL ONLY ===== */
    /* Full-width command-center composition */
    .wrap{
      width:100%;
      max-width:none;
      margin:0;
      padding:14px 22px 42px 84px;
    }

    /* Decorative left HUD rail matching the concept image */
    .wrap::after{
      content:"⌂\A↗\A▥\A⚙\A◉\A▤\A↗";
      white-space:pre;
      position:fixed;
      z-index:12;
      left:16px;
      top:112px;
      width:48px;
      padding:12px 0 10px;
      text-align:center;
      color:#71ddff;
      font-size:17px;
      font-weight:900;
      line-height:52px;
      border:1px solid rgba(27,153,226,.72);
      border-radius:15px;
      background:
        linear-gradient(180deg,rgba(4,29,53,.95),rgba(2,16,32,.96)),
        radial-gradient(circle at 50% 0%,rgba(0,156,255,.18),transparent 48%);
      box-shadow:
        inset 0 0 24px rgba(15,161,255,.08),
        0 0 0 1px rgba(38,198,255,.08),
        0 0 28px rgba(0,133,255,.16),
        0 22px 45px rgba(0,0,0,.28);
      text-shadow:0 0 12px rgba(64,215,255,.72);
      pointer-events:none;
    }

    /* More cinematic background depth */
    body{
      background:
        radial-gradient(1000px 440px at 57% -60px,rgba(0,141,255,.34),transparent 62%),
        radial-gradient(900px 500px at 100% 28%,rgba(0,95,255,.12),transparent 65%),
        radial-gradient(900px 560px at 0% 70%,rgba(0,201,255,.08),transparent 64%),
        linear-gradient(180deg,#010914 0%,#010a14 48%,#01060c 100%);
    }
    body:before{
      opacity:.48;
      background-image:
        linear-gradient(rgba(39,149,215,.09) 1px,transparent 1px),
        linear-gradient(90deg,rgba(39,149,215,.075) 1px,transparent 1px);
      background-size:38px 38px;
      mask-image:linear-gradient(to bottom,#000 0%,rgba(0,0,0,.82) 68%,transparent 100%);
    }
    body:after{
      width:1250px;
      height:560px;
      top:-330px;
      border:1px solid rgba(73,211,255,.26);
      box-shadow:
        0 0 120px rgba(0,136,255,.24),
        inset 0 0 110px rgba(25,170,255,.10);
      background:
        repeating-radial-gradient(circle at 50% 100%,rgba(59,194,255,.13) 0 1px,transparent 1px 25px),
        repeating-linear-gradient(90deg,transparent 0 39px,rgba(55,178,255,.075) 40px 41px);
    }

    /* Header: graph + holographic globe */
    .topbar{
      min-height:92px;
      padding:12px 18px 16px;
      margin:0 0 10px;
      border:1px solid rgba(28,158,235,.72);
      border-top:1px solid rgba(63,205,255,.28);
      border-radius:14px;
      background:
        linear-gradient(180deg,rgba(2,16,31,.97),rgba(2,23,43,.92)),
        radial-gradient(700px 190px at 58% 0%,rgba(0,137,255,.18),transparent 70%);
      box-shadow:
        inset 0 1px 0 rgba(120,230,255,.08),
        inset 0 -1px 0 rgba(48,185,255,.14),
        0 0 0 1px rgba(0,108,190,.08),
        0 0 34px rgba(0,127,255,.14),
        0 20px 42px rgba(0,0,0,.26);
      overflow:hidden;
    }
    .topbar:before{
      inset:auto 18% 0 25%;
      height:78px;
      opacity:.72;
      background:
        linear-gradient(164deg,
          transparent 0 8%,
          rgba(62,216,255,.95) 9% 10%,
          transparent 11% 17%,
          rgba(24,132,255,.85) 18% 19%,
          transparent 20% 28%,
          rgba(93,230,255,.96) 29% 30%,
          transparent 31% 39%,
          rgba(28,139,255,.78) 40% 41%,
          transparent 42% 50%,
          rgba(76,220,255,.88) 51% 52%,
          transparent 53% 61%,
          rgba(32,151,255,.82) 62% 63%,
          transparent 64%);
      filter:drop-shadow(0 0 9px rgba(41,196,255,.72));
      clip-path:polygon(0 80%,7% 68%,13% 74%,20% 46%,27% 58%,34% 31%,41% 52%,48% 22%,55% 48%,62% 37%,69% 58%,76% 30%,84% 45%,91% 20%,100% 35%,100% 100%,0 100%);
    }
    .topbar:after{
      content:"";
      position:absolute;
      z-index:1;
      pointer-events:none;
      width:230px;
      height:230px;
      top:-105px;
      right:29%;
      border-radius:50%;
      border:1px solid rgba(67,210,255,.28);
      background:
        repeating-radial-gradient(circle at 50% 50%,transparent 0 24px,rgba(76,213,255,.09) 25px 26px),
        repeating-linear-gradient(0deg,transparent 0 21px,rgba(60,187,255,.08) 22px 23px),
        repeating-linear-gradient(90deg,transparent 0 29px,rgba(60,187,255,.07) 30px 31px),
        radial-gradient(circle at 42% 38%,rgba(38,180,255,.13),rgba(4,55,98,.05) 58%,transparent 72%);
      box-shadow:
        inset 0 0 40px rgba(42,186,255,.10),
        0 0 40px rgba(0,133,255,.18),
        0 0 75px rgba(0,106,255,.10);
      opacity:.88;
    }
    .topbar>*{position:relative;z-index:3}

    .brandMark{
      width:62px;
      height:62px;
      border:1px solid #31c9ff;
      border-radius:17px;
      background:linear-gradient(145deg,rgba(9,105,170,.96),rgba(3,27,54,.98));
      box-shadow:
        inset 0 0 32px rgba(73,229,255,.16),
        0 0 0 1px rgba(56,211,255,.14),
        0 0 17px rgba(20,190,255,.36),
        0 0 42px rgba(0,104,255,.20);
      color:#9cf8ff;
      text-shadow:0 0 17px rgba(89,235,255,.92);
    }
    h1{
      font-size:31px;
      text-shadow:
        0 0 12px rgba(90,221,255,.14),
        0 0 28px rgba(0,133,255,.16);
    }

    /* KPI strip stronger neon frames and chart feel */
    .kpiStrip{
      gap:8px;
      margin:9px 0 11px;
    }
    .kpiCard{
      min-height:74px;
      border:1px solid rgba(26,158,232,.90);
      border-radius:11px;
      background:
        linear-gradient(145deg,rgba(5,37,67,.98),rgba(2,19,38,.96)),
        radial-gradient(210px 86px at 100% 0%,rgba(36,174,255,.22),transparent 68%);
      box-shadow:
        inset 0 1px 0 rgba(153,235,255,.05),
        inset 0 0 28px rgba(0,128,220,.055),
        0 0 0 1px rgba(23,147,223,.08),
        0 0 16px rgba(0,139,255,.13),
        0 10px 26px rgba(0,0,0,.20);
    }
    .kpiCard:after{
      opacity:.78;
      right:-4px;
      bottom:-12px;
      width:105px;
      height:58px;
      filter:drop-shadow(0 0 6px rgba(35,205,255,.72));
    }
    .kpiCard:hover{
      transform:translateY(-2px);
      border-color:#50dcff;
      box-shadow:
        0 0 0 1px rgba(86,224,255,.26),
        0 0 24px rgba(33,193,255,.25),
        0 0 48px rgba(0,111,255,.13),
        0 12px 28px rgba(0,0,0,.25);
    }

    /* Main dashboard proportions closer to the concept */
    .mainGrid{
      grid-template-columns:minmax(520px,1.12fr) minmax(455px,.98fr) minmax(285px,.56fr);
      gap:9px;
      align-items:stretch;
    }

    /* High-energy blue sci-fi panels */
    .card,
    .sideCard,
    .tableCard,
    .ordersDetails{
      border:1px solid rgba(24,155,231,.92) !important;
      border-radius:12px;
      background:
        linear-gradient(150deg,rgba(5,31,56,.985),rgba(2,16,32,.975)),
        radial-gradient(460px 190px at 100% 0%,rgba(0,135,255,.105),transparent 65%);
      box-shadow:
        inset 0 1px 0 rgba(124,229,255,.055),
        inset 0 0 42px rgba(0,134,228,.045),
        0 0 0 1px rgba(34,176,255,.055),
        0 0 17px rgba(0,139,255,.14),
        0 17px 38px rgba(0,0,0,.32);
    }
    .card:before{
      opacity:.62;
      background:
        linear-gradient(122deg,rgba(46,218,255,.045),transparent 28%,rgba(21,111,255,.026)),
        linear-gradient(90deg,rgba(33,166,255,.14),transparent 1px) 0 0/74px 100%;
    }
    .card:after,
    .sideCard:before{
      box-shadow:
        inset 0 0 0 1px rgba(88,222,255,.075),
        inset 0 0 28px rgba(48,171,255,.045);
    }
    .card:hover,
    .sideCard:hover{
      border-color:#43d4ff !important;
      box-shadow:
        inset 0 1px 0 rgba(130,238,255,.08),
        0 0 0 1px rgba(78,218,255,.16),
        0 0 25px rgba(35,196,255,.23),
        0 0 52px rgba(0,105,255,.12),
        0 20px 42px rgba(0,0,0,.34);
    }
    .cardHead::after,
    .tableHead::after{
      bottom:-8px;
      background:linear-gradient(90deg,rgba(51,209,255,0),rgba(55,218,255,.95),rgba(26,138,255,.48),rgba(51,209,255,0));
      box-shadow:0 0 13px rgba(50,205,255,.60);
      opacity:.82;
    }

    /* Title icons more like illuminated HUD modules */
    .titleIcon{
      width:35px;
      height:35px;
      border-color:#2ab9ee;
      background:linear-gradient(180deg,#08628a,#07324b);
      color:#8bf8ff;
      box-shadow:
        inset 0 0 16px rgba(76,237,255,.10),
        0 0 14px rgba(0,177,255,.23);
      text-shadow:0 0 10px rgba(91,236,255,.72);
    }

    /* Brighter controls without changing behavior */
    .btn{
      border-color:#2f7cae;
      background:linear-gradient(180deg,rgba(9,49,84,.98),rgba(4,26,50,.98));
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.055),
        0 0 0 1px rgba(28,124,188,.04),
        0 7px 18px rgba(0,0,0,.16);
    }
    .btn:hover{
      border-color:#53dcff;
      box-shadow:
        0 0 17px rgba(58,211,255,.25),
        0 0 32px rgba(0,119,255,.12),
        0 9px 25px rgba(0,0,0,.24);
    }
    .btn.active{
      border-color:#38e7ff;
      background:linear-gradient(180deg,rgba(10,102,155,.96),rgba(5,49,91,.98));
      box-shadow:
        inset 0 0 0 1px rgba(77,229,255,.38),
        0 0 17px rgba(37,218,255,.42),
        0 0 34px rgba(0,110,255,.15);
    }
    .heroControls .start{
      border-color:#2bf5bc;
      background:linear-gradient(135deg,#078a68,#0aaa74);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.10),
        0 0 19px rgba(16,237,169,.34),
        0 0 38px rgba(0,141,255,.09);
    }
    .heroControls .stop{
      border-color:#f0b232;
      background:linear-gradient(135deg,#80540d,#a47112);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.075),
        0 0 18px rgba(255,180,38,.28),
        0 0 34px rgba(0,111,255,.06);
    }
    .heroControls .danger{
      border-color:#ff5176;
      background:linear-gradient(135deg,#901737,#c31b49);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.08),
        0 0 21px rgba(255,50,100,.30),
        0 0 39px rgba(0,116,255,.07);
    }

    /* Online/status glow */
    .banner.good{
      border-color:#20c998;
      background:
        linear-gradient(180deg,rgba(7,91,66,.98),rgba(3,56,43,.98)),
        radial-gradient(300px 70px at 75% 0%,rgba(65,255,207,.10),transparent 70%);
      box-shadow:
        inset 0 0 19px rgba(58,255,200,.045),
        0 0 16px rgba(24,220,162,.20),
        0 0 34px rgba(0,133,255,.075);
    }

    /* Right rail */
    .sideRail{gap:8px}
    .sideCard{
      box-shadow:
        inset 0 1px 0 rgba(123,228,255,.05),
        inset 0 0 30px rgba(0,127,219,.04),
        0 0 18px rgba(0,140,255,.13),
        0 14px 34px rgba(0,0,0,.30);
    }
    .exportBtn{
      border-color:#45dbff;
      background:linear-gradient(180deg,#0c90d1,#08669d);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.12),
        0 0 18px rgba(45,207,255,.31),
        0 0 34px rgba(0,114,255,.13);
    }

    /* Stronger lower terminal section */
    .tableCard{
      margin-top:9px;
      border-color:#1aa7ef !important;
      box-shadow:
        inset 0 1px 0 rgba(101,224,255,.055),
        inset 0 0 36px rgba(0,123,216,.045),
        0 0 0 1px rgba(39,190,255,.07),
        0 0 21px rgba(0,134,255,.15),
        0 20px 42px rgba(0,0,0,.31);
    }
    .tableHead{
      padding:11px 14px 9px;
      background:
        linear-gradient(180deg,rgba(7,53,89,.88),rgba(3,28,51,.60)),
        radial-gradient(380px 90px at 40% 0%,rgba(0,144,255,.09),transparent 72%);
    }
    th{
      background:linear-gradient(180deg,#062c49,#041c30);
      box-shadow:
        inset 0 -1px 0 rgba(54,196,255,.13),
        0 2px 8px rgba(0,0,0,.14);
    }
    tbody tr:nth-child(even){background:rgba(5,34,58,.33)}
    tbody tr:hover{
      background:linear-gradient(90deg,rgba(9,93,143,.48),rgba(4,39,68,.22));
      box-shadow:
        inset 3px 0 0 rgba(58,221,255,.62),
        0 0 19px rgba(0,130,255,.11);
    }
    .filterBtn.active{
      border-color:#3ee8ff;
      color:#f0feff;
      background:linear-gradient(180deg,#0d74ac,#074971);
      box-shadow:
        inset 0 0 11px rgba(55,229,255,.08),
        0 0 14px rgba(43,219,255,.39),
        0 0 28px rgba(0,109,255,.12);
    }

    /* Ambient bottom/city-floor reflection illusion */
    .ordersDetails::after,
    .tableCard::after{
      content:"";
      pointer-events:none;
      position:absolute;
      left:8%;
      right:8%;
      bottom:-26px;
      height:30px;
      background:radial-gradient(ellipse at center,rgba(12,157,255,.18),transparent 70%);
      filter:blur(8px);
      opacity:.68;
    }

    @media(max-width:1240px){
      .wrap{padding-left:20px}
      .wrap::after{display:none}
      .topbar:after{right:18%}
      .mainGrid{grid-template-columns:1.08fr .92fr}
    }
    @media(max-width:850px){
      .wrap{padding:10px 9px 30px}
      .topbar{min-height:0}
      .topbar:after{display:none}
    }
</style></head><body><div class="card"><div class="logo">📈</div><h1>Trading Bot</h1><div class="muted">Sikker fjernkontroll for PC og telefon.</div>{% if error %}<div class="err">{{error}}</div>{% endif %}<form method="post"><input name="password" type="password" autocomplete="current-password" placeholder="Dashboard-passord" autofocus required><button type="submit">LOGG INN</button></form></div></body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_REQUIRED:
        session["auth"] = True
        return redirect(url_for("home"))
    error = ""
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        if login_rate_limited(ip):
            error = "For mange feilforsøk. Vent noen minutter."
        else:
            supplied = str(request.form.get("password") or "")
            if hmac.compare_digest(supplied, DASHBOARD_PASSWORD):
                session.clear()
                session["auth"] = True
                session.permanent = True
                target = request.args.get("next") or "/"
                if not target.startswith("/") or target.startswith("//"):
                    target = "/"
                return redirect(target)
            record_login_failure(ip)
            error = "Feil passord."
    return render_template_string(LOGIN_HTML, error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


HTML = r"""\n<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <meta name="theme-color" content="#04111f"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/>
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
  <link rel="manifest" href="/manifest.webmanifest"/>
  <link rel="apple-touch-icon" href="/static/icon-192.png"/>
  <title>Trading Bot</title>
  <style>
    :root{
      --bg:#010812;
      --bg2:#031222;
      --panel:rgba(3,18,35,.92);
      --panel2:rgba(5,28,50,.82);
      --panel3:rgba(8,39,68,.62);
      --line:#0b5d9b;
      --line2:#0b8fe0;
      --line3:#22c8ff;
      --text:#f4fbff;
      --muted:#78a8c8;
      --blue:#168cff;
      --cyan:#25dcff;
      --cyan2:#7af2ff;
      --teal:#1bf0c4;
      --green:#20e7a3;
      --amber:#ffc34a;
      --red:#ff436a;
      --purple:#9f7cff;
      --shadow:0 22px 60px rgba(0,0,0,.44);
      --glow:0 0 0 1px rgba(19,148,255,.16),0 0 22px rgba(0,145,255,.14);
      --glow-strong:0 0 0 1px rgba(53,193,255,.30),0 0 18px rgba(0,148,255,.24),0 0 42px rgba(0,106,255,.12);
    }

    *{box-sizing:border-box}
    html{background:var(--bg)}
    body{
      margin:0;
      min-height:100vh;
      color:var(--text);
      font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      background:
        radial-gradient(1100px 520px at 54% -80px,rgba(0,125,255,.25),transparent 64%),
        radial-gradient(760px 470px at 101% 6%,rgba(30,105,255,.15),transparent 58%),
        radial-gradient(680px 440px at -5% 50%,rgba(0,205,255,.08),transparent 60%),
        linear-gradient(180deg,#020d19 0%,#020a13 44%,#01070d 100%);
      -webkit-tap-highlight-color:transparent;
      overflow-x:hidden;
    }
    body:before{
      content:"";
      position:fixed;
      inset:0;
      pointer-events:none;
      z-index:0;
      opacity:.34;
      background-image:
        linear-gradient(rgba(36,135,196,.08) 1px,transparent 1px),
        linear-gradient(90deg,rgba(36,135,196,.06) 1px,transparent 1px);
      background-size:42px 42px;
      mask-image:linear-gradient(to bottom,#000 0%,rgba(0,0,0,.72) 58%,transparent 100%);
    }
    body:after{
      content:"";
      position:fixed;
      pointer-events:none;
      z-index:0;
      width:760px;
      height:360px;
      left:50%;
      top:-210px;
      transform:translateX(-50%);
      border-radius:50%;
      border:1px solid rgba(41,186,255,.12);
      box-shadow:
        0 0 90px rgba(0,130,255,.16),
        inset 0 0 70px rgba(17,149,255,.07);
      background:
        repeating-radial-gradient(circle at 50% 100%,rgba(33,172,255,.08) 0 1px,transparent 1px 28px),
        repeating-linear-gradient(90deg,transparent 0 39px,rgba(35,165,255,.05) 40px 41px);
    }

    .wrap{
      max-width:1580px;
      margin:0 auto;
      padding:17px 22px 44px;
      position:relative;
      z-index:1;
    }

    .topbar{
      position:sticky;
      top:0;
      z-index:20;
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:18px;
      padding:11px 12px 15px;
      margin:0 -2px;
      border:1px solid rgba(16,112,176,.28);
      border-top:0;
      border-radius:0 0 18px 18px;
      background:
        linear-gradient(180deg,rgba(1,12,25,.97),rgba(2,18,34,.88)),
        radial-gradient(500px 130px at 55% 0%,rgba(0,123,255,.14),transparent 70%);
      backdrop-filter:blur(20px) saturate(145%);
      box-shadow:
        0 16px 45px rgba(0,0,0,.30),
        inset 0 -1px 0 rgba(40,187,255,.10);
      overflow:hidden;
    }
    .topbar:before{
      content:"";
      position:absolute;
      inset:auto 30% 0 24%;
      height:54px;
      pointer-events:none;
      opacity:.32;
      background:
        linear-gradient(165deg,transparent 0 11%,rgba(36,196,255,.9) 12% 13%,transparent 14% 22%,rgba(26,146,255,.65) 23% 24%,transparent 25% 38%,rgba(60,224,255,.72) 39% 40%,transparent 41% 58%,rgba(41,169,255,.62) 59% 60%,transparent 61%);
      filter:drop-shadow(0 0 8px rgba(30,180,255,.45));
      clip-path:polygon(0 84%,7% 66%,15% 74%,23% 41%,31% 52%,39% 25%,48% 48%,57% 30%,66% 57%,74% 34%,84% 52%,92% 26%,100% 40%,100% 100%,0 100%);
    }
    .topbar>*{position:relative;z-index:2}

    .brand{display:flex;align-items:center;gap:15px;min-width:0}
    .brandMark{
      width:58px;
      height:58px;
      border:1px solid #1a9ae0;
      border-radius:18px;
      display:grid;
      place-items:center;
      background:
        linear-gradient(145deg,rgba(13,91,142,.82),rgba(3,22,42,.96));
      box-shadow:
        inset 0 0 28px rgba(38,211,255,.15),
        0 0 0 1px rgba(32,209,255,.12),
        0 0 26px rgba(0,157,255,.25);
      font-weight:950;
      font-size:18px;
      color:#74f5ff;
      text-shadow:0 0 15px rgba(51,220,255,.75);
      letter-spacing:-1px;
    }
    .brandCopy{min-width:0}
    .brandTitleRow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
    h1{
      font-size:30px;
      line-height:1;
      margin:0;
      letter-spacing:-.75px;
      text-shadow:0 0 24px rgba(59,175,255,.18);
    }
    .revBadge{
      font-size:10px;
      font-weight:900;
      color:#a6f9ff;
      border:1px solid #0d89c9;
      background:linear-gradient(180deg,rgba(5,70,104,.86),rgba(3,38,62,.92));
      border-radius:999px;
      padding:5px 10px;
      letter-spacing:.055em;
      box-shadow:0 0 16px rgba(0,144,255,.16);
    }
    .sub{color:var(--muted);font-size:11px;line-height:1.5}
    .sub strong{color:#c9f4ff}
    .metaLine{display:flex;gap:9px;flex-wrap:wrap;margin-top:5px}
    .metaDot:before{
      content:"";
      display:inline-block;
      width:6px;
      height:6px;
      background:var(--teal);
      border-radius:50%;
      margin:0 7px 1px 0;
      box-shadow:0 0 12px var(--teal);
    }

    .navActions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
    .systemBox{
      display:flex;
      gap:9px;
      align-items:center;
      padding:9px 13px;
      border:1px solid #176395;
      border-radius:12px;
      background:linear-gradient(180deg,rgba(7,34,58,.90),rgba(4,23,40,.84));
      box-shadow:inset 0 0 18px rgba(0,151,255,.06),0 0 18px rgba(0,151,255,.07);
    }
    .systemBox .dot{margin-right:2px}
    .sysLabel{font-size:8px;color:#6d9dbd;letter-spacing:.13em}
    .sysValue{font-size:12px;font-weight:950}
    .runChip{
      padding:7px 10px;
      border-radius:999px;
      font-size:10px;
      font-weight:950;
      letter-spacing:.05em;
      border:1px solid;
      backdrop-filter:blur(8px);
    }
    .runChip.on{color:#71ffd5;border-color:#19aa7f;background:rgba(3,54,42,.83);box-shadow:0 0 14px rgba(25,215,159,.12)}
    .runChip.off{color:#ffe39a;border-color:#9a7627;background:rgba(61,44,6,.84)}
    .dot{width:10px;height:10px;border-radius:50%;background:#71809d}
    .dot.ok{background:#20efa4;box-shadow:0 0 6px #20efa4,0 0 17px rgba(32,239,164,.75);animation:pulseGlow 2s ease-in-out infinite}
    .dot.bad{background:#ff5579;box-shadow:0 0 14px #ff557955}
    .loginTag{font-size:11px;color:#9ec4dc}

    .btn{
      appearance:none;
      border:1px solid #246b9f;
      background:linear-gradient(180deg,rgba(10,43,72,.97),rgba(5,25,47,.97));
      color:#f0fbff;
      padding:11px 15px;
      border-radius:10px;
      cursor:pointer;
      font-weight:850;
      min-height:43px;
      transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease,filter .16s ease;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 5px 18px rgba(0,0,0,.12);
    }
    .btn:hover{
      border-color:#34c8ff;
      transform:translateY(-1px);
      box-shadow:0 8px 24px rgba(0,0,0,.22),0 0 18px rgba(19,179,255,.17);
      filter:brightness(1.07);
    }
    .btn:active{transform:translateY(0)}
    .btn.ghost{background:linear-gradient(180deg,rgba(8,31,56,.86),rgba(4,20,37,.88));text-decoration:none}
    .btn.active{
      border-color:#29dfff;
      color:#e3fbff;
      background:linear-gradient(180deg,rgba(10,82,126,.88),rgba(5,42,75,.94));
      box-shadow:inset 0 0 0 1px rgba(39,223,255,.36),0 0 17px rgba(39,223,255,.24);
    }

    .installHelp{
      display:none;
      background:linear-gradient(180deg,#0b3152,#09243e);
      border:1px solid #2787bd;
      border-radius:12px;
      padding:10px 12px;
      margin:10px 0;
      color:#def6ff;
      font-size:13px;
      box-shadow:var(--glow);
    }

    .kpiStrip{
      display:grid;
      grid-template-columns:repeat(7,minmax(0,1fr));
      gap:9px;
      margin:12px 0 13px;
    }
    .kpiCard{
      min-width:0;
      position:relative;
      overflow:hidden;
      padding:12px 13px;
      border:1px solid rgba(23,126,190,.72);
      border-radius:12px;
      background:
        linear-gradient(145deg,rgba(6,34,60,.96),rgba(3,19,37,.92)),
        radial-gradient(180px 70px at 100% 0%,rgba(31,172,255,.13),transparent 70%);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.035),
        inset 0 0 30px rgba(0,104,188,.045),
        0 0 0 1px rgba(0,91,160,.06),
        0 8px 24px rgba(0,0,0,.14);
      transition:.18s ease;
    }
    .kpiCard:hover{
      border-color:#22bfff;
      transform:translateY(-1px);
      box-shadow:var(--glow-strong);
    }
    .kpiCard:after{
      content:"";
      position:absolute;
      right:-18px;
      bottom:-20px;
      width:95px;
      height:57px;
      opacity:.42;
      background:
        linear-gradient(155deg,transparent 0 10%,rgba(38,211,255,.72) 11% 12%,transparent 13% 28%,rgba(22,143,255,.65) 29% 30%,transparent 31% 44%,rgba(50,221,255,.58) 45% 46%,transparent 47%);
      clip-path:polygon(0 86%,14% 65%,26% 76%,39% 30%,51% 56%,65% 23%,78% 46%,90% 13%,100% 34%,100% 100%,0 100%);
      filter:drop-shadow(0 0 5px rgba(25,190,255,.55));
      pointer-events:none;
    }
    .kpiHead{
      display:flex;
      align-items:center;
      gap:8px;
      color:#89b9d6;
      font-size:9px;
      text-transform:uppercase;
      letter-spacing:.075em;
    }
    .kpiIcon{
      width:29px;
      height:29px;
      display:grid;
      place-items:center;
      border-radius:8px;
      color:#6eefff;
      background:linear-gradient(180deg,rgba(8,71,104,.88),rgba(4,37,60,.90));
      border:1px solid #176f94;
      font-size:13px;
      box-shadow:inset 0 0 13px rgba(46,229,255,.08),0 0 12px rgba(0,154,255,.06);
    }
    .kpiValue{
      font-weight:950;
      font-size:18px;
      margin-top:7px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
      text-shadow:0 0 16px rgba(83,235,255,.08);
    }
    .kpiValue.pos{color:#3df2c1;text-shadow:0 0 14px rgba(61,242,193,.16)}
    .kpiValue.neg{color:#ff708d}
    .kpiValue.warn{color:#ffd16f}

    .mainGrid{
      display:grid;
      grid-template-columns:minmax(520px,1.08fr) minmax(430px,.95fr) 270px;
      gap:11px;
      align-items:stretch;
    }
    .card{
      background:
        linear-gradient(155deg,rgba(5,28,50,.95),rgba(2,15,30,.94)),
        radial-gradient(420px 180px at 100% 0%,rgba(0,133,255,.09),transparent 65%);
      border:1px solid rgba(16,112,177,.78);
      border-radius:14px;
      padding:15px;
      box-shadow:
        var(--shadow),
        inset 0 1px 0 rgba(255,255,255,.03),
        inset 0 0 35px rgba(0,122,214,.035);
      position:relative;
      overflow:hidden;
    }
    .card:before{
      content:"";
      position:absolute;
      inset:0;
      pointer-events:none;
      background:
        linear-gradient(120deg,rgba(39,205,255,.035),transparent 30%,rgba(25,124,255,.018)),
        linear-gradient(90deg,rgba(33,166,255,.12),transparent 1px) 0 0/78px 100%;
      opacity:.45;
    }
    .card:after{
      content:"";
      position:absolute;
      pointer-events:none;
      inset:0;
      border-radius:inherit;
      box-shadow:inset 0 0 0 1px rgba(38,194,255,.045);
    }
    .card>*{position:relative}
    .card h2{margin:0;font-size:20px;letter-spacing:-.3px;text-shadow:0 0 18px rgba(96,201,255,.10)}
    .card h3{margin:14px 0 8px;font-size:11px;color:#bfe3f4;text-transform:uppercase;letter-spacing:.075em}
    .cardHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
    .cardTitle{display:flex;align-items:center;gap:10px}
    .titleIcon{
      width:33px;
      height:33px;
      border-radius:9px;
      display:grid;
      place-items:center;
      color:#69f3ff;
      border:1px solid #1888b3;
      background:linear-gradient(180deg,#07526d,#073047);
      box-shadow:inset 0 0 16px rgba(70,244,221,.08),0 0 14px rgba(0,158,255,.10);
    }
    .cardHint{font-size:10px;color:#6f9cb9;margin-left:43px;margin-top:-9px;margin-bottom:12px}

    .heroControls{display:grid;grid-template-columns:1fr 1fr;gap:9px}
    .heroControls .btn{
      min-height:61px;
      font-size:15px;
      border-radius:11px;
      letter-spacing:.01em;
    }
    .heroControls .start{
      background:linear-gradient(135deg,#087f61,#09a46d);
      border-color:#20e8ab;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 0 22px rgba(6,225,150,.18);
    }
    .heroControls .start:hover{box-shadow:0 0 24px rgba(6,225,150,.28),inset 0 1px 0 rgba(255,255,255,.10)}
    .heroControls .stop{
      background:linear-gradient(135deg,#77500d,#9a6d13);
      border-color:#e4aa2c;
      color:#fff5dc;
      box-shadow:0 0 18px rgba(244,174,33,.10);
    }
    .heroControls .danger{
      grid-column:1/-1;
      background:linear-gradient(135deg,#81152f,#b01a40);
      border-color:#ff5274;
      box-shadow:0 0 23px rgba(255,44,91,.14),inset 0 1px 0 rgba(255,255,255,.06);
    }
    .heroControls .danger:hover{box-shadow:0 0 28px rgba(255,44,91,.25)}
    .heroSub{display:block;font-size:10px;font-weight:650;opacity:.78;margin-top:3px}

    .controlSection{padding-top:2px}
    .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
    .segmented.two{grid-template-columns:repeat(2,minmax(0,180px))}
    .segmented .btn{width:100%;min-height:47px}
    .btn.good{
      background:linear-gradient(180deg,#07845d,#075440);
      border-color:#18be87;
      box-shadow:0 0 14px rgba(26,218,151,.08);
    }
    .profileRow .btn{min-width:135px}

    input,select{
      background:linear-gradient(180deg,#05192d,#031321);
      color:#edfaff;
      border:1px solid #286d9c;
      border-radius:9px;
      padding:11px 12px;
      font-size:14px;
      min-height:42px;
      outline:none;
      box-shadow:inset 0 0 14px rgba(0,98,164,.04);
    }
    input:focus,select:focus{
      border-color:#28d8ff;
      box-shadow:0 0 0 2px rgba(39,223,255,.12),0 0 14px rgba(39,223,255,.08);
    }
    .strategyRow{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:7px}
    .strategyRow select{width:100%}

    details.utility{
      margin-top:12px;
      border:1px solid #16527a;
      border-radius:10px;
      background:linear-gradient(180deg,rgba(5,28,48,.92),rgba(3,19,34,.92));
      overflow:hidden;
    }
    .utility summary{
      list-style:none;
      cursor:pointer;
      padding:11px 12px;
      font-weight:850;
      color:#c4e7f8;
      font-size:11px;
      display:flex;
      justify-content:space-between;
    }
    .utility summary::-webkit-details-marker{display:none}
    .utility summary:after{content:"＋";color:#52d9ec}
    .utility[open] summary:after{content:"−"}
    .utilityBody{padding:0 11px 12px;border-top:1px solid #123d59}
    .utilityBody .row{margin-top:10px}
    .muted{color:#739db7;font-size:10px;line-height:1.45}

    .banner{
      padding:12px 13px;
      border-radius:10px;
      margin-bottom:12px;
      font-weight:900;
      font-size:13px;
      box-shadow:inset 0 0 20px rgba(255,255,255,.02);
    }
    .banner.bad{background:linear-gradient(180deg,#531528,#3c1020);border:1px solid #a43756}
    .banner.good{
      background:linear-gradient(180deg,rgba(5,78,58,.96),rgba(3,49,39,.96));
      border:1px solid #20a77c;
      color:#caffef;
      box-shadow:0 0 18px rgba(24,220,162,.08),inset 0 0 18px rgba(41,255,194,.03);
    }
    .banner.warn{background:#49370e;border:1px solid #85661c;color:#ffe6a0}

    .statusGrid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
    .statusItem{
      display:grid;
      grid-template-columns:118px minmax(0,1fr);
      gap:8px;
      align-items:center;
      min-height:33px;
      border-bottom:1px solid rgba(27,86,122,.38);
      font-size:11px;
    }
    .statusItem.wide{grid-column:1/-1;grid-template-columns:118px minmax(0,1fr)}
    .statusLabel{color:#7faac4}
    .statusVal{min-width:0;overflow:hidden;text-overflow:ellipsis}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    .small{font-size:10px}
    .pill{
      display:inline-block;
      padding:3px 8px;
      border-radius:999px;
      font-weight:900;
      font-size:9px;
    }
    .pill.good{background:#063c2f;color:#55ffc1;border:1px solid #14795d;box-shadow:0 0 10px rgba(30,242,172,.06)}
    .pill.bad{background:#461425;color:#ff83a3;border:1px solid #7d2440}
    .pill.neutral{background:#102f4d;color:#bdeaff;border:1px solid #2b6687}
    .lastId{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

    .sideRail{display:grid;gap:11px}
    .sideCard{
      position:relative;
      overflow:hidden;
      background:
        linear-gradient(155deg,rgba(5,29,51,.97),rgba(2,16,31,.95)),
        radial-gradient(180px 110px at 100% 0%,rgba(24,140,255,.09),transparent 70%);
      border:1px solid rgba(22,118,179,.78);
      border-radius:14px;
      padding:13px;
      box-shadow:var(--shadow),inset 0 0 24px rgba(0,122,212,.035);
    }
    .sideCard:before{
      content:"";
      position:absolute;
      pointer-events:none;
      inset:0;
      border-radius:inherit;
      box-shadow:inset 0 0 0 1px rgba(43,202,255,.05);
    }
    .sideCard:hover{border-color:#218bca}
    .sideTitle{display:flex;align-items:center;gap:8px;font-weight:950;font-size:15px;margin-bottom:8px}
    .sideTitle .accent{color:#ffd164}
    .profitRow{
      display:grid;
      grid-template-columns:62px 1fr 15px;
      gap:8px;
      align-items:center;
      padding:11px 0;
      border-bottom:1px solid rgba(30,78,108,.52);
      cursor:pointer;
      user-select:none;
      transition:.15s ease;
    }
    .profitRow:hover{background:linear-gradient(90deg,rgba(8,65,102,.32),transparent)}
    .profitLabel{color:#9bbbd0;font-size:10px}
    .profitValue{font-weight:950;text-align:right;font-size:17px}
    .profitValue.pos{color:#3ff2c0;text-shadow:0 0 12px rgba(63,242,192,.10)}
    .profitValue.neg{color:#ff708d}
    .profitValue.neutral{color:#d9ecf6}
    .profitPct{display:block;font-weight:650;font-size:9px;color:#7898af;margin-top:2px}
    .profitChev{color:#7092aa;font-size:10px}
    .profitDetails{display:none;padding:5px 0 8px;border-bottom:1px solid #183f5a}
    .profitDetails.open{display:block}
    .profitTrade{padding:7px 2px;border-bottom:1px dashed #17445f}
    .profitTradeTop{display:flex;justify-content:space-between;gap:7px;font-size:10px}
    .profitTradeSym{font-weight:900}
    .profitTradePnl{font-weight:900;color:#58f1bf}
    .profitTradeMeta,.profitFoot{font-size:9px;color:#719ab2;line-height:1.35;margin-top:6px}
    .profitTradeSummary,.profitLoading,.profitEmpty{font-size:10px;color:#91b4ca;padding:7px 2px}
    .exportBox select{width:100%;margin:4px 0 8px}
    .exportBtn{
      width:100%;
      min-height:43px;
      padding:9px 10px;
      background:linear-gradient(180deg,#0b7fbd,#075a91);
      border:1px solid #2fd4ff;
      border-radius:9px;
      color:#fff;
      font-weight:950;
      cursor:pointer;
      box-shadow:0 0 18px rgba(34,186,240,.13),inset 0 1px 0 rgba(255,255,255,.08);
    }
    .exportBtn:hover{box-shadow:0 0 24px rgba(34,186,240,.28)}

    .sectionSpace{margin-top:11px}
    .tableCard{
      padding:0;
      overflow:hidden;
      border-color:#1777ae;
      background:linear-gradient(180deg,rgba(4,26,46,.96),rgba(2,14,27,.96));
    }
    .tableHead{
      display:flex;
      justify-content:space-between;
      gap:12px;
      align-items:center;
      padding:12px 14px 10px;
      border-bottom:1px solid #16527a;
      background:linear-gradient(180deg,rgba(5,38,65,.72),rgba(4,26,46,.42));
    }
    .tableTitle{display:flex;align-items:center;gap:9px}
    .filterArea{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
    .filterBtn{
      border:1px solid #236b97;
      background:linear-gradient(180deg,#08233b,#06192b);
      color:#9fc4da;
      padding:7px 11px;
      border-radius:8px;
      font-size:9px;
      font-weight:900;
      cursor:pointer;
      transition:.15s ease;
    }
    .filterBtn:hover{border-color:#2ecfff;color:#e8fbff}
    .filterBtn.active{
      border-color:#27ddff;
      color:#e8fdff;
      background:linear-gradient(180deg,#0c6095,#073e69);
      box-shadow:0 0 15px rgba(37,216,255,.22),inset 0 0 12px rgba(37,216,255,.08);
    }
    .searchBox{
      min-height:33px!important;
      width:190px!important;
      padding:7px 10px!important;
      font-size:10px!important;
      background:#04192a!important;
    }
    .tableWrap{overflow:auto;max-width:100%;-webkit-overflow-scrolling:touch}
    table{width:100%;border-collapse:collapse;font-size:10px;min-width:1020px}
    th,td{
      padding:8px 9px;
      border-bottom:1px solid rgba(19,66,97,.46);
      text-align:left;
      white-space:nowrap;
    }
    th{
      color:#72a7c5;
      font-size:8px;
      text-transform:uppercase;
      letter-spacing:.07em;
      background:linear-gradient(180deg,#05223a,#04192b);
      position:sticky;
      top:0;
      z-index:2;
    }
    tbody tr:nth-child(even){background:rgba(5,31,51,.26)}
    tbody tr{transition:.13s}
    tbody tr:hover{
      background:linear-gradient(90deg,rgba(8,74,111,.36),rgba(4,36,61,.18));
      box-shadow:inset 3px 0 0 rgba(38,212,255,.32);
    }
    .symName{color:#f1fbff}
    .marketBadge{
      padding:3px 7px;
      border-radius:6px;
      font-size:8px;
      font-weight:900;
      background:#112b43;
      color:#b4d4e7;
    }
    .marketBadge.crypto{
      color:#ffe18e;
      border:1px solid #89681f;
      background:linear-gradient(180deg,#473510,#302407);
      box-shadow:0 0 8px rgba(255,197,50,.05);
    }
    .marketBadge.stock{
      color:#8ff2ff;
      border:1px solid #1f7392;
      background:linear-gradient(180deg,#0b3c50,#082d3c);
    }
    .edgeCell{color:#a6cee1}
    .pos{color:#53f0bd!important}
    .neg{color:#ff617f!important}

    .ordersDetails{
      margin-top:11px;
      border:1px solid #176292;
      border-radius:12px;
      background:linear-gradient(180deg,#041b31,#031522);
      overflow:hidden;
      box-shadow:var(--shadow);
    }
    .ordersDetails summary{
      cursor:pointer;
      list-style:none;
      padding:12px 15px;
      font-size:11px;
      font-weight:900;
      color:#b2d8ea;
    }
    .ordersDetails summary::-webkit-details-marker{display:none}
    .ordersDetails summary:after{content:" Vis";float:right;color:#52d9ec}
    .ordersDetails[open] summary:after{content:" Skjul"}
    .ordersDetails .tableHead{padding-top:9px}
    .statusNotes{margin-top:10px;padding-top:9px;border-top:1px solid #173f59}

    @keyframes pulseGlow{
      0%,100%{filter:brightness(1);box-shadow:0 0 5px #20efa4,0 0 14px rgba(32,239,164,.55)}
      50%{filter:brightness(1.18);box-shadow:0 0 8px #20efa4,0 0 23px rgba(32,239,164,.75)}
    }

    @media(max-width:1240px){
      .mainGrid{grid-template-columns:1.08fr .92fr}
      .sideRail{grid-column:1/-1;grid-template-columns:1fr 1fr}
      .kpiStrip{grid-template-columns:repeat(4,1fr)}
    }
    @media(max-width:850px){
      .wrap{padding:10px 9px 30px}
      .topbar{position:relative;align-items:flex-start;border-radius:0 0 14px 14px}
      .brandMark{width:46px;height:46px;border-radius:13px}
      .brandTitleRow h1{font-size:23px}
      .navActions{width:100%;justify-content:flex-start}
      .mainGrid{grid-template-columns:1fr}
      .sideRail{grid-column:auto;grid-template-columns:1fr}
      .kpiStrip{grid-template-columns:repeat(2,1fr)}
      .statusGrid{grid-template-columns:1fr}
      .statusItem.wide{grid-column:auto}
      .heroControls .btn{min-height:55px}
      .strategyRow{grid-template-columns:1fr}
      .segmented.two{grid-template-columns:1fr 1fr}
      .tableHead{align-items:flex-start;flex-direction:column}
      .filterArea{width:100%}
      .searchBox{width:100%!important}
      .card{padding:12px}
      .sub.desktopOnly{display:none}
      .topbar:before{display:none}
    }
    @media(max-width:480px){
      .kpiStrip{grid-template-columns:1fr 1fr}
      .kpiCard{padding:10px}
      .kpiValue{font-size:15px}
      .segmented{grid-template-columns:1fr}
      .segmented.two{grid-template-columns:1fr 1fr}
      .heroControls{grid-template-columns:1fr}
      .heroControls .danger{grid-column:auto}
      .systemBox{display:none}
      .navActions .btn{padding:9px 10px;min-height:38px}
      .statusItem{grid-template-columns:105px 1fr}
      .profileRow .btn{min-width:0;flex:1}
    }
    @media(prefers-reduced-motion:reduce){
      *,*:before,*:after{animation:none!important;transition:none!important}
    }
  
    /* ===== FORSLAG 4 EXACT DESKTOP V4 - VISUAL/LAYOUT ONLY ===== */
    html,body{
      width:100vw !important;
      max-width:none !important;
      min-width:0 !important;
    }
    body{
      overflow-x:hidden !important;
      background:
        radial-gradient(1050px 460px at 57% -40px,rgba(0,145,255,.34),transparent 63%),
        radial-gradient(720px 390px at 100% 18%,rgba(0,104,255,.13),transparent 65%),
        radial-gradient(760px 520px at 0% 68%,rgba(0,205,255,.08),transparent 66%),
        linear-gradient(180deg,#010812 0%,#010914 49%,#01050b 100%) !important;
    }
    body:before{
      opacity:.52 !important;
      background-size:38px 38px !important;
    }

    /* The previous pseudo rail is disabled; this is a real layout element. */
    .wrap::after{display:none !important}
    .hudRailActual{
      position:fixed;
      z-index:60;
      left:14px;
      top:78px;
      bottom:22px;
      width:52px;
      display:flex;
      flex-direction:column;
      align-items:center;
      gap:12px;
      padding:12px 7px;
      color:#78eaff;
      font-size:19px;
      font-weight:950;
      border:1px solid rgba(32,171,244,.88);
      border-radius:15px;
      background:
        linear-gradient(180deg,rgba(4,28,53,.985),rgba(2,15,30,.985)),
        radial-gradient(circle at 50% 0%,rgba(0,164,255,.20),transparent 38%);
      box-shadow:
        inset 0 0 30px rgba(9,161,255,.08),
        0 0 0 1px rgba(62,214,255,.08),
        0 0 26px rgba(0,145,255,.22),
        0 18px 42px rgba(0,0,0,.35);
      text-shadow:0 0 14px rgba(76,224,255,.82);
      pointer-events:none;
    }
    .hudRailActual>div{
      width:36px;
      height:36px;
      flex:0 0 36px;
      display:grid;
      place-items:center;
      border:1px solid transparent;
      border-radius:10px;
      opacity:.85;
    }
    .hudRailActual .hudHome{
      border-color:#34cfff;
      color:#d2fbff;
      background:linear-gradient(180deg,#0d72a9,#07456d);
      box-shadow:
        inset 0 0 13px rgba(91,235,255,.13),
        0 0 15px rgba(35,207,255,.35);
      opacity:1;
    }

    .wrap{
      box-sizing:border-box !important;
      width:calc(100vw - 76px) !important;
      max-width:none !important;
      min-width:0 !important;
      margin:0 0 0 76px !important;
      padding:14px 18px 42px 8px !important;
    }

    .topbar{
      position:relative !important;
      min-height:100px !important;
      margin:0 0 9px !important;
      padding:12px 20px 15px !important;
      border:1px solid rgba(35,170,246,.90) !important;
      border-radius:13px !important;
      background:
        linear-gradient(180deg,rgba(2,15,30,.985),rgba(2,23,44,.945)),
        radial-gradient(720px 180px at 58% 0%,rgba(0,139,255,.20),transparent 72%) !important;
      box-shadow:
        inset 0 1px 0 rgba(123,231,255,.08),
        inset 0 -1px 0 rgba(61,201,255,.18),
        0 0 0 1px rgba(0,102,190,.08),
        0 0 28px rgba(0,139,255,.18),
        0 18px 40px rgba(0,0,0,.28) !important;
      overflow:hidden !important;
    }
    .topbar:before{
      display:none !important;
    }
    .topbar:after{
      display:none !important;
    }
    .brand{z-index:5 !important}
    .navActions{z-index:6 !important}

    .headerHeroArt{
      position:absolute;
      z-index:2;
      inset:0 250px 0 390px;
      overflow:hidden;
      pointer-events:none;
      opacity:.96;
    }
    .heroGrid{
      position:absolute;
      inset:0;
      opacity:.48;
      background-image:
        linear-gradient(rgba(43,177,255,.08) 1px,transparent 1px),
        linear-gradient(90deg,rgba(43,177,255,.07) 1px,transparent 1px);
      background-size:29px 29px;
      mask-image:linear-gradient(90deg,transparent,#000 18%,#000 82%,transparent);
    }
    .heroGlobe{
      position:absolute;
      width:245px;
      height:245px;
      left:46%;
      top:-120px;
      transform:translateX(-50%);
      border-radius:50%;
      border:1px solid rgba(79,218,255,.42);
      background:
        repeating-radial-gradient(circle at 50% 50%,transparent 0 23px,rgba(71,208,255,.10) 24px 25px),
        repeating-linear-gradient(0deg,transparent 0 22px,rgba(67,194,255,.085) 23px 24px),
        repeating-linear-gradient(90deg,transparent 0 30px,rgba(67,194,255,.075) 31px 32px),
        radial-gradient(circle at 42% 38%,rgba(38,180,255,.18),rgba(4,55,98,.06) 58%,transparent 72%);
      box-shadow:
        inset 0 0 45px rgba(42,186,255,.13),
        0 0 44px rgba(0,145,255,.24),
        0 0 78px rgba(0,105,255,.13);
    }
    .heroChart{
      position:absolute;
      left:0;
      right:0;
      bottom:2px;
      width:100%;
      height:74px;
      fill:none;
      stroke:#31cfff;
      stroke-width:2;
      vector-effect:non-scaling-stroke;
      filter:drop-shadow(0 0 6px rgba(49,207,255,.86));
      opacity:.74;
    }
    .headerTagline{
      position:absolute;
      z-index:5;
      right:14px;
      top:10px;
      color:#3dc9ff;
      font-size:8px;
      font-weight:900;
      line-height:1.45;
      letter-spacing:.08em;
      text-align:left;
      opacity:.85;
      text-shadow:0 0 12px rgba(31,185,255,.48);
      pointer-events:none;
      transform:translateX(70px);
    }
    .navActions{
      margin-right:88px;
    }

    .brandMark{
      width:64px !important;
      height:64px !important;
      border-radius:17px !important;
      border-color:#36d2ff !important;
      box-shadow:
        inset 0 0 32px rgba(78,234,255,.18),
        0 0 0 1px rgba(52,210,255,.16),
        0 0 17px rgba(17,193,255,.42),
        0 0 45px rgba(0,102,255,.22) !important;
    }
    h1{font-size:31px !important}

    .kpiStrip{
      grid-template-columns:repeat(7,minmax(0,1fr)) !important;
      gap:8px !important;
      margin:8px 0 9px !important;
    }
    .kpiCard{
      min-height:79px !important;
      padding:12px 13px !important;
      border:1px solid rgba(29,169,243,.96) !important;
      border-radius:10px !important;
      background:
        linear-gradient(145deg,rgba(5,38,69,.99),rgba(2,18,36,.97)),
        radial-gradient(210px 90px at 100% 0%,rgba(39,179,255,.24),transparent 68%) !important;
      box-shadow:
        inset 0 1px 0 rgba(149,237,255,.055),
        inset 0 0 30px rgba(0,134,225,.06),
        0 0 0 1px rgba(26,156,229,.08),
        0 0 18px rgba(0,144,255,.16),
        0 10px 25px rgba(0,0,0,.22) !important;
    }
    .kpiCard:after{
      opacity:.92 !important;
      width:112px !important;
      height:63px !important;
      right:-3px !important;
      bottom:-11px !important;
      filter:drop-shadow(0 0 7px rgba(39,213,255,.82)) !important;
    }
    .kpiValue{font-size:18px !important}

    .mainGrid{
      grid-template-columns:minmax(0,1.10fr) minmax(0,.99fr) 305px !important;
      gap:8px !important;
      align-items:stretch !important;
    }
    .controlCard,.statusCard{min-width:0 !important}
    .sideRail{min-width:0 !important;gap:8px !important}

    .card,.sideCard,.tableCard,.ordersDetails{
      border:1px solid rgba(28,166,242,.98) !important;
      border-radius:11px !important;
      background:
        linear-gradient(150deg,rgba(5,32,58,.99),rgba(2,15,31,.985)),
        radial-gradient(480px 195px at 100% 0%,rgba(0,140,255,.12),transparent 65%) !important;
      box-shadow:
        inset 0 1px 0 rgba(125,232,255,.06),
        inset 0 0 44px rgba(0,140,235,.05),
        0 0 0 1px rgba(39,185,255,.065),
        0 0 20px rgba(0,143,255,.18),
        0 18px 38px rgba(0,0,0,.34) !important;
    }
    .card:hover,.sideCard:hover{
      border-color:#4cddff !important;
      box-shadow:
        inset 0 1px 0 rgba(132,239,255,.09),
        0 0 0 1px rgba(85,225,255,.18),
        0 0 28px rgba(37,201,255,.27),
        0 0 54px rgba(0,108,255,.13),
        0 20px 42px rgba(0,0,0,.35) !important;
    }

    .heroControls .start{
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.12),
        0 0 20px rgba(18,240,171,.39),
        0 0 39px rgba(0,142,255,.10) !important;
    }
    .heroControls .stop{
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.09),
        0 0 20px rgba(255,180,38,.34),
        0 0 37px rgba(0,112,255,.07) !important;
    }
    .heroControls .danger{
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.09),
        0 0 22px rgba(255,48,99,.35),
        0 0 40px rgba(0,117,255,.08) !important;
    }
    .btn.active{
      border-color:#42edff !important;
      box-shadow:
        inset 0 0 0 1px rgba(79,234,255,.42),
        0 0 18px rgba(44,224,255,.46),
        0 0 36px rgba(0,113,255,.16) !important;
    }
    .banner.good{
      box-shadow:
        inset 0 0 21px rgba(58,255,200,.055),
        0 0 19px rgba(24,220,162,.24),
        0 0 35px rgba(0,133,255,.09) !important;
    }

    .tableCard{
      margin-top:8px !important;
      border-color:#20b3f7 !important;
      box-shadow:
        inset 0 1px 0 rgba(106,229,255,.06),
        inset 0 0 38px rgba(0,127,219,.05),
        0 0 0 1px rgba(43,197,255,.08),
        0 0 24px rgba(0,139,255,.18),
        0 20px 43px rgba(0,0,0,.32) !important;
    }
    .tableHead{
      background:
        linear-gradient(180deg,rgba(7,56,94,.91),rgba(3,29,52,.64)),
        radial-gradient(420px 90px at 40% 0%,rgba(0,148,255,.11),transparent 72%) !important;
    }
    th{
      background:linear-gradient(180deg,#07304e,#041d32) !important;
    }

    @media(max-width:1080px){
      .hudRailActual{display:none}
      .wrap{
        width:100vw !important;
        margin-left:0 !important;
        padding:10px 10px 30px !important;
      }
      .headerHeroArt,.headerTagline{display:none}
      .navActions{margin-right:0}
      .mainGrid{
        grid-template-columns:minmax(0,1.05fr) minmax(0,.95fr) !important;
      }
      .sideRail{
        grid-column:1/-1 !important;
        grid-template-columns:1fr 1fr !important;
      }
      .kpiStrip{grid-template-columns:repeat(4,1fr) !important}
    }
    @media(max-width:760px){
      .mainGrid{grid-template-columns:1fr !important}
      .sideRail{grid-column:auto !important;grid-template-columns:1fr !important}
      .kpiStrip{grid-template-columns:repeat(2,1fr) !important}
    }

    /* ===== FORSLAG 4 SINGLE PAGE V5 - VISUAL/LAYOUT ONLY ===== */
    /* Desktop target: one-screen 16:9 dashboard, same functions and DOM. */
    @media (min-width:1081px) and (min-height:720px){
      html,body{
        width:100% !important;
        height:100% !important;
        min-height:100% !important;
        overflow:hidden !important;
      }
      body{
        background:
          radial-gradient(1200px 470px at 55% -70px,rgba(0,146,255,.36),transparent 64%),
          radial-gradient(820px 470px at 100% 18%,rgba(0,107,255,.15),transparent 66%),
          radial-gradient(800px 540px at 0% 70%,rgba(0,211,255,.09),transparent 66%),
          linear-gradient(180deg,#010914 0%,#010a14 51%,#01060c 100%) !important;
      }
      body:before{
        opacity:.55 !important;
        background-size:36px 36px !important;
      }

      .hudRailActual{
        left:8px !important;
        top:10px !important;
        bottom:10px !important;
        width:46px !important;
        padding:10px 5px !important;
        gap:8px !important;
        border-color:#29cfff !important;
        background:
          linear-gradient(180deg,rgba(4,31,58,.99),rgba(2,15,31,.99)),
          radial-gradient(circle at 50% 0%,rgba(0,166,255,.22),transparent 38%) !important;
        box-shadow:
          inset 0 0 30px rgba(17,177,255,.10),
          0 0 0 1px rgba(77,224,255,.10),
          0 0 24px rgba(0,151,255,.32) !important;
      }
      .hudRailActual>div{
        width:34px !important;
        height:34px !important;
        flex-basis:34px !important;
        font-size:16px !important;
      }

      .wrap{
        width:calc(100vw - 60px) !important;
        height:100vh !important;
        max-width:none !important;
        min-width:0 !important;
        margin:0 0 0 58px !important;
        padding:8px 10px 8px 4px !important;
        display:grid !important;
        grid-template-rows:
          minmax(82px,9.5vh)
          minmax(70px,8vh)
          minmax(330px,43vh)
          minmax(210px,1fr)
          30px !important;
        gap:7px !important;
        overflow:hidden !important;
      }

      .topbar{
        height:100% !important;
        min-height:0 !important;
        margin:0 !important;
        padding:9px 15px 10px !important;
        border:1px solid #1ebcff !important;
        border-radius:12px !important;
        background:
          linear-gradient(180deg,rgba(2,16,32,.99),rgba(2,25,47,.96)),
          radial-gradient(760px 180px at 57% 0%,rgba(0,143,255,.24),transparent 72%) !important;
        box-shadow:
          inset 0 1px 0 rgba(137,239,255,.10),
          inset 0 -1px 0 rgba(70,210,255,.20),
          0 0 0 1px rgba(0,111,204,.08),
          0 0 28px rgba(0,151,255,.26),
          0 15px 32px rgba(0,0,0,.30) !important;
      }
      .headerHeroArt{
        inset:0 230px 0 340px !important;
      }
      .heroGlobe{
        width:270px !important;
        height:270px !important;
        top:-138px !important;
        border-color:rgba(87,229,255,.56) !important;
        box-shadow:
          inset 0 0 52px rgba(52,199,255,.16),
          0 0 52px rgba(0,158,255,.35),
          0 0 90px rgba(0,107,255,.18) !important;
      }
      .heroChart{
        height:78px !important;
        stroke:#38d8ff !important;
        stroke-width:2.2 !important;
        filter:drop-shadow(0 0 7px rgba(56,216,255,.96)) !important;
        opacity:.90 !important;
      }
      .heroGrid{opacity:.62 !important}
      .headerTagline{display:none !important}
      .navActions{margin-right:0 !important}
      .brandMark{
        width:55px !important;
        height:55px !important;
        border-color:#42dfff !important;
        box-shadow:
          inset 0 0 30px rgba(83,242,255,.22),
          0 0 0 1px rgba(70,226,255,.18),
          0 0 18px rgba(31,211,255,.52),
          0 0 42px rgba(0,111,255,.24) !important;
      }
      h1{font-size:28px !important}
      .sub{font-size:11px !important}
      .revBadge{font-size:10px !important;padding:4px 8px !important}

      #installHelp{display:none !important}

      .kpiStrip{
        height:100% !important;
        margin:0 !important;
        gap:7px !important;
        grid-template-columns:repeat(7,minmax(0,1fr)) !important;
      }
      .kpiCard{
        height:100% !important;
        min-height:0 !important;
        padding:9px 11px !important;
        border:1px solid #1ebfff !important;
        border-radius:10px !important;
        background:
          linear-gradient(145deg,rgba(5,40,73,.995),rgba(2,20,39,.985)),
          radial-gradient(220px 92px at 100% 0%,rgba(43,190,255,.28),transparent 70%) !important;
        box-shadow:
          inset 0 1px 0 rgba(163,243,255,.07),
          inset 0 0 30px rgba(0,141,233,.08),
          0 0 0 1px rgba(33,180,255,.10),
          0 0 16px rgba(0,156,255,.24) !important;
      }
      .kpiCard:after{
        width:100px !important;
        height:55px !important;
        bottom:-10px !important;
        right:-2px !important;
        opacity:1 !important;
        filter:drop-shadow(0 0 7px rgba(40,221,255,.96)) !important;
      }
      .kpiHead{font-size:9px !important}
      .kpiIcon{
        width:30px !important;
        height:30px !important;
        border-color:#2cc9f5 !important;
        box-shadow:0 0 13px rgba(30,208,255,.24) !important;
      }
      .kpiValue{
        font-size:18px !important;
        margin-top:4px !important;
      }

      .mainGrid{
        height:100% !important;
        min-height:0 !important;
        margin:0 !important;
        grid-template-columns:minmax(0,1.08fr) minmax(0,1.02fr) 300px !important;
        gap:7px !important;
        align-items:stretch !important;
      }
      .mainGrid>.card,
      .sideRail,
      .sideCard{
        min-height:0 !important;
      }
      .sideRail{
        display:grid !important;
        grid-template-columns:1fr !important;
        grid-template-rows:1fr 1fr !important;
        gap:7px !important;
      }
      .card,.sideCard,.tableCard,.ordersDetails{
        border:1px solid #1ec4ff !important;
        border-radius:11px !important;
        background:
          linear-gradient(150deg,rgba(5,34,61,.995),rgba(2,16,32,.99)),
          radial-gradient(520px 210px at 100% 0%,rgba(0,147,255,.15),transparent 65%) !important;
        box-shadow:
          inset 0 1px 0 rgba(137,240,255,.07),
          inset 0 0 44px rgba(0,146,240,.06),
          0 0 0 1px rgba(46,198,255,.08),
          0 0 20px rgba(0,156,255,.23),
          0 14px 30px rgba(0,0,0,.32) !important;
      }
      .card{
        padding:12px !important;
        overflow:hidden !important;
      }
      .cardHead{
        margin-bottom:9px !important;
      }
      .card h2{font-size:19px !important}
      .card h3{
        margin:9px 0 6px !important;
        font-size:10px !important;
      }
      .cardHint{
        margin-left:40px !important;
        margin-top:-8px !important;
        margin-bottom:9px !important;
        font-size:10px !important;
      }
      .titleIcon{
        width:32px !important;
        height:32px !important;
        border-color:#38d9ff !important;
        background:linear-gradient(180deg,#08729b,#07354f) !important;
        box-shadow:
          inset 0 0 15px rgba(87,244,255,.15),
          0 0 15px rgba(28,211,255,.35) !important;
      }

      .heroControls{
        gap:7px !important;
      }
      .heroControls .btn{
        min-height:50px !important;
        padding:7px 10px !important;
        font-size:14px !important;
      }
      .heroControls .start{
        border-color:#35ffc3 !important;
        box-shadow:
          inset 0 1px 0 rgba(255,255,255,.13),
          0 0 20px rgba(25,246,178,.46),
          0 0 36px rgba(0,142,255,.10) !important;
      }
      .heroControls .stop{
        border-color:#ffc536 !important;
        box-shadow:
          inset 0 1px 0 rgba(255,255,255,.10),
          0 0 20px rgba(255,188,45,.40),
          0 0 34px rgba(0,112,255,.07) !important;
      }
      .heroControls .danger{
        min-height:47px !important;
        border-color:#ff5b7e !important;
        box-shadow:
          inset 0 1px 0 rgba(255,255,255,.10),
          0 0 22px rgba(255,55,104,.42),
          0 0 40px rgba(0,117,255,.08) !important;
      }
      .heroSub{font-size:9px !important}

      .segmented{gap:6px !important}
      .segmented .btn{
        min-height:38px !important;
        padding:7px 8px !important;
        font-size:12px !important;
      }
      .segmented.two{grid-template-columns:repeat(2,minmax(0,150px)) !important}
      .strategyRow{
        grid-template-columns:minmax(0,1fr) 165px !important;
        gap:6px !important;
      }
      input,select,.strategyRow .btn{
        min-height:36px !important;
        padding:7px 10px !important;
        font-size:12px !important;
      }
      details.utility{
        margin-top:8px !important;
      }
      .utility summary{
        padding:8px 10px !important;
        font-size:10px !important;
      }

      .banner{
        padding:9px 11px !important;
        margin-bottom:8px !important;
        font-size:12px !important;
      }
      .banner.good{
        border-color:#29e1ad !important;
        box-shadow:
          inset 0 0 21px rgba(58,255,200,.07),
          0 0 20px rgba(24,229,169,.34),
          0 0 35px rgba(0,144,255,.11) !important;
      }
      .statusGrid{
        gap:0 10px !important;
      }
      .statusItem{
        grid-template-columns:105px minmax(0,1fr) !important;
        min-height:28px !important;
        font-size:10px !important;
      }
      .statusItem.wide{
        grid-template-columns:105px minmax(0,1fr) !important;
      }
      .pill{font-size:8px !important;padding:2px 6px !important}
      .statusNotes{
        margin-top:7px !important;
        padding-top:6px !important;
      }

      .sideCard{
        padding:10px !important;
        overflow:hidden !important;
      }
      .sideTitle{
        font-size:14px !important;
        margin-bottom:6px !important;
      }
      .profitRow{
        padding:8px 0 !important;
      }
      .profitValue{font-size:15px !important}
      .profitFoot{font-size:8px !important}
      .exportBox select{margin:3px 0 6px !important}
      .exportBtn{
        min-height:37px !important;
        font-size:11px !important;
        border-color:#4ce9ff !important;
        box-shadow:
          inset 0 1px 0 rgba(255,255,255,.13),
          0 0 20px rgba(40,216,255,.42),
          0 0 35px rgba(0,115,255,.14) !important;
      }

      .tableCard{
        height:100% !important;
        min-height:0 !important;
        margin:0 !important;
        display:grid !important;
        grid-template-rows:auto minmax(0,1fr) !important;
        overflow:hidden !important;
        border-color:#26cfff !important;
        box-shadow:
          inset 0 1px 0 rgba(111,234,255,.07),
          inset 0 0 40px rgba(0,135,226,.06),
          0 0 0 1px rgba(48,208,255,.09),
          0 0 25px rgba(0,152,255,.26),
          0 18px 36px rgba(0,0,0,.32) !important;
      }
      .tableHead{
        padding:9px 12px 7px !important;
        background:
          linear-gradient(180deg,rgba(7,63,104,.95),rgba(3,31,55,.70)),
          radial-gradient(460px 100px at 40% 0%,rgba(0,154,255,.14),transparent 72%) !important;
      }
      .tableTitle .titleIcon{
        width:31px !important;
        height:31px !important;
      }
      .tableTitle h2{font-size:18px !important}
      .filterArea{gap:5px !important}
      .filterBtn{
        padding:6px 9px !important;
        font-size:9px !important;
      }
      .searchBox{
        width:165px !important;
        min-height:30px !important;
        padding:5px 8px !important;
        font-size:9px !important;
      }
      .filterBtn.active{
        border-color:#46efff !important;
        box-shadow:
          inset 0 0 11px rgba(63,234,255,.11),
          0 0 15px rgba(45,226,255,.48),
          0 0 28px rgba(0,113,255,.14) !important;
      }

      .tableCard>.tableWrap{
        min-height:0 !important;
        height:100% !important;
        overflow:auto !important;
        scrollbar-width:thin;
        scrollbar-color:#159edd #031321;
      }
      .symbolsTable{
        min-width:1000px !important;
        font-size:10px !important;
      }
      .symbolsTable th,
      .symbolsTable td{
        padding:5px 8px !important;
        line-height:1.08 !important;
      }
      .symbolsTable th{
        font-size:8px !important;
        background:linear-gradient(180deg,#073656,#041f35) !important;
      }
      .symbolsTable tbody tr:nth-child(even){
        background:rgba(5,36,62,.36) !important;
      }
      .symbolsTable tbody tr:hover{
        background:linear-gradient(90deg,rgba(10,101,154,.54),rgba(4,42,72,.24)) !important;
        box-shadow:
          inset 3px 0 0 rgba(65,229,255,.70),
          0 0 18px rgba(0,135,255,.12) !important;
      }
      .marketBadge{font-size:7px !important;padding:2px 5px !important}

      .ordersDetails{
        height:30px !important;
        min-height:30px !important;
        margin:0 !important;
        overflow:hidden !important;
        border-color:#1599dd !important;
      }
      .ordersDetails summary{
        height:29px !important;
        padding:7px 11px !important;
        font-size:9px !important;
      }
      .ordersDetails[open]{
        position:fixed !important;
        z-index:100 !important;
        left:70px !important;
        right:12px !important;
        bottom:10px !important;
        height:min(360px,45vh) !important;
        background:#031523 !important;
        box-shadow:
          0 0 0 1px rgba(49,205,255,.12),
          0 0 32px rgba(0,155,255,.30),
          0 20px 55px rgba(0,0,0,.55) !important;
      }
      .ordersDetails[open] .tableWrap{
        height:calc(100% - 72px) !important;
        overflow:auto !important;
      }
    }
</style>
</head>
<body>
<!-- FORSLAG4-ACTUAL-HUD -->
<aside class="hudRailActual" aria-hidden="true">
  <div class="hudHome">⌂</div>
  <div>↗</div>
  <div>▥</div>
  <div>⚙</div>
  <div>◉</div>
  <div>▤</div>
  <div>↗</div>
</aside>
<div class="wrap">
  <header class="topbar">
    <div class="brand">
      <div class="brandMark">AI</div>
      <div class="brandCopy">
        <div class="brandTitleRow"><h1>Trading Bot</h1><span class="revBadge">REV {{ bot_version }}</span><span id="headerRunChip" class="runChip off">TRADING PAUSET</span></div>
        <div class="sub desktopOnly">BUILD {{ build_id }} &nbsp;•&nbsp; AUTO 24/7 &nbsp;•&nbsp; PC + mobil</div>
        <div class="sub metaLine"><span class="metaDot" id="auditSummary">Kontrollerer historikk…</span></div>
      </div>
    </div>
    <div class="headerHeroArt" aria-hidden="true">
      <div class="heroGrid"></div>
      <div class="heroGlobe"></div>
      <svg class="heroChart" viewBox="0 0 560 100" preserveAspectRatio="none">
        <polyline points="0,82 48,70 85,76 124,45 168,58 216,29 255,54 306,34 354,62 401,39 448,51 503,24 560,38"/>
      </svg>
    </div>
    <div class="headerTagline" aria-hidden="true">ALGORITMER<br>MARKEDER<br>24/7<br>UTEN GRENSER</div>
    <div class="navActions">
      <div class="systemBox"><span id="liveDot" class="dot"></span><div><div class="sysLabel">SYSTEM</div><div id="liveText" class="sysValue">KOBLER TIL</div></div></div>
      <button id="installBtn" class="btn ghost" onclick="installApp()">⇩ Installer app</button>
      <a class="btn ghost" href="/logout">↪ Logg ut</a>
    </div>
  </header>

  <div id="installHelp" class="installHelp"></div>

  <section class="kpiStrip" aria-label="Nøkkeltall">
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">↗</span>PnL i dag</div><div id="kpi_today" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">3D</span>3 dager</div><div id="kpi_3d" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">7D</span>7 dager</div><div id="kpi_7d" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">✓</span>Ordresikkerhet</div><div id="kpi_guard" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">☁</span>Cloud 24/7</div><div id="kpi_cloud" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">A</span>Alpaca</div><div id="kpi_alpaca" class="kpiValue">-</div></div>
    <div class="kpiCard"><div class="kpiHead"><span class="kpiIcon">◷</span>Sist oppdatert</div><div id="kpi_updated" class="kpiValue">-</div></div>
  </section>

  <main class="mainGrid">
    <section class="card controlCard">
      <div class="cardHead"><div class="cardTitle"><span class="titleIcon">▶</span><h2>Kontroll</h2></div><span class="revBadge">⚡ AUTO</span></div>
      <div class="cardHint">Start, stopp og konfigurer tradingmotoren</div>
      <div class="heroControls">
        <button class="btn start" onclick="startTrading()">▶ START TRADING<span class="heroSub">Armed + resume</span></button>
        <button class="btn stop" onclick="stopTrading()">■ STOPP TRADING<span class="heroSub">Disarm + pause</span></button>
        <button class="btn danger" onclick="emergencyStop()">⚠ NØDSTOPP<span class="heroSub">Blokkerer nye handler umiddelbart</span></button>
      </div>

      <div class="controlSection">
        <h3>Marked</h3>
        <div class="segmented"><button id="m_auto" class="btn" onclick="setMarket('auto')">◎ AUTO 24/7</button><button id="m_stocks" class="btn" onclick="setMarket('stocks')">▥ AKSJER</button><button id="m_crypto" class="btn" onclick="setMarket('crypto')">₿ KRYPTO</button></div>
        <h3>Aktivitetsprofil</h3>
        <div class="segmented two profileRow"><button class="btn good" onclick="setProfile('active')">AKTIV</button><button class="btn" onclick="setProfile('balanced')">BALANSERT</button></div>
        <h3>Strategi</h3>
        <div class="strategyRow"><select id="strategySel"><option value="scalp">scalp</option><option value="hype">hype</option><option value="trend">trend</option><option value="cross">cross</option></select><button class="btn" onclick="setStrategy()">◎ Bruk strategi</button></div>
      </div>

      <details class="utility">
        <summary>Avansert og testordre</summary>
        <div class="utilityBody">
          <div class="row"><button class="btn ghost" onclick="toggleBool('extended_hours')">Extended av/på</button><button class="btn ghost" onclick="toggleBool('test_mode')">TestMode av/på</button><button class="btn ghost" onclick="postControl('/api/control',{kill:false})">Nullstill nødstop</button></div>
          <div id="paperTests"><h3>Testordre – kun paper</h3><div class="row"><input id="buySym" value="AAPL" style="width:110px"><input id="buyQty" type="number" value="1" step="0.001" style="width:90px"><button class="btn good" onclick="testBuy()">↑ TEST BUY</button><input id="sellSym" value="AAPL" style="width:110px"><button class="btn danger" onclick="testSellAll()">↓ TEST SELL ALL</button></div></div>
        </div>
      </details>
    </section>

    <section class="card statusCard">
      <div class="cardHead"><div class="cardTitle"><span class="titleIcon">⌁</span><h2>Status</h2></div><span class="sub">Live systemstatus</span></div>
      <div id="authBanner" class="banner warn">Kobler til motoren…</div>
      <div class="statusGrid">
        <div class="statusItem"><div class="statusLabel">Motor</div><div id="st_engine" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Versjon</div><div id="st_version" class="statusVal mono small">-</div></div>
        <div class="statusItem"><div class="statusLabel">Alpaca</div><div id="st_auth" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Trading</div><div id="st_running" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Cloud 24/7</div><div id="st_cloud" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Ordresikkerhet</div><div id="st_guard" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Marked</div><div id="st_market" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Aksje-session</div><div id="st_session" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Crypto 24/7</div><div id="st_crypto" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Paper</div><div id="st_paper" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Strategi</div><div id="st_strategy" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Intelligence</div><div id="st_intel" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Edge-motor</div><div id="st_edge" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Adaptiv V4.5</div><div id="st_adaptive" class="statusVal small">-</div></div>
        <div class="statusItem"><div class="statusLabel">Markedsregime</div><div id="st_regime" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Global risiko</div><div id="st_risk" class="statusVal">-</div></div>
        <div class="statusItem wide"><div class="statusLabel">Siste nyhet</div><div id="st_news" class="statusVal small">-</div></div>
        <div class="statusItem"><div class="statusLabel">PnL i dag</div><div id="st_pnl" class="statusVal">-</div></div>
        <div class="statusItem"><div class="statusLabel">Sist oppdatert</div><div id="st_last" class="statusVal mono small">-</div></div>
        <div class="statusItem wide"><div class="statusLabel">Siste ordre</div><div id="lastOrderId" class="statusVal mono small lastId">-</div></div>
      </div>
      <div id="st_notes" class="muted statusNotes"></div>
    </section>

    <aside class="sideRail">
      <section class="sideCard">
        <div class="sideTitle"><span class="accent">▥</span> Resultat</div>
        <div id="profit_row_1" class="profitRow" onclick="toggleProfitTrades(1)"><div class="profitLabel">I dag</div><div id="profit_today" class="profitValue neutral">-</div><div class="profitChev">▼</div></div>
        <div id="profit_details_1" class="profitDetails"></div>
        <div id="profit_row_3" class="profitRow" onclick="toggleProfitTrades(3)"><div class="profitLabel">3 dager</div><div id="profit_3d" class="profitValue neutral">-</div><div class="profitChev">▼</div></div>
        <div id="profit_details_3" class="profitDetails"></div>
        <div id="profit_row_7" class="profitRow" onclick="toggleProfitTrades(7)"><div class="profitLabel">7 dager</div><div id="profit_7d" class="profitValue neutral">-</div><div class="profitChev">▼</div></div>
        <div id="profit_details_7" class="profitDetails"></div>
        <div class="profitFoot">Trykk på periodene for faktiske vinnende avsluttede trades. Hovedtallet er Alpaca portfolio equity og inkluderer åpne posisjoner.</div>
      </section>

      <section class="sideCard exportBox">
        <div class="sideTitle"><span style="color:var(--purple)">✦</span> AI-Analyse</div>
        <select id="exportDays" aria-label="Velg analyseperiode"><option value="1">Siste 1 dag</option><option value="3">Siste 3 dager</option><option value="7" selected>Siste 7 dager</option><option value="14">Siste 14 dager</option><option value="30">Siste 30 dager</option></select>
        <button class="exportBtn" onclick="downloadAnalysis()">⇩ LAST NED ÉN AI-FIL</button>
        <div class="profitFoot">Komplett sanitert audit for analyse: trades, exits, score/EV, P/L, ordre, fills, datahelse og rådata. Ingen API-nøkler.</div>
      </section>
    </aside>
  </main>

  <section class="card tableCard sectionSpace">
    <div class="tableHead">
      <div class="tableTitle"><span class="titleIcon">≋</span><div><h2>Symboler / Posisjoner</h2><div class="sub">Live marked, edge og handling</div></div></div>
      <div class="filterArea">
        <button class="filterBtn active" onclick="setSymbolFilter('all',this)">Alle</button>
        <button class="filterBtn" onclick="setSymbolFilter('stocks',this)">Aksjer</button>
        <button class="filterBtn" onclick="setSymbolFilter('crypto',this)">Krypto</button>
        <button class="filterBtn" onclick="setSymbolFilter('active',this)">Aktive</button>
        <input id="symbolSearch" class="searchBox" placeholder="Søk symbol…" oninput="filterSymbols()">
        <button class="filterBtn" onclick="refresh(true)">↻ Oppdater</button>
      </div>
    </div>
    <div class="tableWrap"><table class="symbolsTable"><thead><tr><th>Marked</th><th>Symbol</th><th>Pris</th><th>Bid</th><th>Ask</th><th>Spread bps</th><th>Edge</th><th>Score</th><th>Signal</th><th>Pos</th><th>Avg</th><th>Handling</th><th>Feil</th></tr></thead><tbody id="tblSymbols"></tbody></table></div>
  </section>

  <details class="ordersDetails">
    <summary>Ordrehistorikk – siste 30</summary>
    <div class="tableHead"><div class="sub">Broker-/ordreoversikt</div><button class="filterBtn" onclick="refresh(true)">↻ Oppdater ordre</button></div>
    <div class="tableWrap"><table><thead><tr><th>Tid</th><th>Symbol</th><th>Side</th><th>Qty/notional</th><th>Type</th><th>Status</th><th>ID</th></tr></thead><tbody id="tblOrders"></tbody></table></div>
  </details>
</div>
<script>
let installPrompt=null, alertsEnabled=false,symbolFilter='all';
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e;document.getElementById('installBtn').style.display='inline-block'});
if('serviceWorker' in navigator){window.addEventListener('load',()=>navigator.serviceWorker.register('/service-worker.js').catch(()=>{}));}
async function installApp(){const h=document.getElementById('installHelp');if(installPrompt){installPrompt.prompt();await installPrompt.userChoice;installPrompt=null;h.style.display='none';return;}const ios=/iphone|ipad|ipod/i.test(navigator.userAgent);h.style.display='block';h.innerHTML=ios?'På iPhone/iPad: trykk <b>Del</b> → <b>Legg til på Hjem-skjerm</b>.':'I nettlesermenyen: velg <b>Installer app</b> eller <b>Legg til på startskjermen</b>.';}
function pill(v){let c='neutral';if(v===true||String(v)==='true')c='good';if(v===false||String(v)==='false')c='bad';return `<span class="pill ${c}">${v}</span>`}
function fmt(x){if(x===null||x===undefined||x==='')return '-';const n=Number(x);if(Number.isNaN(n))return String(x);const a=Math.abs(n);return a>=100?n.toFixed(2):a>=1?n.toFixed(4):n.toFixed(6)}
function money(x){if(x===null||x===undefined||x==='')return '-';const n=Number(x);if(Number.isNaN(n))return '-';return `${n>0?'+':n<0?'-':''}$${Math.abs(n).toFixed(2)}`}
function renderProfit(p){[['profit_today','today_usd','today_pct'],['profit_3d','days3_usd','days3_pct'],['profit_7d','days7_usd','days7_pct']].forEach(([id,k,pk])=>{const el=document.getElementById(id),v=p?.[k],pct=p?.[pk];if(v===null||v===undefined){el.textContent='-';el.className='profitValue neutral';return;}const n=Number(v);el.className='profitValue '+(n>0?'pos':n<0?'neg':'neutral');el.innerHTML=`${money(n)}${pct!==null&&pct!==undefined?`<span class="profitPct">${Number(pct)>=0?'+':''}${Number(pct).toFixed(2)}%</span>`:''}`})}
const profitTradeLoaded={1:false,3:false,7:false};
function holdText(sec){const n=Math.max(0,Number(sec||0));if(n<60)return Math.round(n)+'s';if(n<3600)return Math.round(n/60)+'m';return (n/3600).toFixed(1)+'t'}
function safeHtml(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function toggleProfitTrades(days){const row=document.getElementById('profit_row_'+days),box=document.getElementById('profit_details_'+days);const opening=!box.classList.contains('open');box.classList.toggle('open',opening);row.classList.toggle('open',opening);if(!opening||profitTradeLoaded[days])return;box.innerHTML='<div class="profitLoading">Henter faktiske vinnende trades…</div>';try{const r=await apiFetch('/api/profit_trades?days='+days+'&ts='+Date.now());const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.detail||d.error||'ukjent feil');const trades=d.trades||[];profitTradeLoaded[days]=true;let h=`<div class="profitTradeSummary">${Number(d.winner_count||0)} vinnende trade${Number(d.winner_count||0)===1?'':'s'} • realisert brutto ${money(d.gross_profit_usd||0)}${Number(d.shown||0)<Number(d.winner_count||0)?' • viser '+Number(d.shown||0):''}</div>`;if(!trades.length){h+='<div class="profitEmpty">Ingen avsluttede vinnende trades funnet i perioden.</div>'}else{h+=trades.map(t=>`<div class="profitTrade"><div class="profitTradeTop"><span class="profitTradeSym">${safeHtml(t.symbol)}</span><span class="profitTradePnl">${money(t.gross_pnl_usd)} (${Number(t.gross_return_pct||0)>=0?'+':''}${Number(t.gross_return_pct||0).toFixed(2)}%)</span></div><div class="profitTradeMeta">${fmt(t.entry_price)} → ${fmt(t.exit_price)} • ${safeHtml(t.qty)} stk • holdt ${holdText(t.hold_sec)}<br>${safeHtml(String(t.exit_time||'').replace('T',' ').replace('Z',' UTC'))}</div></div>`).join('')}h+='<div class="profitTradeMeta" style="padding:6px 3px 0">Brutto P/L fra faktiske Alpaca fills. Gebyrer er ikke fordelt på enkelt-trades.</div>';box.innerHTML=h}catch(e){box.innerHTML='<div class="profitEmpty neg">Kunne ikke hente trade-detaljer: '+safeHtml(e.message||e)+'</div>'}}
async function apiFetch(url,opt={}){const r=await fetch(url,{cache:'no-store',...opt});if(r.status===401){location.href='/login';throw new Error('login_required')}return r}
async function postControl(url,payload){await apiFetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await refresh(true)}
async function startTrading(){await postControl('/api/control',{armed:true,paused:false,kill:false})}
async function stopTrading(){await postControl('/api/control',{armed:false,paused:true})}
async function emergencyStop(){if(confirm('Aktivere NØDSTOPP? Nye handler blokkeres umiddelbart.'))await postControl('/api/control',{armed:false,paused:true,kill:true})}
async function toggleBool(k){const r=await apiFetch('/api/control?ts='+Date.now());const c=await r.json();await postControl('/api/control',{[k]:!Boolean(c[k])})}
async function setMarket(m){await postControl('/api/control',{market_mode:m})}
async function setStrategy(){const s=document.getElementById('strategySel').value;await postControl('/api/control',{strategy_mode:s,crypto_strategy_mode:s==='hype'?'scalp':s})}
async function setProfile(n){const p=n==='balanced'?{activity_profile:'balanced',intelligence_entry_score:67,intelligence_exit_score:42,strategy_mode:'scalp',fast_sma:8,slow_sma:24,hype_mom_n:5,min_mom_pct:.001,min_move_pct:.0003,min_slope_pct:.00025,cooldown_sec:90,take_profit_pct:.006,stop_loss_pct:.004,trailing_activate_pct:.003,trailing_stop_pct:.003,max_hold_sec:2700,refresh_bars_every_sec:15,crypto_strategy_mode:'scalp',crypto_cooldown_sec:60,crypto_take_profit_pct:.012,crypto_stop_loss_pct:.007,crypto_trailing_activate_pct:.007,crypto_trailing_stop_pct:.0045,crypto_max_hold_sec:3600,crypto_max_spread_bps:15,crypto_taker_fee_bps:25,crypto_min_net_edge_bps:30,crypto_entry_confirm_bars:2}:{activity_profile:'active',intelligence_entry_score:60,intelligence_exit_score:38,strategy_mode:'scalp',fast_sma:6,slow_sma:18,hype_mom_n:4,min_mom_pct:.0006,min_move_pct:.00015,min_slope_pct:.00015,cooldown_sec:45,take_profit_pct:.004,stop_loss_pct:.003,trailing_activate_pct:.002,trailing_stop_pct:.0025,max_hold_sec:1800,refresh_bars_every_sec:10,crypto_strategy_mode:'scalp',crypto_cooldown_sec:30,crypto_take_profit_pct:.01,crypto_stop_loss_pct:.006,crypto_trailing_activate_pct:.006,crypto_trailing_stop_pct:.004,crypto_max_hold_sec:2700,crypto_max_spread_bps:25,crypto_taker_fee_bps:25,crypto_min_net_edge_bps:20,crypto_entry_confirm_bars:2};await postControl('/api/control',p)}
async function testBuy(){await postControl('/api/test_buy',{symbol:document.getElementById('buySym').value.trim().toUpperCase(),qty:Number(document.getElementById('buyQty').value||1)})}
async function testSellAll(){await postControl('/api/test_sell_all',{symbol:document.getElementById('sellSym').value.trim().toUpperCase()})}
function downloadAnalysis(){const el=document.getElementById('exportDays');let d=parseInt(el?.value||'7',10);if(!Number.isFinite(d))d=7;d=Math.max(1,Math.min(30,d));window.location.href='/api/export?days='+encodeURIComponent(d)}
function setSymbolFilter(f,btn){symbolFilter=f;document.querySelectorAll('.filterBtn').forEach(x=>x.classList.remove('active'));if(btn)btn.classList.add('active');filterSymbols()}
function filterSymbols(){const q=(document.getElementById('symbolSearch')?.value||'').trim().toUpperCase();document.querySelectorAll('#tblSymbols tr').forEach(tr=>{const market=(tr.dataset.market||'').toLowerCase(),pos=Number(tr.dataset.pos||0);const matchFilter=symbolFilter==='all'||(symbolFilter==='stocks'&&market==='stock')||(symbolFilter==='crypto'&&market==='crypto')||(symbolFilter==='active'&&pos!==0);const matchQ=!q||(tr.dataset.symbol||'').includes(q);tr.style.display=matchFilter&&matchQ?'':'none'})}
function renderSymbols(s){const b=document.getElementById('tblSymbols');b.innerHTML='';Object.keys(s||{}).sort((a,c)=>(s[a]?.market==='CRYPTO'?0:1)-(s[c]?.market==='CRYPTO'?0:1)||a.localeCompare(c)).forEach(sym=>{const x=s[sym]||{},sig=x.signal===1?'BUY':x.signal===-1?'SELL':(x.signal??'0');const tr=document.createElement('tr');tr.dataset.market=String(x.market||'');tr.dataset.pos=String(x.pos_qty??0);tr.dataset.symbol=String(sym).toUpperCase();const edge=x.debug?.p_tp_before_sl_pct!=null?('TP '+x.debug.p_tp_before_sl_pct+'% / EV '+(x.debug.ev_bps>=0?'+':'')+x.debug.ev_bps+'bp'):(x.debug?.prob_up_pct!=null?('P '+x.debug.prob_up_pct+'% / EV '+(x.debug.ev_bps>=0?'+':'')+x.debug.ev_bps+'bp'):'-');const marketBadge='<span class="marketBadge '+String(x.market||'').toLowerCase()+'">'+safeHtml(x.market||'-')+'</span>';const action=String(x.action||'NONE');const actionClass=/BUY|ENTRY|SUBMIT|FILLED/.test(action)?'pos':/SELL|EXIT|STOP|LOSS|ERROR|RECONCILE/.test(action)?'neg':'';tr.innerHTML=`<td>${marketBadge}</td><td><b class="symName">${safeHtml(sym)}</b></td><td>${fmt(x.price)}</td><td>${fmt(x.bid)}</td><td>${fmt(x.ask)}</td><td>${x.debug?.spread_bps??'-'}</td><td class="edgeCell">${edge}</td><td><b>${x.debug?.intel_score??'-'}</b></td><td>${sig}</td><td>${x.pos_qty??0}</td><td>${fmt(x.avg_entry)}</td><td class="${actionClass}">${safeHtml(action)}</td><td class="neg">${safeHtml(x.error||'')}</td>`;b.appendChild(tr)});filterSymbols()}
function renderOrders(o){const b=document.getElementById('tblOrders');b.innerHTML='';(o||[]).forEach(x=>{const tr=document.createElement('tr'),t=String(x.filled_at||x.submitted_at||x.created_at||'').replace('T',' ').replace('Z','');tr.innerHTML=`<td class="mono small">${t||'-'}</td><td><b>${x.symbol||''}</b></td><td>${x.side||''}</td><td>${x.qty||x.notional||''}</td><td>${x.type||''}</td><td>${x.status||''}</td><td class="mono small">${x.id||''}</td>`;b.appendChild(tr)})}
function setKpi(id,value,kind=''){const el=document.getElementById(id);if(!el)return;el.textContent=value??'-';el.className='kpiValue '+kind}
function updateKpis(s,running,cr,guardBad,guardPending){const p=s.profit_summary||{};const cls=v=>Number(v)>0?'pos':Number(v)<0?'neg':'';setKpi('kpi_today',money(p.today_usd??s.day_pnl_usd),cls(p.today_usd??s.day_pnl_usd));setKpi('kpi_3d',p.days3_usd==null?'-':money(p.days3_usd),cls(p.days3_usd));setKpi('kpi_7d',p.days7_usd==null?'-':money(p.days7_usd),cls(p.days7_usd));setKpi('kpi_guard',guardBad?'AVSTEM':guardPending?'EXIT':'OK',guardBad?'neg':'pos');setKpi('kpi_cloud',cr.render?(cr.always_on_expected?'24/7 OK':'FREE'):'LOKAL',cr.always_on_expected?'pos':cr.render?'warn':'');setKpi('kpi_alpaca',s.auth_ok===true?'Tilkoblet':s.auth_ok===false?'Feil':'Venter',s.auth_ok===true?'pos':s.auth_ok===false?'neg':'');const raw=String(s.last_update||'');let t='-';try{const d=new Date(raw);if(!Number.isNaN(d.getTime()))t=d.toLocaleTimeString('no-NO',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){}setKpi('kpi_updated',t);const chip=document.getElementById('headerRunChip');if(chip){chip.textContent=running?'TRADING AKTIV':'TRADING PAUSET';chip.className='runChip '+(running?'on':'off')}}
async function refresh(force=false){try{const r=await apiFetch('/api/status?ts='+Date.now()+(force?'&force=1':''));const s=await r.json();const live=Boolean(s.auth_ok)&&String(s.engine_state||'').toUpperCase()==='RUNNING';document.getElementById('liveDot').className='dot '+(live?'ok':'bad');document.getElementById('liveText').textContent=live?'ONLINE':'IKKE KLAR';const running=Boolean(s.armed)&&!Boolean(s.paused)&&!Boolean(s.kill);document.getElementById('st_engine').textContent=s.engine_state||'-';document.getElementById('st_version').textContent=s.version||'-';document.getElementById('st_auth').innerHTML=pill(s.auth_ok);document.getElementById('st_running').innerHTML=pill(running);const cr=s.cloud_runtime||{};const cloudEl=document.getElementById('st_cloud');if(cr.render){cloudEl.innerHTML=cr.always_on_expected?'<span class="pill good">24/7 OK</span>':'<span class="pill bad">RENDER FREE</span>'}else{cloudEl.innerHTML='<span class="pill neutral">LOKAL</span>'}const eg=s.execution_guard||{};const guardBad=Number(eg.reconcile_required||0)>0;const guardPending=Number(eg.active_exits||0)>0;document.getElementById('st_guard').innerHTML=guardBad?'<span class="pill bad">AVSTEM '+Number(eg.reconcile_required||0)+'</span>':guardPending?'<span class="pill neutral">EXIT '+Number(eg.active_exits||0)+'</span>':'<span class="pill good">OK</span>';updateKpis(s,running,cr,guardBad,guardPending);document.getElementById('st_market').textContent=(s.market_mode||'auto').toUpperCase();document.getElementById('st_session').textContent=s.stock_session||'-';document.getElementById('st_crypto').innerHTML=pill(s.crypto_24_7);document.getElementById('st_paper').innerHTML=pill(s.paper);document.getElementById('st_strategy').textContent=s.strategy_mode||'-';const ii=s.intelligence||{};document.getElementById('st_intel').innerHTML=pill(ii.enabled!==false);const pe=s.profit_engine||{};document.getElementById('st_edge').innerHTML=pill(pe.enabled!==false);const ah=s.audit_health||{},lh=s.learning_health||{},bh=s.broker_history||{},sb=s.storage_backup||{};const storageLabel=sb.configured?(sb.last_error?'backupfeil':sb.last_success_utc?'databasebackup hver 5. min':'backup venter'):ah.persistent===false?'midlertidig lagring':'lokal lagring';document.getElementById('auditSummary').textContent='Historikk: '+((ah.last_error||lh.last_error)?'lagringsfeil':storageLabel)+' • Alpaca '+(bh.stream||'starter')+' • '+(bh.sync_complete?'historikk hentet':'historikk venter');const ae=s.adaptive_edge||{},md=s.market_data||{};const sh=ae.shadow?.models||{};const shN=Object.values(sh).reduce((a,x)=>a+Number(x?.samples||0),0);document.getElementById('st_adaptive').textContent=`${String(md.stock_feed||'iex').toUpperCase()} • router ${ae.router?'ON':'OFF'} • univers ${Number(ae.active_symbols||0)}/${Number(ae.total_symbols||0)} • shadow ${shN}`;document.getElementById('st_regime').textContent=(ii.regime_label||'-')+' '+(ii.regime_score??'');document.getElementById('st_risk').textContent=(ii.global_risk??'-')+'/100';document.getElementById('st_news').textContent=ii.last_headline||'-';document.getElementById('st_pnl').textContent=fmt(s.day_pnl_usd);renderProfit(s.profit_summary||{today_usd:s.day_pnl_usd});document.getElementById('st_last').textContent=s.last_update||'';document.getElementById('st_notes').textContent=s.notes||'';document.getElementById('strategySel').value=s.strategy_mode||'scalp';document.getElementById('paperTests').style.display=s.paper===false?'none':'block';['auto','stocks','crypto'].forEach(m=>document.getElementById('m_'+m).classList.toggle('active',(s.market_mode||'auto')===m));const ab=document.getElementById('authBanner');if(s.auth_ok===false){ab.className='banner bad';ab.textContent='Alpaca avviser API-nøklene.'}else if(live&&cr.render&&cr.always_on_expected===false){ab.className='banner warn';ab.textContent='Trading kjører, men Render FREE kan sovne når dashboardet lukkes. Oppgrader Compute til 0.5c-512mb for ekte 24/7.'}else if(live){ab.className='banner good';ab.textContent=running?'Motor ONLINE – trading er STARTET.':'Motor ONLINE – trading er STOPPET/PAUSET.'}else{ab.className='banner warn';ab.textContent='Motoren starter eller restarter…'}renderSymbols(s.symbols||{});renderOrders(s.orders||[]);const lo=(s.orders||[])[0]?.id||'-';document.getElementById('lastOrderId').textContent=lo}catch(e){document.getElementById('liveDot').className='dot bad';document.getElementById('liveText').textContent='OFFLINE'}}
setInterval(()=>refresh(false),2500);refresh(false);
</script></body></html>\n"""


@app.get("/")
def home():
    # Dashboard HTML contains modern JavaScript/CSS braces. Serving it directly
    # avoids Jinja interpreting client-side syntax while still substituting the
    # two server values the page needs. Login remains a normal Jinja template.
    page = HTML.replace("{{ bot_version }}", str(BOT_VERSION)).replace("{{ build_id }}", str(BUILD_ID))
    return Response(page, mimetype="text/html")


@app.get("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest")


@app.get("/service-worker.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/healthz")
def healthz():
    st = safe_read_json(STATUS_PATH, {})
    last = st.get("last_update") if isinstance(st, dict) else None
    age = None
    if last:
        try:
            age = max(0.0, time.time() - datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    engine = st.get("engine_state") if isinstance(st, dict) else None
    ok = engine not in {"AUTH_ERROR", "CRASHED"}
    return jsonify({"ok": bool(ok), "engine_state": engine or "STARTING", "status_age_sec": age, "version": BOT_VERSION, "cloud_runtime": st.get("cloud_runtime", {}), "broker_history": st.get("broker_history", {}), "storage_backup": st.get("storage_backup", {})}), (200 if ok else 503)


@app.get("/api/control")
def api_control_get():
    return jsonify(safe_read_json(CONTROL_PATH, {}))


@app.post("/api/control")
def api_control_post():
    patch = request.get_json(force=True, silent=True) or {}
    allowed = {
        "paused","kill","armed","extended_hours","test_mode","strategy_mode","crypto_strategy_mode","market_mode",
        "fast_sma","slow_sma","hype_mom_n","min_mom_pct","min_move_pct","min_slope_pct","cooldown_sec",
        "take_profit_pct","stop_loss_pct","trailing_activate_pct","trailing_stop_pct","max_hold_sec","refresh_bars_every_sec",
        "crypto_cooldown_sec","crypto_take_profit_pct","crypto_stop_loss_pct","crypto_trailing_activate_pct","crypto_trailing_stop_pct",
        "crypto_max_hold_sec","crypto_max_spread_bps","crypto_taker_fee_bps","crypto_min_net_edge_bps","crypto_entry_confirm_bars",
        "activity_profile","intelligence_enabled","intelligence_entry_score","intelligence_exit_score",
        "soft_exit_min_hold_sec","soft_exit_confirm_sec","entry_require_signal_reset","stock_entry_cutoff_min","stock_flatten_before_close_min","stock_loss_streak_window_sec","stock_loss_streak_count","stock_loss_streak_cooldown_sec","decision_log_interval_sec",
        "adaptive_router_enabled","dynamic_universe_enabled","dynamic_universe_top_n","sector_confirmation_enabled","champion_challenger_enabled","shadow_log_interval_sec"
    }
    clean = {k:v for k,v in patch.items() if k in allowed}
    return jsonify(patch_control(clean))


@app.get("/api/status")
def api_status():
    st = safe_read_json(STATUS_PATH, {})
    defaults = {"version":BOT_VERSION,"paper":True,"auth_ok":None,"engine_state":"STARTING","armed":False,"paused":True,"kill":False,"extended_hours":False,"auto_extended":False,"market_mode":"auto","stock_session":"CLOSED","crypto_24_7":True,"test_mode":False,"strategy_mode":"scalp","last_update":"","day_pnl_usd":0,"profit_summary":{"today_usd":0,"today_pct":0,"days3_usd":None,"days3_pct":None,"days7_usd":None,"days7_pct":None},"execution_guard":{"enabled":True,"active_exits":0,"blocked_entries":0,"reconcile_required":0},"adaptive_edge":{"enabled":True,"router":True,"active_symbols":0,"total_symbols":0},"market_data":{"stock_feed":"iex"},"notes":"","symbols":{},"orders":[]}
    if isinstance(st, dict): defaults.update(st)
    defaults["version"] = BOT_VERSION
    defaults["build_id"] = BUILD_ID
    defaults["server_started_at_utc"] = STARTED_AT
    return jsonify(defaults)


@app.get("/api/profit_trades")
def api_profit_trades():
    try:
        days = int(request.args.get("days", "1"))
    except Exception:
        days = 1
    if days not in {1, 3, 7}:
        return jsonify({"ok": False, "error": "days_must_be_1_3_or_7"}), 400
    now = time.time()
    cached = _PROFIT_TRADES_CACHE.get(days)
    if cached and now - cached[0] < 60:
        return jsonify({"ok": True, **cached[1], "cached": True})
    try:
        import trading_bot
        from trade_export import realized_winning_trades
        cfg = trading_bot.load_cfg()
        api = trading_bot.AlpacaRest(cfg)
        data = realized_winning_trades(api, days, max_items=50)
        _PROFIT_TRADES_CACHE[days] = (now, data)
        return jsonify({"ok": True, **data, "cached": False})
    except Exception as exc:
        return jsonify({"ok": False, "error": "profit_trades_failed", "detail": str(exc)[:500]}), 500


@app.get("/api/export")
def api_export():
    try:
        days = int(request.args.get("days", "7"))
    except Exception:
        days = 7
    if days < 1 or days > 30:
        return jsonify({"ok": False, "error": "days_must_be_1_to_30"}), 400
    try:
        from trade_export import build_ai_analysis_export
        blob, manifest = build_ai_analysis_export(days, STATE_DIR, BOT_VERSION)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"TradingBot_AI_ANALYSE_{days}d_{stamp}UTC.zip"
        resp = send_file(io.BytesIO(blob), mimetype="application/zip", as_attachment=True, download_name=filename, max_age=0)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["X-TradingBot-Export-Version"] = str(manifest.get("export_schema") or "")
        return resp
    except Exception as exc:
        return jsonify({"ok": False, "error": "export_failed", "detail": str(exc)[:500]}), 500


@app.post("/api/test_buy")
def api_test_buy():
    body=request.get_json(force=True,silent=True) or {}; sym=(body.get("symbol") or "AAPL").strip().upper(); qty=float(body.get("qty") or 1)
    c=safe_read_json(CONTROL_PATH,{}); c=c if isinstance(c,dict) else {}; c["test_buy"]={"symbol":sym,"qty":qty,"go":True}; safe_write_json(CONTROL_PATH,c)
    return jsonify({"ok":True,"symbol":sym,"qty":qty})


@app.post("/api/test_sell_all")
def api_test_sell_all():
    body=request.get_json(force=True,silent=True) or {}; sym=(body.get("symbol") or "AAPL").strip().upper()
    c=safe_read_json(CONTROL_PATH,{}); c=c if isinstance(c,dict) else {}; c["test_sell_all"]={"symbol":sym,"go":True}; safe_write_json(CONTROL_PATH,c)
    return jsonify({"ok":True,"symbol":sym})


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_BIND", "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "5050")))
    app.run(host=host, port=port, debug=False, threaded=True)

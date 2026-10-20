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
      --bg:#03101c;--bg2:#06182a;--panel:rgba(7,25,43,.88);--panel2:rgba(8,31,52,.80);
      --line:#174769;--line2:#1b5f87;--text:#eef9ff;--muted:#83a8bf;--cyan:#20d9ff;
      --teal:#18f0c0;--green:#20e394;--amber:#ffc94d;--red:#ff5576;--purple:#b38cff;
      --shadow:0 22px 55px rgba(0,0,0,.28);--glow:0 0 26px rgba(32,217,255,.10);
    }
    *{box-sizing:border-box}
    html{background:var(--bg)}
    body{margin:0;color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;background:
      radial-gradient(900px 500px at 12% -10%,rgba(10,136,184,.20),transparent 62%),
      radial-gradient(760px 420px at 95% 3%,rgba(13,183,166,.10),transparent 58%),
      linear-gradient(180deg,#04111f 0%,#03101b 52%,#020b14 100%);min-height:100vh;-webkit-tap-highlight-color:transparent}
    body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.18;background-image:linear-gradient(rgba(57,170,204,.08) 1px,transparent 1px),linear-gradient(90deg,rgba(57,170,204,.06) 1px,transparent 1px);background-size:48px 48px;mask-image:linear-gradient(to bottom,#000,transparent 70%)}
    .wrap{max-width:1500px;margin:0 auto;padding:18px 20px 42px;position:relative}
    .topbar{position:sticky;top:0;z-index:12;background:linear-gradient(180deg,rgba(3,16,28,.96),rgba(3,16,28,.82));backdrop-filter:blur(18px);display:flex;align-items:center;justify-content:space-between;gap:18px;padding:10px 0 14px;border-bottom:1px solid rgba(38,117,157,.28)}
    .brand{display:flex;align-items:center;gap:14px;min-width:0}.brandMark{width:54px;height:54px;border:1px solid #1fb8d5;border-radius:16px;display:grid;place-items:center;background:linear-gradient(145deg,#0b3049,#071a2b);box-shadow:inset 0 0 22px rgba(32,217,255,.14),0 0 24px rgba(32,217,255,.14);font-weight:950;color:var(--teal);letter-spacing:-1px}.brandCopy{min-width:0}.brandTitleRow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}h1{font-size:29px;line-height:1;margin:0;letter-spacing:-.6px}.revBadge{font-size:11px;font-weight:900;color:#82fbef;border:1px solid #1e8291;background:#08313c;border-radius:999px;padding:5px 9px;letter-spacing:.06em}.sub{color:var(--muted);font-size:12px;line-height:1.5}.sub strong{color:#bfefff}.metaLine{display:flex;gap:9px;flex-wrap:wrap;margin-top:5px}.metaDot:before{content:"";display:inline-block;width:5px;height:5px;background:#1be6ac;border-radius:50%;margin:0 6px 2px 0;box-shadow:0 0 10px #1be6ac}
    .navActions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.systemBox{display:flex;gap:10px;align-items:center;padding:9px 13px;border:1px solid #17466a;border-radius:12px;background:rgba(7,27,46,.68)}.systemBox .dot{margin-right:2px}.sysLabel{font-size:9px;color:#6f9bb6;letter-spacing:.1em}.sysValue{font-size:12px;font-weight:900}.runChip{padding:7px 10px;border-radius:999px;font-size:10px;font-weight:900;letter-spacing:.05em;border:1px solid}.runChip.on{color:#5fffd0;border-color:#169d77;background:#06382d}.runChip.off{color:#ffd978;border-color:#8f6e27;background:#3b2b08}
    .dot{width:10px;height:10px;border-radius:50%;background:#71809d}.dot.ok{background:#20d98b;box-shadow:0 0 14px #20d98b}.dot.bad{background:#ff5579;box-shadow:0 0 12px #ff557955}.loginTag{font-size:11px;color:#a2bdd1}
    .btn{appearance:none;border:1px solid #285f87;background:linear-gradient(180deg,#0d2a46,#091d33);color:#eff9ff;padding:11px 15px;border-radius:11px;cursor:pointer;font-weight:800;min-height:43px;transition:.18s ease;box-shadow:inset 0 1px 0 rgba(255,255,255,.025)}.btn:hover{border-color:#35b9e1;transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,0,0,.22),0 0 18px rgba(32,217,255,.08)}.btn:active{transform:translateY(0)}.btn.ghost{background:rgba(6,20,35,.72);text-decoration:none}.btn.active{border-color:#27dfff;color:#bff8ff;box-shadow:0 0 0 1px #27dfff inset,0 0 18px rgba(39,223,255,.12)}
    .installHelp{display:none;background:#0b2944;border:1px solid #236c98;border-radius:12px;padding:10px 12px;margin:10px 0;color:#d6efff;font-size:13px}
    .kpiStrip{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:9px;margin:13px 0 14px}.kpiCard{min-width:0;padding:12px 13px;border:1px solid #164c70;border-radius:13px;background:linear-gradient(145deg,rgba(7,31,51,.93),rgba(5,22,39,.86));box-shadow:inset 0 1px 0 rgba(255,255,255,.025),var(--glow)}.kpiHead{display:flex;align-items:center;gap:8px;color:#83a8bf;font-size:10px;text-transform:uppercase;letter-spacing:.055em}.kpiIcon{width:27px;height:27px;display:grid;place-items:center;border-radius:8px;color:#38e9d3;background:#073b43;border:1px solid #11636d;font-size:13px}.kpiValue{font-weight:950;font-size:17px;margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.kpiValue.pos{color:#4ff1bd}.kpiValue.neg{color:#ff6d8b}.kpiValue.warn{color:#ffd16f}
    .mainGrid{display:grid;grid-template-columns:minmax(500px,1.08fr) minmax(410px,.92fr) 250px;gap:13px;align-items:start}.card{background:linear-gradient(155deg,rgba(9,31,52,.93),rgba(5,20,36,.91));border:1px solid #155276;border-radius:17px;padding:15px;box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.025);position:relative;overflow:hidden}.card:before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(120deg,rgba(25,216,255,.025),transparent 35%,rgba(16,225,176,.018));}.card>*{position:relative}.card h2{margin:0;font-size:20px;letter-spacing:-.25px}.card h3{margin:14px 0 8px;font-size:12px;color:#b8d4e4;text-transform:uppercase;letter-spacing:.065em}.cardHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.cardTitle{display:flex;align-items:center;gap:10px}.titleIcon{width:31px;height:31px;border-radius:9px;display:grid;place-items:center;color:#46f4dd;border:1px solid #17667c;background:#083544;box-shadow:0 0 14px rgba(31,224,221,.08)}.cardHint{font-size:11px;color:#7399b1;margin-left:41px;margin-top:-9px;margin-bottom:12px}
    .heroControls{display:grid;grid-template-columns:1fr 1fr;gap:10px}.heroControls .btn{min-height:63px;font-size:15px;border-radius:13px}.heroControls .start{background:linear-gradient(135deg,#087b58,#079366);border-color:#19dda0;box-shadow:inset 0 1px 0 #ffffff18,0 0 20px #0bd68b1c}.heroControls .stop{background:linear-gradient(135deg,#704b0c,#80601b);border-color:#d19c2d;color:#fff4d6}.heroControls .danger{grid-column:1/-1;background:linear-gradient(135deg,#76152d,#971d3b);border-color:#ff4c6d;box-shadow:0 0 20px #ff355c16}.heroSub{display:block;font-size:10px;font-weight:600;opacity:.75;margin-top:3px}
    .controlSection{padding-top:2px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.segmented.two{grid-template-columns:repeat(2,minmax(0,180px))}.segmented .btn{width:100%;min-height:49px}.btn.good{background:linear-gradient(180deg,#08714f,#07543d);border-color:#17aa78}.profileRow .btn{min-width:135px}
    input,select{background:#061726;color:#eaf7ff;border:1px solid #245779;border-radius:10px;padding:11px 12px;font-size:14px;min-height:43px;outline:none}input:focus,select:focus{border-color:#27dfff;box-shadow:0 0 0 2px #27dfff16}.strategyRow{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:8px}.strategyRow select{width:100%}
    details.utility{margin-top:13px;border:1px solid #164563;border-radius:12px;background:#061827}.utility summary{list-style:none;cursor:pointer;padding:11px 12px;font-weight:850;color:#bad8e8;font-size:12px;display:flex;justify-content:space-between}.utility summary::-webkit-details-marker{display:none}.utility summary:after{content:"＋";color:#52d9ec}.utility[open] summary:after{content:"−"}.utilityBody{padding:0 11px 12px;border-top:1px solid #113c57}.utilityBody .row{margin-top:10px}.muted{color:#7599b2;font-size:11px;line-height:1.45}
    .banner{padding:12px 13px;border-radius:11px;margin-bottom:12px;font-weight:850;font-size:13px}.banner.bad{background:#491526;border:1px solid #97314f}.banner.good{background:#07392d;border:1px solid #16825e;color:#bffff0}.banner.warn{background:#49370e;border:1px solid #85661c;color:#ffe6a0}
    .statusGrid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}.statusItem{display:grid;grid-template-columns:118px minmax(0,1fr);gap:8px;align-items:center;min-height:34px;border-bottom:1px solid rgba(31,78,108,.38);font-size:12px}.statusItem.wide{grid-column:1/-1;grid-template-columns:118px minmax(0,1fr)}.statusLabel{color:#7fa2bb}.statusVal{min-width:0;overflow:hidden;text-overflow:ellipsis}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.small{font-size:11px}.pill{display:inline-block;padding:3px 8px;border-radius:999px;font-weight:900;font-size:10px}.pill.good{background:#073d2f;color:#4bffc0;border:1px solid #116d54}.pill.bad{background:#461425;color:#ff83a3;border:1px solid #7d2440}.pill.neutral{background:#112d4a;color:#b9e8ff;border:1px solid #285f80}.lastId{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .sideRail{display:grid;gap:12px}.sideCard{background:linear-gradient(155deg,rgba(8,30,50,.95),rgba(4,18,32,.92));border:1px solid #155276;border-radius:16px;padding:13px;box-shadow:var(--shadow)}.sideTitle{display:flex;align-items:center;gap:8px;font-weight:950;font-size:15px;margin-bottom:8px}.sideTitle .accent{color:#ffd164}.profitRow{display:grid;grid-template-columns:62px 1fr 15px;gap:8px;align-items:center;padding:12px 0;border-bottom:1px solid rgba(30,78,108,.48);cursor:pointer;user-select:none}.profitRow:hover{background:#0a2840}.profitLabel{color:#9ab5c8;font-size:11px}.profitValue{font-weight:950;text-align:right;font-size:16px}.profitValue.pos{color:#4ff1bd}.profitValue.neg{color:#ff708d}.profitValue.neutral{color:#d9ecf6}.profitPct{display:block;font-weight:650;font-size:9px;color:#7898af;margin-top:2px}.profitChev{color:#7092aa;font-size:10px}.profitDetails{display:none;padding:5px 0 8px;border-bottom:1px solid #183f5a}.profitDetails.open{display:block}.profitTrade{padding:7px 2px;border-bottom:1px dashed #17445f}.profitTradeTop{display:flex;justify-content:space-between;gap:7px;font-size:10px}.profitTradeSym{font-weight:900}.profitTradePnl{font-weight:900;color:#58f1bf}.profitTradeMeta,.profitFoot{font-size:9px;color:#6e92aa;line-height:1.35;margin-top:6px}.profitTradeSummary,.profitLoading,.profitEmpty{font-size:10px;color:#91b4ca;padding:7px 2px}.exportBox select{width:100%;margin:4px 0 8px}.exportBtn{width:100%;min-height:43px;padding:9px 10px;background:linear-gradient(180deg,#0a5e8b,#08476d);border:1px solid #22baf0;border-radius:9px;color:#fff;font-weight:900;cursor:pointer}.exportBtn:hover{box-shadow:0 0 18px #22baf022}
    .sectionSpace{margin-top:13px}.tableCard{padding:0;overflow:hidden}.tableHead{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 15px 12px;border-bottom:1px solid #164462}.tableTitle{display:flex;align-items:center;gap:9px}.filterArea{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.filterBtn{border:1px solid #1e5576;background:#081d31;color:#91afc2;padding:7px 11px;border-radius:8px;font-size:10px;font-weight:850;cursor:pointer}.filterBtn.active{border-color:#25d8ff;color:#d9f9ff;background:#0a3148;box-shadow:0 0 12px #25d8ff12}.searchBox{min-height:34px!important;width:175px!important;padding:7px 10px!important;font-size:11px!important}.tableWrap{overflow:auto;max-width:100%;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse;font-size:11px;min-width:1020px}th,td{padding:9px 9px;border-bottom:1px solid rgba(24,66,92,.48);text-align:left;white-space:nowrap}th{color:#7399b1;font-size:9px;text-transform:uppercase;letter-spacing:.055em;background:#061827;position:sticky;top:0;z-index:2}tbody tr{transition:.14s}tbody tr:hover{background:rgba(14,67,91,.18)}.symName{color:#eefaff}.marketBadge{padding:3px 7px;border-radius:6px;font-size:9px;font-weight:850;background:#112b43;color:#b4d4e7}.marketBadge.crypto{color:#ffd985;border:1px solid #725720;background:#362b0c}.marketBadge.stock{color:#87e9ff;border:1px solid #1d617a;background:#0a3040}.edgeCell{color:#9fc6d8}.pos{color:#53f0bd!important}.neg{color:#ff708d!important}
    .ordersDetails{margin-top:13px;border:1px solid #174967;border-radius:14px;background:#051827;overflow:hidden}.ordersDetails summary{cursor:pointer;list-style:none;padding:12px 15px;font-size:12px;font-weight:900;color:#9fc0d2}.ordersDetails summary::-webkit-details-marker{display:none}.ordersDetails summary:after{content:" Vis";float:right;color:#52d9ec}.ordersDetails[open] summary:after{content:" Skjul"}.ordersDetails .tableHead{padding-top:9px}.statusNotes{margin-top:10px;padding-top:9px;border-top:1px solid #173f59}
    @media(max-width:1220px){.mainGrid{grid-template-columns:1.08fr .92fr}.sideRail{grid-column:1/-1;grid-template-columns:1fr 1fr}.kpiStrip{grid-template-columns:repeat(4,1fr)}}
    @media(max-width:850px){.wrap{padding:10px 9px 30px}.topbar{position:relative;align-items:flex-start}.brandMark{width:45px;height:45px;border-radius:13px}.brandTitleRow h1{font-size:23px}.navActions{width:100%;justify-content:flex-start}.mainGrid{grid-template-columns:1fr}.sideRail{grid-column:auto;grid-template-columns:1fr}.kpiStrip{grid-template-columns:repeat(2,1fr)}.statusGrid{grid-template-columns:1fr}.statusItem.wide{grid-column:auto}.heroControls .btn{min-height:55px}.strategyRow{grid-template-columns:1fr}.segmented.two{grid-template-columns:1fr 1fr}.tableHead{align-items:flex-start;flex-direction:column}.filterArea{width:100%}.searchBox{width:100%!important}.card{padding:12px}.sub.desktopOnly{display:none}}
    @media(max-width:480px){.kpiStrip{grid-template-columns:1fr 1fr}.kpiCard{padding:10px}.kpiValue{font-size:15px}.segmented{grid-template-columns:1fr}.segmented.two{grid-template-columns:1fr 1fr}.heroControls{grid-template-columns:1fr}.heroControls .danger{grid-column:auto}.systemBox{display:none}.navActions .btn{padding:9px 10px;min-height:38px}.statusItem{grid-template-columns:105px 1fr}.profileRow .btn{min-width:0;flex:1}}
  </style>
</head>
<body><div class="wrap">
  <header class="topbar">
    <div class="brand">
      <div class="brandMark">AI</div>
      <div class="brandCopy">
        <div class="brandTitleRow"><h1>Trading Bot</h1><span class="revBadge">REV {{ bot_version }}</span><span id="headerRunChip" class="runChip off">TRADING PAUSET</span></div>
        <div class="sub desktopOnly">BUILD {{ build_id }} &nbsp;•&nbsp; AUTO 24/7 &nbsp;•&nbsp; PC + mobil</div>
        <div class="sub metaLine"><span class="metaDot" id="auditSummary">Kontrollerer historikk…</span></div>
      </div>
    </div>
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

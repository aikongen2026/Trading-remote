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
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = Path(__file__).resolve().parent
STATE_DIR = Path((os.getenv("TRADINGBOT_STATE_DIR") or str(HERE)).strip() or str(HERE))
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTROL_PATH = STATE_DIR / "control.json"
STATUS_PATH = STATE_DIR / "status.json"
VERSION_PATH = HERE / "VERSION.txt"
BOT_VERSION = VERSION_PATH.read_text(encoding="utf-8", errors="ignore").strip() if VERSION_PATH.exists() else "4.0.0-research-edge"

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


HTML = r"""
<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <meta name="theme-color" content="#0b1220"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/>
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
  <link rel="manifest" href="/manifest.webmanifest"/>
  <link rel="apple-touch-icon" href="/static/icon-192.png"/>
  <title>Trading Bot</title>
  <style>
    *{box-sizing:border-box} body{margin:0;background:#08101d;color:#eaf0ff;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;-webkit-tap-highlight-color:transparent}
    .wrap{max-width:1180px;margin:0 auto;padding:14px 14px 40px}.topbar{position:sticky;top:0;z-index:9;background:#08101df2;backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:8px 0 10px}
    h1{font-size:26px;margin:0}.sub{color:#91a4c8;font-size:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{background:#0f1a33;border:1px solid #21365c;border-radius:16px;padding:14px;box-shadow:0 8px 24px #0005}.card h2{margin:0 0 12px;font-size:21px}.card h3{margin:14px 0 8px;font-size:15px;color:#b9c9e7}
    .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.btn{appearance:none;background:#142b56;color:#fff;border:1px solid #294579;padding:11px 14px;border-radius:11px;cursor:pointer;font-weight:750;min-height:44px}.btn:active{transform:translateY(1px)}.btn.good{background:#0e5b41;border-color:#147b58}.btn.start{background:#087443;border-color:#11a45f;font-size:16px}.btn.stop{background:#5c3d0d;border-color:#8a5d13}.btn.danger{background:#68172a;border-color:#9a2841}.btn.ghost{background:#0b162d}.btn.active{outline:2px solid #6fa8ff;box-shadow:0 0 0 3px #6fa8ff22}
    .heroControls{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:10px}.heroControls .danger{grid-column:1/-1}.pill{display:inline-block;padding:4px 9px;border-radius:999px;font-weight:800;font-size:12px}.pill.good{background:#073c2d;color:#4bffc0}.pill.bad{background:#461425;color:#ff83a3}.pill.neutral{background:#16284a;color:#bdd0ff}.kv{display:grid;grid-template-columns:145px 1fr;gap:8px 12px;align-items:center}.kv .k{color:#a8b7d2}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.small{font-size:12px}.muted{color:#92a4c3;font-size:12px}.banner{padding:11px 12px;border-radius:11px;margin-bottom:12px;font-weight:800}.banner.bad{background:#471625;border:1px solid #92324e}.banner.good{background:#0e3a2d;border:1px solid #1e7255}.banner.warn{background:#49370e;border:1px solid #85661c}.statusline{display:flex;gap:8px;align-items:center}.dot{width:10px;height:10px;border-radius:50%;background:#71809d}.dot.ok{background:#20d98b;box-shadow:0 0 10px #20d98b}.dot.bad{background:#ff5579}
    input,select{background:#081327;color:#eaf0ff;border:1px solid #29406b;border-radius:10px;padding:11px;font-size:15px;min-height:44px}.tableWrap{overflow:auto;max-width:100%;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}th,td{padding:9px 8px;border-bottom:1px solid #21345a;text-align:left;white-space:nowrap}th{color:#aebedb;position:sticky;top:0;background:#0f1a33}.pos{color:#6fffc4}.neg{color:#ff7897}.navActions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.sectionSpace{margin-top:14px}.lastId{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.loginTag{font-size:12px;color:#9eb0ce}
    .installHelp{display:none;background:#10264a;border:1px solid #2f5388;border-radius:12px;padding:10px;margin-top:8px;color:#d6e4ff;font-size:13px}
    @media(max-width:760px){.wrap{padding:8px 8px 28px}.grid{grid-template-columns:1fr}.topbar{padding-top:max(7px,env(safe-area-inset-top))}h1{font-size:21px}.card{padding:12px;border-radius:14px}.heroControls{grid-template-columns:1fr 1fr}.btn{padding:12px 13px;flex:1}.row .btn{min-width:calc(50% - 6px)}.kv{grid-template-columns:120px 1fr;font-size:14px}.navActions .btn{min-width:auto;flex:0 0 auto}.lastId{display:none}.desktopOnly{display:none}.tableWrap{margin:0 -4px}table{font-size:12px}.symbolsTable{min-width:1050px}}
  </style>
</head>
<body><div class="wrap">
  <div class="topbar">
    <div><h1>Trading Bot</h1><div class="sub">AUTO 24/7 • PC + mobil</div></div>
    <div class="navActions"><div class="statusline"><span id="liveDot" class="dot"></span><span id="liveText" class="loginTag">kobler til…</span></div><button id="installBtn" class="btn ghost" onclick="installApp()">Installer app</button><a class="btn ghost" href="/logout">Logg ut</a></div>
  </div>

  <div id="installHelp" class="installHelp"></div>

  <div class="grid">
    <div class="card">
      <h2>Kontroll</h2>
      <div class="heroControls">
        <button class="btn start" onclick="startTrading()">▶ START TRADING</button>
        <button class="btn stop" onclick="stopTrading()">■ STOPP TRADING</button>
        <button class="btn danger" onclick="emergencyStop()">⛔ NØDSTOPP</button>
      </div>
      <div class="muted">START = armed + resume. STOPP = disarm + pause. NØDSTOPP blokkerer nye handler umiddelbart, men lukker ikke automatisk eksisterende posisjoner.</div>

      <h3>Marked</h3><div class="row"><button id="m_auto" class="btn" onclick="setMarket('auto')">AUTO 24/7</button><button id="m_stocks" class="btn" onclick="setMarket('stocks')">AKSJER</button><button id="m_crypto" class="btn" onclick="setMarket('crypto')">KRYPTO</button></div>
      <h3>Aktivitetsprofil</h3><div class="row"><button class="btn good" onclick="setProfile('active')">AKTIV</button><button class="btn" onclick="setProfile('balanced')">BALANSERT</button></div>
      <h3>Strategi</h3><div class="row"><select id="strategySel"><option value="scalp">scalp</option><option value="hype">hype</option><option value="trend">trend</option><option value="cross">cross</option></select><button class="btn" onclick="setStrategy()">Bruk strategi</button></div>
      <h3>Avansert</h3><div class="row"><button class="btn ghost" onclick="toggleBool('extended_hours')">Extended av/på</button><button class="btn ghost" onclick="toggleBool('test_mode')">TestMode av/på</button><button class="btn ghost" onclick="postControl('/api/control',{kill:false})">Nullstill nødstop</button></div>
      <div id="paperTests"><h3>Testordre (kun paper)</h3><div class="row"><input id="buySym" value="AAPL" style="width:120px"><input id="buyQty" type="number" value="1" step="0.001" style="width:100px"><button class="btn good" onclick="testBuy()">TEST BUY</button></div><div class="row" style="margin-top:8px"><input id="sellSym" value="AAPL" style="width:120px"><button class="btn danger" onclick="testSellAll()">TEST SELL ALL</button></div></div>
    </div>

    <div class="card">
      <h2>Status</h2><div id="authBanner" class="banner warn">Kobler til motoren…</div>
      <div class="kv">
        <div class="k">Motor</div><div id="st_engine">-</div><div class="k">Alpaca</div><div id="st_auth">-</div><div class="k">Trading</div><div id="st_running">-</div><div class="k">Marked</div><div id="st_market">-</div><div class="k">Aksje-session</div><div id="st_session">-</div><div class="k">Crypto 24/7</div><div id="st_crypto">-</div><div class="k">Paper</div><div id="st_paper">-</div><div class="k">Strategi</div><div id="st_strategy">-</div><div class="k">Intelligence</div><div id="st_intel">-</div><div class="k">Edge-motor</div><div id="st_edge">-</div><div class="k">Markedsregime</div><div id="st_regime">-</div><div class="k">Global risiko</div><div id="st_risk">-</div><div class="k">Siste nyhet</div><div id="st_news" class="small">-</div><div class="k">PnL i dag</div><div id="st_pnl">-</div><div class="k">Sist oppdatert</div><div class="mono small" id="st_last">-</div><div class="k">Siste ordre</div><div class="mono small lastId" id="lastOrderId">-</div>
      </div><div class="muted" id="st_notes" style="margin-top:10px"></div>
    </div>
  </div>

  <div class="card sectionSpace"><div class="row" style="justify-content:space-between"><h2>Symboler</h2><button class="btn ghost" onclick="refresh(true)">Oppdater</button></div><div class="tableWrap"><table class="symbolsTable"><thead><tr><th>Marked</th><th>Symbol</th><th>Pris</th><th>Bid</th><th>Ask</th><th>Spread bps</th><th>Edge</th><th>Score</th><th>Signal</th><th>Pos</th><th>Avg</th><th>Action</th><th>Feil</th></tr></thead><tbody id="tblSymbols"></tbody></table></div></div>
  <div class="card sectionSpace"><div class="row" style="justify-content:space-between"><h2>Orders (siste 30)</h2><button class="btn ghost" onclick="refresh(true)">Refresh orders</button></div><div class="tableWrap"><table><thead><tr><th>Tid</th><th>Symbol</th><th>Side</th><th>Qty/notional</th><th>Type</th><th>Status</th><th>ID</th></tr></thead><tbody id="tblOrders"></tbody></table></div></div>
</div>
<script>
let installPrompt=null, alertsEnabled=false;
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e;document.getElementById('installBtn').style.display='inline-block'});
if('serviceWorker' in navigator){window.addEventListener('load',()=>navigator.serviceWorker.register('/service-worker.js').catch(()=>{}));}
async function installApp(){const h=document.getElementById('installHelp');if(installPrompt){installPrompt.prompt();await installPrompt.userChoice;installPrompt=null;h.style.display='none';return;}const ios=/iphone|ipad|ipod/i.test(navigator.userAgent);h.style.display='block';h.innerHTML=ios?'På iPhone/iPad: trykk <b>Del</b> → <b>Legg til på Hjem-skjerm</b>.':'I nettlesermenyen: velg <b>Installer app</b> eller <b>Legg til på startskjermen</b>.';}
function pill(v){let c='neutral';if(v===true||String(v)==='true')c='good';if(v===false||String(v)==='false')c='bad';return `<span class="pill ${c}">${v}</span>`}
function fmt(x){if(x===null||x===undefined||x==='')return '-';const n=Number(x);if(Number.isNaN(n))return String(x);const a=Math.abs(n);return a>=100?n.toFixed(2):a>=1?n.toFixed(4):n.toFixed(6)}
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
function renderSymbols(s){const b=document.getElementById('tblSymbols');b.innerHTML='';Object.keys(s||{}).sort((a,c)=>(s[a]?.market==='CRYPTO'?0:1)-(s[c]?.market==='CRYPTO'?0:1)||a.localeCompare(c)).forEach(sym=>{const x=s[sym]||{},sig=x.signal===1?'BUY':x.signal===-1?'SELL':(x.signal??'0');const tr=document.createElement('tr');tr.innerHTML=`<td>${x.market||'-'}</td><td><b>${sym}</b></td><td>${fmt(x.price)}</td><td>${fmt(x.bid)}</td><td>${fmt(x.ask)}</td><td>${x.debug?.spread_bps??'-'}</td><td>${x.debug?.prob_up_pct!=null?('P '+x.debug.prob_up_pct+'% / EV '+(x.debug.ev_bps>=0?'+':'')+x.debug.ev_bps+'bp'):'-'}</td><td><b>${x.debug?.intel_score??'-'}</b></td><td>${sig}</td><td>${x.pos_qty??0}</td><td>${fmt(x.avg_entry)}</td><td>${x.action||'NONE'}</td><td class="neg">${x.error||''}</td>`;b.appendChild(tr)})}
function renderOrders(o){const b=document.getElementById('tblOrders');b.innerHTML='';(o||[]).forEach(x=>{const tr=document.createElement('tr'),t=String(x.filled_at||x.submitted_at||x.created_at||'').replace('T',' ').replace('Z','');tr.innerHTML=`<td class="mono small">${t||'-'}</td><td><b>${x.symbol||''}</b></td><td>${x.side||''}</td><td>${x.qty||x.notional||''}</td><td>${x.type||''}</td><td>${x.status||''}</td><td class="mono small">${x.id||''}</td>`;b.appendChild(tr)})}
async function refresh(force=false){try{const r=await apiFetch('/api/status?ts='+Date.now()+(force?'&force=1':''));const s=await r.json();const live=Boolean(s.auth_ok)&&String(s.engine_state||'').toUpperCase()==='RUNNING';document.getElementById('liveDot').className='dot '+(live?'ok':'bad');document.getElementById('liveText').textContent=live?'ONLINE':'IKKE KLAR';const running=Boolean(s.armed)&&!Boolean(s.paused)&&!Boolean(s.kill);document.getElementById('st_engine').textContent=s.engine_state||'-';document.getElementById('st_auth').innerHTML=pill(s.auth_ok);document.getElementById('st_running').innerHTML=pill(running);document.getElementById('st_market').textContent=(s.market_mode||'auto').toUpperCase();document.getElementById('st_session').textContent=s.stock_session||'-';document.getElementById('st_crypto').innerHTML=pill(s.crypto_24_7);document.getElementById('st_paper').innerHTML=pill(s.paper);document.getElementById('st_strategy').textContent=s.strategy_mode||'-';const ii=s.intelligence||{};document.getElementById('st_intel').innerHTML=pill(ii.enabled!==false);const pe=s.profit_engine||{};document.getElementById('st_edge').innerHTML=pill(pe.enabled!==false);document.getElementById('st_regime').textContent=(ii.regime_label||'-')+' '+(ii.regime_score??'');document.getElementById('st_risk').textContent=(ii.global_risk??'-')+'/100';document.getElementById('st_news').textContent=ii.last_headline||'-';document.getElementById('st_pnl').textContent=fmt(s.day_pnl_usd);document.getElementById('st_last').textContent=s.last_update||'';document.getElementById('st_notes').textContent=s.notes||'';document.getElementById('strategySel').value=s.strategy_mode||'scalp';document.getElementById('paperTests').style.display=s.paper===false?'none':'block';['auto','stocks','crypto'].forEach(m=>document.getElementById('m_'+m).classList.toggle('active',(s.market_mode||'auto')===m));const ab=document.getElementById('authBanner');if(s.auth_ok===false){ab.className='banner bad';ab.textContent='Alpaca avviser API-nøklene.'}else if(live){ab.className='banner good';ab.textContent=running?'Motor ONLINE – trading er STARTET.':'Motor ONLINE – trading er STOPPET/PAUSET.'}else{ab.className='banner warn';ab.textContent='Motoren starter eller restarter…'}renderSymbols(s.symbols||{});renderOrders(s.orders||[]);const lo=(s.orders||[])[0]?.id||'-';document.getElementById('lastOrderId').textContent=lo}catch(e){document.getElementById('liveDot').className='dot bad';document.getElementById('liveText').textContent='OFFLINE'}}
setInterval(()=>refresh(false),2500);refresh(false);
</script></body></html>
"""


@app.get("/")
def home():
    return render_template_string(HTML)


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
    return jsonify({"ok": bool(ok), "engine_state": engine or "STARTING", "status_age_sec": age, "version": BOT_VERSION}), (200 if ok else 503)


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
        "activity_profile","intelligence_enabled","intelligence_entry_score","intelligence_exit_score"
    }
    clean = {k:v for k,v in patch.items() if k in allowed}
    return jsonify(patch_control(clean))


@app.get("/api/status")
def api_status():
    st = safe_read_json(STATUS_PATH, {})
    defaults = {"paper":True,"auth_ok":None,"engine_state":"STARTING","armed":False,"paused":True,"kill":False,"extended_hours":False,"auto_extended":False,"market_mode":"auto","stock_session":"CLOSED","crypto_24_7":True,"test_mode":False,"strategy_mode":"scalp","last_update":"","day_pnl_usd":0,"notes":"","symbols":{},"orders":[]}
    if isinstance(st, dict): defaults.update(st)
    return jsonify(defaults)


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

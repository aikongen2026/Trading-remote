"""Browser OAuth bootstrap for the SIM sidecar; never submits trading orders."""
from __future__ import annotations

import base64
import hmac
import secrets
import threading
import time
from urllib.parse import urlencode

from flask import make_response, redirect, render_template_string, request

SIM_AUTHORIZE_URL = "https://sim.logonvalidation.net/authorize"
SIM_TOKEN_URL = "https://sim.logonvalidation.net/token"
SIM_REDIRECT_URI = "https://trading-remote.onrender.com/saxo/oauth/callback"
COOKIE = "__Host-saxo_sim_oauth"
START_COOKIE = "__Host-saxo_sim_start"
FLOW_TTL = 600

PAGE = """<!doctype html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Saxo SIM-innlogging</title><style>
body{font:16px system-ui;background:#08101d;color:#eef4ff;margin:0;padding:32px 20px}
main{max-width:620px;margin:6vh auto;padding:28px;background:#0f1a33;border:1px solid #30466f;border-radius:16px}
h1{font-size:26px}p,li{line-height:1.6}a{color:#8acfff}button{padding:14px 20px;background:#16754f;color:white;border:0;border-radius:8px;font:inherit;font-weight:700;cursor:pointer}
code{overflow-wrap:anywhere}</style></head><body><main><h1>Saxo SIM-innlogging</h1>
<p>{{ message }}</p>
{% if missing %}<p>Legg inn disse variablene i Render, og velg Save, rebuild, and deploy:</p>
<ul>{% for name in missing %}<li><code>{{ name }}</code></li>{% endfor %}</ul>{% endif %}
{% if csrf %}<form method="post" action="/saxo/oauth/start">
<input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Logg inn hos Saxo SIM</button></form>{% endif %}
<p><a href="/">Tilbake til TradingBot</a></p></main></body></html>"""


def register_saxo_oauth(app, engine):
    """Register on the password-protected dashboard, retaining Strict session cookies.

    Only the callback is exempt from dashboard login. It instead requires both
    a one-time server-held state and its own short-lived browser-bound Lax cookie.
    App credentials remain in Render; neither tokens nor authorization codes
    are returned in HTML, dashboard status, cookies, or application logs.
    """
    pending = {}
    start_tokens = {}
    pending_lock = threading.Lock()

    def page(message, *, status=200, csrf=None, missing=None):
        response = make_response(render_template_string(
            PAGE, message=message, csrf=csrf, missing=missing or [],
        ), status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://sim.logonvalidation.net; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def config_missing():
        return [name for name, value in (
            ("SAXO_SIM_APP_KEY", engine.app_key),
            ("SAXO_SIM_APP_SECRET", engine.app_secret),
        ) if not value]

    @app.get("/saxo/oauth")
    def saxo_oauth_setup():
        missing = config_missing()
        if not engine.enabled:
            missing.insert(0, "SAXO_SIM_ENABLED=true")
        if missing:
            return page("Saxo SIM trenger appopplysningene før du kan logge inn.", missing=missing)
        # Permanent Flask sessions are refreshed by dashboard background polls.
        # A late poll can overwrite a cookie carrying a newly added CSRF value.
        # Keep form protection in its own browser-bound HttpOnly Strict cookie
        # plus server-side expiry; do not mutate the dashboard session.
        now = time.monotonic()
        csrf = request.cookies.get(START_COOKIE, "")
        with pending_lock:
            for token in list(start_tokens):
                if start_tokens[token] <= now:
                    del start_tokens[token]
            if csrf not in start_tokens:
                csrf = secrets.token_urlsafe(32)
                if len(start_tokens) >= 128:
                    return page("For mange pågående innlogginger. Prøv igjen om noen minutter.", status=429)
                start_tokens[csrf] = now + FLOW_TTL
        response = page("Koble TradingBot til Saxos demomiljø. Innloggingen sender ingen handelsordrer.", csrf=csrf)
        response.set_cookie(START_COOKIE, csrf, max_age=FLOW_TTL,
                            secure=True, httponly=True, samesite="Strict", path="/")
        return response

    @app.post("/saxo/oauth/start")
    def saxo_oauth_start():
        expected = request.cookies.get(START_COOKIE, "")
        supplied = request.form.get("csrf", "")
        with pending_lock:
            valid_form = (bool(expected) and start_tokens.get(supplied, 0) > time.monotonic()
                          and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")))
        if not valid_form:
            # Recover a stale/cached form without bypassing validation or
            # starting any OAuth request. The fresh page issues a new form.
            response = redirect("/saxo/oauth", code=303)
            response.headers["Cache-Control"] = "no-store"
            return response
        if config_missing() or not engine.enabled:
            return page("Saxo SIM-oppsettet mangler. Kontroller variablene i Render.", status=409)
        state = secrets.token_urlsafe(32)
        browser_nonce = secrets.token_urlsafe(32)
        now = time.monotonic()
        with pending_lock:
            for key in list(pending):
                if pending[key][1] <= now:
                    del pending[key]
            if len(pending) >= 32:
                return page("For mange pågående innlogginger. Prøv igjen om noen minutter.", status=429)
            pending[state] = (browser_nonce, now + FLOW_TTL)
        response = redirect(SIM_AUTHORIZE_URL + "?" + urlencode({
            "response_type": "code", "client_id": engine.app_key,
            "redirect_uri": SIM_REDIRECT_URI, "state": state,
        }))
        response.set_cookie(COOKIE, browser_nonce, max_age=FLOW_TTL,
                            secure=True, httponly=True, samesite="Lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/saxo/oauth/callback")
    def saxo_oauth_callback():
        state = request.args.get("state", "")
        cookie = request.cookies.get(COOKIE, "")
        with pending_lock:
            flow = pending.get(state)
            valid = (len(request.args.getlist("state")) == 1 and flow is not None
                     and flow[1] > time.monotonic() and bool(cookie)
                     and hmac.compare_digest(flow[0].encode("utf-8"), cookie.encode("utf-8")))
            if valid:
                del pending[state]
        def finish(outcome):
            # Remove the short-lived authorization code from the address bar.
            response = redirect("/saxo/oauth/result?outcome=" + outcome, code=303)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="Lax", path="/")
            return response
        if not valid:
            return finish("invalid")
        code = request.args.get("code", "")
        if request.args.get("error") or len(request.args.getlist("code")) != 1 or not code or len(code) > 8192:
            return finish("declined")
        elif config_missing() or not engine.enabled:
            return finish("config")
        else:
            try:
                # Share the refresh lock with the running sidecar so bootstrap
                # cannot overwrite a simultaneously rotated token pair.
                with engine.lock:
                    if not engine._render_allows_norway() or not engine._budget_ok():
                        return finish("guard")
                    raw_auth = f"{engine.app_key}:{engine.app_secret}".encode("utf-8")
                    token_response = engine.session.post(
                        SIM_TOKEN_URL,
                        headers={"Authorization": "Basic " + base64.b64encode(raw_auth).decode("ascii"),
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        data={"grant_type": "authorization_code", "code": code,
                              "redirect_uri": SIM_REDIRECT_URI},
                        timeout=20, allow_redirects=False,
                    )
                    if token_response.status_code != 200:
                        raise ValueError("Token exchange rejected")
                    data = token_response.json()
                    access = data.get("access_token") if isinstance(data, dict) else None
                    refresh = data.get("refresh_token") if isinstance(data, dict) else None
                    if not isinstance(access, str) or not access.strip() or not isinstance(refresh, str) or not refresh.strip():
                        raise ValueError("Token pair missing")
                    now = time.time()
                    access_ttl = engine._expiry(data.get("expires_in"))
                    refresh_ttl = engine._expiry(data.get("refresh_token_expires_in"))
                    engine.access_token = access.strip()
                    engine.refresh_token = refresh.strip()
                    engine.token_expires_at = now + access_ttl if access_ttl else 0.0
                    engine.refresh_expires_at = now + refresh_ttl if refresh_ttl else 0.0
                    engine.auth_error = None
                    engine.token_store_error = None
                    engine.account_connected = False
                    engine._tokens_dirty = True
                    engine._persist_tokens()
                    # The freshly persisted token pair must survive accounting
                    # failure. Do not expose response bodies through errors.
                    try:
                        engine._record_bytes(len(code.encode("utf-8")), len(token_response.content or b""))
                    except OSError:
                        pass
                return finish("success")
            except Exception:
                # Network/storage errors may carry credentials or codes. Never
                # include exception strings or raw OAuth errors in the response.
                return finish("failed")

    @app.get("/saxo/oauth/result")
    def saxo_oauth_result():
        # This informational page contains no credentials or account state.
        # The dashboard's account_connected field is the authoritative check.
        messages = {
            "success": "SIM-innloggingen er mottatt og lagret. Kontroller kontotilkoblingen i dashboardet, normalt innen to minutter.",
            "invalid": "Innloggingen er utløpt eller startet i en annen nettleser. Start på nytt fra TradingBot.",
            "declined": "Saxo-innloggingen ble ikke fullført. Start på nytt fra TradingBot.",
            "config": "Saxo SIM-oppsettet mangler. Kontroller variablene i Render.",
            "guard": "SIM-innloggingen er midlertidig stoppet av trafikkgrensen.",
            "failed": "Saxo SIM-innloggingen kunne ikke lagres eller fullføres. Kontroller appopplysningene og prøv igjen fra TradingBot.",
        }
        return page(messages.get(request.args.get("outcome"), "Kontroller Saxo SIM-status i TradingBot."))

"""Microsoft OAuth authorization for hourly offsite evidence backups.

The initial page requires an authenticated dashboard session. The redirect
callback requires its own one-time state, PKCE verifier and Lax browser cookie.
No Graph tokens, authorization codes or secrets enter the dashboard HTML.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlencode

from flask import make_response, redirect, render_template_string, request

from onedrive_backup import AUTH_BASE, REDIRECT_URI, SCOPE

COOKIE = "__Host-tradingbot_onedrive"
FLOW_TTL = 600
PAGE = """<!doctype html><html lang="no"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OneDrive sikkerhetskopi</title><style>
body{margin:0;padding:24px;background:#08101d;color:#e6f8ff;font:16px system-ui}
main{max-width:650px;margin:8vh auto;padding:28px;border:1px solid #247fb8;border-radius:14px;background:#0b2039}
a{color:#8fe8ff}code{overflow-wrap:anywhere}li{line-height:1.7}
</style></head><body><main><h2>OneDrive – Trading Bot backup</h2>
<p>{{ message }}</p>
{% if steps %}<ol>{% for step in steps %}<li><code>{{ step }}</code></li>{% endfor %}</ol>{% endif %}
<p><a href="/onedrive/backup">Kontroller / koble til OneDrive</a> · <a href="/">Trading Bot</a></p>
</main></body></html>"""


def register_onedrive_oauth(app, backup):
    pending = {}
    lock = threading.Lock()

    def page(message, steps=None):
        resp = make_response(render_template_string(PAGE, message=message, steps=steps or []))
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
        )
        return resp

    @app.get("/onedrive/backup")
    def onedrive_backup_home():
        if not backup.configured():
            return page("Kobling venter på Microsoft-appregistrering i Render. Ingen filer er slettet.",
                        ["ONEDRIVE_CLIENT_ID = Application (client) ID fra Microsoft Entra",
                         "ONEDRIVE_CLIENT_SECRET = ny Client Secret (lagres kun i Render Environment)",
                         "Redirect URI for Web = " + REDIRECT_URI,
                         "API: Microsoft Graph delegated Files.ReadWrite, User.Read og offline_access"])
        if backup.connected():
            status = backup.status()
            return page(
                "Microsoft-tilkoblingen er registrert. Siste verifiserte sikkerhetskopi: " +
                str(status["last_success_utc"] or "ingen ennå") + ".",
                ["Mappe: " + status["destination"],
                 "Antall databaser i siste verifiserte kopi: " + str(status["files_verified"]),
                 "Backupfeil (hvis noen): " + str(status["last_error"] or "ingen"),
                 "Kun PAPER. Originale databaser og ordrebeskyttelse beholdes på Render."],
            )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        with lock:
            now = time.monotonic()
            for key in list(pending):
                if pending[key][2] <= now:
                    del pending[key]
            if len(pending) >= 24:
                return page("For mange innlogginger. Prøv igjen om noen minutter.")
            pending[state] = (nonce, verifier, now + FLOW_TTL)
        url = f"{AUTH_BASE}/{backup.tenant}/oauth2/v2.0/authorize?" + urlencode({
            "client_id": backup.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "response_mode": "query", "scope": SCOPE, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "select_account",
        })
        response = redirect(url)
        response.set_cookie(COOKIE, nonce, max_age=FLOW_TTL, secure=True,
                            httponly=True, samesite="Lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/onedrive/oauth/callback")
    def onedrive_oauth_callback():
        state = request.args.get("state", "")
        browser = request.cookies.get(COOKIE, "")
        with lock:
            flow = pending.get(state)
            valid = (len(request.args.getlist("state")) == 1 and flow is not None
                     and flow[2] > time.monotonic() and bool(browser)
                     and hmac.compare_digest(flow[0], browser))
            if valid:
                del pending[state]

        def finish(label):
            response = redirect("/onedrive/oauth/result?outcome=" + label, code=303)
            response.delete_cookie(COOKIE, secure=True, httponly=True,
                                   samesite="Lax", path="/")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        if not valid:
            return finish("invalid")
        code = request.args.get("code", "")
        if request.args.get("error") or len(request.args.getlist("code")) != 1 or not code or len(code) > 8192:
            return finish("declined")
        try:
            backup.complete_login(code, flow[1])
            return finish("connected")
        except Exception:
            # Never expose Graph response bodies, OAuth codes, or tokens.
            return finish("failed")

    @app.get("/onedrive/oauth/result")
    def onedrive_oauth_result():
        messages = {
            "connected": "OneDrive er koblet til. Boten starter første kontrollerte backup automatisk. Ingen lokale data slettes før backup er kontrollert.",
            "invalid": "Innloggingen er utløpt eller fullført i en annen nettleser.",
            "declined": "Microsoft-innloggingen ble avbrutt.",
            "failed": "Kunne ikke koble til OneDrive. Kontroller Microsoft-appens tillatelser, oppgitt konto og mappen AI/TR bots/Lagring.",
        }
        return page(messages.get(request.args.get("outcome"), "Ukjent innloggingsstatus."))

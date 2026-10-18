# ruff: noqa: E501  (embedded HTML/JS of the sign-in page)
"""Interactive API docs (``/api/docs``, ``/api/redoc``, ``/api/openapi.json``) with an
administrator gate.

* dev / test / qa / staging: open, exactly like FastAPI's defaults.
* prod (``config.DOCS_REQUIRE_ADMIN``): only administrator accounts. A browser has no bearer
  token, so ``/api/docs`` shows a small sign-in page instead of a 401: it derives the auth key
  from e-mail + password *in the browser* (same PBKDF2/HKDF as the PWA — the password never
  reaches the server), signs in through ``/api/v1/auth/login`` like any device, and hands the
  session token to ``POST /api/docs/login``, which checks the administrator role and stores the
  token in an HttpOnly cookie scoped to ``/api``. API clients can use ``Authorization: Bearer``
  instead. ``POST /api/docs/logout`` clears the cookie and revokes that session.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

import accounts
import auth
import config
import models
from database import SessionLocal

COOKIE = "pa_docs_session"
DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"
REDOC_URL = "/api/redoc"


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE)


def _admin_for(db: Session, token: str | None) -> auth.Principal | None:
    """The administrator this token belongs to, or None (invalid, expired, revoked, not admin)."""
    if not token:
        return None
    try:
        principal = auth.current_principal(f"Bearer {token}", db)
    except HTTPException:
        return None
    return principal if principal.account.role == models.ROLE_ADMIN else None


def allowed(request: Request) -> bool:
    if not config.DOCS_REQUIRE_ADMIN:
        return True
    with SessionLocal() as db:
        ok = _admin_for(db, _token_from(request)) is not None
        db.commit()  # current_principal touches last_seen_at
        return ok


def _set_cookie(response: Response, token: str | None) -> None:
    if token:
        response.set_cookie(
            COOKIE,
            token,
            max_age=config.TOKEN_TTL_SECONDS,
            path="/api",
            httponly=True,
            secure=config.ENV == "prod",
            samesite="strict",
        )
    else:
        response.delete_cookie(COOKIE, path="/api")


def install(app: FastAPI) -> None:
    """Replace FastAPI's built-in docs routes (the app is created with docs_url=None …)."""
    router = APIRouter(include_in_schema=False)

    @router.get(OPENAPI_URL)
    def openapi(request: Request):
        if not allowed(request):
            return JSONResponse({"detail": "Administrator account required."}, status_code=401)
        return JSONResponse(app.openapi())

    @router.get(DOCS_URL, response_class=HTMLResponse)
    def docs(request: Request):
        if not allowed(request):
            return HTMLResponse(gate_page(), status_code=401)
        return get_swagger_ui_html(openapi_url=OPENAPI_URL, title=f"{app.title} — docs")

    @router.get(REDOC_URL, response_class=HTMLResponse)
    def redoc(request: Request):
        if not allowed(request):
            return HTMLResponse(gate_page(), status_code=401)
        return get_redoc_html(openapi_url=OPENAPI_URL, title=f"{app.title} — ReDoc")

    @router.post(DOCS_URL + "/login")
    async def docs_login(request: Request):
        """Body: {"token": "<session token from /api/v1/auth/login>"} → cookie for the docs."""
        try:
            body = await request.json()
            token = str(body.get("token", ""))
        except (ValueError, AttributeError):
            token = ""
        with SessionLocal() as db:
            principal = _admin_for(db, token)
            if principal is None:
                raise HTTPException(status_code=403, detail="This needs an administrator account.")
            accounts.audit(db, principal.account, "docs.login", principal.account, device=principal.device.id)
            db.commit()
        response = JSONResponse({"ok": True, "redirect": DOCS_URL})
        _set_cookie(response, token)
        return response

    @router.post(DOCS_URL + "/logout")
    def docs_logout(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            with SessionLocal() as db:
                principal = _admin_for(db, token)
                if principal:
                    principal.device.revoked_at = models.utcnow()
                    db.commit()
        response = RedirectResponse(DOCS_URL, status_code=303)
        _set_cookie(response, None)
        return response

    app.include_router(router)


def gate_page() -> str:
    """Administrator sign-in for the docs. Key derivation mirrors apps/web/src/app/core/sync/crypto.ts."""
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>API docs — administrator sign-in</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:420px;margin:60px auto;padding:0 16px;line-height:1.5;color:#111827}}
label{{display:block;font-size:.9rem;color:#6b7280;margin-top:12px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font-size:16px;border:1px solid #e2e5ea;border-radius:8px}}
button{{margin-top:16px;padding:10px 18px;font-size:16px;border-radius:8px;border:none;background:#4f46e5;color:#fff;cursor:pointer}}
button[disabled]{{opacity:.6}}
.err{{color:#dc2626;margin-top:12px;min-height:1.5em}}
.muted{{color:#6b7280;font-size:.85rem}}
</style>
<h2>API documentation</h2>
<p>In production the interactive docs are available to <strong>administrator accounts</strong> only.
Sign in with your account — the password is turned into a key in this browser and never sent.</p>
<form id="f">
<label for="email">E-mail</label><input id="email" type="email" autocomplete="username" required>
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password" required>
<button id="go" type="submit">Sign in</button>
<div class="err" id="err" role="alert"></div>
</form>
<p class="muted">Environment: {config.ENV}. Not an administrator? Ask one to promote your account from the admin panel.</p>
<script>
const enc = new TextEncoder();
const hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
async function deriveAuthKey(password, email) {{
  const salt = await crypto.subtle.digest('SHA-256', enc.encode(email.trim().toLowerCase()));
  const base = await crypto.subtle.importKey('raw', enc.encode(password.normalize('NFKC')), 'PBKDF2', false, ['deriveBits']);
  const master = await crypto.subtle.deriveBits({{ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: 600000 }}, base, 256);
  const hkdf = await crypto.subtle.importKey('raw', master, 'HKDF', false, ['deriveBits']);
  return hex(await crypto.subtle.deriveBits({{ name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: enc.encode('auth') }}, hkdf, 256));
}}
document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const go = document.getElementById('go'), err = document.getElementById('err');
  go.disabled = true; err.textContent = ''; go.textContent = 'Signing in…';
  try {{
    if (!crypto.subtle) throw new Error('This page needs HTTPS (or localhost) for WebCrypto.');
    const email = document.getElementById('email').value, password = document.getElementById('password').value;
    const auth_key = await deriveAuthKey(password, email);
    let r = await fetch('/api/v1/auth/login', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ email, auth_key, device: {{ platform: 'api-docs' }} }}) }});
    let data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Sign-in failed.');
    r = await fetch('{DOCS_URL}/login', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      credentials: 'same-origin', body: JSON.stringify({{ token: data.token }}) }});
    data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Not allowed.');
    location.href = data.redirect || '{DOCS_URL}';
  }} catch (ex) {{
    err.textContent = ex.message || String(ex);
    go.disabled = false; go.textContent = 'Sign in';
  }}
}});
</script></html>"""

"""Authentication: client-derived auth keys, stateless signed access tokens, device identity.

The client derives ``auth_key`` from the password on the device (PBKDF2/HKDF, see
apps/web/src/app/core/sync/crypto.ts); the password itself never reaches us. We salt+hash the
auth key again (scrypt) so a database leak does not yield login credentials. Tokens are
``base64(payload).base64(hmac)`` — standard library only, no python-jose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

import config
import models
from database import get_db

# ---------------------------------------------------------------------------- hashing


def hash_auth_key(auth_key: str, salt: str) -> str:
    digest = hashlib.scrypt(auth_key.encode("utf-8"), salt=salt.encode("utf-8"), n=2**14, r=8, p=1, dklen=32)
    return digest.hex()


def verify_auth_key(auth_key: str, salt: str, expected_hex: str) -> bool:
    return hmac.compare_digest(hash_auth_key(auth_key, salt), expected_hex)


def new_salt() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------- tokens


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(account_id: str, device_id: str, generation: int, ttl: int | None = None) -> str:
    payload = {
        "sub": account_id,
        "dev": device_id,
        "gen": generation,
        "exp": int(time.time()) + (ttl or config.TOKEN_TTL_SECONDS),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(config.TOKEN_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(config.TOKEN_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------- dependency


@dataclass
class Principal:
    account: models.Account
    device: models.Device


def current_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Please sign in to use this feature.")
    payload = decode_token(authorization[7:].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    device = db.get(models.Device, payload["dev"])
    account = db.get(models.Account, payload["sub"])
    if not device or not account or device.account_id != account.id or account.deleted_at or device.revoked_at:
        raise HTTPException(status_code=401, detail="This device was signed out. Please sign in again.")
    if device.token_generation != payload.get("gen"):
        raise HTTPException(status_code=401, detail="This device was signed out. Please sign in again.")
    device.last_seen_at = models.utcnow()
    return Principal(account=account, device=device)


def apply_bootstrap_role(account: models.Account) -> None:
    """ADMIN_EMAILS (config) promotes an account on register/login — the way the first
    administrator of a fresh deployment is created. Never demotes."""
    if account.email.lower() in config.ADMIN_EMAILS and account.role != models.ROLE_ADMIN:
        account.role = models.ROLE_ADMIN


def admin_principal(p: Principal = Depends(current_principal)) -> Principal:
    """A signed-in *administrator account* (role = admin). Used by /admin/*: every action is
    attributable to a person and lands in the audit log."""
    if p.account.role != models.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="This needs an administrator account.")
    return p


def _pipeline_token_ok(x_admin_token: str | None) -> bool:
    return bool(
        config.RELEASE_ADMIN_TOKEN and x_admin_token and hmac.compare_digest(x_admin_token, config.RELEASE_ADMIN_TOKEN)
    )


def require_admin(
    x_admin_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal | None:
    """Either the deploy pipeline's shared token (X-Admin-Token) or a signed-in administrator
    account. Returns the principal for the latter (None for the pipeline)."""
    if _pipeline_token_ok(x_admin_token):
        return None
    if authorization:
        p = current_principal(authorization, db)
        if p.account.role == models.ROLE_ADMIN:
            return p
    raise HTTPException(status_code=403, detail="Admin token or administrator account required.")

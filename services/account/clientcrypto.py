"""Python twin of the PWA's key derivation (apps/web/src/app/core/sync/crypto.ts).

The server never needs this to *operate* — it only ever sees the derived auth key. It exists so
that accounts created on the server side (seed.py: fake users for testing) are real accounts
the PWA can sign in to: the auth key must match what the device derives, and the wrapped data
key must unwrap with the device's wrap key. Parameters are pinned to KDF_PARAMS in crypto.ts.

    password + email ──PBKDF2-SHA256(600k)──▶ master ──HKDF("auth")──▶ auth key (hex)
                                                      ──HKDF("wrap")──▶ wrap key (AES-256-GCM)
    wrapped_data_key = base64(iv ‖ AES-GCM(wrap key, data key, aad="data-key"))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

KDF_PARAMS = {"kdf": "pbkdf2-sha256", "iterations": 600_000, "hkdf": "sha256", "v": 1}


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"\x00" * 32) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def derive_keys(password: str, email: str, iterations: int = KDF_PARAMS["iterations"]) -> tuple[str, bytes]:
    """(auth_key_hex, wrap_key_bytes) — exactly what crypto.ts → deriveKeys() produces."""
    salt = hashlib.sha256(email.strip().lower().encode("utf-8")).digest()
    password_bytes = unicodedata.normalize("NFKC", password).encode("utf-8")
    master = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, iterations, dklen=32)
    return hkdf_sha256(master, b"auth").hex(), hkdf_sha256(master, b"wrap")


def wrap_data_key(data_key: bytes, wrap_key: bytes) -> str:
    """AES-256-GCM wrap with aad "data-key", output base64(iv ‖ ciphertext ‖ tag) — crypto.ts → wrapDataKey()."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("The 'cryptography' package is required to create seeded accounts.") from exc
    iv = os.urandom(12)
    return base64.b64encode(iv + AESGCM(wrap_key).encrypt(iv, data_key, b"data-key")).decode("ascii")


def unwrap_data_key(wrapped: str, wrap_key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = base64.b64decode(wrapped)
    return AESGCM(wrap_key).decrypt(blob[:12], blob[12:], b"data-key")

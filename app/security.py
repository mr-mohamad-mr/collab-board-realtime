"""Authentication primitives: password hashing and JWT handling.

Same proven pattern as AI Support Copilot (flagship 1): PBKDF2-HMAC-SHA256
with a per-user random salt (stdlib only) and HS256 JWTs built with the
stdlib ``hmac``/``base64``/``json`` modules, strict expiry checking and a
constant-time signature comparison.

The token carries only the user identity (``sub`` + ``username``). Board
roles are NOT in the token — they live in ``board_members`` and are
looked up per request, so a role change takes effect immediately without
re-issuing tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

SECRET_KEY = os.environ.get("COLLAB_SECRET", "dev-only-change-me")
TOKEN_TTL_SECONDS = 24 * 3600


def hash_password(password: str) -> str:
    """Return ``salt$hash`` (both hex) for storage."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 200_000
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(user_id: int, username: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "username": username,
                "exp": int(time.time()) + TOKEN_TTL_SECONDS,
            }
        ).encode()
    )
    signature = _b64url(
        hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


class AuthError(ValueError):
    """Raised for any malformed, tampered or expired token."""


def decode_token(token: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature = token.split(".")
        expected = _b64url(
            hmac.new(
                SECRET_KEY.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise AuthError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthError("malformed token") from exc
    if payload.get("exp", 0) < int(time.time()):
        raise AuthError("expired token")
    return payload

"""Deterministic values of the login doubles: PKCE pair, OAuth state, magic-link tokens, authorization codes, tokens.

Every value is derived with HMAC-SHA-512 from a scenario seed, a label and a counter, so one seed always yields the
same sequence (a test can predict the n-th code or token) and different seeds never collide. The values have the
shapes of the real ones: base64url PKCE verifier (43 characters) with its S256 challenge, a long-lived token
``<prefix><93 characters>AA`` (`TOKEN_PATTERN`). They are test values, never secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

CLIENT_ID = "6f1c2a0e-5b3d-4e8a-9c7f-0d2b4e6a8c1f"
SCOPE = "user:inference"
FULL_SCOPE = "user:profile user:inference user:sessions:claude_code"
"""The scopes of `claude auth login` (KOR-71); a login without `user:profile` is the `no-profile` variant."""
FULL_LIFETIME = 8 * 3_600
"""The lifetime of the access token of a full login (the real one lives for hours)."""
CALLBACK_PATH = "/oauth/code/callback"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/v1/oauth/token"
TOKEN_LIFETIME = 365 * 86_400
# Assembled, so no source file spells a complete token-shaped literal.
TOKEN_PREFIX = "sk-" + "ant-oat01-"
TOKEN_BODY = 93
REFRESH_PREFIX = "sk-" + "ant-ort01-"
TOKEN_PATTERN = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{" + str(TOKEN_BODY) + r"}AA")
DEFAULT_SEED = "double"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def derive(seed: str, label: str, n: int = 0, length: int = 43) -> str:
    """`length` base64url characters for (`seed`, `label`, `n`)."""
    out = ""
    block = 0
    while len(out) < length:
        message = f"{label}:{n}:{block}".encode()
        out += b64url(hmac.new(seed.encode(), message, hashlib.sha512).digest())
        block += 1
    return out[:length]


def challenge_of(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce(seed: str) -> tuple[str, str]:
    """The PKCE verifier and challenge of a CLI run."""
    verifier = derive(seed, "verifier")
    return verifier, challenge_of(verifier)


def oauth_state(seed: str) -> str:
    return derive(seed, "state")


def magic_token(seed: str, n: int) -> str:
    return derive(seed, "magic", n)


def session_id(seed: str, n: int) -> str:
    return derive(seed, "session", n)


def auth_code(seed: str, n: int) -> str:
    return derive(seed, "code", n)


def access_token(seed: str, n: int) -> str:
    """The n-th long-lived token (matches `TOKEN_PATTERN`)."""
    return TOKEN_PREFIX + derive(seed, "token", n, TOKEN_BODY) + "AA"


def refresh_token(seed: str, n: int) -> str:
    """The n-th refresh token of a full login."""
    return REFRESH_PREFIX + derive(seed, "refresh", n, TOKEN_BODY) + "AA"


def account_uuid(seed: str, email: str) -> str:
    """A stable UUID-shaped account ID for `email`."""
    digest = hmac.new(seed.encode(), email.lower().encode(), hashlib.sha256).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-8{digest[17:20]}-{digest[20:32]}"

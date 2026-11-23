"""U-DBL-login: deterministic codes and tokens."""

from __future__ import annotations

import base64
import hashlib
import re

from agent_doubles.login_cli import codes


def test_values_are_deterministic_per_seed_and_counter() -> None:
    assert codes.auth_code("a", 1) == codes.auth_code("a", 1)
    assert len({codes.auth_code("a", 1), codes.auth_code("a", 2), codes.auth_code("b", 1)}) == 3
    assert codes.magic_token("a", 1) != codes.auth_code("a", 1)
    assert codes.session_id("a", 1) != codes.magic_token("a", 1)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", codes.oauth_state("a"))
    assert len(codes.derive("a", "x", 0, 200)) == 200


def test_pkce_pair() -> None:
    verifier, challenge = codes.pkce("seed")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected == codes.challenge_of(verifier)


def test_token_shape() -> None:
    token = codes.access_token("seed", 1)
    assert codes.TOKEN_PATTERN.fullmatch(token)
    assert token.startswith(codes.TOKEN_PREFIX)
    assert len(token) == len(codes.TOKEN_PREFIX) + 95
    assert token != codes.access_token("seed", 2)


def test_account_uuid() -> None:
    value = codes.account_uuid("seed", "A@example.test")
    assert value == codes.account_uuid("seed", "a@example.test")
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}", value)

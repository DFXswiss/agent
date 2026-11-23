"""The credential check of the model double, per access kind (docs/platform/spec/05-runner.md section 10.2).

The guest model gateway injects exactly one credential header per access kind; the double checks that the request
carries exactly that header, with the configured token, and no other credential header:

- ``api_key`` (Anthropic API key): ``x-api-key: <token>``, no ``authorization``.
- ``openrouter`` (platform access through OpenRouter): ``authorization: Bearer <token>``, no ``x-api-key``.
- ``subscription`` (Claude subscription token): ``authorization: Bearer <token>``, no ``x-api-key``, and
  ``oauth-2025-04-20`` among the comma-separated values of ``anthropic-beta``.

A failed check is answered with ``401 authentication_error`` in the Anthropic error shape. Tokens are compared in
constant time and never appear in a reason text.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

KINDS = ("api_key", "openrouter", "subscription")
OAUTH_BETA = "oauth-2025-04-20"


class AccessError(ValueError):
    """An access configuration that is not one of the kinds or has no token."""


@dataclass(frozen=True)
class Access:
    """The credential a request must carry: the access kind and the token."""

    kind: str
    token: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise AccessError(f"access kind must be one of {KINDS}")
        if not self.token:
            raise AccessError("access token must not be empty")

    def __repr__(self) -> str:
        return f"Access(kind={self.kind!r}, token=<redacted>)"

    def refusal(self, headers: Mapping[str, str]) -> str | None:
        """Why `headers` (lower-case names) do not carry this credential; None when they do."""
        if self.kind == "api_key":
            return self._api_key_refusal(headers)
        return self._bearer_refusal(headers)

    def _api_key_refusal(self, headers: Mapping[str, str]) -> str | None:
        if "authorization" in headers:
            return "authorization header not expected for an API key"
        if not _same(headers.get("x-api-key", ""), self.token):
            return "invalid x-api-key"
        return None

    def _bearer_refusal(self, headers: Mapping[str, str]) -> str | None:
        if "x-api-key" in headers:
            return "x-api-key header not expected for a bearer token"
        if not _same(headers.get("authorization", ""), f"Bearer {self.token}"):
            return "invalid bearer token"
        betas = {b.strip() for b in headers.get("anthropic-beta", "").split(",")}
        if self.kind == "subscription" and OAUTH_BETA not in betas:
            return "OAuth authentication is currently not supported."
        return None


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def parse_access(raw: Any) -> Access | None:
    """An Access from ``{"kind": ..., "token": ...}``; None for a JSON null."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"kind", "token"}:
        raise AccessError("access must be null or an object with kind and token")
    kind, token = raw["kind"], raw["token"]
    if not isinstance(kind, str) or not isinstance(token, str):
        raise AccessError("access kind and token must be strings")
    return Access(kind, token)

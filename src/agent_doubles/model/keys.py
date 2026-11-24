"""The OpenRouter key management API of the model double (ADR-080 section 7, KOR-50).

OpenRouter lets a **management key** create limited keys and read, change and delete them; the platform's DFX key
issuer uses it to give each org device its own key. The double keeps those keys in memory:

- ``POST /v1/keys {name, limit?}`` → ``201 {"data": KEY, "key": "<plaintext>"}`` (the plaintext only here);
- ``GET /v1/keys?offset=N`` → ``{"data": [KEY, ...]}``, ``page_size`` keys per page in creation order;
- ``GET /v1/keys/{hash}`` → ``{"data": KEY}``; ``PATCH /v1/keys/{hash} {name?, limit?, disabled?}`` →
  ``{"data": KEY}``; ``DELETE /v1/keys/{hash}`` → ``{"deleted": true}``; an unknown hash is ``404``.

``KEY`` = ``{hash, name, label, disabled, limit, limit_remaining, usage, created_at, updated_at}``; ``usage`` and
``limit`` are USD (``limit`` null = no limit). Every route needs ``Authorization: Bearer <management token>``; any
other credential (also a created key or the platform key) is ``401``, as at the provider, and so is every call while
the double has no management token. A malformed body is ``400``.

A **created key** is an OpenRouter credential of its own for ``POST /v1/messages`` (`KeyStore.by_bearer`): a disabled
key is refused with ``401``, a key whose ``usage`` reached its ``limit`` with ``402`` (the provider's "key limit
reached"); otherwise the request is answered as scripted and its cost (``usage.cost`` of the OpenRouter extras) is
added to the key's ``usage``. ``hash`` is the SHA-256 of the plaintext. Plaintexts are never journaled (headers are
redacted, bodies carry none) and never part of a reason text.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from typing import Any

KEYS_PATH = "/v1/keys"
DEFAULT_PAGE_SIZE = 100
_PATCH_FIELDS = frozenset({"name", "limit", "disabled"})
_CREATE_FIELDS = frozenset({"name", "limit"})
_TIME = "2026-11-24T12:00:00.000Z"


class KeyRequestError(ValueError):
    """A management request with a malformed body (``400``)."""


@dataclass
class SubKey:
    """One created key: its hash, name, plaintext (in memory only), limit and accumulated usage in USD."""

    hash: str
    name: str
    secret: str
    limit: float | None
    usage: float = 0.0
    disabled: bool = False

    def __repr__(self) -> str:
        return f"SubKey(hash={self.hash!r}, name={self.name!r}, secret=<redacted>)"

    def document(self) -> dict[str, Any]:
        remaining = None if self.limit is None else max(self.limit - self.usage, 0.0)
        return {
            "hash": self.hash,
            "name": self.name,
            "label": f"sk-or-v1-{self.hash[:3]}...{self.hash[-3:]}",
            "disabled": self.disabled,
            "limit": self.limit,
            "limit_remaining": remaining,
            "usage": round(self.usage, 9),
            "created_at": _TIME,
            "updated_at": _TIME,
        }

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.usage >= self.limit


def _limit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise KeyRequestError("limit must be null or a non-negative number")
    return float(value)


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise KeyRequestError("name must be a non-empty string")
    return value


class KeyStore:
    """The keys of one double (thread-safe)."""

    def __init__(self, management_token: str | None = None, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self._lock = threading.Lock()
        self.management_token = management_token
        self.page_size = page_size
        self._keys: dict[str, SubKey] = {}

    def authorized(self, authorization: str) -> bool:
        """The header names the management token (compared in constant time)."""
        token = self.management_token
        return token is not None and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())

    def create(self, body: Any) -> tuple[SubKey, str]:
        if not isinstance(body, dict) or "name" not in body or not set(body) <= _CREATE_FIELDS:
            raise KeyRequestError("body must be an object with name and an optional limit")
        name, limit = _name(body["name"]), _limit(body.get("limit"))
        secret = "sk-or-v1-" + secrets.token_hex(32)
        key = SubKey(hashlib.sha256(secret.encode()).hexdigest(), name, secret, limit)
        with self._lock:
            self._keys[key.hash] = key
        return key, secret

    def page(self, offset: int) -> list[dict[str, Any]]:
        with self._lock:
            chosen = list(self._keys.values())[offset : offset + self.page_size]
            return [key.document() for key in chosen]

    def get(self, key_hash: str) -> SubKey | None:
        with self._lock:
            return self._keys.get(key_hash)

    def patch(self, key_hash: str, body: Any) -> SubKey | None:
        if not isinstance(body, dict) or not set(body) <= _PATCH_FIELDS:
            raise KeyRequestError("body must be an object with name, limit or disabled")
        if "disabled" in body and not isinstance(body["disabled"], bool):
            raise KeyRequestError("disabled must be a boolean")
        name = _name(body["name"]) if "name" in body else None
        limit = _limit(body["limit"]) if "limit" in body else None
        with self._lock:
            key = self._keys.get(key_hash)
            if key is None:
                return None
            if name is not None:
                key.name = name
            if "limit" in body:
                key.limit = limit
            if "disabled" in body:
                key.disabled = body["disabled"]
            return key

    def delete(self, key_hash: str) -> bool:
        with self._lock:
            return self._keys.pop(key_hash, None) is not None

    def by_bearer(self, authorization: str) -> SubKey | None:
        """The created key an ``Authorization: Bearer <key>`` names, None for any other header."""
        if not authorization.startswith("Bearer "):
            return None
        return self.get(hashlib.sha256(authorization[len("Bearer ") :].encode()).hexdigest())

    def charge(self, key: SubKey, cost: float) -> None:
        """Add the cost of an answered request to the key's usage."""
        with self._lock:
            key.usage += cost

    def documents(self) -> list[dict[str, Any]]:
        """Every key as ``GET /v1/keys`` describes it (the control route ``GET /_double/keys``)."""
        with self._lock:
            return [key.document() for key in self._keys.values()]

"""The OpenRouter management API of the DFX key issuer (ADR-081 section 7).

Routes below `MODEL_UPSTREAM_OPENROUTER` (`https://openrouter.ai/api`), each with `Authorization: Bearer
<management key>`, 8 s timeout, no redirects:

- `GET /v1/keys?offset=N&include_disabled=true` → `{"data": [KEY, ...]}` (pages until an empty one);
- `POST /v1/keys {name, limit}` → `{"data": KEY, "key": "<plaintext>"}` (the plaintext is returned only here);
- `GET /v1/keys/{hash}` → `{"data": KEY}` (`404`: the key does not exist);
- `PATCH /v1/keys/{hash} {limit?, disabled?}` → `{"data": KEY}`;
- `DELETE /v1/keys/{hash}` (`404` counts as deleted).

`KEY` is `{hash, name, disabled, limit: USD | null, usage: USD, ...}`; amounts are kept as integer micro-USD.

The management key is read from its file for each request (`provision.read_platform_key`: regular file of this user,
0600, one printable token) and is never logged, stored or put into an error; a `ManagementError` carries a closed
reason only (`key_unusable`, `status_<code>`, the transport error's type name, `invalid`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from agent_runner.model.dfxkey import KEY_REF_RE
from agent_runner.quota.subscription import ClientFactory, default_client

MICRO = 1_000_000
MAX_PAGES = 100
_TOKEN_CHARS = frozenset(chr(code) for code in range(0x21, 0x7F))


class ManagementError(Exception):
    """A failed management call; `reason` is the only text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def to_micro(value: object) -> int | None:
    """A finite non-negative USD amount in micro-USD, None otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return round(value * MICRO)


def to_usd(micro: int) -> float:
    return micro / MICRO


@dataclass(frozen=True)
class KeyMeta:
    """A key as the provider describes it (never the plaintext)."""

    hash: str
    name: str
    disabled: bool
    limit_micro: int | None
    usage_micro: int


def parse_key(value: object) -> KeyMeta:
    """A `KEY` document, else `ManagementError("invalid")`."""
    if not isinstance(value, Mapping):
        raise ManagementError("invalid")
    hash_, name, disabled = value.get("hash"), value.get("name"), value.get("disabled")
    limit, usage = value.get("limit"), to_micro(value.get("usage"))
    limit_micro = None if limit is None else to_micro(limit)
    if (
        not isinstance(hash_, str)
        or not KEY_REF_RE.fullmatch(hash_)
        or not isinstance(name, str)
        or not isinstance(disabled, bool)
        or usage is None
        or (limit is not None and limit_micro is None)
    ):
        raise ManagementError("invalid")
    return KeyMeta(hash=hash_, name=name, disabled=disabled, limit_micro=limit_micro, usage_micro=usage)


@dataclass
class Management:
    upstream: str
    key: Callable[[], str | None]
    client: ClientFactory = default_client

    def url(self, path: str) -> str:
        return self.upstream.rstrip("/") + "/v1/keys" + path

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        missing: bool = False,
        parse: bool = True,
    ) -> Any:
        """The JSON answer of a `2xx` (None without `parse`); None for `404` when `missing` allows it."""
        key = self.key()
        if key is None:
            raise ManagementError("key_unusable")
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        try:
            async with self.client() as client:
                response = await client.request(method, self.url(path), headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ManagementError(type(exc).__name__) from None
        status = response.status_code
        if status == 404 and missing:
            return None
        if not 200 <= status < 300:
            raise ManagementError(f"status_{status}")
        if not parse:
            return None
        try:
            return response.json()
        except ValueError:
            raise ManagementError("invalid") from None

    @staticmethod
    def _data(answer: Any) -> Any:
        if not isinstance(answer, Mapping):
            raise ManagementError("invalid")
        return answer.get("data")

    async def named(self, name: str) -> list[KeyMeta]:
        """Every key with the name `name` (all pages, disabled keys included)."""
        return [meta for meta in await self.listing() if meta.name == name]

    async def listing(self) -> list[KeyMeta]:
        """Every key of the account (all pages, disabled keys included)."""
        found: list[KeyMeta] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page = self._data(await self._call("GET", f"?offset={offset}&include_disabled=true"))
            if not isinstance(page, list):
                raise ManagementError("invalid")
            if not page:
                break
            found.extend(map(parse_key, page))
            offset += len(page)
        return found

    async def create(self, name: str, limit_micro: int) -> tuple[KeyMeta, str]:
        """A new key with the name and the USD limit; the plaintext is returned once."""
        answer = await self._call("POST", "", body={"name": name, "limit": to_usd(limit_micro)})
        meta = parse_key(self._data(answer))
        secret = answer.get("key")
        if not isinstance(secret, str) or not secret or not set(secret) <= _TOKEN_CHARS:
            raise ManagementError("invalid")
        return meta, secret

    async def get(self, key_ref: str) -> KeyMeta | None:
        """The key, None when the provider does not know it."""
        answer = await self._call("GET", f"/{key_ref}", missing=True)
        return None if answer is None else parse_key(self._data(answer))

    async def patch(self, key_ref: str, changes: Mapping[str, Any]) -> KeyMeta:
        """Change `limit` (USD) and/or `disabled`."""
        return parse_key(self._data(await self._call("PATCH", f"/{key_ref}", body=changes)))

    async def delete(self, key_ref: str) -> None:
        """Delete the key (a key that does not exist is deleted)."""
        await self._call("DELETE", f"/{key_ref}", missing=True, parse=False)

"""Talking to the hub (11-agent-access.md sections 4 and 8.3).

- `anon_request`: the anonymous endpoints (`GET /v1/config`, `POST /v1/agent-pairings`, `POST /v1/invites/lookup`).
  The hub does not sign answers to unsigned requests; what the tool takes from them is checked otherwise (the hub key
  is pinned from `/v1/config` at pairing; the pairing answer must name the pairing; an invitation package is
  authenticated by its code).
- `AgentClient`: every other request, signed with `agent-request-v1` under the key ID `agent.<32 hex>` by the
  runner's signed client (`agent_runner.sync.http.HubClient`), whose answers must be signed by the pinned hub key.

Failures become `PlatformError`s: a connection failure `hub_unreachable`; a verified refusal `hub_refused` with the
hub's code (`hub_error`, a code of the closed list `agent_runner.sync.codes`); an answer that does not verify
`hub_refused` with `reason` (a closed code of the signed client).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from agent_platform.errors import PlatformError
from agent_runner.crypto.keys import Identity
from agent_runner.sync.codes import HUB_ERROR_CODES
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError, encode_body

ANON_TIMEOUT_S = 30.0
MAX_ANON_BYTES = 4 * 1024 * 1024


def key_id(agent_id: str) -> str:
    """`agent.<32 hex>`: the agent ID without dashes."""
    return "agent." + agent_id.replace("-", "")


def segment(value: str) -> str:
    """One path segment (IDs are UUIDs; anything else is quoted)."""
    return quote(value, safe="")


def refused(code: str, status: int, retry_after: int | None = None) -> PlatformError:
    """A verified refusal; `retry_after` the seconds of the hub's `Retry-After` (a `429 rate_limited`)."""
    extra = {} if retry_after is None else {"retry_after": retry_after}
    return PlatformError("hub_refused", hub_error=code, status=status, **extra)


def anon_request(origin: str, method: str, path: str, body: object | None = None) -> dict[str, Any]:
    """One anonymous request; the JSON object of a 2xx answer."""
    content = None if body is None else encode_body(body)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        with httpx.Client(timeout=ANON_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, origin + path, content=content, headers=headers)
    except httpx.HTTPError:
        raise PlatformError("hub_unreachable") from None
    try:
        data = json.loads(response.content[:MAX_ANON_BYTES])
    except ValueError:
        data = None
    if response.status_code >= 400:
        code = data.get("error") if isinstance(data, dict) else None
        if isinstance(code, str) and code in HUB_ERROR_CODES:
            raise refused(code, response.status_code)
        raise PlatformError("hub_refused", reason="response_invalid", status=response.status_code)
    if not isinstance(data, dict) or not 200 <= response.status_code < 300:
        raise PlatformError("hub_refused", reason="response_invalid", status=response.status_code)
    return data


class AgentClient:
    """Signed requests of the agent with verified answers (synchronous facade over the runner's async client)."""

    def __init__(
        self,
        origin: str,
        agent_id: str,
        agent: Identity,
        hub_key: bytes,
        *,
        on_revoked: Callable[[], None] = lambda: None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._client = HubClient(hub_url=origin, key_id=key_id(agent_id), signing_key=agent.sig, hub_key=hub_key)
        self._on_revoked = on_revoked

    def close(self) -> None:
        self._loop.run_until_complete(self._client.aclose())
        self._loop.close()

    def call(self, method: str, path: str, body: object | None = None) -> dict[str, Any]:
        """The verified JSON object of a 2xx answer; `PlatformError` otherwise (`key_revoked` runs `on_revoked`)."""
        try:
            return self._loop.run_until_complete(self._client.request(method, path, body))
        except HubRefusal as refusal:
            if refusal.code == "key_revoked":
                self._on_revoked()
                raise PlatformError("agent_revoked") from None
            if refusal.code == "key_unknown":
                raise PlatformError("not_paired", hub_error="key_unknown") from None
            raise refused(refusal.code, refusal.status, refusal.retry_after) from None
        except HubTransportError as error:
            if error.reason == "hub_unreachable":
                raise PlatformError("hub_unreachable") from None
            raise PlatformError("hub_refused", reason=error.reason) from None

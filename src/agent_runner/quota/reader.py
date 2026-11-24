"""One read of the Anthropic OAuth usage endpoint with a given subscription token (05-runner.md section 10.6; KOR-50,
ADR-080 sections 2 and 6).

`read(context, token, now)` is the read the exhaustion check (`model.limits`) and the operator quota poller
(`quota.operator`) share with the owner quota polling (`quota.subscription`): `GET <ANTHROPIC_USAGE_URL>` with
`Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, `Accept: application/json`, 8 s timeout, no
redirects. The answer is a `Reading`:

- `200` with a valid document: `readable` with its windows (`parsers.windows_snapshot`, possibly none);
- `401`/`403`: not readable (a setup token may not be accepted by the endpoint, KOR-40);
- anything else (another status, a malformed document, a transport error): None - nothing is known.

The token is used for this request only and never logged, stored or put into an error. Tests set `quota_options`
(`QuotaOptions`) on the context to inject the HTTP client factory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_runner.quota.parsers import UsageInvalid, windows_snapshot

TIMEOUT_S = 8.0
OAUTH_BETA = "oauth-2025-04-20"

ClientFactory = Callable[[], httpx.AsyncClient]


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, trust_env=False)


@dataclass
class QuotaOptions:
    client: ClientFactory = default_client


@dataclass(frozen=True)
class Reading:
    """What one read found: the windows of the subscription, or `readable` False when the endpoint refused the token."""

    readable: bool
    windows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fetched_at: float = 0.0


def client_of(context: Any) -> ClientFactory:
    options = getattr(context, "quota_options", None)
    return options.client if isinstance(options, QuotaOptions) else default_client


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}


async def read(context: Any, token: str, now: float) -> Reading | None:
    """The reading of the subscription behind `token` (module docstring)."""
    try:
        async with client_of(context)() as client:
            response = await client.get(context.config.anthropic_usage_url, headers=headers(token))
    except httpx.HTTPError:
        return None
    if response.status_code in (401, 403):
        return Reading(readable=False, fetched_at=now)
    if response.status_code != 200:
        return None
    try:
        windows = windows_snapshot(response.json())
    except (UsageInvalid, ValueError):
        return None
    return Reading(readable=True, windows=tuple(windows), fetched_at=now)

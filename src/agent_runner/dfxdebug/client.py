"""The two requests of `dfx.debug` at the DFX API (ADR-102 section 4; the flow of the dfx-debug CLI).

1. `POST {api_url}/auth/signIn` `{address, signature}` -> `{accessToken}`: the sign-in of the wallet with the Debug
   role. The token lives only in this function's frame for the one query and is never stored, logged or returned.
2. `POST {api_url}/support/debug` with the DTO and `Authorization: Bearer <token>` -> `{keys, rows}`.

There is no other request: the client has no write path (the CLI's `poll`, `POST /payIn/poll`, does not exist here;
a test keeps it so). Every request has its own `TIMEOUT_SECONDS`, redirects are not followed (the sign-in must not
travel to another host) and the API URL is `https` (the credential's form). A refusal is `Refused(stage, status,
code, message)` with the endpoint's message and code as it sent them (a message list joined by `; `), never the
request; a body that is no JSON object is `Refused(stage, status, None, None)`. A slow or unreachable API raises
`httpx.TimeoutException` / `httpx.TransportError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from agent_runner.dfxdebug.access import SignIn

SIGN_IN = "auth/signIn"
DEBUG = "support/debug"
TIMEOUT_SECONDS = 30.0
transport: httpx.AsyncBaseTransport | None = None
"""Tests replace the transport (None: the network)."""


class Refused(Exception):
    """A non-2xx answer or an unusable body of the sign-in (`stage` `sign_in`) or the query (`query`)."""

    def __init__(self, stage: str, status: int, code: str | None, message: str | None) -> None:
        super().__init__(stage)
        self.stage = stage
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Answer:
    keys: list[Any]
    rows: list[Any]


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        found = response.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _refused(stage: str, response: httpx.Response) -> Refused:
    body = _json(response) or {}
    message = body.get("message")
    if isinstance(message, list):
        message = "; ".join(str(item) for item in message)
    code = body.get("code")
    return Refused(
        stage,
        response.status_code,
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


async def query(sign_in: SignIn, dto: Mapping[str, Any]) -> Answer:
    """Sign in and run one query (module docstring)."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False, headers={"Accept": "application/json"}
    ) as client:
        response = await client.post(
            f"{sign_in.api_url}/{SIGN_IN}", json={"address": sign_in.address, "signature": sign_in.signature}
        )
        if not response.is_success:
            raise _refused("sign_in", response)
        token = (_json(response) or {}).get("accessToken")
        if not isinstance(token, str) or not token:
            raise Refused("sign_in", response.status_code, None, None)
        response = await client.post(
            f"{sign_in.api_url}/{DEBUG}", json=dict(dto), headers={"Authorization": f"Bearer {token}"}
        )
        del token
    if not response.is_success:
        raise _refused("query", response)
    body = _json(response) or {}
    keys, rows = body.get("keys"), body.get("rows")
    if not isinstance(keys, list) or not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
        raise Refused("query", response.status_code, None, None)
    return Answer(keys, rows)

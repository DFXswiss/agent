"""Test kit of the DBG-01 runner suites: a double of the DFX API (sign-in and debug endpoint over an httpx transport,
every request recorded), debug sign-in records sealed in their owner's `cred` scope and served by the credential
rig's hub, and the collaborators an executor gets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import httpx

from agent_runner.crypto.ids import uuid7
from agent_runner.operations.extensions import OperationContext
from agent_runner.sessions.records import LocalSession
from tests.platform.runner.credentials.kit import ALICE, Env, make_record, pid
from tests.platform.runner.mail.kit import OpEnv

Json = dict[str, Any]
KIND = "dfx_debug"
ORG = pid("a/org")
WG = pid("a/wg/default")
OTHER_WG = pid("a/wg/one")
API_URL = "https://dfx-api.example.test/v1"
ADDRESS = "wallet-double-address-0001"
SIGNATURE = "double-signature-of-the-sign-in-message-7f3a"
TOKEN = "header.double-jwt-payload.signature-of-the-token"
KEYS = ["id", "created", "amlCheck"]
ROWS = [[1, "2026-11-20T10:00:00.000Z", "Pass"], [2, "2026-11-21T10:00:00.000Z", "Fail"]]

__all__ = ["ALICE", "OpEnv"]


def sign_in_body(*, wgs: list[str] | None = None, **changes: Any) -> Json:
    body: Json = {"address": ADDRESS, "signature": SIGNATURE, "api_url": API_URL}
    if wgs is not None:
        body["use"] = {"wgs": wgs}
    body.update(changes)
    return body


def store(env: Env, body: Json, *, owner: str = ALICE, author: str = "alice", version: int = 1) -> str:
    """Put a sealed `dfx_debug` record of `owner` on the rig's hub; its record ID."""
    record_id = uuid7()
    env.hub.records[(owner, KIND)] = make_record(
        body, owner=owner, kind=KIND, author=author, record_id=record_id, version=version
    )
    return record_id


async def forget(env: Env) -> None:
    """Empty the device's credential cache (the hub's hint after a change makes the device read the record again)."""
    async with env.pool.connection() as conn:
        await conn.execute("DELETE FROM runner.credential_cache")


@dataclass
class DfxApi:
    """The DFX API as far as `dfx.debug` uses it: `POST /v1/auth/signIn` and `POST /v1/support/debug`."""

    keys: list[Any] = field(default_factory=lambda: list(KEYS))
    rows: list[Any] = field(default_factory=lambda: [list(row) for row in ROWS])
    sign_in: tuple[int, Any] | None = None
    """An answer of the sign-in instead of the token."""
    debug: tuple[int, Any] | None = None
    """An answer of the debug endpoint instead of `{keys, rows}`."""
    raises: BaseException | None = None
    requests: list[tuple[str, str, Json, dict[str, str]]] = field(default_factory=list)
    """`(method, path, body, headers)` of every request."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"null")
        self.requests.append((request.method, request.url.path, body, dict(request.headers)))
        if self.raises is not None:
            raise self.raises
        if request.url.path == "/v1/auth/signIn":
            if self.sign_in is not None:
                return _answer(*self.sign_in)
            ok = body == {"address": ADDRESS, "signature": SIGNATURE}
            return _answer(201, {"accessToken": TOKEN}) if ok else _answer(401, {"message": "Invalid credentials"})
        if request.url.path == "/v1/support/debug":
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return _answer(401, {"message": "Unauthorized"})
            if self.debug is not None:
                return _answer(*self.debug)
            return _answer(201, {"keys": self.keys, "rows": self.rows})
        return _answer(404, {"message": "Cannot POST"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [path for _method, path, _body, _headers in self.requests]


def _answer(status: int, body: Any) -> httpx.Response:
    if isinstance(body, bytes):
        return httpx.Response(status, content=body)
    return httpx.Response(status, json=body)


def session(wg: str = WG, owner: str | None = ALICE) -> LocalSession:
    return cast(
        LocalSession,
        SimpleNamespace(id=pid("session/one"), task_id=pid("task/one"), org_id=ORG, wg_id=wg, owner_id=owner, depth=0),
    )


def operation(actor: str | None = ALICE, wg: str = WG) -> Any:
    return SimpleNamespace(
        id=uuid7(), task_id=pid("task/one"), session_id=pid("session/one"), org_id=ORG, wg_id=wg, actor_id=actor
    )


def operation_context(
    env: Env, op_env: OpEnv, params: Json, *, wg: str = WG, actor: str | None = ALICE, owner: str | None = ALICE
) -> OperationContext:
    return OperationContext(
        context=env.context,
        operation=operation(actor, wg),
        session=session(wg, owner),
        params=params,
        data={},
        attempt=1,
        env=op_env,
    )

"""Test kit of the KOR-74 runner suites: the hub's `GET /v1/device/mailboxes` installed on the credential rig's hub
(requests and answers validated against the contract), mailbox records sealed in their owner's `cred` scope, and the
collaborators an executor gets (an outbox that keeps its items, the device clocks)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agent_doubles.mail.double import MailDouble
from agent_runner.crypto.ids import uuid7
from agent_runner.operations.extensions import OperationContext
from agent_runner.sessions.records import LocalSession
from tests.platform.runner.credentials.kit import ALICE, WALL, CredHub, Env, make_record, pid

Json = dict[str, Any]
KIND = "mailbox"
ORG = pid("a/org")
WG = pid("a/wg/default")
OTHER_WG = pid("a/wg/one")
PASSWORD = "double-mailbox-secret-7"


def mailbox_body(double: MailDouble, user: str, *, name: str | None = None, wgs: list[str] | None = None) -> Json:
    assert double.imaps is not None
    body: Json = {
        "imap_host": "127.0.0.1",
        "imap_port": double.imaps.port,
        "imap_user": user,
        "imap_password": PASSWORD,
        "email": user,
    }
    if name is not None:
        body["name"] = name
    if wgs is not None:
        body["use"] = {"wgs": wgs}
    return body


@dataclass
class MailboxHub:
    """`GET /v1/device/mailboxes` (03-api.md section 6.1) on a rig hub: the served records with their mirror."""

    hub: CredHub
    served: list[tuple[str, str, list[str], Json]] = field(default_factory=list)
    """`(record id, owner, mirrored workgroups, record)`, newest change first."""
    refusal: tuple[int, Json] | None = None
    raw: Json | None = None
    asked: list[str] = field(default_factory=list)

    def install(self) -> MailboxHub:
        original = self.hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            split = urlsplit(target)
            if method == "GET" and split.path == "/v1/device/mailboxes":
                return self.listing(parse_qs(split.query))
            answer: tuple[int, dict[str, Any]] = original(method, target, body)
            return answer

        self.hub.route = route  # type: ignore[method-assign]
        return self

    def listing(self, query: dict[str, list[str]]) -> tuple[int, Json]:
        self.hub.check("api/device-mailboxes-list.request.json", {k: v[0] for k, v in query.items()})
        wg = query["wg_id"][0]
        self.asked.append(wg)
        if self.refusal is not None:
            return self.refusal
        if self.raw is not None:
            return 200, self.raw
        items = [
            {"id": record_id, "owner_id": owner, "primary": False, "record": record}
            for record_id, owner, wgs, record in self.served
            if wg in wgs
        ]
        answer = {"mailboxes": items}
        self.hub.check("api/device-mailboxes-list.response.json", answer)
        return 200, answer

    def add(
        self,
        body: Json,
        *,
        owner: str = ALICE,
        author: str = "alice",
        mirror: list[str] | None = None,
        record_id: str | None = None,
        version: int = 1,
    ) -> str:
        record_id = record_id or uuid7()
        self.served = [item for item in self.served if item[0] != record_id]
        record = make_record(body, owner=owner, kind=KIND, author=author, record_id=record_id, version=version)
        wgs = mirror if mirror is not None else list(body.get("use", {}).get("wgs", []))
        self.served.insert(0, (record_id, owner, wgs, record))
        return record_id


@dataclass
class Outbox:
    items: list[tuple[str, Json]] = field(default_factory=list)

    async def enqueue(self, _conn: Any, kind: str, body: Json) -> None:
        self.items.append((kind, body))


@dataclass
class OpEnv:
    """The executor's collaborators: an outbox that keeps its items and the device clocks."""

    env: Env
    outbox: Outbox = field(default_factory=Outbox)
    now: float = WALL
    started: float = 0.0

    def wall(self) -> float:
        return self.now

    def clock(self) -> float:
        return time.monotonic() - self.started

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self.env.pool.connection() as conn:
            yield conn

    def audits(self) -> list[Json]:
        return [body for kind, body in self.outbox.items if kind == "audit"]


def session(wg: str = WG) -> LocalSession:
    return cast(
        LocalSession,
        SimpleNamespace(id=pid("session/one"), task_id=pid("task/one"), org_id=ORG, wg_id=wg, owner_id=ALICE, depth=0),
    )


def operation_context(env: Env, op_env: OpEnv, params: Json, *, wg: str = WG) -> OperationContext:
    operation = SimpleNamespace(
        id=uuid7(), task_id=pid("task/one"), session_id=pid("session/one"), org_id=ORG, wg_id=wg
    )
    return OperationContext(
        context=env.context,
        operation=cast(Any, operation),
        session=session(wg),
        params=params,
        data={},
        attempt=1,
        env=op_env,
    )

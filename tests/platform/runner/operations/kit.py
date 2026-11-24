"""Test kit of the RUN-07 suites: a hub that decides operations, a guest that takes pending answers, local sessions,
journaled requests and a `Pipeline` over the RUN-04 kit.

- `OpsHub` adds `POST /v1/device/operations` to the rig's hub: every request is validated against
  `api/device-operations.request.json`, its params event is checked (device signature) and opened with the
  workgroup key, and the answer comes from a script (default `accepted`); every answer is validated against the
  response schema. Scripted `(status, body)` pairs send refusals; `unreachable` raises a transport error.
- `OpGuest` extends the RUN-04 fake guest with `POST /v1/pending/{id}/answer` (recorded, failures injected).
- `setup(env, executors=..., prechecks=...)` builds the kit (verified logs and keys of org `a`), a pipeline with the
  rig's fake clock and the real sync runtime (outbox, sealer, hub client), and `OKit` helpers: `session()`,
  `request(...)` (an `operation.request` journal record through the real handler), `connect()` (a cached GitHub
  credential of the owner), `settings(...)` (a verified `settings.workgroup` event pulled by the real inbox).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.event import open_event
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import JournalRecord
from agent_runner.inbox.pull import inbox_for
from agent_runner.operations import records
from agent_runner.operations.extensions import Executor, Notify, Precheck
from agent_runner.operations.journal import HANDLERS
from agent_runner.operations.pipeline import Pipeline
from agent_runner.sessions import records as session_records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sync.http import HubTransportError
from agent_runner.vm.records import VmRow

from ..reconcile.rig import Env, device_keys, scope_key, seal, standard_world
from ..sessions.kit import ORG, OWNER, TASK, WG, FakeGuest, Kit, kit, session_id

MODEL = "deepseek/deepseek-v4.1-flash-1017"
REQUESTED_AT = "2026-11-21T12:00:01.000Z"
Json = dict[str, Any]


@dataclass
class OpGuest(FakeGuest):
    answer_errors: list[BaseException] = field(default_factory=list)

    async def answer_pending(self, pending_id: str, answer: Mapping[str, Any]) -> None:
        self._call("answer_pending", (pending_id, dict(answer)))
        if self.answer_errors:
            raise self.answer_errors.pop(0)

    def answers(self) -> list[tuple[str, Json]]:
        return [value for name, value in self.calls if name == "answer_pending"]


@dataclass
class OpsHub:
    """The operations endpoint of the hub (installed on the rig's hub instance)."""

    env: Env
    decisions: list[Json] = field(default_factory=list)
    """Answer bodies (state `accepted` when empty)."""
    refusals: list[tuple[int, Json]] = field(default_factory=list)
    """Refusals sent before the next decision."""
    unreachable: int = 0
    """Transport errors before the next answer."""
    raw: list[Json] = field(default_factory=list)
    """Answers sent before the next decision without the contract check (a hub that breaks the contract)."""
    requests: list[Json] = field(default_factory=list)

    def install(self) -> None:
        hub = self.env.hub
        original = hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            if (method, target) == ("POST", "/v1/device/operations"):
                return self.handle(body)
            return original(method, target, body)

        hub.route = route  # type: ignore[method-assign]
        client = self.env.sync.client
        send = client.request

        async def request(method: str, target: str, body: object | None = None) -> dict[str, object]:
            if target == "/v1/device/operations" and self.unreachable:
                self.unreachable -= 1
                raise HubTransportError("hub_unreachable")
            return await send(method, target, body)

        client.request = request  # type: ignore[method-assign]

    def handle(self, body: Json) -> tuple[int, Json]:
        hub = self.env.hub
        hub.check("api/device-operations.request.json", body)
        self.requests.append(body)
        if self.refusals:
            return self.refusals.pop(0)
        if self.raw:
            return 200, self.raw.pop(0)
        answer = self.decisions.pop(0) if self.decisions else {"state": "accepted"}
        hub.check("api/device-operations.response.json", answer)
        return 200, answer

    def params_event(self, index: int = -1) -> tuple[Json, Json]:
        """`(header, body)` of a registration's params event, checked with the device key and opened."""
        event = self.requests[index]["params_event"]
        header = event["header"]
        key = scope_key("wg", header["scope_id"], header["epoch"])
        opened = parse_json(open_event(key, event, b64u_decode(device_keys().sig_public)))
        assert isinstance(opened, dict)
        return header, opened


@dataclass
class OKit:
    env: Env
    kit: Kit
    guest: OpGuest
    hub: OpsHub
    pipeline: Pipeline
    notified: list[str]
    refreshed: list[int]
    seq: int = 0
    scope_seq: int = 0

    @property
    def lines(self) -> list[str]:
        return self.kit.lines

    async def vm(self) -> VmRow:
        return await self.kit.vms.ensure_vm(ORG, WG)

    async def session(
        self, n: int = 1, *, depth: int = 0, model_access: str | None = "platform", actor: str | None = OWNER
    ) -> str:
        """A working session of the task; `actor` is the acting user of its turn (the owner's prompt by default)."""
        row = await self.vm()
        sid = session_id(n)
        async with self.env.pool.connection() as conn, conn.transaction():
            await session_records.insert(
                conn,
                session_id=sid,
                task_id=TASK,
                org_id=ORG,
                wg_id=WG,
                parent_id=None if depth == 0 else session_id(99),
                depth=depth,
                owner_id=OWNER,
                created_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
            )
            await session_records.update(
                conn, sid, state="working", vm_id=row.id, model_access=model_access, model_id=MODEL
            )
            await conn.execute(
                "UPDATE runner.session SET turn_actor = %s, turn_open = true WHERE id = %s", (actor, sid)
            )
        return sid

    def reader(self, row: VmRow) -> VmJournal:
        return VmJournal(self.kit.session_env, row, handlers=HANDLERS)

    async def journal(self, sid: str | None, body: Json, *, row: VmRow | None = None) -> None:
        self.seq += 1
        record = JournalRecord(seq=self.seq, ts=REQUESTED_AT, session_id=sid, kind="operation.request", body=body)
        await self.reader(row or await self.vm()).handle([record])

    async def request(
        self,
        sid: str,
        op_type: str,
        params: Json,
        *,
        key: str | None = None,
        rejected: Json | None = None,
        **extra: Any,
    ) -> str:
        """Journal one `operation.request` of `sid`; returns the operation ID."""
        op_id = uuid7()
        body: Json = {
            "pending_id": uuid7(),
            "operation_id": op_id,
            "type": op_type,
            "params": params,
            "requested_at": REQUESTED_AT,
            "local_check": "passed" if rejected is None else "rejected",
        }
        if rejected is not None:
            body["local_error"] = rejected
        if key is not None:
            body["idempotency_key"] = key
        body.update(extra)
        await self.journal(sid, body)
        return op_id

    async def run(self, sid: str, op_type: str, params: Json, **kw: Any) -> records.Operation:
        """Request and process one operation; returns its final row."""
        op_id = await self.request(sid, op_type, params, **kw)
        await self.pipeline.process(op_id)
        return await self.op(op_id)

    async def op(self, op_id: str) -> records.Operation:
        async with self.env.pool.connection() as conn:
            found = await records.get(conn, op_id)
        assert found is not None
        return found

    async def age(self, op_id: str, seconds: float) -> None:
        async with self.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
                (seconds, op_id),
            )

    async def connect(self, owner: str = OWNER) -> None:
        async with self.env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, 'github', 1, %s)",
                (owner, Jsonb({})),
            )

    async def settings(self, repositories: list[str], protected: list[str] | None = None, draft: bool = True) -> None:
        """A verified `settings.workgroup` event of the default workgroup, pulled by the real inbox."""
        self.scope_seq += 1
        body = {
            "repositories": repositories,
            "protected_branches": protected if protected is not None else ["main", "master", "develop", "release/*"],
            "pr_draft_default": draft,
        }
        org = self.env.world.orgs[ORG]
        sealed = seal(org, "alice", "settings.workgroup", label=f"settings{self.scope_seq}", body=body)
        self.env.world.add_event(WG, self.scope_seq, sealed)
        await pull(self.env)

    async def outbox(self) -> list[tuple[str, Json]]:
        return await self.kit.outbox()

    def answers(self) -> list[Json]:
        return [answer["result"] for _pending, answer in self.guest.answers()]


async def pull(env: Env) -> int:
    inbox = inbox_for(env.context)
    assert inbox is not None
    return await inbox.pull_all()


async def setup(
    env: Env,
    *,
    executors: Mapping[str, Executor] | None = None,
    prechecks: Mapping[str, Precheck] | None = None,
    notify: Notify | bool | None = True,
    refresh: Callable[[], Awaitable[object]] | None = None,
    guest: OpGuest | None = None,
) -> OKit:
    guest = guest or OpGuest()
    found = await kit(env, guest=guest)
    hub = OpsHub(env)
    hub.install()
    notified: list[str] = []
    refreshed: list[int] = []

    async def record(_context: Any, operation: records.Operation) -> None:
        notified.append(operation.id)

    async def pulled() -> object:
        refreshed.append(1)
        return await pull(env)

    pipeline = Pipeline(
        found.session_env,
        client=env.sync.client,
        sealer=env.sync.pusher.sealer,
        executors=executors if executors is not None else {},
        prechecks=prechecks,
        notify=record if notify is True else (notify or None),
        refresh=refresh or pulled,
        sleep=env.clock.sleep,
        random=lambda: 0.5,
    )
    return OKit(env=env, kit=found, guest=guest, hub=hub, pipeline=pipeline, notified=notified, refreshed=refreshed)


def run(rig: Any, body: Callable[[Env], Awaitable[None]], world: Any = None) -> None:
    async def main() -> None:
        async with rig.env(world or standard_world()) as env:
            await body(env)

    asyncio.run(main())


async def ok(_ctx: Any) -> Json:
    return {}


__all__ = ["ORG", "OWNER", "TASK", "WG", "OKit", "OpGuest", "ok", "pull", "run", "setup"]

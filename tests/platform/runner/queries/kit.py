"""Test kit of the RUN-08 suites: a fixture org, its verified events and the full read path.

`fixture_world()` is org `a` of the rig with two more workgroups and several members, all real signed log entries:

| seq | entry |
|---|---|
| 0 | genesis by alice (admin), default workgroup `WG` |
| 1, 2 | workgroups `WG_ONE` and `WG_TWO` |
| 3, 4 | bob, member, `read` on `WG_ONE` |
| 5, 6 | carol, member, `write` on `WG_ONE` |
| 7, 8 | org device `odev`, assigned to `WG_ONE` by its invitation |
| 9, 10 | the runner device `rdev` assigned to `WG` and `WG_ONE` (not to `WG_TWO`) |

with the wraps of the org key and both workgroup keys for `rdev`. `Events` adds sealed events (profiles, prompts,
outputs, whiteboards) to the world's scopes with the next scope sequence number; `pull` runs the real inbox.

`setup(env)` builds the RUN-11 kit (operation pipeline, session service, subagent controller with the `platform.query`
dispatcher, ADR-038) with every registered read tool, so a read goes the real way: guest journal record
`platform.query` -> journal handler -> dispatcher -> tool of this area -> `platform.read` event, audit, answer.
`RKit.read(sid, tool, arguments)` returns the answer the guest received; `RKit.session(...)` inserts a working session
of any task, workgroup and owner on the workgroup's VM; `RKit.instruct(sid, actor)` sets the acting user of its turn
(the instructing human of ADR-087; None, the default of a fresh session: unknown).

`TaskHub` adds `GET /v1/device/workgroups/{wg}/tasks` and `GET /v1/device/tasks/{task}` to the rig's hub, answering
from `tasks` (every answer checked against `contracts/`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_runner.crypto import event as event_format
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log import vectors as v
from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations.executors import EXECUTORS as OP_EXECUTORS
from agent_runner.operations.pipeline import install as install_pipeline
from agent_runner.sessions import records as session_records
from agent_runner.subagents.extensions import query_tools
from agent_runner.sync.http import HubTransportError
from agent_runner.vm import records as vm_records

from ..operations.kit import pull
from ..reconcile.rig import CERT_SEQ, DEVICE, Env, OrgSpec, World, pid, scope_key, wrap_for
from ..sessions.kit import verify_world
from ..subagents.kit import SKit
from ..subagents.kit import setup as subagents_setup
from ..sync.fakehub import SCHEMAS

Json = dict[str, Any]
ORG = pid("a/org")
WG = pid("a/wg/default")
WG_ONE = pid("a/wg/one")
WG_TWO = pid("a/wg/two")
ALICE, BOB, CAROL, ODEV, RDEV = pid("alice"), pid("bob"), pid("carol"), pid("odev"), pid(DEVICE)
TASK = "01930004-0000-7000-8000-000000000001"
TASK_B = "01930004-0000-7000-8000-000000000002"
TASK_ONE = "01930004-0000-7000-8000-000000000011"
TASK_TWO = "01930004-0000-7000-8000-000000000021"
T0 = "2026-11-21T12:00:00.000Z"
TS = "2026-11-21T12:30:00.000Z"


def fixture_log() -> v.LogBuilder:
    builder = v.LogBuilder(org=ORG)
    builder.genesis("alice", default_wg=WG, private=True)  # 0
    builder.add("wg_create", "alice", wg=WG_ONE, epoch=1)  # 1
    builder.add("wg_create", "alice", wg=WG_TWO, epoch=1)  # 2
    builder.invite_user("alice", "bob", wgs={WG_ONE: "read"})  # 3
    builder.accept("bob")  # 4
    builder.invite_user("alice", "carol", wgs={WG_ONE: "write"})  # 5
    builder.accept("carol")  # 6
    builder.invite_device("alice", "odev", wgs=[WG_ONE])  # 7
    builder.accept("odev", kind="device")  # 8
    builder.add("device_assign", "alice", device=RDEV, wg=WG, platform_seq=CERT_SEQ)  # 9
    builder.add("device_assign", "alice", device=RDEV, wg=WG_ONE, platform_seq=CERT_SEQ)  # 10
    return builder


def fixture_world() -> World:
    world = World()
    spec = world.add_org(OrgSpec(label="a", log=fixture_log(), default_wg=WG))
    for scope, scope_id in (("org", ORG), ("wg", WG), ("wg", WG_ONE)):
        world.wraps.append(wrap_for(spec.founder, scope, scope_id, 1))
    return world


@dataclass
class Events:
    """Sealed events of org `a`, appended to the world's scopes in order."""

    world: World
    seqs: dict[str, int] = field(default_factory=dict)
    count: int = 0

    @property
    def spec(self) -> OrgSpec:
        return self.world.orgs[ORG]

    def add(
        self,
        author: str,
        kind: str,
        body: Json,
        *,
        scope: str = "wg",
        scope_id: str = WG,
        ts: str | None = None,
        **header: str,
    ) -> str:
        """Seal and publish one event; returns its ID."""
        self.count += 1
        label = f"{kind}-{self.count}"
        sealed = sealed_event(
            self.spec, author, kind, scope=scope, scope_id=scope_id, label=label, body=body, ts=ts or TS, **header
        )
        seq = self.seqs.get(scope_id, 0) + 1
        self.seqs[scope_id] = seq
        self.world.add_event(scope_id, seq, sealed)
        return str(sealed["header"]["id"])

    def profiles(self) -> None:
        self.add("alice", "org.profile", {"name": "Acme"}, scope="org", scope_id=ORG)
        for wg, name in ((WG, "General"), (WG_ONE, "Research"), (WG_TWO, "Secret Lab")):
            self.add("alice", "wg.profile", {"name": name}, scope="org", scope_id=ORG, wg=wg)
        for label, name in (("alice", "Alice"), ("bob", "Bob"), ("carol", "Carol")):
            self.add(label, "member.profile", {"display_name": name}, scope="org", scope_id=ORG, ref=pid(label))
        self.add("alice", "device.profile", {"label": "Runner"}, scope="org", scope_id=ORG, ref=RDEV)

    def title(self, task: str, title: str, wg: str = WG, author: str = "alice") -> str:
        return self.add(author, "task.meta", {"title": title}, scope_id=wg, task=task)

    def prompt(self, task: str, text: str, wg: str = WG, author: str = "alice") -> str:
        return self.add(author, "prompt", {"text": text, "attachments": []}, scope_id=wg, task=task)

    def output(self, task: str, text: str, session: str, wg: str = WG) -> str:
        body = {"message_id": f"msg_{self.count}", "role": "assistant", "text": text, "turn": 1}
        return self.add(DEVICE, "session.output", body, scope_id=wg, task=task, session=session)

    def board(
        self, kind: str, entry: str, body: Json, *, level: str, wg: str = WG, task: str = "", author: str = "alice"
    ) -> str:
        if level == "org":
            return self.add(author, kind, body, scope="org", scope_id=ORG, ref=entry)
        return self.add(author, kind, body, scope_id=wg, task=task, ref=entry)


def sealed_event(
    spec: OrgSpec, author: str, kind: str, *, scope: str, scope_id: str, label: str, body: Json, ts: str, **extra: str
) -> Json:
    """A sealed event of org `a` by `author` (epoch 1, `lh` = the current head of the builder)."""
    wg = scope_id if scope == "wg" else extra.pop("wg", "")
    header = event_format.new_header(
        id=pid(f"event/a/{label}"),
        org=ORG,
        scope=scope,
        scope_id=scope_id,
        epoch=1,
        author=pid(author),
        kind=kind,
        ts=ts,
        lh=len(spec.log.entries) - 1,
        wg=wg,
        **extra,
    )
    return event_format.seal_event(scope_key(scope, scope_id, 1), header, body_json(body), v.identity(author).sig)


def task_row(task: str, wg: str = WG, *, owner: str = ALICE, state: str = "working", n: int = 0) -> Json:
    stamp = f"2026-11-21T12:{n:02d}:00.000Z"
    return {
        "task_id": task,
        "org_id": ORG,
        "wg_id": wg,
        "owner_id": owner,
        "state": state,
        "state_reason": None,
        "created_at": stamp,
        "state_at": stamp,
        "last_activity_at": stamp,
        "closed_at": None,
        "main_session_id": None,
        "title_event_id": None,
    }


@dataclass
class TaskHub:
    """The device task endpoints of the hub, installed on the rig's hub instance."""

    env: Env
    tasks: list[Json] = field(default_factory=list)
    """Task rows, newest first."""
    sessions: dict[str, list[Json]] = field(default_factory=dict)
    operations: dict[str, list[Json]] = field(default_factory=dict)
    raw: dict[str, tuple[int, Json]] = field(default_factory=dict)
    """Path -> answer sent without the contract check (a hub that breaks the contract)."""
    unreachable: bool = False
    queries: list[str] = field(default_factory=list)

    def install(self) -> None:
        hub = self.env.hub
        original = hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            path = urlsplit(target).path
            if method == "GET" and path.startswith(("/v1/device/workgroups/", "/v1/device/tasks/")):
                self.queries.append(target)
                if path in self.raw:
                    return self.raw[path]
                return self.answer(path, parse_qs(urlsplit(target).query))
            return original(method, target, body)

        hub.route = route  # type: ignore[method-assign]
        client = self.env.sync.client
        send = client.request

        async def request(method: str, target: str, body: object | None = None) -> dict[str, object]:
            if self.unreachable and target.startswith(("/v1/device/workgroups/", "/v1/device/tasks/")):
                raise HubTransportError("hub_unreachable")
            return await send(method, target, body)

        client.request = request  # type: ignore[method-assign]

    def answer(self, path: str, query: dict[str, list[str]]) -> tuple[int, Json]:
        hub = self.env.hub
        parts = path.split("/")
        if parts[3] == "workgroups":
            hub.check("api/device-workgroup-tasks.request.json", {k: vals[0] for k, vals in query.items()})
            wg = parts[4]
            if wg not in (WG, WG_ONE):
                return 403, {"error": "forbidden"}
            rows = [row for row in self.tasks if row["wg_id"] == wg]
            if "state" in query:
                rows = [row for row in rows if row["state"] == query["state"][0]]
            if "before" in query:
                index = next(i for i, row in enumerate(rows) if row["task_id"] == query["before"][0].replace("_", "-"))
                rows = rows[index + 1 :]
            limit = int(query.get("limit", ["50"])[0])
            more = len(rows) > limit
            chosen = rows[:limit]
            answer: Json = {
                "tasks": chosen,
                "next_before": chosen[-1]["task_id"].replace("-", "_") if more else None,
            }
            hub.check("api/device-workgroup-tasks.response.json", answer)
            return 200, answer
        task = parts[4]
        found = [row for row in self.tasks if row["task_id"] == task and row["wg_id"] in (WG, WG_ONE)]
        if not found:
            return 404, {"error": "not_found"}
        answer = {
            "task": found[0],
            "sessions": self.sessions.get(task, []),
            "operations": self.operations.get(task, []),
        }
        hub.check("api/device-task-get.response.json", answer)
        return 200, answer


def session_row(sid: str, task: str, *, state: str = "working", depth: int = 0, parent: str | None = None) -> Json:
    return {
        "session_id": sid,
        "task_id": task,
        "org_id": ORG,
        "wg_id": WG,
        "owner_id": ALICE,
        "device_id": RDEV,
        "vm_id": "vm-00000001",
        "parent_session_id": parent,
        "origin_operation_id": None,
        "depth": depth,
        "state": state,
        "state_reason": None,
        "model_access": "platform",
        "model_id": "deepseek/deepseek-v4.1-flash-1017",
        "agent_kind": "claude-code",
        "agent_version": "2.1.330",
        "estimate_ms": None,
        "created_at": T0,
        "ready_at": T0,
        "first_working_at": T0,
        "ended_at": None,
    }


@dataclass
class RKit:
    env: Env
    skit: SKit
    events: Events
    hub: TaskHub
    seq: int = 5000

    async def pull(self) -> int:
        return await pull(self.env)

    async def session(
        self,
        n: int,
        *,
        task: str = TASK,
        wg: str = WG,
        owner: str = ALICE,
        depth: int = 0,
        parent: str | None = None,
    ) -> str:
        row = await self.skit.ops.kit.vms.ensure_vm(ORG, wg)
        sid = f"01930005-0000-7000-8000-{n:012d}"
        async with self.env.pool.connection() as conn, conn.transaction():
            await session_records.insert(
                conn,
                session_id=sid,
                task_id=task,
                org_id=ORG,
                wg_id=wg,
                parent_id=parent,
                depth=depth,
                owner_id=owner,
                created_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
            )
            await session_records.update(
                conn, sid, state="working", vm_id=row.id, model_access="platform", model_id="deepseek/x-1"
            )
        return sid

    async def instruct(self, sid: str, actor: str | None) -> None:
        """Make `actor` the acting user of the session's current turn (`runner.session.turn_actor`, as an accepted
        prompt of that person does; None: several people or none), read by the next forwarded read (ADR-087)."""
        async with self.env.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE runner.session SET turn_actor = %s WHERE id = %s", (actor, sid))

    async def journal_read(self, sid: str, tool: str, arguments: Json | None = None) -> str:
        """Journal a forwarded read of `sid` without waiting for its answer; its pending ID."""
        async with self.env.pool.connection() as conn:
            found = await session_records.get(conn, sid)
        assert found is not None
        assert found.vm_id is not None
        row = await vm_records.get(self.env.pool, found.vm_id)
        assert row is not None
        pending = uuid7()
        self.seq += 1
        body = {"pending_id": pending, "tool": tool, "arguments": arguments or {}}
        record = JournalRecord(
            seq=self.seq, ts="2026-11-21T12:00:02.000Z", session_id=sid, kind="platform.query", body=body
        )
        await self.skit.reader(row).handle([record])
        return pending

    def answer(self, pending: str, tool: str) -> Json:
        """The one answer the guest received for `pending` (results checked against the contract)."""
        answers = [answer for pid_, answer in self.skit.guest.answers() if pid_ == pending]
        assert len(answers) == 1, answers
        answer = answers[0]
        if answer["status"] == "ok":
            assert SCHEMAS.errors(f"mcp/tools/{tool}.output.json", answer["result"]) == [], answer["result"]
        else:
            assert SCHEMAS.errors("mcp/tool-error.json", answer["error"]) == [], answer["error"]
        return answer

    async def read(self, sid: str, tool: str, arguments: Json | None = None) -> Json:
        """A forwarded read of `sid`; the answer the guest received (results checked against the contract)."""
        pending = await self.journal_read(sid, tool, arguments)
        await self.skit.settle_queries()
        return self.answer(pending, tool)

    async def ok(self, sid: str, tool: str, arguments: Json | None = None) -> Any:
        answer = await self.read(sid, tool, arguments)
        assert answer["status"] == "ok", answer
        return answer["result"]

    async def error(self, sid: str, tool: str, arguments: Json | None = None) -> str:
        answer = await self.read(sid, tool, arguments)
        assert answer["status"] == "error", answer
        return str(answer["error"]["error"])

    async def reads(self) -> list[tuple[Json, Json]]:
        """`(header, body)` of every pushed `platform.read` event."""
        return [(h, b) for h, b in await self.skit.ops.kit.pushed_events() if h["kind"] == "platform.read"]

    async def read_audits(self) -> list[Json]:
        return [a for a in await self.env.audits() if a["action"].startswith("read.session.")]

    async def extend(self, *entries: Callable[[v.LogBuilder], object]) -> None:
        """Append log entries to org `a` and let the device verify the longer log."""
        for build in entries:
            build(self.events.spec.log)
        await verify_world(self.env)


async def setup(env: Env, *, profiles: bool = True) -> RKit:
    skit = await subagents_setup(env, tools=query_tools(), executors=OP_EXECUTORS)
    install_pipeline(env.context, skit.ops.pipeline)
    hub = TaskHub(env)
    hub.install()
    events = Events(env.world)
    if profiles:
        events.profiles()
    return RKit(env=env, skit=skit, events=events, hub=hub)


def run(rig: Any, body: Callable[[RKit], Awaitable[None]], *, profiles: bool = True) -> None:
    async def main() -> None:
        async with rig.env(fixture_world()) as env:
            await body(await setup(env, profiles=profiles))

    asyncio.run(main())

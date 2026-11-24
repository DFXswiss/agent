"""Test kit of the RUN-05 suites: real verified prompt events, a guest that takes prompts, local sessions in any
state and a `Deliverer` over the RUN-04 kit.

- `add_prompt(env, seq, label, ...)` seals a prompt of org `a` (default workgroup, task `TASK`) with the reference
  builders and lets the rig's hub serve it at `seq`; `pull(env)` runs the real inbox (verification, consumers).
- `PromptGuest` extends the RUN-04 fake guest with `POST .../prompts` (calls recorded, transient errors queued).
- `setup(env)` builds the RUN-04 kit (verified logs and keys of the rig) and installs a `Deliverer` on the context;
  `local(...)` writes a local main session in a chosen state on the kit's VM; `journal(...)` hands guest
  `session.state` records to the real journal handlers with the prompt observer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runner.contexts import session_request
from agent_runner.guestlink.client import JournalRecord
from agent_runner.inbox.pull import inbox_for
from agent_runner.prompts import first, session_state
from agent_runner.prompts.deliver import Deliverer, Routing, install
from agent_runner.prompts.queue import Delivery
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS
from agent_runner.vm.records import VmRow

from ..reconcile.rig import Env, seal
from ..sessions.kit import ORG, OWNER, TASK, WG, FakeGuest, Kit, kit, session_id

CLAUDE_ID = "11111111-2222-4333-8444-555555555555"
MODEL = "deepseek/deepseek-v4.1-flash-1017"


def add_prompt(
    env: Env, seq: int, label: str, *, task: str = TASK, author: str = "alice", text: str | None = None, **extra: Any
) -> str:
    """A prompt event at `seq` of the default workgroup; returns its event ID."""
    org = env.world.orgs[ORG]
    body = {"text": text if text is not None else f"Prompt {label}", "attachments": []}
    sealed = seal(org, author, "prompt", label=label, task=task, body=body, **extra)
    env.world.add_event(WG, seq, sealed)
    return str(sealed["header"]["id"])


async def pull(env: Env) -> int:
    inbox = inbox_for(env.context)
    assert inbox is not None
    return await inbox.pull_all()


@dataclass
class PromptGuest(FakeGuest):
    prompt_errors: list[BaseException] = field(default_factory=list)
    """Raised by the next `register_prompt` calls, one each."""

    async def register_prompt(self, sid: str, prompt_id: str, text: str) -> dict[str, Any]:
        self._call("register_prompt", (sid, prompt_id, text))
        if self.prompt_errors:
            raise self.prompt_errors.pop(0)
        return {"state": "registered"}

    def sent(self) -> list[str]:
        """The prompt IDs of every `POST .../prompts`, in order."""
        return [value[1] for name, value in self.calls if name == "register_prompt"]

    def texts(self) -> list[str]:
        return [value[2] for name, value in self.calls if name == "register_prompt"]


@dataclass
class PKit:
    env: Env
    kit: Kit
    guest: PromptGuest
    deliverer: Deliverer
    stops: list[str]
    seq: int = 0

    @property
    def lines(self) -> list[str]:
        return self.kit.lines

    async def vm(self) -> VmRow:
        return await self.kit.vms.ensure_vm(ORG, WG)

    async def add_session(
        self,
        n: int = 1,
        state: str = "idle",
        *,
        claude: str | None = CLAUDE_ID,
        depth: int = 0,
        task: str = TASK,
        vm_id: str | None = None,
    ) -> str:
        """A local session in `state` on the workgroup's VM."""
        row = await self.vm()
        sid = session_id(n)
        async with self.env.pool.connection() as conn, conn.transaction():
            await records.insert(
                conn,
                session_id=sid,
                task_id=task,
                org_id=ORG,
                wg_id=WG,
                parent_id=None if depth == 0 else session_id(99),
                depth=depth,
                owner_id=OWNER,
                created_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
            )
            await records.update(
                conn,
                sid,
                state=state,
                vm_id=vm_id or row.id,
                claude_session_id=claude,
                model_access="platform",
                model_id=MODEL,
            )
        return sid

    async def session(self, sid: str) -> records.LocalSession:
        return await self.kit.stored(sid)

    async def set_state(self, sid: str, state: str) -> None:
        async with self.env.pool.connection() as conn:
            await records.update(conn, sid, state=state)

    async def rows(self, sid: str | None = None) -> dict[str, str]:
        """event ID -> delivery state (of one session when given)."""
        sql = "SELECT event_id::text, state FROM runner.prompt_delivery"
        found = await self.env.rows(sql + (" WHERE session_id = %s" if sid else ""), *([sid] if sid else []))
        return {str(event_id): str(state) for event_id, state in found}

    async def row(self, event_id: str) -> Delivery:
        from agent_runner.prompts import queue  # noqa: PLC0415

        async with self.env.pool.connection() as conn:
            found = await queue.delivery(conn, event_id)
        assert found is not None
        return found

    async def route(self, sid: str, seq: int, event_id: str, *, task: str = TASK, scope: str = WG) -> None:
        await self.deliverer.route([Routing(task, sid, scope, seq, event_id)])

    async def age(self, event_id: str, seconds: int) -> None:
        """Make a row look `seconds` old."""
        async with self.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.prompt_delivery SET updated_at = now() - make_interval(secs => %s) WHERE event_id = %s",
                (seconds, event_id),
            )

    def reader(self, row: VmRow) -> VmJournal:
        return VmJournal(self.kit.session_env, row, handlers=HANDLERS, observers=session_state.OBSERVERS)

    async def journal(self, sid: str, **body: Any) -> None:
        """One guest `session.state` record through the real handlers and the prompt observer."""
        self.seq += 1
        record = JournalRecord(
            seq=self.seq, ts="2026-11-21T12:00:01.000Z", session_id=sid, kind="session.state", body=dict(body)
        )
        await self.reader(await self.vm()).handle([record])

    async def audit_codes(self) -> list[str]:
        return [
            str(item["detail"]["code"])
            for item in await self.env.audits()
            if item["action"] == "runner.inbound_rejected"
        ]

    async def statuses(self) -> list[dict[str, Any]]:
        return [item["body"] for item in await self.kit.of_kind("event") if item["header"]["kind"] == "session.status"]

    async def advance(self, sid: str, *, later: float = 0.0) -> None:
        self.env.clock.now += later
        await self.deliverer.advance(sid)


async def setup(env: Env, *, guest: PromptGuest | None = None, idle_seconds: float = 60.0, **options: Any) -> PKit:
    guest = guest or PromptGuest()

    async def refresh() -> None:
        await pull(env)

    found = await kit(env, guest=guest, refresh=refresh, **options)
    found.session_env.first_prompt = first.first_prompt
    found.session_env.amenders = list(session_request.AMENDERS)
    from ..contexts.kit import install_contexts  # noqa: PLC0415 - the context kit builds on this kit

    install_contexts(env, found)
    stops: list[str] = []
    deliverer = install(env.context, Deliverer(found.session_env, idle_seconds=idle_seconds, stop=stops.append))
    return PKit(env=env, kit=found, guest=guest, deliverer=deliverer, stops=stops)


def run(rig: Any, body: Any, world: Any = None) -> None:
    from ..reconcile.rig import standard_world  # noqa: PLC0415

    async def main() -> None:
        async with rig.env(world or standard_world()) as env:
            await body(env)

    asyncio.run(main())

"""Test kit of the RUN-11 suites: the RUN-07 operation kit (contract-checking hub, fake guest with pending answers, a
real pipeline) plus the RUN-04 session service and the subagent controller, installed on the rig's daemon context.

- `setup(env)` builds everything; the session start uses only this area's request amender, the journal uses every
  registered journal handler and state observer of the runner (the real ones).
- `SKit.parent()` inserts a working main session on the workgroup VM; `SKit.subagent(sid, ...)` journals a
  `subagent.request` of that session and lets the pipeline process it with the scripted hub decision (default:
  accepted with a fresh child ID); `SKit.emit(sid, kind, body)` hands one guest journal record to the real handlers.
- `SKit.read(sid, arguments)` journals a `platform.query` `get_subagent_result` and waits for its answer.
- The controller's wall clock is `SKit.now` (seconds), moved by `advance`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner import registry
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations import records as op_records
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.extensions import RequestAmender, state_observers
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.service import SessionService
from agent_runner.subagents import session_request
from agent_runner.subagents.controller import Subagents, install
from agent_runner.subagents.executors import EXECUTORS
from agent_runner.subagents.extensions import FinishedPing, QueryTool
from agent_runner.subagents.op_prechecks import PRECHECKS
from agent_runner.subagents.query_tools import TOOLS

from ..operations.kit import OKit, OpGuest
from ..operations.kit import setup as ops_setup
from ..reconcile.rig import Env, standard_world
from ..sessions.kit import NOW, ORG, OWNER, TASK, WG, session_id
from ..settle import settle

Json = dict[str, Any]
PURPOSE = "Review the pull request"
PROMPT = "Please review the change in repos/example."


@dataclass
class SKit:
    env: Env
    ops: OKit
    service: SessionService
    controller: Subagents
    pinged: list[FinishedPing]
    now: list[float]
    seq: int = 1000

    @property
    def guest(self) -> OpGuest:
        return self.ops.guest

    @property
    def lines(self) -> list[str]:
        return self.ops.lines

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    async def parent(self, n: int = 1) -> str:
        return await self.ops.session(n)

    async def stored(self, sid: str) -> LocalSession:
        async with self.env.pool.connection() as conn:
            found = await records.get(conn, sid)
        assert found is not None
        return found

    async def subagent(
        self,
        sid: str,
        *,
        purpose: str = PURPOSE,
        prompt: str = PROMPT,
        decision: Json | None = None,
        child: str | None = None,
        **params: Any,
    ) -> op_records.Operation:
        """Journal a `subagent.request` of `sid`, let the pipeline process it; returns the final operation row."""
        child = child or uuid7()
        if decision is None:
            decision = {"state": "accepted", "child_session_id": child}
        self.ops.hub.decisions.append(decision)
        op = await self.ops.run(sid, "subagent.request", {"purpose": purpose, "prompt": prompt, **params})
        await self.service.idle()
        return op

    def reader(self, row: Any) -> VmJournal:
        return VmJournal(
            self.ops.kit.session_env, row, handlers=registry.journal_handlers(), observers=state_observers()
        )

    async def emit(self, sid: str | None, kind: str, body: Json) -> None:
        self.seq += 1
        record = JournalRecord(seq=self.seq, ts="2026-11-21T12:00:02.000Z", session_id=sid, kind=kind, body=body)
        await self.reader(await self.ops.vm()).handle([record])

    async def output(self, sid: str, text: str, message_id: str = "msg_1", turn: int = 1) -> None:
        await self.emit(
            sid, "session.output", {"message_id": message_id, "role": "assistant", "text": text, "turn": turn}
        )

    async def state(self, sid: str, state: str, **extra: Any) -> None:
        await self.emit(sid, "session.state", {"state": state, "at": "2026-11-21T12:00:02.000Z", **extra})

    async def hook(self, sid: str, event: str) -> None:
        await self.emit(sid, "session.hook", {"event": event, "decision": "none"})

    async def read(self, sid: str, arguments: Json, tool: str = "get_subagent_result") -> Json:
        """A forwarded read of `sid`; returns the answer the guest received."""
        pending = uuid7()
        await self.emit(sid, "platform.query", {"pending_id": pending, "tool": tool, "arguments": arguments})
        await self.settle_queries()
        found = [answer for name, (pid, answer) in self.guest_answers() if pid == pending]
        assert len(found) == 1, found
        return found[0]

    def guest_answers(self) -> list[tuple[str, tuple[str, Json]]]:
        return [(name, value) for name, value in self.guest.calls if name == "answer_pending"]

    async def settle_queries(self) -> None:
        await settle(lambda: list(self.controller.queries), "the forwarded reads")

    async def settle_ends(self) -> None:
        await settle(lambda: list(self.controller.ending.values()), "the child ends")

    async def tick(self) -> None:
        await self.controller.tick()
        await self.settle_ends()
        await self.service.idle()


async def setup(
    env: Env,
    *,
    notify: bool = True,
    tools: Mapping[str, QueryTool] | None = None,
    executors: Mapping[str, Any] | None = None,
) -> SKit:
    ops = await ops_setup(
        env,
        executors={**EXECUTORS, **(executors or {})},
        prechecks=PRECHECKS,
        notify=False,
    )
    session_env = ops.kit.session_env
    session_env.amenders = [RequestAmender("subagents.child", session_request.amend)]
    service = SessionService(session_env, sleep=env.clock.sleep)
    pinged: list[FinishedPing] = []
    now = [NOW.timestamp()]

    async def record(_context: Any, ping: FinishedPing) -> None:
        pinged.append(ping)

    controller = Subagents(
        session_env,
        service,
        notify=record if notify else None,
        tools=TOOLS if tools is None else tools,
        sleep=env.clock.sleep,
        wall=lambda: now[0],
    )
    install(env.context, controller)
    return SKit(env=env, ops=ops, service=service, controller=controller, pinged=pinged, now=now)


def run(rig: Any, body: Callable[[Env], Awaitable[None]]) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(env)

    asyncio.run(main())


@dataclass
class Book:
    """A verified-log stand-in for the reconcile step: the assigned workgroups."""

    assigned: list[tuple[str, str]] = field(default_factory=lambda: [(ORG, WG)])

    def assignments(self) -> list[tuple[str, str]]:
        return list(self.assigned)


__all__ = ["ORG", "OWNER", "PROMPT", "PURPOSE", "TASK", "WG", "Book", "SKit", "run", "session_id", "setup"]

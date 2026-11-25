"""The life of a child session that runs on a current guest: turns, follow-up instructions, end (ADR-088 section 1;
05-runner.md section 13, 03-api.md sections 8.1 and 9).

`Turns` (one per controller, `controller.Subagents.turns`) holds this part of the controller. A child whose VM runs
a guest that knows ADR-088 (`guest_features.current_guest`, read once per child) is **not** ended after its first
turn; a child on an older guest keeps the one-shot end of ADR-038 (`controller.Subagents._finish`) and the former
answers of `get_subagent_result`.

- **Instructions** (`instructions.py`): the `subagent.request` is the first (recorded by `record_request` when the
  start begins, handed over with the start), every `subagent.instruct` another one. `instruct` (the executor) records
  it and waits for its turn: it is handed over only when every earlier live instruction of the child was accepted and
  its turn ended and the child is not working (`HANDABLE`; one instruction per turn, in the order of creation). A
  suspended child is resumed first (`prompts.resume.Resumer`, with the child's limits and harness line,
  `controller.Subagents.resume_request`). The prompt is registered at the child's guest with the child's sender line
  (`speaker.for_child`: written by the parent, which works on an instruction of the operation's acting user) and
  prompt ID = operation ID, so a repeated delivery never types twice (`state_conflict` = registered before). The
  result is `{session_id, instruction_id}` once registered. A child that ended (or is stopping) while the instruction
  waited fails it `not_found`; a guest that cannot be reached, or a resume to try again, fails it retryable
  (`upstream_error`: the pipeline retries); a resume that ended the child fails it `not_found`.
- **Turn end** (`work`, from `controller.Subagents.tick`): `GRACE_S` after the child became `idle` with its newest
  instruction's turn ended, the turn is handled once per daemon: a turn that ended with `StopFailure` fails the child
  `agent_exited` (as before); otherwise, once settled (`SETTLE_S` after the end was first seen and the VM's journal
  read up to the guest's head of that moment, at the latest `SETTLE_MAX_S`), the parent - when it has not ended - gets
  the ping `subagent.finished` with a ping ID stable per child and instruction (`ping_id`; `ref` stays the child). A
  restart pings the last turn again with the same ID, which the guest recognises.
- **Idle -> suspend**: a child `idle` for the device's idle time (`AGENT_RUNNER_SESSION_IDLE_SECONDS`, as a main
  session) whose newest instruction's turn ended gets the guest control `suspend` and becomes `suspended`; never while
  an instruction is waiting or being handed over (the same per-child lock).
- **End**: `stop` (the executor of `subagent.stop`) stops the child (`sessions.stop.stop_session`) and answers
  `{session_id, state}`; a child whose parent ended (`stopped`/`failed`) is stopped by the tick (`parent_ended`).
  The end of a child pings its parent with the ping ID of its newest instruction (`final_ping_id`).
- **Result** (`view`): the newest live instruction: `working` (or the start state) with its `instruction_id` while it
  waits or its turn runs; `idle`/`suspended` with `final_text`, `finished_at` (the end of the turn) and
  `instruction_id` once its turn ended and settled; `stopped` with the last turn's `final_text` and `instruction_id`;
  `failed`; `stopping` until an end settled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.orgs.db import session as store_session
from agent_runner.pings.send import stable_ping_id
from agent_runner.prompts import speaker
from agent_runner.prompts.resume import RESUMED, RETRY, Resumer, vm_running
from agent_runner.sessions import records
from agent_runner.sessions.events import fail
from agent_runner.sessions.records import FINAL, START_CHAIN, LocalSession, time_text
from agent_runner.sessions.stop import stop_guest, stop_session
from agent_runner.subagents import instructions, results, store, target_checks
from agent_runner.subagents.guest_features import current_guest
from agent_runner.subagents.instructions import Instruction

KIND = "subagent.finished"
HANDABLE = frozenset({"idle", "sandbox_ready", "agent_starting", "prompt_delivering"})
"""Child states in which the next instruction is handed over (after a resume the guest queues it until the agent is
ready); `suspended` and `resuming` are resumed first."""
SUSPENDED = frozenset({"suspended", "resuming"})
NOT_CHILD = "The session is no running subagent of this session."
ENDED = "The subagent ended before it received the instruction."
UNREACHABLE = "The computer of the subagent cannot be reached now."
REFUSED = "The computer of the subagent refused the instruction."
WAIT_S = 1.0
TASK_LOCK = "task:"
"""Prefix of the lock key that serialises resuming children of one task (running limit, VM room)."""


def ping_id(child_session_id: str, instruction_id: str) -> str:
    """The stable ping ID of the end of one instruction's turn of a child (`pings.send.stable_ping_id`, UUIDv7-shaped
    from the instruction ID, distinct per child and instruction)."""
    return stable_ping_id(f"{KIND}:{child_session_id}", instruction_id)


def own_child(child: LocalSession | None, parent: LocalSession) -> LocalSession:
    """`child` when it is a child of `parent` in the same task that has not ended; `OperationFailed` otherwise."""
    if (
        child is None
        or child.parent_id != parent.id
        or child.task_id != parent.task_id
        or child.state in FINAL
        or child.state == "stopping"
    ):
        raise OperationFailed("not_found", message=NOT_CHILD)
    return child


class Turns:
    def __init__(self, controller: Any) -> None:
        self.c = controller
        self.env = controller.env
        self.resumer = Resumer(controller.env)
        self.locks: dict[str, asyncio.Lock] = {}
        self.current_of: dict[str, bool] = {}
        self.turned: set[str] = set()
        self.seen: dict[str, float] = {}
        self.heads: dict[str, int] = {}
        self.texts: dict[str, str] = {}
        self.changed = asyncio.Event()

    def lock(self, child_id: str) -> asyncio.Lock:
        return self.locks.setdefault(child_id, asyncio.Lock())

    def wake(self) -> None:
        """A child's state changed: waiting instructions look again."""
        self.changed.set()
        self.changed = asyncio.Event()

    async def current(self, child: LocalSession) -> bool:
        """The child's VM runs a guest that knows ADR-088 (read once per child: a child keeps its VM)."""
        found = self.current_of.get(child.id)
        if found is None:
            found = self.current_of[child.id] = await current_guest(self.env.db, child.vm_id)
        return found

    def prune(self, watched: set[str]) -> None:
        for kept in (self.current_of, self.locks):
            for child_id in [key for key in kept if key not in watched]:
                del kept[child_id]
        for keyed in (self.seen, self.heads, self.texts):
            for key in [key for key in keyed if key.split(":")[0] not in watched]:
                del keyed[key]
        self.turned = {key for key in self.turned if key.split(":")[0] in watched}

    # -- instructions --------------------------------------------------------------------------------------------

    async def record_request(self, operation: Any, child_id: str, parent_id: str) -> None:
        """The `subagent.request` as the child's first instruction, handed over with the start."""
        async with store_session(self.env.db) as conn:
            await instructions.add(
                conn,
                operation_id=operation.id,
                child_id=child_id,
                parent_id=parent_id,
                actor_id=operation.actor_id,
                created_at=operation.created_at,
                delivered_at=operation.created_at,
            )

    async def instruct(self, ctx: OperationContext) -> dict[str, Any]:
        op, parent = ctx.operation, ctx.session
        child_id, prompt = str(ctx.params["session_id"]), str(ctx.params["prompt"])
        async with store_session(self.env.db) as conn:
            own_child(await records.get(conn, child_id), parent)
            await instructions.add(
                conn,
                operation_id=op.id,
                child_id=child_id,
                parent_id=parent.id,
                actor_id=op.actor_id,
                created_at=op.created_at,
            )
        while True:
            changed = self.changed
            async with self.lock(child_id):
                handed = await self._hand_over(op, parent, child_id, prompt)
            if handed is not None:
                return handed
            await self._wait(changed)

    async def _wait(self, changed: asyncio.Event) -> None:
        waiters = {asyncio.ensure_future(changed.wait()), asyncio.ensure_future(self.c.sleep(WAIT_S))}
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _hand_over(self, op: Any, parent: LocalSession, child_id: str, prompt: str) -> dict[str, Any] | None:
        """Hand the instruction over when it is its turn (module docstring); None: not yet."""
        result = {"session_id": child_id, "instruction_id": op.id}
        async with store_session(self.env.db) as conn:
            child = await records.get(conn, child_id)
            mine = await instructions.get(conn, op.id)
            blocked = await instructions.ahead(conn, child_id, op.id)
        if mine is not None and mine.delivered_at is not None:
            return result
        if child is None or child.state in FINAL or child.state == "stopping":
            raise OperationFailed("not_found", message=ENDED)
        if blocked or (child.state not in HANDABLE and child.state not in SUSPENDED):
            return None
        if child.state in SUSPENDED:
            async with self.lock(TASK_LOCK + child.task_id):
                async with store_session(self.env.db) as conn:
                    refused = await target_checks.resume_refusal(self.env.context, conn, child)
                if refused is not None:
                    raise OperationFailed("limit_exceeded", message=refused)
                outcome = await self.resumer.resume(child, self.c.resume_request)
            if outcome == RETRY:
                raise OperationFailed("upstream_error", retryable=True, message=UNREACHABLE)
            if outcome != RESUMED:
                raise OperationFailed("not_found", message=ENDED)
            async with store_session(self.env.db) as conn:
                child = await records.get(conn, child_id)
            assert child is not None
        row = await vm_running(self.env, child)
        if row is None:
            raise OperationFailed("upstream_error", retryable=True, message=UNREACHABLE)
        async with store_session(self.env.db) as conn:
            line = await speaker.for_child(
                conn, self.env.storage_key, org_id=child.org_id, parent_id=parent.id, actor_id=op.actor_id, text=prompt
            )
        # write-ahead: the child may accept the prompt (journal) before this coroutine sees the guest's answer
        at = datetime.fromtimestamp(self.env.wall(), UTC)
        async with self.env.transaction() as conn:
            await instructions.delivered(conn, op.id, at)
            await records.update(conn, child_id, last_prompt_at=at)
        try:
            async with await self.env.client(row) as guest:
                await speaker.register(
                    self.env, guest, vm_id=row.id, session_id=child_id, prompt_id=op.id, text=prompt, line=line
                )
        except GuestRefused as exc:
            if exc.error != "state_conflict":
                await self._undeliver(op.id)
                raise OperationFailed("upstream_error", message=REFUSED) from None
        except (GuestError, RunnerError):
            await self._undeliver(op.id)
            raise OperationFailed("upstream_error", retryable=True, message=UNREACHABLE) from None
        self.c.line(f"instructed child_session_id={child_id} operation_id={op.id}")
        return result

    async def _undeliver(self, operation_id: str) -> None:
        """The guest did not take the prompt: the instruction is not handed over (`instructions.undelivered`)."""
        async with self.env.transaction() as conn:
            await instructions.undelivered(conn, operation_id)

    # -- end -----------------------------------------------------------------------------------------------------

    async def stop(self, ctx: OperationContext) -> dict[str, Any]:
        """The executor of `subagent.stop`: `{session_id, state}` with the child's state after the stop."""
        child_id = str(ctx.params["session_id"])
        async with store_session(self.env.db) as conn:
            child = await records.get(conn, child_id)
        if child is not None and child.state in FINAL and child.parent_id == ctx.session.id:
            return {"session_id": child_id, "state": child.state}
        own_child(child, ctx.session)
        async with self.lock(child_id):
            await stop_session(self.env, child_id)
        self.c.line(f"stop_requested child_session_id={child_id} operation_id={ctx.operation.id}")
        self.c.wake()
        async with store_session(self.env.db) as conn:
            after = await records.get(conn, child_id)
        assert after is not None
        return {"session_id": child_id, "state": after.state if after.state in FINAL else "stopping"}

    async def parent_ended(self, child: LocalSession) -> None:
        stopped = await stop_session(self.env, child.id)
        self.c.line(f"parent_ended child_session_id={child.id} stopped={int(stopped)}")

    async def final_ping_id(self, child: LocalSession) -> str | None:
        """The ping ID of a child's end: of its newest instruction; None without one (the former ID per child)."""
        async with store_session(self.env.db) as conn:
            newest = (await instructions.newest(conn, [child.id])).get(child.id)
        return None if newest is None else ping_id(child.id, newest.operation_id)

    # -- turns ---------------------------------------------------------------------------------------------------

    async def survey(self, live: list[LocalSession]) -> tuple[dict[str, Instruction], set[str]]:
        """The newest instruction of each of the `live` children and those whose parent ended."""
        ids = [child.id for child in live]
        async with store_session(self.env.db) as conn:
            return await instructions.newest(conn, ids), await store.parents_ended(conn, ids)

    def surveyor(self, live: list[LocalSession]) -> Callable[[], Awaitable[tuple[dict[str, Instruction], set[str]]]]:
        """`survey(live)` on first use, once per tick (a tick without a full child asks nothing)."""
        found: list[tuple[dict[str, Instruction], set[str]]] = []

        async def survey() -> tuple[dict[str, Instruction], set[str]]:
            if not found:
                found.append(await self.survey(live))
            return found[0]

        return survey

    @staticmethod
    def key(child_id: str, newest: Instruction | None) -> str:
        return child_id if newest is None else f"{child_id}:{newest.operation_id}"

    def due(self, child: LocalSession, newest: Instruction | None, idle_for: float) -> bool:
        """Work for an `idle` child (`GRACE_S` passed): the end of its newest turn to handle, or the suspension."""
        if newest is not None and newest.ended_at is None:
            return False
        return self.key(child.id, newest) not in self.turned or idle_for >= self.c.idle_seconds

    async def work(self, child: LocalSession, newest: Instruction | None) -> None:
        """What `due` found: the end of the newest turn when it was not handled yet, else the suspension."""
        key = self.key(child.id, newest)
        if key not in self.turned:
            await self._turn_ended(child, newest, key)
        else:
            await self._suspend(child)

    async def _turn_ended(self, child: LocalSession, newest: Instruction | None, key: str) -> None:
        if not await self.settled(key, child.vm_id):
            return
        async with store_session(self.env.db) as conn:
            ended_by = await results.turn_end(conn, self.env.storage_key, child)
        if ended_by == "StopFailure":
            async with self.env.transaction() as conn:
                moved = await fail(conn, self.env.outbox, child.id, "agent_exited", at=self.env.at())
            self.c.line(f"turn_failed child_session_id={child.id}")
            if moved is not None:
                await stop_guest(self.env, child.id)
            return
        self.turned.add(key)
        instruction = "-" if newest is None else newest.operation_id
        self.c.line(f"turn_ended child_session_id={child.id} instruction_id={instruction}")
        await self.c.notify_parent(child, None if newest is None else ping_id(child.id, newest.operation_id))

    async def ping_earlier(self, live: list[LocalSession]) -> None:
        """The turn ping of every earlier instruction whose turn ended while a later one already followed (the parent
        instructed again before the newest turn's end was handled, `due`): no settling is needed, the child's records
        of that turn came before the later prompt. Once per daemon and instruction; within `store.RECENT_S`."""
        if not live:
            return
        since = datetime.fromtimestamp(self.env.wall() - store.RECENT_S, UTC)
        async with store_session(self.env.db) as conn:
            earlier = await instructions.ended_before_newest(conn, [child.id for child in live], since)
        children = {child.id: child for child in live}
        for row in earlier:
            key = self.key(row.child_session_id, row)
            if key in self.turned:
                continue
            self.turned.add(key)
            self.c.line(f"turn_ended child_session_id={row.child_session_id} instruction_id={row.operation_id}")
            await self.c.notify_parent(children[row.child_session_id], ping_id(row.child_session_id, row.operation_id))

    async def settled(self, key: str, vm_id: str | None) -> bool:
        """The end of a turn whose last records arrived (`controller.Subagents.settled_since`)."""
        return bool(await self.c.settled_since(key, vm_id, self.seen, self.heads))

    async def _suspend(self, child: LocalSession) -> None:
        env = self.env
        async with self.lock(child.id):
            async with store_session(env.db) as conn:
                current = await records.get(conn, child.id)
                newest = (await instructions.newest(conn, [child.id])).get(child.id)
            if current is None or current.state != "idle" or (newest is not None and newest.ended_at is None):
                return
            row = await vm_running(env, current)
            if row is None:
                return
            try:
                async with await env.client(row) as guest:
                    await guest.control(child.id, "suspend")
            except GuestRefused as exc:
                self.c.idle_since[child.id] = env.clock()  # a turn started meanwhile: count again from now
                self.c.line(f"suspend_refused child_session_id={child.id} error={exc.error}")
                return
            except (GuestError, RunnerError) as exc:
                self.c.line(f"suspend_retry child_session_id={child.id} code={exc.code}")
                return
            async with env.transaction() as conn:
                await records.advance(conn, env.outbox, child.id, "suspended", at=env.at())
            self.c.idle_since.pop(child.id, None)
            self.c.line(f"suspended child_session_id={child.id}")

    # -- get_subagent_result -------------------------------------------------------------------------------------

    async def view(self, row: LocalSession) -> dict[str, Any] | None:
        """The answer for a child of the caller on a current guest; None for a child without instructions (the
        former answer applies)."""
        async with store_session(self.env.db) as conn:
            newest = (await instructions.newest(conn, [row.id])).get(row.id)
        if newest is None:
            return None
        child_id, instruction_id = row.id, newest.operation_id
        if row.state in FINAL or row.state == "stopping":
            return await self._ended_view(row, instruction_id)
        key = self.key(child_id, newest)
        if newest.ended_at is None or not await self.settled(key, row.vm_id):
            state = row.state if row.state in START_CHAIN else "working"
            return {"session_id": child_id, "state": state, "instruction_id": instruction_id}
        return {
            "session_id": child_id,
            "state": "suspended" if row.state in SUSPENDED else "idle",
            "final_text": await self._text(key, row),
            "finished_at": time_text(newest.ended_at.timestamp()),
            "instruction_id": instruction_id,
        }

    async def _ended_view(self, row: LocalSession, instruction_id: str) -> dict[str, Any]:
        if row.state == "stopping" or not await self.c.settled(row):
            return {"session_id": row.id, "state": "stopping"}
        assert row.ended_at is not None
        finished = time_text(row.ended_at.timestamp())
        if row.state == "failed":
            return {"session_id": row.id, "state": "failed", "finished_at": finished}
        text = await self._text(row.id, row)
        return {
            "session_id": row.id,
            "state": "stopped",
            "final_text": text,
            "finished_at": finished,
            "instruction_id": instruction_id,
        }

    async def _text(self, key: str, row: LocalSession) -> str:
        text = self.texts.get(key)
        if text is None:
            async with store_session(self.env.db) as conn:
                text = await results.final_text(conn, self.env.storage_key, row)
            self.texts[key] = text
        return text

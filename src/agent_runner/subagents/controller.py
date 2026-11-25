"""Controlled subagents of one daemon (05-runner.md section 13, 03-api.md sections 8.1, 8.3 and 9, ADR-038, ADR-088).

`Subagents` (one per daemon, `controller_for(context)`) is the platform side of a child session's life:

- **Start** (`start_child`, the executor of `subagent.request`, `executors.py`): only after the hub accepted the
  operation and named the child (`child_session_id`). The child is started through the session start of RUN-04
  (`SessionService.schedule_start`) with `depth = parent.depth + 1`, the parent as `parent_session_id`, the parent's
  task, owner, VM and model access (or the requested, checked `model_id`). The request amender (`amend`,
  `session_request.py`) gives it the initial prompt `"Purpose: <purpose>\\n\\n<prompt>"` (prompt ID = operation ID)
  with the child's sender line (ADR-083 section 1, `prompts.speaker.for_child`: written by the parent session, which
  works on an instruction of the acting user of the `subagent.request`, `operation.actor_id`),
  the resource limits (`rules.child_limits`: `RuntimeMaxSec` = the runtime setting when set) and the harness line of
  05 section 13; a session with `depth` > 0 that this controller did not ask for is never started (the amender
  refuses it, the start fails). The operation's result is `{session_id}` once the start ran; a child whose start
  failed is `failed` like any session. The request is the child's first instruction (`turns.Turns.record_request`).
- **Full child** (ADR-088 section 1, `turns.py`): a child whose VM runs a guest that knows ADR-088
  (`guest_features.current_guest`) keeps running after a turn: the parent is pinged per turn, follow-up instructions
  (`subagent.instruct`) are handed over one per turn, an idle child is suspended after the device's idle time
  (`idle_seconds`) and resumed by its next instruction, and it ends with `subagent.stop`, its parent's end, the task's
  close or the runtime setting. The rest of this docstring describes what both kinds share and the older guest.
- **One shot** (`tick`, task `subagents`, woken by the state observer of `session_state.py`): a child on an older
  guest that became `idle` - its first turn ended - is ended after `GRACE_S` (the guest writes the last assistant
  message of the transcript after one quiet second): `stopping`, the guest's `stop`, `stopped`
  (`sessions.stop.stop_session`). When that turn ended with `StopFailure` (the guest's hook record), the child is
  `failed` with `agent_exited` instead.
- **Runtime** (`tick`): when the hub sets a runtime (`limits`, setting `subagent_max_runtime_seconds` > 0, ADR-088
  section 2), a child running that long since its creation is `failed` with `runtime_exceeded` and its
  guest session is stopped; the unit's own `RuntimeMaxSec` (started later) is the second line.
- **Result** (`view`, `get_subagent_result`, `query_tools.py`): only for a child of the calling session (same task):
  its state; once `stopped` and settled the final text (`results.final_text`) and the end time; `failed` with the end
  time. Settled = `SETTLE_S` after the end was first seen (transcript records that follow the end) and the VM's journal
  handled up to the guest's journal head of that moment, at the latest after `SETTLE_MAX_S`. Waits up to
  `wait_seconds` for an answer with `finished_at`. A full child answers about its newest instruction
  (`turns.Turns.view`); this is the answer of a child on an older guest.
- **Ping** (`tick`): when a child's end has settled, once per daemon, the `subagent.finished` notifier
  (`extensions.finished_notifier`, RUN-14) is called for a parent that has not ended, with a stable ping ID (a restart
  pings recently ended children again; the guest recognises the ID; of a full child the ID of its newest
  instruction, `turns.ping_id`); without one the end is logged.
- **Reads** (`spawn_query`, the `platform.query` journal handler): every forwarded read of a tool with a provider is
  answered by the registered `QueryTool` with the instructing human the journal handler captured when the read arrived
  (ADR-089 section 1), recorded as `read.session.<tool>` audit (naming the workgroup read and that human,
  `read_wg_id`/`triggered_by`) and - when the tool checked the arguments - as `platform.read` event, then answered to
  the guest - once per pending call, at most
  `MAX_READS_PER_SESSION` at a time per session. The event body must match `events/platform.read.json`, whose
  `arguments` are the tool's input schema, so arguments no tool vouched for are never sealed (decisions KOR-03, AUD-01
  A-18): a call the tool refused as `params_invalid` or that failed inside the tool gets only the audit (`failed`) and
  a fact line (`query_arguments_refused` / `query_failed`). A read of a tool without provider is left to the guest's
  timeout.
- **Reconcile** (`reconcile`, step 55): what RUN-04's step leaves to this area for `depth` > 0 - stop a child the hub
  no longer lists (unless it was created within `FRESH_S`: the desired state may predate its start) or marks
  `stopping`, or whose workgroup is no longer assigned (`unassigned`); fail a child left in a start state without a
  start task (`device_shutdown`) and stop its guest session; fail a child the hub lists as `requested` that this
  device never started and no pending request of its task can still start (`agent_start_failed`); end a child the hub
  still lists in any other state that the store does not know and no start or pending request of its task can still
  record (a wiped store: `SessionService.store_lost`, `device_shutdown` or `stopped`; the parent's ping follows from
  `tick`, KOR-09). The reconcile never starts a child.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink import cursor
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.orgs.db import session as store_session
from agent_runner.pings.send import stable_ping_id
from agent_runner.prompts import speaker
from agent_runner.sessions import records
from agent_runner.sessions.desired import Desired
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.events import fail, session_event
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.records import FINAL, LocalSession, time_text
from agent_runner.sessions.stop import stop_guest, stop_session
from agent_runner.subagents import limits as subagent_limits
from agent_runner.subagents import results, store
from agent_runner.subagents.extensions import (
    FinishedPing,
    Notify,
    QueryCall,
    QueryOutcome,
    QueryTool,
    failure,
    finished_notifier,
    query_tools,
)
from agent_runner.subagents.instructions import Instruction
from agent_runner.subagents.rules import child_limits, harness_line, initial_prompt
from agent_runner.subagents.turns import Turns
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow

ATTRIBUTE = "subagent_controller"
TICK_S = 1.0
GRACE_S = 2.0
SETTLE_S = 2.0
SETTLE_MAX_S = 30.0
FRESH_S = 60.0
MAX_READS_PER_SESSION = 4
PENDING_MEMORY = 4096
WAIT_POLL_S = 0.5
MAX_WAIT_S = 30
ANSWER_ATTEMPTS = 3
ANSWER_BACKOFF_S = 1.0
IDLE_S = 1800.0
"""The default of `AGENT_RUNNER_SESSION_IDLE_SECONDS` (`config.py`); the daemon passes its setting."""
INTERRUPTED = frozenset({"requested", "vm_assigning", "vm_ready", "sandbox_ready"})
ORPHAN_REASON = "agent_start_failed"
NO_LOCAL = frozenset({"requested", "stopped", "failed"})
"""Desired states for which a missing local child is expected (`sessions.service.NO_LOCAL`, compared by a test)."""
NOT_CHILD = "Only child sessions of this session can be read."
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ping_id(child_session_id: str) -> str:
    """A stable ping ID per child, so a repeated ping is recognised by the guest (`ping_id` is idempotent);
    UUIDv7-shaped as the guest contract requires (`pings.send.stable_ping_id`, RUN-14)."""
    return stable_ping_id("subagent.finished", child_session_id)


@dataclass(frozen=True)
class ChildSpec:
    """What the platform asked for: the operation, the parent and its VM, purpose and prompt."""

    operation_id: str
    parent_id: str
    task_id: str
    vm_id: str | None
    purpose: str
    prompt: str
    actor_id: str | None = None
    """The acting user of the `subagent.request` (ADR-070): the person the child's sender line names."""


class Subagents:
    def __init__(
        self,
        env: SessionEnv,
        service: Any,
        *,
        notify: Notify | None = None,
        tools: Mapping[str, QueryTool] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall: Callable[[], float] | None = None,
        idle_seconds: float = IDLE_S,
    ) -> None:
        self.env = env
        self.service = service
        self.idle_seconds = idle_seconds
        """An idle full child is suspended after this time (`AGENT_RUNNER_SESSION_IDLE_SECONDS`, as a main session)."""
        self.notify = notify
        self.tools = dict(tools or {})
        self.sleep = sleep
        self.wall = wall or env.wall
        self.specs: dict[str, ChildSpec] = {}
        self.idle_since: dict[str, float] = {}
        self.ending: dict[str, asyncio.Task[None]] = {}
        self.final_seen: dict[str, float] = {}
        self.texts: dict[str, str] = {}
        self.notified: set[str] = set()
        self.heads: dict[str, int] = {}
        self.answered: dict[str, None] = {}
        self.reading: Counter[str] = Counter()
        self.queries: set[asyncio.Task[None]] = set()
        self.trigger = asyncio.Event()
        self.turns = Turns(self)

    def line(self, text: str) -> None:
        self.env.log(f"runner.subagents {text}")

    def wake(self) -> None:
        self.trigger.set()
        self.turns.wake()

    # -- start ---------------------------------------------------------------------------------------------------

    async def start_child(self, ctx: OperationContext) -> dict[str, Any]:
        op, parent = ctx.operation, ctx.session
        child = op.child_session_id
        if child is None or child == parent.id or parent.owner_id is None or parent.model_access is None:
            raise OperationFailed("upstream_error", message="The hub named no valid child session.")
        if parent.state in FINAL or parent.state == "stopping":
            raise OperationFailed("not_found", message="The requesting session has ended.")
        too_deep = subagent_limits.depth_refusal(subagent_limits.of(self.env.desired), parent.depth)
        if too_deep is not None:
            raise OperationFailed("limit_exceeded", message=too_deep)
        async with store_session(self.env.db) as conn:
            known = await records.get(conn, child)
        if known is not None:
            if known.parent_id == parent.id and known.task_id == parent.task_id:
                return {"session_id": child}
            raise OperationFailed("upstream_error", message="The hub named another session as the child.")
        desired = Desired(
            session_id=child,
            task_id=parent.task_id,
            org_id=parent.org_id,
            wg_id=parent.wg_id,
            state="requested",
            parent_session_id=parent.id,
            depth=parent.depth + 1,
            owner_id=parent.owner_id,
            model_access_pref=parent.model_access,
            model_id_pref=ctx.params.get("model_id") or parent.model_id,
        )
        self.specs[child] = ChildSpec(
            operation_id=op.id,
            parent_id=parent.id,
            task_id=parent.task_id,
            vm_id=parent.vm_id,
            purpose=str(ctx.params["purpose"]),
            prompt=str(ctx.params["prompt"]),
            actor_id=op.actor_id,
        )
        await self.turns.record_request(op, child, parent.id)
        try:
            if not self.service.schedule_start(desired):
                raise OperationFailed("upstream_error", message="The child session is already being handled.")
            with contextlib.suppress(Exception):
                await asyncio.shield(self.service.tasks[child])
        finally:
            self.specs.pop(child, None)
        self.line(f"started child_session_id={child} parent_session_id={parent.id} operation_id={op.id}")
        self.wake()
        return {"session_id": child}

    async def amend(self, session: LocalSession, request: dict[str, Any]) -> None:
        """The `POST /v1/sessions` request of a child: initial prompt, limits, harness line (05 section 13)."""
        spec = self.specs.get(session.id)
        if spec is None or spec.parent_id != session.parent_id or spec.vm_id != session.vm_id:
            raise RunnerError("subagent_not_requested")
        text = initial_prompt(spec.purpose, spec.prompt)
        async with store_session(self.env.db) as conn:
            line = await speaker.for_child(
                conn,
                self.env.storage_key,
                org_id=session.org_id,
                parent_id=spec.parent_id,
                actor_id=spec.actor_id,
                text=text,
            )
        request["initial_prompt"] = InitialPrompt(prompt_id=spec.operation_id, text=text, speaker=line).body()
        request["limits"] = child_limits(subagent_limits.of(self.env.desired)).body()
        harness = request["harness"]
        line = harness_line(spec.parent_id, spec.task_id, current=await self.turns.current(session))
        harness["context_markdown"] = str(harness["context_markdown"]).rstrip("\n") + "\n" + line

    async def resume_request(self, session: LocalSession, request: dict[str, Any]) -> None:
        """The `POST /v1/sessions` request of a resumed full child (`turns.py`): its limits and harness line."""
        request["limits"] = child_limits(subagent_limits.of(self.env.desired)).body()
        harness = request["harness"]
        assert session.parent_id is not None
        line = harness_line(session.parent_id, session.task_id, current=True)
        harness["context_markdown"] = str(harness["context_markdown"]).rstrip("\n") + "\n" + line

    # -- life of the children ------------------------------------------------------------------------------------

    def observed(self, session: LocalSession) -> None:
        """A `session.state` record of a child was handled (journal transaction): note an idle start, wake the loop."""
        if session.state == "idle":
            self.idle_since.setdefault(session.id, self.env.clock())
        self.wake()

    async def settled(self, child: LocalSession) -> bool:
        """An ended child whose last records arrived (a failed child has no final text): `SETTLE_S` after its end was
        first seen, and the VM's journal handled up to the guest's journal head of that moment (a lagging reader); at
        the latest `SETTLE_MAX_S` after the end."""
        if child.state == "failed":
            return True
        return await self.settled_since(child.id, child.vm_id, self.final_seen, self.heads)

    async def settled_since(
        self, key: str, vm_id: str | None, seen_at: dict[str, float], heads: dict[str, int]
    ) -> bool:
        """The end named `key` (a child's, or a turn's, `turns.py`) settled: `seen_at` and `heads` keep when it was
        first seen and the guest's journal head of that moment."""
        now = self.env.clock()
        seen = seen_at.setdefault(key, now)
        if now - seen < SETTLE_S:
            return False
        if now - seen >= SETTLE_MAX_S or vm_id is None:
            return True
        head = heads.get(key)
        if head is None:
            head = heads[key] = await self._journal_head(vm_id)
        return await cursor.load_cursor(self.env.db, vm_id) >= head

    async def _journal_head(self, vm_id: str) -> int:
        """The guest journal's head of the VM, 0 when the VM does not run or cannot be reached (nothing to wait for)."""
        row = await vm_records.get(self.env.db, vm_id)
        if row is None or row.state != "running":
            return 0
        try:
            async with await self.env.client(row) as guest:
                page = await guest.journal(await cursor.load_cursor(self.env.db, vm_id), limit=1, wait=0)
        except (GuestError, RunnerError):
            return 0
        return page.head

    def _spawn_end(self, child_id: str, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        self.ending[child_id] = task
        task.add_done_callback(functools.partial(self._ended_task, child_id))

    def _ended_task(self, child_id: str, task: asyncio.Task[None]) -> None:
        self.ending.pop(child_id, None)
        if not task.cancelled() and task.exception() is not None:
            self.line(f"end_failed child_session_id={child_id} error={type(task.exception()).__name__}")

    async def tick(self) -> None:
        """One pass over the children (see the module docstring)."""
        async with store_session(self.env.db) as conn:
            found = await store.children(conn)
        now = self.env.clock()
        self._prune({child.id for child in found})
        settings = subagent_limits.of(self.env.desired)
        runtime = 0 if settings is None else settings.max_runtime_seconds
        survey = self.turns.surveyor([child for child in found if child.state not in FINAL])
        for child in found:
            if child.state != "idle":
                self.idle_since.pop(child.id, None)
            if child.state in FINAL:
                await self._ended(child)
            elif child.id in self.ending or child.state == "stopping":
                continue
            elif runtime and self.wall() - child.created_at.timestamp() >= runtime:
                self._spawn_end(child.id, self._runtime_exceeded(child))
            elif await self.turns.current(child):
                self._full(child, now, await survey())
            elif child.state == "idle" and now - self.idle_since.setdefault(child.id, now) >= GRACE_S:
                self._spawn_end(child.id, self._finish(child))
        await self.turns.ping_earlier(
            [child for child in found if child.state not in FINAL and await self.turns.current(child)]
        )

    def _full(self, child: LocalSession, now: float, survey: tuple[dict[str, Instruction], set[str]]) -> None:
        """A full child that has not ended (`turns.py`): stopped when its parent ended, else the work of an idle one;
        `survey`: the newest instructions and the children whose parent ended (`turns.Turns.survey`)."""
        newest, orphaned = survey
        if child.id in orphaned:
            self._spawn_end(child.id, self.turns.parent_ended(child))
        elif child.state == "idle":
            idle_for = now - self.idle_since.setdefault(child.id, now)
            if idle_for >= GRACE_S and self.turns.due(child, newest.get(child.id), idle_for):
                self._spawn_end(child.id, self.turns.work(child, newest.get(child.id)))

    def _prune(self, watched: set[str]) -> None:
        """Forget children that are no longer watched (ended more than `store.RECENT_S` ago)."""
        for kept in (self.idle_since, self.final_seen, self.texts, self.heads):
            for child_id in [key for key in kept if key not in watched]:
                del kept[child_id]
        self.notified &= watched
        self.turns.prune(watched)

    async def _runtime_exceeded(self, child: LocalSession) -> None:
        async with self.env.transaction() as conn:
            moved = await fail(conn, self.env.outbox, child.id, "runtime_exceeded", at=self.env.at())
        if moved is not None:
            self.line(f"runtime_exceeded child_session_id={child.id}")
            await stop_guest(self.env, child.id)

    async def _finish(self, child: LocalSession) -> None:
        """The first turn of the child ended: `stopped`, or `failed` (`agent_exited`) after a `StopFailure`."""
        async with store_session(self.env.db) as conn:
            ended_by = await results.turn_end(conn, self.env.storage_key, child)
        if ended_by == "StopFailure":
            async with self.env.transaction() as conn:
                moved = await fail(conn, self.env.outbox, child.id, "agent_exited", at=self.env.at())
            self.line(f"turn_failed child_session_id={child.id}")
            if moved is not None:
                await stop_guest(self.env, child.id)
            return
        stopped = await stop_session(self.env, child.id)
        self.line(f"completed child_session_id={child.id} stopped={int(stopped)}")

    async def _ended(self, child: LocalSession) -> None:
        """Ping once per daemon; a child that ended before a restart is pinged again with the same stable ping ID,
        which the guest recognises."""
        if child.id in self.notified or not await self.settled(child):
            return
        self.notified.add(child.id)
        await self._notify(child)

    async def _notify(self, child: LocalSession) -> None:
        self.line(f"finished child_session_id={child.id} parent_session_id={child.parent_id} state={child.state}")
        found = await self.turns.final_ping_id(child) if await self.turns.current(child) else None
        await self.notify_parent(child, found)

    async def notify_parent(self, child: LocalSession, found_id: str | None) -> None:
        """The `subagent.finished` ping for a parent that has not ended, with the ping ID `found_id` (None: the ID per
        child, `ping_id`)."""
        parent_id = child.parent_id
        if self.notify is None or parent_id is None:
            return
        async with store_session(self.env.db) as conn:
            parent = await records.get(conn, parent_id)
        if parent is None or parent.state in FINAL:
            return
        try:
            await self.notify(self.env.context, FinishedPing(parent_id, child.id, found_id or ping_id(child.id)))
        except Exception as exc:
            self.line(f"notify_failed child_session_id={child.id} error={type(exc).__name__}")

    async def loop(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                self.trigger.clear()
                try:
                    await self.tick()
                except Exception as exc:
                    self.line(f"tick_failed error={type(exc).__name__}")
                await self._wait(stopping)
        finally:
            pending = [*self.ending.values(), *self.queries]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _wait(self, stopping: asyncio.Event) -> None:
        waiters = {
            asyncio.ensure_future(self.trigger.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(self.sleep(TICK_S)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    # -- get_subagent_result -------------------------------------------------------------------------------------

    async def view(self, caller: LocalSession, child_id: str) -> dict[str, Any] | None:
        """The `get_subagent_result` result for a child of `caller`, None for anything else."""
        async with store_session(self.env.db) as conn:
            row = await records.get(conn, child_id)
            if row is None:
                requested = await store.requested_child(conn, caller.id, child_id)
                return {"session_id": child_id, "state": "requested"} if requested else None
            if row.parent_id != caller.id or row.task_id != caller.task_id:
                return None
        full = await self.turns.view(row) if await self.turns.current(row) else None
        return full if full is not None else await self._former_view(row)

    async def _former_view(self, row: LocalSession) -> dict[str, Any]:
        """The answer about a child on an older guest (or without instructions): its state, at its end the result."""
        child_id = row.id
        if row.state not in FINAL:
            return {"session_id": child_id, "state": row.state}
        if not await self.settled(row):
            return {"session_id": child_id, "state": "stopping"}
        assert row.ended_at is not None
        finished = time_text(row.ended_at.timestamp())
        if row.state == "failed":
            return {"session_id": child_id, "state": "failed", "finished_at": finished}
        text = self.texts.get(child_id)
        if text is None:
            async with store_session(self.env.db) as conn:
                text = await results.final_text(conn, self.env.storage_key, row)
            self.texts[child_id] = text
        return {"session_id": child_id, "state": "stopped", "final_text": text, "finished_at": finished}

    async def result(self, call: QueryCall) -> QueryOutcome:
        child_id, wait = call.arguments.get("session_id"), call.arguments.get("wait_seconds", 0)
        valid_wait = isinstance(wait, int) and not isinstance(wait, bool) and 0 <= wait <= MAX_WAIT_S
        known = set(call.arguments) <= {"session_id", "wait_seconds"}
        if not known or not isinstance(child_id, str) or not _UUID_RE.fullmatch(child_id) or not valid_wait:
            return failure("params_invalid", "The arguments do not match the tool.")
        deadline = self.env.clock() + wait
        while True:
            found = await self.view(call.session, child_id)
            if found is None:
                return failure("not_found", NOT_CHILD, target_id=child_id)
            remaining = deadline - self.env.clock()
            if "finished_at" in found or remaining <= 0:
                return QueryOutcome(result=found, result_count=1, target_id=child_id)
            await self.sleep(min(WAIT_POLL_S, remaining))

    # -- forwarded reads -----------------------------------------------------------------------------------------

    def spawn_query(
        self,
        vm: VmRow,
        session: LocalSession,
        pending_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        actor_id: str | None = None,
    ) -> asyncio.Task[None] | None:
        """Answer a forwarded read instructed by `actor_id` in the background; None for a pending call already taken (a
        journal page read again) or a tool without provider (left to the guest's timeout, as before any provider
        existed)."""
        if pending_id in self.answered:
            return None
        if tool not in self.tools:
            self.line(f"query_unrouted tool={tool} session_id={session.id}")
            return None
        self.answered[pending_id] = None
        while len(self.answered) > PENDING_MEMORY:
            del self.answered[next(iter(self.answered))]
        task = asyncio.ensure_future(self._query(vm, session, pending_id, tool, arguments, actor_id=actor_id))
        self.queries.add(task)
        task.add_done_callback(self.queries.discard)
        return task

    async def _query(
        self,
        vm: VmRow,
        session: LocalSession,
        pending_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        actor_id: str | None,
    ) -> None:
        self.reading[session.id] += 1
        try:
            if self.reading[session.id] > MAX_READS_PER_SESSION:
                self.line(f"query_refused tool={tool_name} session_id={session.id}")
                outcome = failure("unavailable", "Too many reads of this session run at once. Try again later.")
            else:
                checked: Mapping[str, Any] | None = None
                try:
                    call = QueryCall(self.env.context, session, tool_name, arguments, actor_id)
                    outcome = await self.tools[tool_name](call)
                except Exception as exc:
                    self.line(f"query_failed tool={tool_name} error={type(exc).__name__}")
                    outcome = failure("unavailable", "The platform could not answer the read.")
                else:
                    if outcome.error == "params_invalid":
                        self.line(f"query_arguments_refused tool={tool_name} session_id={session.id}")
                    else:
                        checked = arguments
                await self._record(session, tool_name, checked, outcome, actor_id)
            await self._answer(vm, pending_id, outcome.answer())
        except Exception as exc:
            self.line(f"query_unanswered tool={tool_name} error={type(exc).__name__}")
        finally:
            self.reading[session.id] -= 1
            if self.reading[session.id] <= 0:
                del self.reading[session.id]

    async def _record(
        self,
        session: LocalSession,
        tool: str,
        arguments: Mapping[str, Any] | None,
        outcome: QueryOutcome,
        actor_id: str | None,
    ) -> None:
        """`read.session.<tool>` audit and - for `arguments` the tool checked, else none - `platform.read` event of an
        answered read (local storage first).

        The event's `ref` is the read target (04-crypto.md section 4.3: the hub refuses an empty one), the audited
        target ID - a UUID, as the header contract requires: the tools name checked IDs or scope IDs, else the session.
        The audit detail also names the workgroup read (`read_wg_id`, null on the organization level) and the
        instructing human (`triggered_by`, null when unknown; ADR-089 section 4).
        """
        at = self.env.at()
        target = outcome.target_id or session.id
        audit = {
            "action": f"read.session.{tool}",
            "result": outcome.audit_result,
            "task_id": session.task_id,
            "session_id": session.id,
            "target_kind": outcome.target_kind,
            "target_id": target,
            "detail": {
                "result_count": outcome.result_count,
                "read_wg_id": outcome.read_wg_id,
                "triggered_by": actor_id,
            },
        }
        async with self.env.transaction() as conn:
            if arguments is not None:
                read = {"tool": tool, "arguments": dict(arguments), "result_count": outcome.result_count}
                await session_event(conn, self.env.outbox, session, kind="platform.read", body=read, ts=at, ref=target)
            await self.env.outbox.enqueue(conn, "audit", audit)

    async def _answer(self, vm: VmRow, pending_id: str, answer: Mapping[str, Any]) -> None:
        for attempt in range(ANSWER_ATTEMPTS):
            try:
                async with await self.env.client(vm) as client:
                    await client.answer_pending(pending_id, answer)
                return
            except GuestRefused as exc:
                self.line(f"answer_refused pending_id={pending_id} error={exc.error}")
                return
            except (GuestError, RunnerError) as exc:
                self.line(f"answer_failed pending_id={pending_id} attempt={attempt + 1} error={exc.code}")
                if attempt + 1 < ANSWER_ATTEMPTS:
                    await self.sleep(ANSWER_BACKOFF_S * 2**attempt)

    # -- reconcile -----------------------------------------------------------------------------------------------

    async def reconcile(self, desired: Iterable[Desired], book: Any) -> None:
        assigned = set(book.assignments())
        wanted = {item.session_id: item for item in desired if item.depth > 0}
        async with store_session(self.env.db) as conn:
            local = [session for session in await records.listed(conn) if session.depth > 0]
            known = await records.known_ids(conn, list(wanted))
            busy = await store.busy_tasks(conn)
        local_ids = {session.id for session in local}
        for item in wanted.values():
            if (item.org_id, item.wg_id) not in assigned:
                continue
            if item.state == "requested" and item.session_id not in known:
                if item.task_id not in busy and item.session_id not in self.specs:
                    await self._orphan(item)
            elif item.state == "stopping" and item.session_id in local_ids:
                self.service.schedule_stop(item.session_id)
            elif (
                item.state not in NO_LOCAL
                and item.session_id not in known
                and item.session_id not in self.specs
                and item.task_id not in busy
            ):
                # the store was wiped: recorded and ended like a main session (NA-06), the parent is pinged by `tick`
                await self.service.store_lost(item)
        for session in local:
            if session.id in self.service.tasks or session.id in self.ending:
                continue
            if session.id not in wanted:
                # a child started after the hub's answer was fetched is not listed yet (a stale desired state)
                if self.wall() - session.created_at.timestamp() >= FRESH_S:
                    self.service.schedule_stop(session.id)
            elif (session.org_id, session.wg_id) not in assigned:
                self.service.schedule_stop(session.id, "unassigned")
            elif session.state in INTERRUPTED:
                await self._interrupted(session)

    async def _orphan(self, item: Desired) -> None:
        """A child the hub created that this device never started (its request ended before the start)."""
        async with self.env.transaction() as conn:
            created = await records.insert(
                conn,
                session_id=item.session_id,
                task_id=item.task_id,
                org_id=item.org_id,
                wg_id=item.wg_id,
                parent_id=item.parent_session_id,
                depth=item.depth,
                owner_id=item.owner_id,
                created_at=datetime.fromtimestamp(self.wall(), UTC),
            )
            if created:
                await fail(conn, self.env.outbox, item.session_id, ORPHAN_REASON, at=self.env.at())
        self.line(f"orphan child_session_id={item.session_id} recorded={int(created)}")

    async def _interrupted(self, session: LocalSession) -> None:
        async with self.env.transaction() as conn:
            await fail(conn, self.env.outbox, session.id, "device_shutdown", at=self.env.at())
        self.line(f"start_interrupted child_session_id={session.id}")
        if session.vm_id is not None:
            self._spawn_end(session.id, self._stop_guest(session.id))

    async def _stop_guest(self, session_id: str) -> None:
        await stop_guest(self.env, session_id)


# -- production wiring -------------------------------------------------------------------------------------------


def existing(context: Any) -> Subagents | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, Subagents) else None


def install(context: Any, controller: Subagents) -> Subagents:
    setattr(context, ATTRIBUTE, controller)
    return controller


def controller_for(context: Any) -> Subagents | None:
    """The controller of this daemon over the session area's collaborators; None while the device is not enrolled."""
    found = existing(context)
    if found is not None:
        return found
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points

    service = service_for(context)
    if service is None:
        return None
    package = context.host.package
    built = Subagents(
        service.env,
        service,
        notify=finished_notifier(package),
        tools=query_tools(package),
        sleep=context.host.sleep,
        idle_seconds=float(context.config.session_idle_seconds),
    )
    return install(context, built)

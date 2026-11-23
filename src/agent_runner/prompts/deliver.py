"""Prompt delivery to the sessions of this device (05-runner.md sections 2.2 step 6, 6.2, 6.3; 03-api.md sections
4.6.3, 6.1 and 7.1; 02-data-model.md section 4 `prompt_delivery`; ADR-016, ADR-025).

`Deliverer` (one per daemon, `deliverer_for(context)`) owns every prompt from the hub's routing to the guest:

- **Routing** (`apply_desired`, `hinted`, `route_one`): the hub names the session of each prompt in `pending_prompts`
  of `GET /v1/device/state` (reconcile step 60) and in the `prompt` hint. Each routing is checked against the stored,
  verified prompt event (`verify.check_route`) and recorded as a `prompt_delivery` row (`queue.route`). A prompt the
  device has not pulled yet triggers one inbox pull; when the scope's cursor passed its `scope_seq` and it is still
  missing, the inbox refused it, and so is the routing (`prompt_unverified`). The hub's `delivered_prompt_event_ids`
  are adopted first (`queue.adopt_delivered`): after a store wipe they are never delivered again.
- **Order** (`advance`): per session one lock and strictly the `scope_seq` order of the open rows; the next prompt
  waits until the previous one is accepted or ended (`refused`/`failed`). A head prompt waits up to `HOLD_S` for an
  earlier stored prompt of its task that is not routed yet (a lost hint), so it is not overtaken. A routed prompt
  older than one the task already handed to a guest is stale and refused (`prompt_stale`); a prompt of a final row
  is never delivered again; a write never reopens a final row (the journal may end a row at any time).
- **Hand-over by session state**:
  * `idle`, `prompt_delivering` (a start without initial prompt): write-ahead `registered`, decrypt into memory,
    `POST /v1/sessions/{id}/prompts` (hash registered first, the guest then types it), `typed`;
  * `suspended`, `resuming`: `resume.Resumer` (`--resume`), then as above;
  * `working` and the start states before `prompt_delivering`: wait (woken by the next `session.state`);
  * `stopping`, `stopped`, `failed`: nothing; the rows move with the hub's next routing to a new session.

  The initial prompt of a start goes on the agent's command line (`first.py`, channel `start`); it is never sent
  again while the start runs.
- **Results** (`session_state.py`): `prompt_accepted` -> `accepted`; `prompt_refused` `delivery_failed` -> `failed`
  plus a `session.status` event with the event ID (a session that never accepted its first prompt fails
  `delivery_failed`).
- **Retries without a second delivery**: a guest that cannot be reached leaves the row `registered`; it is sent
  again with the same `prompt_id` after a backoff (1 s doubling to 30 s per session). The guest's registry is keyed by
  `prompt_id` and accepts each registration once, its queue takes a `prompt_id` once, and an accepted one answers
  `state_conflict`; so a repeat never types or accepts a prompt twice. A row `typed` whose session is ready again
  without a result after `TYPED_RETRY_S` (the guest's in-memory queue was lost with a restart of its control
  process) is sent again the same way.
- **Idle -> suspend** (`suspend_idle`): a main session `idle` for `SESSION_IDLE_SECONDS` without open prompts gets the
  guest control action `suspend` (unit stopped, directories and transcript kept) and becomes `suspended`. Idle times
  are kept in memory; after a daemon restart a session counts as idle from the first time it is seen.
- **Controls** (`control`, from the hint `session.control`): `interrupt` -> guest `interrupt` (`Escape`); `stop` ->
  the session area's stop. Nothing else reaches a pane (ADR-016).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.inbox.pull import load_cursors
from agent_runner.orgs import audit
from agent_runner.prompts import queue
from agent_runner.prompts.resume import RESUMED, Resumer, vm_running
from agent_runner.prompts.verify import STALE, PromptRefused, check_route, open_text
from agent_runner.reconcile import wire
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.stop import stop_guest

ATTRIBUTE = "prompt_deliverer"
PERIOD_S = 5.0
TYPED_RETRY_S = 150.0
"""Longer than the guest's wait for a ready TUI (120 s) plus its acceptance window (10 s)."""
HOLD_S = 60.0
"""How long a prompt waits for an earlier prompt of its task that is stored but not routed yet (a lost hint; the next
reconcile, every 30 s, routes it) before it is delivered anyway."""
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
REFRESH_TIMEOUT_S = 2.0
UNVERIFIED = "prompt_unverified"
GUEST_REFUSED = "prompt_guest_refused"
SENDABLE = frozenset({"idle", "prompt_delivering"})
RESUMABLE = frozenset({"suspended", "resuming"})
ENDED = frozenset({"stopping", "stopped", "failed"})
CONTROL_ACTIONS = frozenset({"interrupt", "stop"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Routing:
    """One entry of `pending_prompts` (03-api.md section 6.1)."""

    task_id: str
    session_id: str
    scope_id: str
    scope_seq: int
    event_id: str


def parse_pending(desired: Mapping[str, Any]) -> list[Routing]:
    found = []
    for item in wire.array(desired.get("pending_prompts", []), "pending_prompts"):
        data = wire.obj(item, "pending_prompts")
        if set(data) != {"task_id", "session_id", "scope_id", "scope_seq", "event_id"}:
            raise wire.ResponseInvalid("pending_prompts")
        found.append(
            Routing(
                task_id=wire.uuid_text(data["task_id"], "pending_prompts"),
                session_id=wire.uuid_text(data["session_id"], "pending_prompts"),
                scope_id=wire.uuid_text(data["scope_id"], "pending_prompts"),
                scope_seq=wire.seq(data["scope_seq"], "pending_prompts", 1),
                event_id=wire.uuid_text(data["event_id"], "pending_prompts"),
            )
        )
    return sorted(found, key=lambda item: (item.session_id, item.scope_seq))


def parse_delivered(desired: Mapping[str, Any]) -> dict[str, list[str]]:
    data = wire.obj(desired.get("delivered_prompt_event_ids", {}), "delivered_prompt_event_ids")
    return {
        wire.uuid_text(session_id, "delivered_prompt_event_ids"): [
            wire.uuid_text(event_id, "delivered_prompt_event_ids")
            for event_id in wire.array(event_ids, "delivered_prompt_event_ids")
        ]
        for session_id, event_ids in data.items()
    }


class Deliverer:
    def __init__(self, env: SessionEnv, *, idle_seconds: float, stop: Callable[[str], object] | None = None) -> None:
        self.env = env
        self.idle_seconds = idle_seconds
        self.stop = stop
        """Schedules the stop of a session (the session area's `schedule_stop`) for the control hint `stop`."""
        self.resumer = Resumer(env)
        self.trigger = asyncio.Event()
        self.locks: dict[str, asyncio.Lock] = {}
        self.idle_since: dict[str, float] = {}
        self.not_before: dict[str, float] = {}
        self.delays: dict[str, float] = {}
        self.stops: set[str] = set()
        self.refused: set[tuple[str, str]] = set()

    # -- small helpers -------------------------------------------------------------------------------------------

    def wake(self) -> None:
        self.trigger.set()

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    def line(self, text: str) -> None:
        self.env.log(f"runner.prompts {text}")

    def _backoff(self, session_id: str) -> None:
        delay = self.delays.get(session_id, BACKOFF_MIN_S / 2) * 2
        delay = min(max(delay, BACKOFF_MIN_S), BACKOFF_MAX_S)
        self.delays[session_id] = delay
        self.not_before[session_id] = self.env.clock() + delay

    def _settled(self, session_id: str) -> None:
        self.delays.pop(session_id, None)
        self.not_before.pop(session_id, None)

    async def reject(self, conn: AsyncConnection[Any], code: str, event_id: str, session_id: str) -> None:
        """Audit a refused prompt once per (event, session) and daemon."""
        if (event_id, session_id) in self.refused:
            return
        self.refused.add((event_id, session_id))
        await audit.reject(
            self.env.context,
            cast(Any, self.env.outbox),
            conn,
            source="event",
            code=code,
            target_kind="event",
            target_id=event_id,
        )

    # -- observations (journal) ----------------------------------------------------------------------------------

    def observe(self, session_id: str, state: str) -> None:
        """A session's state from the journal: idle times and a wake-up for the next prompt."""
        if state == "idle":
            self.idle_since[session_id] = self.env.clock()
        else:
            self.idle_since.pop(session_id, None)
        self.wake()

    def stop_later(self, session_id: str) -> None:
        """The guest session of a session that failed in a journal transaction is stopped by the loop."""
        self.stops.add(session_id)
        self.wake()

    # -- routing -------------------------------------------------------------------------------------------------

    async def route_one(self, conn: AsyncConnection[Any], routing: Routing) -> str:
        """Record one routing: the row state, `missing` (event not stored), `unknown_session` or `refused`.

        A prompt whose delivery is final needs no stored event: after the retention removed it (decisions KOR-03) a
        routing of it again answers the final state, never `missing`."""
        prompt = await queue.event(conn, routing.event_id)
        if prompt is None:
            known = await queue.delivery(conn, routing.event_id)
            return known.state if known is not None and known.state in queue.FINAL else "missing"
        session = await records.get(conn, routing.session_id)
        if session is None:
            # not started on this device (yet): the start asks for its first prompt itself
            return "unknown_session"
        try:
            check_route(
                prompt, session, task_id=routing.task_id, scope_id=routing.scope_id, scope_seq=routing.scope_seq
            )
        except PromptRefused as refused:
            await self.reject(conn, refused.code, routing.event_id, routing.session_id)
            return "refused"
        return await queue.route(conn, prompt, routing.session_id)

    async def _route_all(self, routings: list[Routing]) -> list[Routing]:
        missing = []
        async with self.env.transaction() as conn:
            for routing in routings:
                if await self.route_one(conn, routing) == "missing":
                    missing.append(routing)
        return missing

    async def refresh(self) -> None:
        """One inbox pull, bounded (a prompt routed before it was pulled)."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.env.refresh_profiles(), REFRESH_TIMEOUT_S)

    async def _unverified(self, missing: list[Routing]) -> None:
        """Routings of events the inbox passed without storing (refused or unreadable): refused, audited."""
        async with self.env.transaction() as conn:
            cursors = await load_cursors(conn, sorted({routing.scope_id for routing in missing}))
            for routing in missing:
                if cursors[routing.scope_id] >= routing.scope_seq:
                    await self.reject(conn, UNVERIFIED, routing.event_id, routing.session_id)

    async def route(self, routings: list[Routing]) -> None:
        missing = await self._route_all(routings)
        if missing:
            await self.refresh()
            missing = await self._route_all(missing)
        if missing:
            await self._unverified(missing)

    async def apply_desired(self, desired: Mapping[str, Any]) -> None:
        """Reconcile step 60: the delivered list, then the routings of `pending_prompts`; wakes the delivery."""
        delivered = parse_delivered(desired)
        routings = parse_pending(desired)
        async with self.env.transaction() as conn:
            adopted = await queue.adopt_delivered(conn, delivered)
        if adopted:
            self.line(f"delivered_adopted count={adopted}")
        await self.route(routings)
        self.wake()

    async def hinted(self, session_id: str, scope_id: str, scope_seq: int) -> None:
        """The `prompt` hint: pull, then route the event at `(scope_id, scope_seq)` to the session."""
        await self.refresh()
        async with self.env.transaction() as conn:
            prompt = await queue.event_at(conn, scope_id, scope_seq)
        if prompt is None:
            self.line(f"hint_event_missing scope={scope_id} seq={scope_seq}")
            return
        await self.route([Routing(prompt.task_id, session_id, scope_id, scope_seq, prompt.event_id)])
        self.wake()

    # -- first prompt of a start ---------------------------------------------------------------------------------

    async def first_prompt(self, session: LocalSession) -> InitialPrompt | None:
        """The head prompt routed to a new main session, handed over with the start (channel `start`)."""
        if session.depth != 0:
            return None
        desired = self.env.desired
        if desired is not None:
            with contextlib.suppress(ValueError):
                await self.apply_desired(
                    {
                        "pending_prompts": [
                            routing.__dict__ for routing in parse_pending(desired) if routing.session_id == session.id
                        ],
                        "delivered_prompt_event_ids": desired.get("delivered_prompt_event_ids", {}),
                    }
                )
        async with self.lock(session.id):
            while True:
                async with self.env.transaction() as conn:
                    rows = await queue.open_rows(conn, session.id)
                    head = rows[0] if rows else None
                    if head is None or head.state != "pending":
                        return None
                    try:
                        text = await self._checked_text(conn, session, head)
                    except PromptRefused:
                        continue
                    await queue.set_state(
                        conn, head.event_id, "registered", expected=("pending",), channel="start", attempt=True
                    )
                self.line(f"first_prompt session_id={session.id} event_id={head.event_id}")
                return InitialPrompt(prompt_id=head.event_id, text=text)

    async def _checked_text(self, conn: AsyncConnection[Any], session: LocalSession, head: queue.Delivery) -> str:
        """Stale check and the opened text of a pending head; a refusal is recorded (`refused`, audit)."""
        try:
            if head.task_id is None or head.scope_seq is None:
                raise PromptRefused(STALE)
            if await queue.handed_after(conn, head.task_id, head.scope_seq):
                raise PromptRefused(STALE)
            prompt = await queue.event(conn, head.event_id)
            assert prompt is not None
            return await open_text(conn, self.env.storage_key, prompt)
        except PromptRefused as refused:
            await queue.set_state(conn, head.event_id, "refused", expected=queue.OPEN)
            await self.reject(conn, refused.code, head.event_id, session.id)
            self.line(f"refused session_id={session.id} event_id={head.event_id} code={refused.code}")
            raise

    # -- delivery ------------------------------------------------------------------------------------------------

    async def advance(self, session_id: str) -> None:
        """Hand the session's next prompt to the guest when its state allows it (see the module docstring)."""
        if self.env.clock() < self.not_before.get(session_id, 0.0):
            return
        async with self.lock(session_id):
            while True:
                async with self.env.transaction() as conn:
                    session = await records.get(conn, session_id)
                    rows = await queue.open_rows(conn, session_id)
                if session is None or session.depth != 0 or not rows or session.state in ENDED:
                    return
                if not await self._head(session, rows[0]):
                    return

    async def _head(self, session: LocalSession, head: queue.Delivery) -> bool:
        """Act on the head row; True when the next row may be looked at at once (this one was refused)."""
        state = session.state
        if head.state != "pending":
            in_start = state in records.START_CHAIN
            ready = state in SENDABLE and not (in_start and head.channel == "start")
            due = (head.state == "registered" and head.channel == "prompt") or head.age >= TYPED_RETRY_S
            if ready and due:
                await self._send(session, head)
            elif state in RESUMABLE and head.channel == "prompt" and head.state == "registered":
                await self._resume_then_send(session, head)
            return False
        if state in SENDABLE or state in RESUMABLE:
            async with self.env.transaction() as conn:
                held = (
                    head.task_id is not None
                    and head.scope_seq is not None
                    and head.age < HOLD_S
                    and await queue.earlier_unrouted(conn, head.task_id, head.scope_seq)
                )
            if held:
                self.line(f"held session_id={session.id} event_id={head.event_id}")
                return False
            async with self.env.transaction() as conn:
                try:
                    text = await self._checked_text(conn, session, head)
                except PromptRefused:
                    return True
            if state in SENDABLE:
                await self._send(session, head, text)
            else:
                await self._resume_then_send(session, head, text)
        return False

    async def _resume_then_send(self, session: LocalSession, head: queue.Delivery, text: str | None = None) -> None:
        outcome = await self.resumer.resume(session)
        if outcome != RESUMED:
            # `retry`: backoff; `ended`: stopped meanwhile; a failure reason: the session failed
            if outcome == "retry":
                self._backoff(session.id)
            return
        async with self.env.transaction() as conn:
            resumed = await records.get(conn, session.id)
        assert resumed is not None
        await self._send(resumed, head, text)

    async def _send(self, session: LocalSession, head: queue.Delivery, text: str | None = None) -> None:
        """Write-ahead `registered`, `POST /v1/sessions/{id}/prompts`, then `typed` (see the module docstring)."""
        env = self.env
        async with env.transaction() as conn:
            if text is None:
                prompt = await queue.event(conn, head.event_id)
                try:
                    if prompt is None:
                        raise PromptRefused(UNVERIFIED)
                    text = await open_text(conn, env.storage_key, prompt)
                except PromptRefused as refused:
                    await queue.set_state(conn, head.event_id, "refused", expected=queue.OPEN)
                    await self.reject(conn, refused.code, head.event_id, session.id)
                    return
            # the journal may have ended the row (accepted, failed) since it was read: never reopen a final row
            handed = await queue.set_state(
                conn, head.event_id, "registered", expected=queue.OPEN, channel="prompt", attempt=True
            )
        if not handed:
            return
        row = await vm_running(env, session)
        if row is None:
            self.line(f"vm_not_running session_id={session.id}")
            self._backoff(session.id)
            return
        try:
            async with await env.client(row) as guest:
                await guest.register_prompt(session.id, head.event_id, text)
        except GuestRefused as exc:
            await self._guest_refused(session, head, exc.error)
            return
        except (GuestError, RunnerError) as exc:
            self.line(f"send_retry session_id={session.id} event_id={head.event_id} code={exc.code}")
            self._backoff(session.id)
            return
        async with env.transaction() as conn:
            await queue.set_state(conn, head.event_id, "typed", expected=("registered",))
            await records.update(conn, session.id, last_prompt_at=datetime.fromtimestamp(env.wall(), UTC))
        self._settled(session.id)
        self.line(f"sent session_id={session.id} event_id={head.event_id}")

    async def _guest_refused(self, session: LocalSession, head: queue.Delivery, error: str | None) -> None:
        """The guest refused the registration: `state_conflict` (accepted before, or its session ended - the journal
        tells) leaves the prompt with the guest; `not_found` makes it routable again; anything else refuses it."""
        env = self.env
        async with env.transaction() as conn:
            if error == "state_conflict":
                await queue.set_state(conn, head.event_id, "typed", expected=("registered",))
            elif error == "not_found":
                await queue.set_state(conn, head.event_id, "pending", expected=("registered",))
                self._backoff(session.id)
            else:
                await queue.set_state(conn, head.event_id, "refused", expected=("registered",))
                await self.reject(conn, GUEST_REFUSED, head.event_id, session.id)
        self.line(f"guest_refused session_id={session.id} event_id={head.event_id} error={error}")

    # -- idle -> suspend -----------------------------------------------------------------------------------------

    async def suspend_idle(self) -> None:
        env = self.env
        now = env.clock()
        async with env.transaction() as conn:
            local = [session for session in await records.listed(conn) if session.depth == 0]
            busy = set(await queue.sessions_with_open_rows(conn))
        idle = {session.id: session for session in local if session.state == "idle"}
        for session_id in list(self.idle_since):
            if session_id not in idle:
                del self.idle_since[session_id]
        for session_id, session in idle.items():
            since = self.idle_since.setdefault(session_id, now)
            if now - since < self.idle_seconds or session_id in busy:
                continue
            await self._suspend(session)

    async def _suspend(self, session: LocalSession) -> None:
        env = self.env
        async with self.lock(session.id):
            async with env.transaction() as conn:
                current = await records.get(conn, session.id)
            if current is None or current.state != "idle":
                return
            row = await vm_running(env, current)
            if row is None:
                return
            try:
                async with await env.client(row) as guest:
                    await guest.control(session.id, "suspend")
            except GuestRefused as exc:
                # the guest's session is not idle (a turn started meanwhile): count again from now
                self.idle_since[session.id] = env.clock()
                self.line(f"suspend_refused session_id={session.id} error={exc.error}")
                return
            except (GuestError, RunnerError) as exc:
                self.line(f"suspend_retry session_id={session.id} code={exc.code}")
                return
            async with env.transaction() as conn:
                await records.advance(conn, env.outbox, session.id, "suspended", at=env.at())
            self.idle_since.pop(session.id, None)
            self.line(f"suspended session_id={session.id}")

    # -- controls ------------------------------------------------------------------------------------------------

    async def control(self, session_id: str, action: str) -> bool:
        """The hint `session.control`: True when the action was carried out (or scheduled, for `stop`)."""
        env = self.env
        if action not in CONTROL_ACTIONS or not _UUID_RE.fullmatch(session_id):
            self.line("control_invalid")
            return False
        async with env.transaction() as conn:
            session = await records.get(conn, session_id)
        row = None if session is None or session.state in records.FINAL else await vm_running(env, session)
        if action == "stop" and session is not None and session.state not in records.FINAL and self.stop is not None:
            self.stop(session_id)
            self.line(f"control session_id={session_id} action=stop")
            return True
        if action == "stop" or row is None:
            self.line(f"control_ignored session_id={session_id} action={action}")
            return False
        try:
            async with await env.client(row) as guest:
                await guest.control(session_id, "interrupt")
        except (GuestError, RunnerError) as exc:
            self.line(f"control_failed session_id={session_id} action={action} code={exc.code}")
            return False
        self.line(f"control session_id={session_id} action={action}")
        return True

    # -- loop ----------------------------------------------------------------------------------------------------

    async def run_once(self) -> None:
        async with self.env.transaction() as conn:
            sessions = await queue.sessions_with_open_rows(conn)
        await asyncio.gather(*(self.advance(session_id) for session_id in sessions))
        for session_id in sorted(self.stops):
            self.stops.discard(session_id)
            await stop_guest(self.env, session_id)
        await self.suspend_idle()

    async def wait(self, seconds: float, stopping: asyncio.Event, sleep: Callable[[float], Any]) -> None:
        waiters = {
            asyncio.ensure_future(self.trigger.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(sleep(seconds)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def loop(self, stopping: asyncio.Event, sleep: Callable[[float], Any]) -> None:
        """Deliver on every wake-up and every `PERIOD_S` until `stopping`; a failed round is logged and repeated."""
        while not stopping.is_set():
            self.trigger.clear()
            try:
                await self.run_once()
            except Exception as exc:
                self.line(f"round_failed error={type(exc).__name__}")
            await self.wait(PERIOD_S, stopping, sleep)


def existing(context: Any) -> Deliverer | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, Deliverer) else None


def install(context: Any, deliverer: Deliverer) -> Deliverer:
    setattr(context, ATTRIBUTE, deliverer)
    return deliverer


def deliverer_for(context: Any) -> Deliverer | None:
    """The deliverer of this daemon over the session area's collaborators; None while the device is not enrolled."""
    found = existing(context)
    if found is not None:
        return found
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points

    service = service_for(context)
    if service is None:
        return None
    built = Deliverer(service.env, idle_seconds=context.config.session_idle_seconds, stop=service.schedule_stop)
    return install(context, built)

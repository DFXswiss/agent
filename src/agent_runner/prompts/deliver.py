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
  is never delivered again - except the interrupted instruction of a lost session, routed to its successor named in
  the device state's `failovers` (ADR-087 section 3.4, `queue.route`) -; a write never reopens a final row (the
  journal may end a row at any time).
- **Queue of the hub** (ADR-083 section 3, `prompt_queue` of the device state, `hub_queue.py`): `via_agent` (also
  from the `prompt` hint) is kept with the routed rows in the routing's transaction (`queue.mark_via_agent`); every
  event of `withdrawn` is withdrawn (`withdraw`): a row still `pending` becomes `withdrawn`, a prompt without a row
  is remembered so that its routing ends `withdrawn` at once, a row `registered` or later is delivered. Each
  withdrawal is reported once as outbox audit `session.prompt_withdrawn` (target the event), in the one ordered
  outbox before the acceptance of any later prompt.
- **Sender line** (ADR-083 section 1, `speaker.py`): every prompt handed to a guest carries the line of its verified
  author (`speaker`, also the first prompt of a start). An older guest refuses the unknown field with
  `400 request_invalid` (the line passed the contract, so nothing else can be meant): the same registration is sent
  once more without it, logged and reported once per VM (`speaker.register`).
- **Hand-over by session state**:
  * `idle`, `prompt_delivering` (a start without initial prompt): write-ahead `registered`, decrypt into memory,
    `POST /v1/sessions/{id}/prompts` (hash registered first, the guest then types it), `typed`;
  * `suspended`, `resuming`: `resume.Resumer` (`--resume`), then as above;
  * `working` and the start states before `prompt_delivering`: wait (woken by the next `session.state`);
  * `stopping`, `stopped`, `failed`: nothing; the rows move with the hub's next routing to a new session.

  The initial prompt of a start goes on the agent's command line (`first.py`, channel `start`); it is never sent
  again while the start runs. A prompt with attachments (ADR-102) never goes on the command line: the session starts
  without it and the delivery hands it over like a follow-up.
- **Attachments** (`attachments.py`): before a prompt that names files is registered, its files are downloaded from
  the hub, checked against the verified references, decrypted and imported into `attachments/<event id>/` of the
  workspace; the typed text names their paths. A hub that cannot be reached backs off like a guest; a file that is
  missing or does not verify refuses the prompt (`prompt_unreadable`).
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
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.billing import pause
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.inbox.pull import load_cursors
from agent_runner.orchestrator import store as orchestrator_store
from agent_runner.orgs import audit
from agent_runner.orgs.keys import load_scope_key
from agent_runner.prompts import attachments, hub_queue, queue, speaker
from agent_runner.prompts.attachments import AttachmentFetcher
from agent_runner.prompts.resume import RESUMED, Resumer, vm_running
from agent_runner.prompts.verify import STALE, Opened, PromptRefused, check_route, open_prompt
from agent_runner.reconcile import wire
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.failover import failover_of
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
    def __init__(
        self,
        env: SessionEnv,
        *,
        idle_seconds: float,
        stop: Callable[[str], object] | None = None,
        attachments: AttachmentFetcher | None = None,
    ) -> None:
        self.env = env
        self.idle_seconds = idle_seconds
        self.stop = stop
        """Schedules the stop of a session (the session area's `schedule_stop`) for the control hint `stop`."""
        self.attachments = attachments
        """Fetches the files a prompt names (ADR-102); None (a test without a hub): such a prompt waits (backoff)."""
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
        failover = failover_of(self.env.desired, routing.session_id)
        return await queue.route(
            conn, prompt, routing.session_id, failover_from=None if failover is None else failover.from_session
        )

    async def _route_all(self, routings: list[Routing], via_agent: frozenset[str]) -> list[Routing]:
        missing = []
        async with self.env.transaction() as conn:
            for routing in routings:
                if await self.route_one(conn, routing) == "missing":
                    missing.append(routing)
            if via_agent:
                await queue.mark_via_agent(conn, via_agent)
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

    async def route(self, routings: list[Routing], via_agent: frozenset[str] = frozenset()) -> None:
        missing = await self._route_all(routings, via_agent)
        if missing:
            await self.refresh()
            missing = await self._route_all(missing, via_agent)
        if missing:
            await self._unverified(missing)

    async def apply_desired(self, desired: Mapping[str, Any]) -> None:
        """Reconcile step 60: the delivered list, then the routings of `pending_prompts` (with `via_agent`), then the
        withdrawals of `prompt_queue`; wakes the delivery."""
        delivered = parse_delivered(desired)
        routings = parse_pending(desired)
        via_agent, withdrawn = hub_queue.parse(desired)
        async with self.env.transaction() as conn:
            adopted = await queue.adopt_delivered(conn, delivered)
        if adopted:
            self.line(f"delivered_adopted count={adopted}")
        await self.route(routings, via_agent)
        if withdrawn:
            await self.withdraw(withdrawn)
        self.wake()

    async def withdraw(self, event_ids: frozenset[str]) -> None:
        """Withdraw the prompts of `prompt_queue.withdrawn` (module docstring); each one reported once."""
        async with self.env.transaction() as conn:
            for event_id in sorted(event_ids):
                done = await queue.withdraw(conn, event_id)
                if done is False:
                    continue
                if isinstance(done, queue.Delivery):
                    task_id, session_id = done.task_id, done.session_id
                else:
                    stored = await queue.event(conn, event_id)
                    task_id, session_id = (None if stored is None else stored.task_id), None
                item: dict[str, Any] = {
                    "action": "session.prompt_withdrawn",
                    "result": "ok",
                    **_known(task_id=task_id, session_id=session_id),
                    "target_kind": "event",
                    "target_id": event_id,
                    "detail": {},
                }
                await self.env.outbox.enqueue(conn, "audit", item)
                self.line(f"withdrawn event_id={event_id} routed={int(session_id is not None)}")

    async def hinted(self, session_id: str, scope_id: str, scope_seq: int, *, via_agent: bool = False) -> None:
        """The `prompt` hint: pull, then route the event at `(scope_id, scope_seq)` to the session (`via_agent`: the
        hub received it from its author's agent)."""
        await self.refresh()
        async with self.env.transaction() as conn:
            prompt = await queue.event_at(conn, scope_id, scope_seq)
        if prompt is None:
            self.line(f"hint_event_missing scope={scope_id} seq={scope_seq}")
            return
        routing = Routing(prompt.task_id, session_id, scope_id, scope_seq, prompt.event_id)
        await self.route([routing], frozenset({prompt.event_id}) if via_agent else frozenset())
        self.wake()

    # -- first prompt of a start ---------------------------------------------------------------------------------

    async def first_prompt(self, session: LocalSession) -> InitialPrompt | None:
        """The head prompt routed to a new main session with its sender line, handed over with the start (channel
        `start`)."""
        if session.depth != 0:
            return None
        desired = self.env.desired
        if desired is not None:
            with contextlib.suppress(ValueError):
                await self.apply_desired(
                    {
                        **desired,
                        "pending_prompts": [
                            routing.__dict__ for routing in parse_pending(desired) if routing.session_id == session.id
                        ],
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
                        opened = await self._checked_prompt(conn, session, head)
                    except PromptRefused:
                        continue
                    if opened.attachments:
                        # the files must be in the workspace before the agent reads the prompt: the session starts
                        # without it and the delivery imports them and types it (ADR-102 section 5)
                        self.line(f"first_prompt_deferred session_id={session.id} event_id={head.event_id}")
                        return None
                    text = opened.text
                    if not await queue.set_state(
                        conn, head.event_id, "registered", expected=("pending",), channel="start", attempt=True
                    ):
                        # withdrawn meanwhile (a concurrent reconcile): never handed over
                        continue
                    line = await speaker.for_prompt(
                        conn, self.env.storage_key, head.event_id, text, via_agent=head.via_agent
                    )
                self.line(f"first_prompt session_id={session.id} event_id={head.event_id}")
                return InitialPrompt(prompt_id=head.event_id, text=text, speaker=line)

    async def _checked_prompt(self, conn: AsyncConnection[Any], session: LocalSession, head: queue.Delivery) -> Opened:
        """Stale check and the opened prompt of a pending head; a refusal is recorded (`refused`, audit)."""
        try:
            if head.task_id is None or head.scope_seq is None:
                raise PromptRefused(STALE)
            if await queue.handed_after(conn, head.task_id, head.scope_seq):
                raise PromptRefused(STALE)
            prompt = await queue.event(conn, head.event_id)
            assert prompt is not None
            return await open_prompt(conn, self.env.storage_key, prompt)
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
                    opened = await self._checked_prompt(conn, session, head)
                except PromptRefused:
                    return True
            if state in SENDABLE:
                await self._send(session, head, opened)
            else:
                await self._resume_then_send(session, head, opened)
        return False

    async def _resume_then_send(
        self, session: LocalSession, head: queue.Delivery, opened: Opened | None = None
    ) -> None:
        if pause.deferred(self.env, session):
            # the organization's credit is used up: the prompt waits until it is back (KOR-31, ADR-062 section 6)
            return
        outcome = await self.resumer.resume(session)
        if outcome != RESUMED:
            # `retry`: backoff; `ended`: stopped meanwhile; a failure reason: the session failed
            if outcome == "retry":
                self._backoff(session.id)
            return
        async with self.env.transaction() as conn:
            resumed = await records.get(conn, session.id)
        assert resumed is not None
        await self._send(resumed, head, opened)

    async def _send(self, session: LocalSession, head: queue.Delivery, opened: Opened | None = None) -> None:
        """Write-ahead `registered`, the prompt's files into the workspace, `POST /v1/sessions/{id}/prompts`, then
        `typed` (see the module docstring)."""
        env = self.env
        if pause.deferred(env, session):
            # the organization's credit is used up: the prompt waits until it is back (KOR-31, ADR-062 section 6)
            return
        async with env.transaction() as conn:
            if opened is None:
                prompt = await queue.event(conn, head.event_id)
                try:
                    if prompt is None:
                        raise PromptRefused(UNVERIFIED)
                    opened = await open_prompt(conn, env.storage_key, prompt)
                except PromptRefused as refused:
                    await queue.set_state(conn, head.event_id, "refused", expected=queue.OPEN)
                    await self.reject(conn, refused.code, head.event_id, session.id)
                    return
            # the journal may have ended the row (accepted, failed) since it was read: never reopen a final row
            handed = await queue.set_state(
                conn, head.event_id, "registered", expected=queue.OPEN, channel="prompt", attempt=True
            )
            line = await speaker.for_prompt(conn, env.storage_key, head.event_id, opened.text, via_agent=head.via_agent)
        if not handed:
            return
        row = await vm_running(env, session)
        if row is None:
            self.line(f"vm_not_running session_id={session.id}")
            self._backoff(session.id)
            return
        if not await self._hand_over(session, head, row, opened, line):
            return
        async with env.transaction() as conn:
            await queue.set_state(conn, head.event_id, "typed", expected=("registered",))
            await records.update(conn, session.id, last_prompt_at=datetime.fromtimestamp(env.wall(), UTC))
        self._settled(session.id)
        self.line(f"sent session_id={session.id} event_id={head.event_id}")

    async def _hand_over(
        self, session: LocalSession, head: queue.Delivery, row: Any, opened: Opened, line: str | None
    ) -> bool:
        """The prompt's files into the workspace, then `POST /v1/sessions/{id}/prompts`; True when the guest took it.
        A file that is missing or does not verify refuses the prompt, the guest's refusal is `_guest_refused`, an
        unreachable hub or guest backs off."""
        env = self.env
        text = attachments.with_note(opened.text, head.event_id, opened.attachments)
        try:
            files = await self._files(session, opened)
            async with await env.client(row) as guest:
                if files:
                    await self._import(guest, session, head, files)
                await speaker.register(
                    env, guest, vm_id=row.id, session_id=session.id, prompt_id=head.event_id, text=text, line=line
                )
        except PromptRefused as refused:
            # an attachment the hub does not hold complete or that does not verify or open (ADR-102 section 5)
            async with env.transaction() as conn:
                await queue.set_state(conn, head.event_id, "refused", expected=("registered",))
                await self.reject(conn, refused.code, head.event_id, session.id)
            self.line(f"refused session_id={session.id} event_id={head.event_id} code={refused.code}")
            return False
        except GuestRefused as exc:
            await self._guest_refused(session, head, exc.error)
            return False
        except (GuestError, RunnerError) as exc:
            self.line(f"send_retry session_id={session.id} event_id={head.event_id} code={exc.code}")
            self._backoff(session.id)
            return False
        return True

    async def _files(self, session: LocalSession, opened: Opened) -> list[tuple[Mapping[str, Any], bytes]]:
        """The checked and opened files of a prompt (none for a prompt of text only; `attachments.py`)."""
        if not opened.attachments:
            return []
        if self.attachments is None:
            raise RunnerError(attachments.UNAVAILABLE)
        epochs = sorted({cast(int, ref["epoch"]) for ref in opened.attachments})
        async with self.env.transaction() as conn:
            keys = {
                epoch: await load_scope_key(conn, self.env.storage_key, "wg", session.wg_id, epoch) for epoch in epochs
            }
        return await self.attachments.files(opened.attachments, keys)

    async def _import(
        self, guest: Any, session: LocalSession, head: queue.Delivery, files: list[tuple[Mapping[str, Any], bytes]]
    ) -> None:
        """The files into `attachments/<event id>/` of the session's workspace (the guest's import, `replace`)."""
        with attachments.archive(files) as tar:
            size = os.fstat(tar.fileno()).st_size
            await guest.import_files(
                session.id, attachments.directory(head.event_id), attachments.stream(tar), replace=True, size=size
            )
        self.line(f"attachments_imported session_id={session.id} event_id={head.event_id} files={len(files)}")

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
            # the main orchestrator's session stays: a suspend would end its keys in the VM (05-runner.md section 17.6)
            busy |= await orchestrator_store.session_ids(conn)
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


def _known(**fields: str | None) -> dict[str, str]:
    """The fields of an audit item that are known (`task_id`, `session_id` are optional there)."""
    return {name: value for name, value in fields.items() if value is not None}


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
    built = Deliverer(
        service.env,
        idle_seconds=context.config.session_idle_seconds,
        stop=service.schedule_stop,
        attachments=AttachmentFetcher(attachments.hub_of(context)),
    )
    return install(context, built)

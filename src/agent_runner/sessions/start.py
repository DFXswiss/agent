"""Start of a main session (05-runner.md section 6.1 steps 1-5, 03-api.md sections 6.1 and 7.1, ADR-009, ADR-010).

`Starter.start(desired)` runs the steps for one session the hub scheduled on this device:

0. local row `requested` (a session already known locally is never started twice);
1. `vm_assigning`: `VmManager.ensure_vm` in the order of ADR-075 section 4 (the hub's `vm_id_pref`, then the VM
   of the parent session for a subagent or of the `resume_from` session, then a running VM of the workgroup with
   room -> a stopped one with room, or its golden replacement -> pool -> clone when the hub allows a new VM
   (`vm_new_allowed`)); the session row names its VM from then on and every later transition carries it;
2. `vm_ready`: guest health assigned to this workgroup; the journal task starts reading the VM;
3. model access and its credential (`model.access`: the owner's subscription or API key, or the platform key);
4. `sandbox_ready`: `POST /v1/sessions` (harness context, model, initial prompt with its sender line, limits; then
   changed by the request amenders of `sessions.extensions`; an older guest that refuses the sender line with
   `400 request_invalid` gets the same request once more without it, logged and reported once per VM,
   `prompts.speaker.unsupported`, ADR-083), then
   `PUT .../credential` (the transition follows both);
5. `agent_starting`: the guest registers the prompt hash and starts the agent once the credential is there.

`tools_ready` (the agent listed the platform tools, journal `session.tools_ready`, HOT-09), `prompt_delivering` (the
guest moves there only after the tools arrived and the TUI is up) and `working` come from the guest journal
(`sessions/journal.py`). Every transition carries the
device time. Failures end the session `failed` with the reason of `session-failure-reasons.json`:

- VM could not be made ready (clone/start/restart, golden missing): `vm_start_failed`;
- no slot free and no idle VM to stop: `capacity_exhausted`;
- guest not reachable, not healthy or not assigned to the workgroup: `guest_unreachable`;
- no model access for the owner's preference on this device (no usable credential of the owner, no platform key or
  an org device for the platform access): `model_access_unavailable`;
- platform access while the organization's daily platform budget is spent: `budget_exceeded`;
- guest refused the sandbox or the credential: `sandbox_failed`;
- guest reports that the agent did not start: `agent_start_failed` (from the journal).

Any other error of a step (store, extension provider) fails the session with `agent_start_failed`. A guest session
that was created before a later step failed is stopped again (best effort). When the session ends while its start
runs (the guest journal reported `failed`, the hub stopped it), the start stops and does not continue
(`reason="ended"`).

Budget measurement (`StartTimer`): the monotonic time of every step from the start of step 1, logged as fact lines
(`runner.session start_step ...`) with the VM path and `over_budget=1` when a step exceeded its budget of 05 section
6.1 (pool 0.1 s, restart and clone 6 s, model choice 0.05 s, sandbox 0.3 s, tools at the agent 2 s after
`agent_starting`, TUI up 2 s after that, prompt accepted 0.5 s after that). The total from step 1 to `working` is
logged with the path.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.client import GuestClient
from agent_runner.guestlink.errors import GuestError, GuestRefused, GuestUnreachable
from agent_runner.model import access, selection
from agent_runner.prompts import speaker
from agent_runner.sessions import harness, profiles, records
from agent_runner.sessions.desired import Desired
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.events import fail
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.stop import stop_guest
from agent_runner.vm import records as vm_records
from agent_runner.vm.records import VmRow

HEALTH_TIMEOUT_S = 5.0
VM_BUDGET_S = {"running": 0.1, "pool": 0.1, "restart": 6.0, "clone": 6.0}
STEP_BUDGET_S = {"model": 0.05, "sandbox": 0.3}
TOOLS_BUDGET_S = 2.0
TUI_BUDGET_S = 2.0
ACCEPT_BUDGET_S = 0.5
VM_REASONS = frozenset({"vm_start_failed", "capacity_exhausted"})
REFRESH_TIMEOUT_S = 0.5
UNEXPECTED = "agent_start_failed"
MEASURED = ("vm", "vm_ready", "model", "sandbox", "agent_starting", "tools_ready", "prompt_delivering", "working")
JOURNAL_MEASURED = frozenset({"tools_ready", "prompt_delivering", "working"})


def without_speaker(request: Mapping[str, Any], refused: GuestRefused) -> dict[str, Any] | None:
    """The request again without the sender line of its initial prompt when an older guest refused the field
    (`400 request_invalid`, ADR-083 section 1: the runner sends only a line that passed the contract); else None."""
    prompt = request.get("initial_prompt")
    if not isinstance(prompt, Mapping) or "speaker" not in prompt:
        return None
    if not speaker.refused_by_older_guest(refused):
        return None
    return {**request, "initial_prompt": {key: value for key, value in prompt.items() if key != "speaker"}}


class Ended(Exception):
    """The session ended (`failed`, `stopping`, `stopped`) while its start was running."""


class StartFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class StartTimer:
    """Monotonic marks of one session start (seconds since step 1 began)."""

    started: float
    path: str | None = None
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, now: float) -> float:
        elapsed = now - self.started
        self.marks.setdefault(name, elapsed)
        return self.marks[name]

    def step(self, name: str) -> float | None:
        """Duration of a step (its mark minus the previous one)."""
        if name not in self.marks:
            return None
        previous = [self.marks[n] for n in MEASURED[: MEASURED.index(name)] if n in self.marks]
        return self.marks[name] - (previous[-1] if previous else 0.0)

    def over_budget(self, name: str) -> bool:
        duration = self.step(name)
        budget = {
            "vm": VM_BUDGET_S.get(self.path or "", 0.0),
            "model": STEP_BUDGET_S["model"],
            "sandbox": STEP_BUDGET_S["sandbox"],
            "tools_ready": TOOLS_BUDGET_S,
            "prompt_delivering": TUI_BUDGET_S,
            "working": ACCEPT_BUDGET_S,
        }.get(name)
        return duration is not None and budget is not None and duration > budget


@dataclass(frozen=True)
class StartReport:
    session_id: str
    started: bool
    reason: str | None = None
    path: str | None = None
    timer: StartTimer | None = None


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class Starter:
    def __init__(self, env: SessionEnv) -> None:
        self.env = env
        self.timers: dict[str, StartTimer] = {}

    # -- measurements --------------------------------------------------------------------------------------------

    def observe(self, session_id: str, state: str) -> None:
        """A state reached (start steps here, `prompt_delivering`/`working` from the journal): log the mark."""
        timer = self.timers.get(session_id)
        if timer is None:
            return
        if state in records.FINAL or state == "stopping":
            del self.timers[session_id]
            return
        if state not in MEASURED or state in timer.marks:
            return
        elapsed = timer.mark(state, self.env.clock())
        over = " over_budget=1" if timer.over_budget(state) else ""
        self.env.line(f"start_step session_id={session_id} step={state} path={timer.path} ms={_ms(elapsed)}{over}")
        if state == "working":
            self.env.line(f"started session_id={session_id} path={timer.path} total_ms={_ms(elapsed)}")
            del self.timers[session_id]

    def observe_journal(self, session_id: str, state: str) -> None:
        """A transition from the guest journal: only the steps after `agent_starting` are measured there (the guest
        may report `sandbox_ready`/`agent_starting` before the start's own marks), and an end drops the timer."""
        if state in JOURNAL_MEASURED or state in records.FINAL or state == "stopping":
            self.observe(session_id, state)

    # -- steps ---------------------------------------------------------------------------------------------------

    async def _advance(
        self, session_id: str, state: str, *, reason: str | None = None, fields: Mapping[str, object] | None = None
    ) -> records.LocalSession | None:
        async with self.env.transaction() as conn:
            moved = await records.advance(
                conn, self.env.outbox, session_id, state, at=self.env.at(), reason=reason, fields=fields
            )
        return moved

    async def _step(self, session_id: str, state: str, *, fields: Mapping[str, object] | None = None) -> None:
        """Advance to `state`; `Ended` when the session ended meanwhile (the guest journal or a stop ended it). A
        state that the journal already reported (or passed) is fine."""
        if await self._advance(session_id, state, fields=fields) is not None:
            return
        async with self.env.transaction() as conn:
            current = await records.get(conn, session_id)
        if current is None or current.state in records.FINAL or current.state == "stopping":
            raise Ended

    async def _fail(self, session_id: str, reason: str) -> None:
        self.timers.pop(session_id, None)
        self.env.line(f"start_failed session_id={session_id} reason={reason}")
        try:
            async with self.env.transaction() as conn:
                await fail(conn, self.env.outbox, session_id, reason, at=self.env.at())
        except Exception as exc:
            # store unavailable: the session stays in its start state; the next daemon run ends it (device_shutdown)
            self.env.line(f"start_fail_unrecorded session_id={session_id} error={type(exc).__name__}")

    async def start(self, desired: Desired) -> StartReport:
        env = self.env
        sid = desired.session_id
        async with env.transaction() as conn:
            created = await records.insert(
                conn,
                session_id=sid,
                task_id=desired.task_id,
                org_id=desired.org_id,
                wg_id=desired.wg_id,
                parent_id=desired.parent_session_id,
                depth=desired.depth,
                owner_id=desired.owner_id,
                created_at=datetime.fromtimestamp(env.wall(), UTC),
            )
        if not created:
            return StartReport(session_id=sid, started=False, reason="known")
        timer = StartTimer(started=env.clock())
        self.timers[sid] = timer
        created_guest = False
        try:
            await self._step(sid, "vm_assigning")
            row = await self._ensure_vm(desired, timer)
            async with await self._client(row) as guest:
                await self._check_guest(guest, desired)
                await self._step(sid, "vm_ready", fields={"vm_id": row.id})
                self.observe(sid, "vm_ready")
                env.watch(row)
                choice, credential = await self._model(desired)
                await self._set(sid, model_access=choice.access, model_id=choice.model_id)
                self.observe(sid, "model")
                request = await self._request(desired, choice)
                created_guest = True
                await self._create(guest, request, row.id)
                await self._provision(guest, sid, credential)
                # 05 section 6.1 step 4: the sandbox is ready once the session exists and holds its credential
                await self._step(sid, "sandbox_ready")
                self.observe(sid, "sandbox")
                await self._step(sid, "agent_starting")
                self.observe(sid, "agent_starting")
                await env.vms.touch(row.id)
        except Ended:
            self.timers.pop(sid, None)
            if created_guest:
                await self._stop_guest(sid)
            self.env.line(f"start_ended session_id={sid}")
            return StartReport(session_id=sid, started=False, reason="ended", path=timer.path, timer=timer)
        except Exception as exc:
            reason = exc.reason if isinstance(exc, StartFailed) else UNEXPECTED
            if not isinstance(exc, StartFailed):
                self.env.line(f"start_error session_id={sid} error={type(exc).__name__}")
            if created_guest:
                await self._stop_guest(sid)
            await self._fail(sid, reason)
            return StartReport(session_id=sid, started=False, reason=reason, path=timer.path, timer=timer)
        return StartReport(session_id=sid, started=True, path=timer.path, timer=timer)

    async def _set(self, session_id: str, **fields: object) -> None:
        async with self.env.transaction() as conn:
            await records.update(conn, session_id, **fields)

    async def _client(self, row: VmRow) -> GuestClient:
        try:
            return await self.env.client(row)
        except RunnerError:
            # token file or instance directory gone: the VM cannot be reached
            raise StartFailed("guest_unreachable") from None

    async def _origin_vm(self, desired: Desired) -> str | None:
        """The VM of the session this one continues: the parent of a subagent, else the `resume_from` session."""
        origin = desired.parent_session_id or desired.resume_from
        if origin is None:
            return None
        async with self.env.transaction() as conn:
            found = await records.get(conn, origin)
        return None if found is None else found.vm_id

    async def _ensure_vm(self, desired: Desired, timer: StartTimer) -> VmRow:
        env = self.env
        before = {row.id: row.state for row in await vm_records.of_workgroup(env.db, desired.wg_id)}
        pool = {row.id for row in await vm_records.live(env.db, "pool")}
        prefer = [vm for vm in (desired.vm_id_pref, await self._origin_vm(desired)) if vm is not None]
        try:
            row = await env.vms.ensure_vm(
                desired.org_id,
                desired.wg_id,
                session_id=desired.session_id,
                prefer=prefer,
                new_allowed=desired.vm_new_allowed,
            )
        except RunnerError as exc:
            raise StartFailed(exc.code if exc.code in VM_REASONS else "vm_start_failed") from None
        except Exception as exc:
            env.line(f"vm_error session_id={desired.session_id} error={type(exc).__name__}")
            raise StartFailed("vm_start_failed") from None
        if row.id in before:
            timer.path = "running" if before[row.id] == "running" else "restart"
        else:
            timer.path = "pool" if row.id in pool else "clone"
        self.observe(desired.session_id, "vm")
        return row

    async def _check_guest(self, guest: GuestClient, desired: Desired) -> None:
        try:
            health = await guest.health(timeout=HEALTH_TIMEOUT_S)
        except GuestError:
            raise StartFailed("guest_unreachable") from None
        if health.state != "assigned" or health.wg_id != desired.wg_id or health.org_id != desired.org_id:
            raise StartFailed("guest_unreachable")

    async def _model(self, desired: Desired) -> tuple[selection.Choice, dict[str, Any]]:
        """Access kind, model and credential of the session (`model.access`, RUN-12)."""
        try:
            resolved = await access.resolve(self.env, desired)
        except access.AccessRefused as exc:
            raise StartFailed(exc.reason) from None
        return resolved.choice, resolved.credential

    async def _context(self, desired: Desired) -> str:
        env = self.env
        async with env.transaction() as conn:
            names = await self._names(conn, desired)
        if names.task is None or names.org is None:
            # the task.meta of a new task may still be on its way: one pull, never a wait
            with contextlib.suppress(Exception):
                await asyncio.wait_for(env.refresh_profiles(), REFRESH_TIMEOUT_S)
            async with env.transaction() as conn:
                names = await self._names(conn, desired)
        log = env.book.log(desired.org_id)
        state = None if log is None else log.state
        return harness.session_context(
            names=names,
            state=state,
            org_id=desired.org_id,
            wg_id=desired.wg_id,
            task_id=desired.task_id,
            owner_id=desired.owner_id,
            github=await harness.github_login(env.context, desired.owner_id),
        )

    async def _names(self, conn: Any, desired: Desired) -> profiles.Names:
        return await profiles.names(
            conn,
            self.env.storage_key,
            org_id=desired.org_id,
            wg_id=desired.wg_id,
            task_id=desired.task_id,
            owner_id=desired.owner_id,
        )

    async def _request(self, desired: Desired, choice: selection.Choice) -> dict[str, Any]:
        env = self.env
        async with env.transaction() as conn:
            session = await records.get(conn, desired.session_id)
        assert session is not None
        prompt: InitialPrompt | None = await env.first_prompt(env.context, session)
        request: dict[str, Any] = {
            "session_id": desired.session_id,
            "task_id": desired.task_id,
            "parent_session_id": desired.parent_session_id,
            "depth": desired.depth,
            "harness": {"context_markdown": await self._context(desired)},
            "model": {"access": choice.access, "model_id": choice.model_id, "env": dict(choice.env)},
            "limits": harness.MAIN_LIMITS.body(),
        }
        if prompt is not None:
            request["initial_prompt"] = prompt.body()
        for amender in env.amenders:
            await amender.amend(env.context, session, request)
        return request

    async def _create(self, guest: GuestClient, request: Mapping[str, Any], vm_id: str) -> None:
        try:
            try:
                answer = await guest.create_session(request)
            except GuestRefused as refused:
                older = without_speaker(request, refused)
                if older is None:
                    raise
                await speaker.unsupported(self.env, vm_id, f"session_id={request['session_id']} channel=start")
                answer = await guest.create_session(older)
        except GuestUnreachable:
            raise StartFailed("guest_unreachable") from None
        except GuestError:
            raise StartFailed("sandbox_failed") from None
        if answer.get("state") != "sandbox_ready":
            raise StartFailed("sandbox_failed")

    async def _provision(self, guest: GuestClient, session_id: str, credential: Mapping[str, Any]) -> None:
        try:
            await guest.put_credential(session_id, credential)
        except GuestUnreachable:
            raise StartFailed("guest_unreachable") from None
        except GuestError:
            raise StartFailed("sandbox_failed") from None

    async def _stop_guest(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await stop_guest(self.env, session_id)

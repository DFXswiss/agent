"""Resume of a suspended main session and the harness hint of a restarted one (05-runner.md sections 6.3 and 3.5,
03-api.md sections 4.6.3 and 7.1, ADR-025).

`Resumer.resume(session)` - a follow-up prompt for a `suspended` session (or one left `resuming` by a daemon restart):

1. `resuming` (transition);
2. the workgroup's VM is made ready again (`VmManager.ensure_vm`: a VM stopped by the VM idle rule is restarted); a
   VM other than the session's - the session's directories and transcript are gone - fails the session
   (`vm_start_failed`);
3. guest health assigned to the workgroup, model access and credential as for a start (the owner's preference from
   the desired state, else the session's recorded access);
4. `POST /v1/sessions` with the same session ID and `resume_claude_session_id` (the guest keeps user, directories and
   transcript and starts `claude --resume <id>`), then `PUT .../credential`, which starts the agent.

The prompt itself is delivered afterwards by `deliver.Deliverer` through `POST .../prompts`; the guest waits for the
resumed TUI before it types (GST-06). A guest that cannot be reached (or is not healthy for the workgroup) leaves the
session `resuming` and the resume is tried again, at most `MAX_ATTEMPTS` times; any other failure fails the session
with the start's reason (`events.fail`) and stops its guest session. A session that was stopped while its resume
began (`resuming` could not be set) is left alone (`ended`).
A repeated resume of a session the guest already resumed is answered by the guest without a second start.

The steps reuse the start's building blocks (`sessions.start.Starter`: VM and guest checks, model choice, harness
context, guest calls), so a resumed session gets exactly what a started one gets.

`restart_hint` (request amender, `session_request.py`): a **new** main session of a task that had earlier messages
(03-api.md section 4.6.3: the hub names the previous session in `resume_from`, or the session's first prompt is not
the task's first prompt) gets one more line in its harness context telling it to read the history with
`get_task_messages`. The transcript of the previous session belongs to that session's user and directories in the
guest, so a new session never resumes it with `--resume` (decisions/RUN-05.md).
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused, GuestUnreachable
from agent_runner.prompts import queue
from agent_runner.sessions import harness, records
from agent_runner.sessions.desired import Desired, desired_sessions
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.events import fail
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.start import Starter, StartFailed
from agent_runner.sessions.stop import stop_guest
from agent_runner.vm import records as vm_records

RESUMED = "resumed"
RETRY = "retry"
ENDED = "ended"
MAX_ATTEMPTS = 10
"""Resume attempts of one session that end `guest_unreachable` (unreachable, unhealthy or reassigned guest) before
the session fails with that reason."""
UNEXPECTED = "agent_start_failed"
VM_REASONS = frozenset({"vm_start_failed", "capacity_exhausted"})
RESTART_LINE = (
    "- Restarted session: this task had an earlier session. Read the earlier messages of the task with"
    " mcp__platform__get_task_messages (task_id {task_id}) before you continue."
)


def listed(env: SessionEnv, session_id: str) -> Mapping[str, Any] | None:
    """The raw entry of the session in the last desired state, None when it is not listed."""
    desired = env.desired
    if desired is None:
        return None
    for item in desired.get("sessions") or []:
        if isinstance(item, dict) and item.get("session_id") == session_id:
            return item
    return None


def desired_of(env: SessionEnv, session: LocalSession) -> Desired:
    """The session as the start needs it: the hub's entry (owner preferences) when it is listed and well-formed,
    else the local row with the recorded access as preference."""
    entry = listed(env, session.id)
    if entry is not None:
        with contextlib.suppress(ValueError):
            for found in desired_sessions({"sessions": [entry]}):
                if (found.task_id, found.org_id, found.wg_id) == (session.task_id, session.org_id, session.wg_id):
                    return found
    return Desired(
        session_id=session.id,
        task_id=session.task_id,
        org_id=session.org_id,
        wg_id=session.wg_id,
        state=session.state,
        parent_session_id=None,
        depth=0,
        owner_id=session.owner_id or "",
        model_access_pref=session.model_access or "auto",
        model_id_pref=session.model_id,
    )


class Resumer:
    def __init__(self, env: SessionEnv) -> None:
        self.env = env
        self.starter = Starter(env)
        self.attempts: dict[str, int] = {}

    async def resume(self, session: LocalSession) -> str:
        """`resumed`, `retry` (guest or VM not reachable now) or the failure reason the session ended with."""
        env = self.env
        sid = session.id
        async with env.transaction() as conn:
            await records.advance(conn, env.outbox, sid, "resuming", at=env.at())
            current = await records.get(conn, sid)
        if current is None or current.state != "resuming":
            # stopped (or failed) meanwhile: no guest session is started again
            env.line(f"resume_ended session_id={sid}")
            return ENDED
        desired = desired_of(env, session)
        try:
            await self._resume(session, desired)
        except StartFailed as exc:
            attempts = self.attempts.get(sid, 0) + 1
            self.attempts[sid] = attempts
            if exc.reason == "guest_unreachable" and attempts < MAX_ATTEMPTS:
                env.line(f"resume_retry session_id={sid} attempt={attempts}")
                return RETRY
            self.attempts.pop(sid, None)
            return await self._fail(sid, exc.reason)
        except Exception as exc:
            env.line(f"resume_error session_id={sid} error={type(exc).__name__}")
            return await self._fail(sid, UNEXPECTED)
        self.attempts.pop(sid, None)
        env.line(f"resumed session_id={sid}")
        return RESUMED

    async def _resume(self, session: LocalSession, desired: Desired) -> None:
        env = self.env
        try:
            row = await env.vms.ensure_vm(session.org_id, session.wg_id)
        except RunnerError as exc:
            raise StartFailed(exc.code if exc.code in VM_REASONS else "vm_start_failed") from None
        if row.id != session.vm_id:
            # another VM: the session's directories and transcript are not there
            raise StartFailed("vm_start_failed")
        env.watch(row)  # a restarted VM: its journal is read at once
        try:
            guest = await env.client(row)
        except RunnerError:
            raise StartFailed("guest_unreachable") from None
        async with guest:
            await self.starter._check_guest(guest, desired)
            choice, credential = await self.starter._model(desired)
            request: dict[str, Any] = {
                "session_id": session.id,
                "task_id": session.task_id,
                "parent_session_id": None,
                "depth": 0,
                "harness": {"context_markdown": await self.starter._context(desired)},
                "model": {"access": choice.access, "model_id": choice.model_id, "env": dict(choice.env)},
                "limits": harness.MAIN_LIMITS.body(),
            }
            if session.claude_session_id is not None:
                request["resume_claude_session_id"] = session.claude_session_id
            await self._create(guest, session, request)
            await self.starter._provision(guest, session.id, credential)
            async with env.transaction() as conn:
                await records.update(conn, session.id, model_access=choice.access, model_id=choice.model_id)
        await env.vms.touch(row.id)

    @staticmethod
    async def _create(guest: Any, session: LocalSession, request: Mapping[str, Any]) -> None:
        """`POST /v1/sessions` of the resume. A session left `resuming` by an earlier attempt whose request the guest
        already took may be answered `id_conflict` (a name in the context changed meanwhile): the credential that
        follows still starts it."""
        try:
            answer = await guest.create_session(request)
        except GuestUnreachable:
            raise StartFailed("guest_unreachable") from None
        except GuestRefused as exc:
            if exc.error == "id_conflict" and session.state == "resuming":
                return
            raise StartFailed("sandbox_failed") from None
        except GuestError:
            raise StartFailed("sandbox_failed") from None
        if answer.get("state") != "sandbox_ready":
            raise StartFailed("sandbox_failed")

    async def _fail(self, session_id: str, reason: str) -> str:
        env = self.env
        env.line(f"resume_failed session_id={session_id} reason={reason}")
        async with env.transaction() as conn:
            await fail(conn, env.outbox, session_id, reason, at=env.at())
        with contextlib.suppress(Exception):
            await stop_guest(env, session_id)
        return reason


async def restarted(env: SessionEnv, session: LocalSession, request: Mapping[str, Any]) -> bool:
    """Whether a new main session continues a task that had an earlier session (see the module docstring)."""
    if session.depth != 0:
        return False
    entry = listed(env, session.id)
    if entry is not None and entry.get("resume_from") is not None:
        return True
    initial = request.get("initial_prompt")
    if not isinstance(initial, Mapping):
        return False
    async with env.transaction() as conn:
        prompt = await queue.event(conn, str(initial.get("prompt_id")))
        return prompt is not None and await queue.earlier_of_task(conn, prompt.task_id, prompt.scope_seq)


async def restart_hint(env: SessionEnv, session: LocalSession, request: dict[str, Any]) -> None:
    if not await restarted(env, session, request):
        return
    harness_part = request["harness"]
    harness_part["context_markdown"] = (
        str(harness_part["context_markdown"]).rstrip("\n") + "\n" + RESTART_LINE.format(task_id=session.task_id)
    )
    env.line(f"restarted session_id={session.id}")


async def vm_running(env: SessionEnv, session: LocalSession) -> vm_records.VmRow | None:
    """The session's VM when it runs."""
    row = None if session.vm_id is None else await vm_records.get(env.db, session.vm_id)
    return row if row is not None and row.state == "running" else None

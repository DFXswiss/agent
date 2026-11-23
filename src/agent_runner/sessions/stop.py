"""Stop of a session (05-runner.md sections 6.3 and 3.5, 03-api.md sections 6.1 and 7.1).

`stop_session(env, session_id, reason=None)`: `stopping` (with the reason, e.g. `unassigned`), then the guest control
action `stop` (`systemctl stop agent-session-<id>`: SIGTERM, 10 s, SIGKILL; tmux window closed), then `stopped`.
The guest journals `stopping`/`stopped` as well; `records.advance` makes each one transition only.

- A session whose VM is not running (stopped, deleted) or whose guest does not know it (`not_found`) cannot run any
  more: it becomes `stopped` at once.
- A guest that cannot be reached leaves the session `stopping`; the next reconcile run tries again.
"""

from __future__ import annotations

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.vm import records as vm_records


async def _control_stop(env: SessionEnv, session_id: str) -> tuple[str, str | None]:
    """The guest control action `stop` for the session's VM: `(outcome, code)` with outcome `stopped` (confirmed, or
    the guest does not know the session), `absent` (no running VM), `refused` or `pending` (unreachable)."""
    async with env.transaction() as conn:
        session = await records.get(conn, session_id)
    row = None if session is None or session.vm_id is None else await vm_records.get(env.db, session.vm_id)
    if row is None or row.state != "running":
        return "absent", None
    try:
        async with await env.client(row) as guest:
            await guest.control(session_id, "stop")
    except GuestRefused as exc:
        return ("stopped", None) if exc.error == "not_found" else ("refused", exc.error)
    except (GuestError, RunnerError) as exc:
        return "pending", exc.code
    return "stopped", None


async def stop_guest(env: SessionEnv, session_id: str) -> str:
    """Stop the guest session whatever the local state (an interrupted or failed start whose agent may still run);
    returns the outcome (best effort: nobody waits for it)."""
    outcome, _code = await _control_stop(env, session_id)
    env.line(f"guest_stop session_id={session_id} outcome={outcome}")
    return outcome


async def _stopped(env: SessionEnv, session_id: str) -> None:
    async with env.transaction() as conn:
        await records.advance(conn, env.outbox, session_id, "stopped", at=env.at())


async def stop_session(env: SessionEnv, session_id: str, reason: str | None = None) -> bool:
    """Stop the session; True when it ended `stopped`, False when it is unknown, already ended or still `stopping`."""
    async with env.transaction() as conn:
        await records.advance(conn, env.outbox, session_id, "stopping", at=env.at(), reason=reason)
        session = await records.get(conn, session_id)
    if session is None or session.state != "stopping":
        return False
    outcome, code = await _control_stop(env, session_id)
    if outcome == "refused":
        env.line(f"stop_refused session_id={session_id} error={code}")
        return False
    if outcome == "pending":
        env.line(f"stop_pending session_id={session_id} code={code}")
        return False
    await _stopped(env, session_id)
    env.line(f"stopped session_id={session_id}" + (" guest=absent" if outcome == "absent" else ""))
    return True

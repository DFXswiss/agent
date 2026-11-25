"""The session state machine and the derived task state (02-data-model.md section 2.4.1).

**Session states.** Start-up runs `requested` → `vm_assigning` → `vm_ready` → `sandbox_ready` → `agent_starting` →
`prompt_delivering` → `working`; a runner may skip start-up steps forward (e.g. no `vm_assigning` when the VM of the
workgroup is already running), never go back. Then `working` ⇄ `idle`, `idle` → `suspended` ⇄ `resuming` → `working` (a
failed resume falls back to `suspended`). `stopping` is reachable from every live state, `stopped` from every live state
(a session that ends without a separate `stopping` report) and `failed` from every live state. `stopped` and `failed`
are final: a report after them is a refused transition. Reporting the current state again is no transition (`SAME`).

**Task state.** A `closed` task stays closed (only a user leaves it, by a follow-up prompt). Otherwise: any session
`working` that is not interrupted ⇒ `working`; else by the main session (the newest session of depth 0): a live main
session interrupted because its device failed (ADR-086 section 2) ⇒ `scheduling` with the reason `device_failed`;
`requested` without a device ⇒
`scheduling` with the scheduler's reason, every other start-up state and `resuming` ⇒ `starting`, `idle`, `suspended`,
`stopping` and `stopped` ⇒ `idle` (the task waits for the user), `failed` ⇒ `failed` with the session's reason (a
follow-up prompt creates a new main session, so a failed main session is never "retry pending").
"""

from __future__ import annotations

from dataclasses import dataclass

STARTUP = ("requested", "vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering")
WORKING, IDLE, SUSPENDED, RESUMING = "working", "idle", "suspended", "resuming"
STOPPING, STOPPED, FAILED = "stopping", "stopped", "failed"
STATES = (*STARTUP, WORKING, IDLE, SUSPENDED, RESUMING, STOPPING, STOPPED, FAILED)
FINAL = frozenset({STOPPED, FAILED})
ENDING = frozenset({STOPPING, STOPPED, FAILED})
"""States after which a follow-up prompt needs a new main session."""
SAME, ALLOWED, REFUSED = "same", "allowed", "refused"
_LIVE_STEPS: dict[str, frozenset[str]] = {
    WORKING: frozenset({IDLE}),
    IDLE: frozenset({WORKING, SUSPENDED}),
    SUSPENDED: frozenset({RESUMING}),
    RESUMING: frozenset({WORKING, SUSPENDED}),
    STOPPING: frozenset(),
}

SCHEDULING, STARTING, CLOSED = "scheduling", "starting", "closed"
INTERRUPTED_SQL = (
    "(interrupted_at IS NOT NULL AND (interruption_ended_at IS NULL OR interruption_ended_at < interrupted_at))"
)
"""SQL over a `hub.session` row: it is interrupted now (ADR-086 section 2)."""
DEVICE_FAILED = "device_failed"
"""The task's reason while its main session is interrupted, and the end reason of a session the hub moved away from a
failed device (ADR-086)."""
TASK_STATES = (SCHEDULING, STARTING, WORKING, IDLE, FAILED, CLOSED)


def step(current: str, new: str) -> str:
    """`SAME`, `ALLOWED` or `REFUSED` for a reported transition (module docstring)."""
    if new not in STATES:
        return REFUSED
    if new == current:
        return SAME
    if current in FINAL:
        return REFUSED
    if new in (STOPPED, FAILED) or (new == STOPPING and current != STOPPING):
        return ALLOWED
    if current in STARTUP:
        return ALLOWED if new == WORKING or new in STARTUP[STARTUP.index(current) + 1 :] else REFUSED
    return ALLOWED if new in _LIVE_STEPS[current] else REFUSED


@dataclass(frozen=True)
class MainSession:
    """What the task derivation needs of the main session."""

    state: str
    state_reason: str | None
    placed: bool
    interrupted: bool = False
    """Its device failed while it was active (`session.interrupted_at`, ADR-086 section 2)."""


def task_state(
    *, closed: bool, any_working: bool, main: MainSession | None, scheduling_reason: str | None
) -> tuple[str, str | None]:
    """The derived `(state, state_reason)` of a task (module docstring)."""
    if closed:
        return CLOSED, None
    if any_working:
        return WORKING, None
    if (
        main is None
        or (main.state == "requested" and not main.placed)
        or (main.interrupted and main.state not in FINAL)
    ):
        interrupted = main is not None and main.interrupted
        return SCHEDULING, DEVICE_FAILED if interrupted else scheduling_reason
    if main.state in STARTUP or main.state == RESUMING:
        return STARTING, None
    if main.state == FAILED:
        return FAILED, main.state_reason
    return IDLE, None

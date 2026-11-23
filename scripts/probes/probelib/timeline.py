"""Marks and steps of one session start on the clone and the pool path (05-runner.md section 6.1, ADR-009).

A run is a mapping mark -> seconds since `begin` on the host's monotonic clock (the runner device). Marks:

| Mark | Taken when |
|---|---|
| `begin` | `ensure_vm` is called (clone path: pool empty; pool path: one filled pool VM) |
| `cloned` | `limactl clone` returned (clone path) |
| `started` | `limactl start` returned: boot, cloud-init, `agent-guest-harden.service` with the token parameter |
| `healthy` | `GET /v1/health` answered `unassigned` for the first time (clone path) |
| `assigned` | `ensure_vm` returned (`POST /v1/assign` done, row `running`) |
| `session_begin` | before `POST /v1/sessions`; the probe starts the model double after `assigned` (excluded) |
| `session_created` | `POST /v1/sessions` returned (`sandbox_ready`) |
| `credential_put` | `PUT /v1/sessions/{id}/credential` returned; the guest starts the agent with it (step 5) |
| `agent_starting` | the journal record `session.state agent_starting` reached the host (tmux and `systemd-run`) |
| `first_hook` | the journal record of the first hook (`session.hook SessionStart`) reached the host: the TUI is up |
| `prompt_accepted` | the journal record `session.state prompt_accepted` reached the host (step 7, `working`) |
| `answered` | the first gateway `usage` record reached the host (the model double answered; informational) |

`credential_put` and `agent_starting` are not ordered against each other (the guest journals the start while the
PUT is answered). The end-to-end time is `prompt_accepted - begin` without the excluded test setup
(`assigned` .. `session_begin`); the hub round trips and the credential choice (step 3) are not part of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

EXCLUDED = (("assigned", "session_begin"),)
UNORDERED = frozenset({frozenset({"credential_put", "agent_starting"})})


class MarkError(ValueError):
    """A run whose marks are missing or out of order."""


@dataclass(frozen=True)
class Step:
    name: str
    start: str
    end: str
    budget_s: float | None
    reference: str
    optional: bool = False


SESSION_STEPS = (
    Step("sandbox", "session_begin", "credential_put", 0.3, "05 §6.1 step 4: POST /v1/sessions + PUT credential"),
    Step("tui_start", "agent_starting", "first_hook", 2.0, "05 §6.1 step 6: TUI up <= 2 s from step 5"),
    Step("tui_up", "credential_put", "first_hook", None, "PUT credential to the first hook (host view)"),
    Step("prompt_accept", "first_hook", "prompt_accepted", 0.5, "05 §6.1 step 7: prompt_accepted <= 0.5 s"),
    Step("answered", "prompt_accepted", "answered", None, "model double answer (informational)", optional=True),
)
CLONE_STEPS = (
    Step("clone", "begin", "cloned", None, "05 §3.2: limactl clone (APFS)"),
    Step("boot", "cloned", "started", None, "05 §3.2: limactl start with the token parameter, harden unit"),
    Step("health", "started", "healthy", None, "05 §3.2: GET /v1/health until unassigned"),
    Step("assign", "healthy", "assigned", None, "05 §3.2: POST /v1/assign"),
    Step("vm_ready", "begin", "assigned", 6.0, "05 §6.1 step 1: clone <= 6 s"),
    *SESSION_STEPS,
)
POOL_STEPS = (
    Step("vm_ready", "begin", "assigned", 0.1, "05 §6.1 step 1: pool 0.1 s (M0 acceptance <= 0.5 s)"),
    *SESSION_STEPS,
)
_SESSION_MARKS = (
    "session_begin",
    "session_created",
    "credential_put",
    "agent_starting",
    "first_hook",
    "prompt_accepted",
    "answered",
)
CLONE_MARKS = ("begin", "cloned", "started", "healthy", "assigned", *_SESSION_MARKS)
POOL_MARKS = ("begin", "assigned", *_SESSION_MARKS)
OPTIONAL_MARKS = frozenset({"answered"})
# Budgets of the whole start (05 §6.1: warm pool <= 3 s, clone path <= 8.5 s; 03-api.md §4.6.1: < 10 s).
TOTAL_BUDGET_S = {"pool": 3.0, "clone": 8.5}


def _mark(marks: Mapping[str, float], name: str) -> float:
    try:
        return marks[name]
    except KeyError:
        raise MarkError(f"mark {name} missing") from None


def step_durations(marks: Mapping[str, float], steps: Iterable[Step]) -> dict[str, float]:
    """Seconds per step; an optional step without both marks is left out, a required one raises `MarkError`."""
    out: dict[str, float] = {}
    for step in steps:
        if step.optional and (step.start not in marks or step.end not in marks):
            continue
        out[step.name] = _mark(marks, step.end) - _mark(marks, step.start)
    return out


def end_to_end(marks: Mapping[str, float], *, start: str = "begin", end: str = "prompt_accepted") -> float:
    """`end - start` without the excluded test setup intervals."""
    total = _mark(marks, end) - _mark(marks, start)
    for first, last in EXCLUDED:
        total -= _mark(marks, last) - _mark(marks, first)
    return total


def check_order(marks: Mapping[str, float], order: Sequence[str]) -> None:
    """Every required mark of `order` is present, and no mark is earlier than one listed before it."""
    for name in order:
        if name not in OPTIONAL_MARKS:
            _mark(marks, name)
    present = [name for name in order if name in marks]
    for index, later in enumerate(present):
        for earlier in present[:index]:
            if frozenset({earlier, later}) in UNORDERED:
                continue
            if marks[later] < marks[earlier]:
                raise MarkError(f"{later} before {earlier}")

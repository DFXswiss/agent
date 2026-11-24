"""U-HUB-tasks (state machine): every pair of session states and the derived task state of 02-data-model.md
section 2.4.1."""

from __future__ import annotations

import pytest

from agent_hub.sessions.derive import ALLOWED, REFUSED, SAME, STARTUP, STATES, MainSession, step, task_state

LIVE = {
    "working": {"idle"},
    "idle": {"working", "suspended"},
    "suspended": {"resuming"},
    "resuming": {"working", "suspended"},
    "stopping": set(),
}


def expected_step(current: str, new: str) -> str:
    if new == current:
        return SAME
    if current in ("stopped", "failed"):
        return REFUSED
    if new in ("stopping", "stopped", "failed"):
        return ALLOWED
    if current in STARTUP:
        later = STARTUP[STARTUP.index(current) + 1 :]
        return ALLOWED if new == "working" or new in later else REFUSED
    return ALLOWED if new in LIVE[current] else REFUSED


@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("new", STATES)
def test_every_pair_of_states(current: str, new: str) -> None:
    assert step(current, new) == expected_step(current, new)


@pytest.mark.parametrize(
    ("current", "new", "result"),
    [
        ("requested", "vm_assigning", ALLOWED),
        ("requested", "agent_starting", ALLOWED),
        ("vm_ready", "vm_assigning", REFUSED),
        ("prompt_delivering", "working", ALLOWED),
        ("prompt_delivering", "idle", REFUSED),
        ("working", "suspended", REFUSED),
        ("suspended", "working", REFUSED),
        ("stopping", "working", REFUSED),
        ("stopping", "stopped", ALLOWED),
        ("stopped", "failed", REFUSED),
        ("failed", "stopped", REFUSED),
        ("working", "working", SAME),
        ("working", "dancing", REFUSED),
    ],
)
def test_named_transitions(current: str, new: str, result: str) -> None:
    assert step(current, new) == result


def main(state: str, reason: str | None = None, placed: bool = True) -> MainSession:
    return MainSession(state=state, state_reason=reason, placed=placed)


@pytest.mark.parametrize(
    ("closed", "working", "session", "reason", "expected"),
    [
        (True, True, main("working"), None, ("closed", None)),
        (False, True, main("idle"), None, ("working", None)),
        (False, True, main("failed", "oom"), None, ("working", None)),
        (False, False, None, "device_offline", ("scheduling", "device_offline")),
        (False, False, main("requested", placed=False), "capacity_exhausted", ("scheduling", "capacity_exhausted")),
        (False, False, main("requested"), "device_offline", ("starting", None)),
        (False, False, main("prompt_delivering"), None, ("starting", None)),
        (False, False, main("resuming"), None, ("starting", None)),
        (False, False, main("idle"), None, ("idle", None)),
        (False, False, main("suspended"), None, ("idle", None)),
        (False, False, main("stopping"), None, ("idle", None)),
        (False, False, main("stopped"), None, ("idle", None)),
        (False, False, main("failed", "vm_start_failed"), None, ("failed", "vm_start_failed")),
    ],
)
def test_task_derivation(
    closed: bool, working: bool, session: MainSession | None, reason: str | None, expected: tuple[str, str | None]
) -> None:
    assert task_state(closed=closed, any_working=working, main=session, scheduling_reason=reason) == expected

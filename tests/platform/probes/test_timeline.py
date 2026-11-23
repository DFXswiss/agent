"""Step breakdown of the clone and pool paths against the budgets of 05-runner.md section 6.1."""

from __future__ import annotations

import pytest
from probelib.timeline import (
    CLONE_MARKS,
    CLONE_STEPS,
    POOL_MARKS,
    POOL_STEPS,
    MarkError,
    Step,
    check_order,
    end_to_end,
    step_durations,
)

CLONE_RUN = {
    "begin": 0.0,
    "cloned": 0.05,
    "started": 5.2,
    "healthy": 5.6,
    "assigned": 5.65,
    "session_begin": 6.4,  # 0.75 s model-double setup in between (excluded)
    "session_created": 6.5,
    "credential_put": 6.55,
    "agent_starting": 6.56,
    "first_hook": 7.4,
    "prompt_accepted": 7.6,
    "answered": 7.9,
}


def test_clone_steps() -> None:
    found = step_durations(CLONE_RUN, CLONE_STEPS)
    assert found == pytest.approx(
        {
            "clone": 0.05,
            "boot": 5.15,
            "health": 0.4,
            "assign": 0.05,
            "vm_ready": 5.65,
            "sandbox": 0.15,
            "tui_start": 0.84,
            "tui_up": 0.85,
            "prompt_accept": 0.2,
            "answered": 0.3,
        }
    )
    assert end_to_end(CLONE_RUN) == pytest.approx(6.85)
    check_order(CLONE_RUN, CLONE_MARKS)


def test_pool_steps_and_optional_marks() -> None:
    run = {key: value - 5.6 for key, value in CLONE_RUN.items() if key in POOL_MARKS and key != "begin"}
    run["begin"] = 0.0
    del run["answered"]
    found = step_durations(run, POOL_STEPS)
    assert "answered" not in found
    assert found["vm_ready"] == pytest.approx(0.05)
    assert end_to_end(run) == pytest.approx(1.25)
    check_order(run, POOL_MARKS)


def test_budgets_follow_the_specification() -> None:
    budgets = {step.name: step.budget_s for step in CLONE_STEPS if step.budget_s is not None}
    assert budgets == {"vm_ready": 6.0, "sandbox": 0.3, "tui_start": 2.0, "prompt_accept": 0.5}
    assert {step.name: step.budget_s for step in POOL_STEPS}["vm_ready"] == 0.1
    assert all(step.reference for step in (*CLONE_STEPS, *POOL_STEPS))


def test_missing_required_mark() -> None:
    run = dict(CLONE_RUN)
    del run["first_hook"]
    with pytest.raises(MarkError, match="first_hook"):
        step_durations(run, CLONE_STEPS)
    with pytest.raises(MarkError, match="first_hook"):
        check_order(run, CLONE_MARKS)


def test_missing_end_mark_of_the_total() -> None:
    run = dict(CLONE_RUN)
    del run["prompt_accepted"]
    with pytest.raises(MarkError, match="prompt_accepted"):
        end_to_end(run)


def test_marks_out_of_order() -> None:
    run = dict(CLONE_RUN, first_hook=6.0)
    with pytest.raises(MarkError, match="first_hook before session_begin"):
        check_order(run, CLONE_MARKS)


def test_optional_step_with_a_partial_mark() -> None:
    step = Step("x", "a", "b", None, "ref", optional=True)
    assert step_durations({"a": 1.0}, [step]) == {}
    assert step_durations({"a": 1.0, "b": 1.5}, [step]) == {"x": 0.5}

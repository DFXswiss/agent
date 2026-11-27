"""U-HUB-attention (overview): the start page's three numbers read only the attention records - every record of an open
task falls into exactly one of `working`, `waiting` and `no_agent`, a closed task into none (contracts/changes/
HOME-COUNTS.md), including the edge cases of the derivation (a task captured and not started, an ended or failed
session of an open task, a wait for quota, a question while the task is idle)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from agent_hub.attention import derive
from agent_hub.attention.derive import Inputs, Operation

MAIN = uuid.UUID(int=1)
OP = uuid.UUID(int=2)


def test_every_category_belongs_to_exactly_one_overview() -> None:
    owners = [name for categories in derive.OVERVIEW.values() for name in categories]
    assert sorted(owners) == sorted(derive.CATEGORIES)
    assert list(derive.OVERVIEW) == ["working", "waiting", "no_agent"]


@pytest.mark.parametrize(
    ("category", "reason"),
    [(category, reason) for category, (_order, reasons) in derive.CATEGORIES.items() for reason in reasons],
)
def test_every_listed_reason_falls_into_exactly_one_overview_but_closed(category: str, reason: str) -> None:
    found = derive.overview_of(category, reason)
    hits = [name for name, categories in derive.OVERVIEW.items() if category in categories]
    if (category, reason) == (derive.DONE, derive.CLOSED):
        assert found is None
    else:
        assert hits == [found]


@pytest.mark.parametrize(
    ("category", "reason", "expected"),
    [
        ("working", "working", "working"),
        ("working", "queued", "working"),
        ("working", "starting", "working"),
        ("working", "operation", "working"),
        ("working", "subagents", "working"),
        ("working", "quota_wait", "working"),
        ("needs_decision", "question", "waiting"),
        ("needs_decision", "approval", "waiting"),
        ("needs_decision", "quota_choice", "waiting"),
        ("needs_decision", "device_missing", "waiting"),
        ("needs_decision", "credit_empty", "waiting"),
        ("needs_decision", "connection_missing", "waiting"),
        ("blocked", "session_failed", "no_agent"),
        ("blocked", "device_unreachable", "no_agent"),
        ("done", "idle", "no_agent"),
        ("done", "closed", None),
        # a reason a later release adds stays in its category's overview
        ("blocked", "something_new", "no_agent"),
        ("done", "captured", "no_agent"),
        # a category this release does not know counts nowhere
        ("archived", "idle", None),
    ],
)
def test_the_overview_of_a_record(category: str, reason: str, expected: str | None) -> None:
    assert derive.overview_of(category, reason) == expected


def _of(inputs: Inputs) -> str | None:
    found = derive.derive(inputs)
    return derive.overview_of(found.category, found.reason)


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        # captured and not started: no session yet; the task lies idle (a ticket state of a later release)
        (Inputs(task_state="idle", task_reason=None), "no_agent"),
        (Inputs(task_state="todo", task_reason=None), "no_agent"),
        # waiting in the queue for a device or capacity: the platform starts it by itself
        (Inputs(task_state="scheduling", task_reason=None), "working"),
        (Inputs(task_state="scheduling", task_reason="capacity_exhausted"), "working"),
        # no device at all: a person must provide one
        (Inputs(task_state="scheduling", task_reason=None, device_assigned=False), "waiting"),
        (Inputs(task_state="scheduling", task_reason="device_offline"), "no_agent"),
        (Inputs(task_state="starting", task_reason=None, main_id=MAIN, main_state="starting"), "working"),
        (Inputs(task_state="working", task_reason=None, main_id=MAIN, main_state="working"), "working"),
        # the session ended, the task is not closed
        (Inputs(task_state="idle", task_reason=None, main_id=MAIN, main_state="stopped"), "no_agent"),
        (Inputs(task_state="idle", task_reason=None, main_id=MAIN, main_state="idle"), "no_agent"),
        # the session failed or was cancelled, the task is not closed
        (Inputs(task_state="failed", task_reason=None, main_id=MAIN, main_state="failed"), "no_agent"),
        (Inputs(task_state="failed", task_reason="credit_exhausted", main_id=MAIN), "waiting"),
        # a question or an approval waits for a person
        (
            Inputs(
                task_state="idle",
                task_reason=None,
                main_id=MAIN,
                main_state="idle",
                last_turn=(Operation(OP, "human.question", "succeeded", None),),
            ),
            "waiting",
        ),
        (
            Inputs(
                task_state="idle",
                task_reason=None,
                main_id=MAIN,
                main_state="idle",
                last_turn=(Operation(OP, "human.approval", "succeeded", None),),
            ),
            "waiting",
        ),
        # the question was asked, but the session works again (the answer arrived): it works
        (
            Inputs(
                task_state="working",
                task_reason=None,
                main_id=MAIN,
                main_state="working",
                main_working=True,
                last_turn=(Operation(OP, "human.question", "succeeded", None),),
            ),
            "working",
        ),
        # a wait for model quota continues by itself; a choice of DFX access waits for a person
        (
            Inputs(
                task_state="working",
                task_reason=None,
                main_id=MAIN,
                main_state="suspended",
                main_reason="model_quota_exhausted",
                main_wait_until=datetime(2026, 11, 26, 12, tzinfo=UTC),
            ),
            "working",
        ),
        (
            Inputs(
                task_state="working",
                task_reason=None,
                main_id=MAIN,
                main_state="suspended",
                main_reason="model_quota_exhausted",
                main_dfx_choice=True,
            ),
            "waiting",
        ),
        (Inputs(task_state="closed", task_reason=None, main_id=MAIN, main_state="stopped"), None),
    ],
)
def test_every_task_state_counts_in_exactly_one_number(inputs: Inputs, expected: str | None) -> None:
    assert _of(inputs) == expected

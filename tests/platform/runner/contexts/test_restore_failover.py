"""U-RUN-contexts: the successor of a session on a failed device restores the checkpoint the hub pinned at the move
without waiting for a final checkpoint that never comes, and its note says so (ADR-086 section 3.5)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agent_runner.contexts import archive as archive_format
from agent_runner.contexts.restore import (
    HUB_STEP_S,
    STALE_TEXT,
    WORK_VM_TEXT,
    Restored,
    cancelled_text,
    restored_line,
)
from agent_runner.sessions.failover import Failover, failover_of

from ..prompts.kit import run
from ..reconcile.rig import Rig
from ..sessions.kit import session_id
from .kit import context_json
from .test_restore import TS, forge, start
from .test_restore_wait import ckit_with_forge, follow_up

FAILED = "continued on a VM of another device because the device it ran on failed"
RESTARTED = "continued on this device after it was restarted following a failure"


def test_a_successor_after_a_device_failure_restores_the_pinned_checkpoint_without_waiting(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit_with_forge(env)
        pinned = forge(ck)  # the lost session never checkpointed: the hub pinned the one of session 1
        lost = session_id(30)  # ran on the failed device
        follow_up(ck, 2, lost, pinned, ref_session=session_id(1))
        desired = dict(ck.pk.kit.session_env.desired or {})
        desired["failovers"] = [
            "x",
            {"session_id": session_id(9), "from_session": lost, "cancelled_runs": 0},
            {"session_id": session_id(2), "from_session": lost, "cancelled_runs": 2},
        ]
        ck.pk.kit.session_env.desired = desired
        request = await start(ck)
        note = request["restore"]["note"]
        assert request["restore"]["object_id"] == pinned
        assert HUB_STEP_S not in env.clock.sleeps  # nothing was waited for
        assert not any("context_wait" in line for line in ck.lines)
        # the checkpoint was written by this device: the successor runs on the device that was restarted (section 3.7)
        assert note.startswith(f"This is your own earlier conversation of this task, {RESTARTED}, from its checkpoint")
        assert FAILED not in note
        assert f"from its checkpoint of {TS};" in note
        assert "2 test runs that were running there were cancelled" in note
        assert note.endswith(WORK_VM_TEXT)
        assert STALE_TEXT not in note
        assert any("context_restored" in line and "stale=false failover=true" in line for line in ck.lines)

    run(rig, body)


def test_a_failover_entry_of_another_source_is_not_a_failover(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit_with_forge(env)
        pinned = forge(ck)
        source = session_id(31)
        ck.contexts.wait_s = 10.0
        follow_up(ck, 2, source, pinned, ref_session=session_id(1))
        desired = dict(ck.pk.kit.session_env.desired or {})
        desired["failovers"] = [{"session_id": session_id(2), "from_session": session_id(32), "cancelled_runs": 1}]
        ck.pk.kit.session_env.desired = desired
        ck.hub.answers["/v1/device/state"] = [desired] * 5
        note = (await start(ck))["restore"]["note"]
        assert FAILED not in note
        assert RESTARTED not in note
        assert note.endswith(STALE_TEXT)  # it waited for the final checkpoint of its source as before
        assert HUB_STEP_S in env.clock.sleeps
        assert any("context_restored" in line and "failover=false" in line for line in ck.lines)

    run(rig, body)


@pytest.mark.parametrize(
    ("desired", "expected"),
    [
        (None, None),
        ({}, None),
        ({"failovers": "x"}, None),
        ({"failovers": [{"session_id": "s", "from_session": 1, "cancelled_runs": 0}]}, None),
        ({"failovers": [{"session_id": "s", "from_session": "f", "cancelled_runs": -1}]}, None),
        ({"failovers": [{"session_id": "s", "from_session": "f", "cancelled_runs": True}]}, None),
        ({"failovers": [{"session_id": "s", "from_session": "f"}]}, None),
        ({"failovers": [{"session_id": "t", "from_session": "f", "cancelled_runs": 0}]}, None),
        (
            {"failovers": [{"session_id": "s", "from_session": "f", "cancelled_runs": 3}]},
            Failover(from_session="f", cancelled_runs=3),
        ),
    ],
)
def test_the_failover_entry_of_a_session(desired: Any, expected: Failover | None) -> None:
    assert failover_of(desired, "s") == expected


def test_the_failover_note_names_the_cancelled_runs_and_fits_the_contract() -> None:
    assert cancelled_text(0) == ""
    assert cancelled_text(1) == (
        " 1 test run that was running there was cancelled: start it again when you need its result."
    )
    assert cancelled_text(5).startswith(" 5 test runs that were running there were cancelled: start them again")
    context = archive_format.parse_context(json.dumps(context_json()).encode())
    worst = replace(context, workspace_omitted=10**9)  # the longest workspace text
    line = restored_line(
        Restored("o", worst, {"ts": "x" * 64}), where="other_device", stale=False, failover=Failover("f", 10**9)
    )
    assert FAILED in line
    for where in ("same_vm", "other_vm"):
        here = restored_line(Restored("o", worst, {"ts": "t"}), where=where, stale=False, failover=Failover("f", 0))
        assert RESTARTED in here
        assert FAILED not in here
    assert "Work after that checkpoint may be lost: the interrupted instruction is repeated next." in line
    assert 1 <= len(line) <= 4096  # `restore.note` of contracts/guest/sessions-create.request.json

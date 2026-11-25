"""U-RUN-vm golden switch (`vm.golden_switch`, ADR-093, KOR-70): an outdated workgroup VM moves to the newest golden
once every session on it is a paused main session with a complete checkpoint; anything else holds it, visibly, and
nothing is deleted before the new VM runs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.reconcile.engine import Run
from agent_runner.vm import reconcile as vm_reconcile
from agent_runner.vm import records, tasks
from agent_runner.vm.extensions import Retire, RetireHook, retire_hooks
from agent_runner.vm.golden_switch import GoldenSwitch, judge, retry_delay, vm_ids
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.records import VmRow

from .guests import GOLDEN, ORG, WG_A, WG_B, Harness
from .test_lifecycle import settings, stop_vm_of


class Checkpoints:
    """A retire hook whose `checkpoints` answers `reasons` (session -> reason, missing: vouched) and whose `flush`
    applies `after_flush` (the final checkpoints of a stop)."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.reasons: dict[str, str | None] = {}
        self.after_flush: dict[str, str | None] = {}
        self.flushed: list[tuple[str, str]] = []
        self.asked: list[list[str]] = []

    async def flush(self, _context: Any, row: VmRow) -> None:
        state = self.harness.sql("SELECT state FROM runner.vm WHERE id = %s", (row.id,))[0]["state"]
        self.flushed.append((row.id, state))
        self.reasons.update(self.after_flush)

    async def pending(self, _context: Any, _row: VmRow) -> bool:
        return False

    async def checkpoints(self, _context: Any, _row: VmRow, session_ids: list[str]) -> Mapping[str, str | None]:
        self.asked.append(list(session_ids))
        return {sid: self.reasons.get(sid) for sid in session_ids}

    def retire(self) -> Retire:
        return Retire("ctx", [RetireHook("test.ckpt", self.flush, self.pending, self.checkpoints)])


def outdated_stopped(harness: Harness, wg: str = WG_A) -> VmRow:
    """A stopped VM of the workgroup on golden `g1` while `g2` is the newest ready golden."""
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped: VmRow = stop_vm_of(harness, wg)
    harness.add_golden("g2", minute=5)
    return stopped


def run_pass(harness: Harness, hook: Checkpoints, *, hub: bool = True, before: Any = None) -> Any:
    async def body(manager: VmManager) -> Any:
        manager.retire = hook.retire()
        manager.golden_switch.hub_reads_status = hub
        if before is not None:
            await before(manager)
        return await manager.golden_switch.run_pass()

    return harness.run(body)


def failing_starts(harness: Harness) -> list[bool]:
    """Every `limactl start` fails while the returned flag is set (a new VM never becomes ready)."""
    failing = [True]

    def broken(argv: list[str]) -> None:
        if argv[0] == "start" and failing[0]:
            harness.fake.fail["start"] = (1, "", "boom\n")

    harness.fake.hook = broken
    return failing


def upgrades(harness: Harness, vm_id: str) -> list[Any]:
    return [event["upgrade"] for event in harness.events if event["vm_id"] == vm_id and "upgrade" in event]


# -- the switch ---------------------------------------------------------------------------------------------------


def test_a_stopped_vm_with_paused_checkpointed_sessions_moves_and_the_new_vm_runs_before_the_old_goes(
    harness: Harness,
) -> None:
    old = outdated_stopped(harness)
    first = harness.session(old.id, WG_A, "suspended")
    second = harness.session(old.id, WG_A, "suspended")
    harness.session(old.id, WG_A, "stopped")  # ended sessions never hold
    hook = Checkpoints(harness)
    order: list[str] = []
    harness.fake.hook = lambda argv: order.append(argv[0]) if argv[0] in ("clone", "delete") else None

    assert run_pass(harness, hook) == [old.id]

    live = harness.live()
    assert [(row["golden_version"], row["state"]) for row in live] == [("g2", "running")]
    new_id = live[0]["id"]
    assert order.index("clone") < order.index("delete")  # nothing deleted before the new VM exists
    assert harness.sql("SELECT old_vm_id, new_vm_id, golden_version FROM runner.golden_switch") == [
        {"old_vm_id": old.id, "new_vm_id": new_id, "golden_version": "g2"}
    ]
    deleted = [event for event in harness.events if event["vm_id"] == old.id and event["state"] == "deleted"]
    assert deleted[-1]["replaced_by"] == new_id
    switching = upgrades(harness, old.id)[-1]
    assert switching["state"] == "switching"
    assert switching["golden_version"] == "g2"
    assert switching["golden_built_at"] == "2026-11-21T12:05:00.000Z"
    assert switching["holds"] == []
    assert switching["sessions"] == sorted([first, second])
    assert ("vm.golden_switched", {"vm_id": old.id, "new_vm_id": new_id, "golden_version": "g2", "sessions": 2}) in (
        harness.logs
    )
    # the sessions keep their VM: their next prompt restores them elsewhere
    assert {row["vm_id"] for row in harness.sql("SELECT vm_id FROM runner.session")} == {old.id}
    assert harness.sql("SELECT * FROM runner.golden_switch_hold") == []


def test_a_running_vm_is_stopped_with_its_final_checkpoints_first(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row: VmRow = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A))
    harness.add_golden("g2", minute=5)
    paused = harness.session(row.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.reasons[paused] = "checkpoint_due"  # the stop takes it
    hook.after_flush[paused] = None

    assert run_pass(harness, hook) == [row.id]
    assert hook.flushed == [(row.id, "running")]
    assert [item["golden_version"] for item in harness.live()] == ["g2"]


def test_a_hold_found_after_the_final_checkpoints_leaves_the_vm_stopped_as_it_is(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row: VmRow = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A))
    harness.add_golden("g2", minute=5)
    paused = harness.session(row.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.after_flush[paused] = "workspace_incomplete"

    assert run_pass(harness, hook) == []
    assert [(item["id"], item["state"], item["golden_version"]) for item in harness.live()] == [
        (row.id, "stopped", GOLDEN)
    ]
    status = upgrades(harness, row.id)[-1]
    assert status["state"] == "waiting"
    assert status["holds"] == [
        {"reason": "workspace_incomplete", "session_id": paused, "since": "2026-11-21T12:00:00.000Z"}
    ]


@pytest.mark.parametrize(
    ("state", "depth", "reason"),
    [("working", 0, "session_active"), ("idle", 0, "session_active"), ("suspended", 1, "subagent")],
)
def test_sessions_that_are_not_paused_main_sessions_hold_the_vm(
    harness: Harness, state: str, depth: int, reason: str
) -> None:
    old = outdated_stopped(harness)
    holder = harness.session(old.id, WG_A, state)
    harness.sql("UPDATE runner.session SET depth = %s WHERE id = %s", (depth, holder))
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    calls = len(harness.fake.ops())

    assert run_pass(harness, hook) == []
    assert [item["id"] for item in harness.live()] == [old.id]
    status = upgrades(harness, old.id)[-1]
    assert status["holds"] == [{"reason": reason, "session_id": holder, "since": "2026-11-21T12:00:00.000Z"}]
    assert status["sessions"] == [paused]
    assert "start" not in harness.fake.ops()[calls:]  # a held VM is never started for a refresh


def test_the_status_is_sent_once_per_change_and_since_survives_a_restart(harness: Harness) -> None:
    old = outdated_stopped(harness)
    holder = harness.session(old.id, WG_A, "working")
    hook = Checkpoints(harness)
    run_pass(harness, hook)
    harness.now += timedelta(minutes=7)
    run_pass(harness, hook)  # a new daemon: it sends the status again, with the time first seen
    statuses = upgrades(harness, old.id)
    assert len(statuses) == 2
    assert statuses[0] == statuses[1]
    assert statuses[1]["holds"][0]["since"] == "2026-11-21T12:00:00.000Z"

    async def twice(manager: VmManager) -> None:
        manager.retire = hook.retire()
        manager.golden_switch.hub_reads_status = True
        await manager.golden_switch.run_pass()
        await manager.golden_switch.run_pass()

    harness.run(twice)
    assert len(upgrades(harness, old.id)) == 3  # one per daemon, nothing new within one
    harness.sql("UPDATE runner.session SET state = 'stopped' WHERE id = %s", (holder,))
    run_pass(harness, hook)
    assert harness.sql("SELECT * FROM runner.golden_switch_hold") == []


def test_an_older_hub_gets_neither_status_nor_successor(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    assert run_pass(harness, Checkpoints(harness), hub=False) == [old.id]
    assert upgrades(harness, old.id) == []
    assert all("replaced_by" not in event for event in harness.events)


def test_a_vm_that_is_current_again_reports_null(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "working")
    hook = Checkpoints(harness)

    async def body(manager: VmManager) -> None:
        manager.retire = hook.retire()
        manager.golden_switch.hub_reads_status = True
        await manager.golden_switch.run_pass()
        harness.sql("UPDATE runner.golden SET state = 'retired' WHERE version = 'g2'")
        await manager.golden_switch.run_pass()
        await manager.golden_switch.run_pass()

    harness.run(body)
    assert [None if status is None else status["state"] for status in upgrades(harness, old.id)] == ["waiting", None]


def test_without_a_ready_golden_nothing_moves(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    assert run_pass(harness, Checkpoints(harness)) == []
    assert [item["id"] for item in harness.live()] == [stopped.id]
    assert upgrades(harness, stopped.id) == []


# -- refresh, capacity, failures and requests ---------------------------------------------------------------------


def test_a_stopped_vm_with_an_unknown_checkpoint_is_started_to_take_a_fresh_one(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.reasons[paused] = "checkpoint_missing"
    hook.after_flush[paused] = None

    assert run_pass(harness, hook) == [old.id]
    assert ("vm.golden_refresh", {"vm_id": old.id, "attempt": 1}) in harness.logs
    assert hook.flushed == [(old.id, "running")]


def test_a_refresh_that_does_not_help_waits_for_its_backoff(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.reasons[paused] = "checkpoint_missing"

    async def body(manager: VmManager) -> list[str]:
        manager.retire = hook.retire()
        manager.golden_switch.hub_reads_status = True
        await manager.golden_switch.run_pass()  # started, flushed, still missing: stopped again
        await manager.golden_switch.run_pass()  # within the backoff: nothing
        harness.now += timedelta(seconds=retry_delay(1) + 1)
        return await manager.golden_switch.run_pass()  # the second attempt

    assert harness.run(body) == []
    assert [line for line in harness.logs if line[0] == "vm.golden_refresh"] == [
        ("vm.golden_refresh", {"vm_id": old.id, "attempt": 1}),
        ("vm.golden_refresh", {"vm_id": old.id, "attempt": 2}),
    ]
    assert [item["state"] for item in harness.live()] == ["stopped"]


def test_no_other_workgroups_vm_is_stopped_for_a_refresh(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    harness.physical_mib = 32768 + 8192  # one slot

    async def other(manager: VmManager) -> None:
        await manager.ensure_vm(ORG, WG_B)  # takes the only slot, idle at once
        harness.now += timedelta(hours=2)

    hook = Checkpoints(harness)
    hook.reasons[paused] = "checkpoint_due"
    assert run_pass(harness, hook, before=other) == []
    status = upgrades(harness, old.id)[-1]
    assert [hold["reason"] for hold in status["holds"]] == ["checkpoint_due", "capacity"]
    assert sorted(item["state"] for item in harness.live()) == ["running", "stopped"]


def test_no_free_slot_for_the_new_vm_is_capacity_and_deletes_nothing(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    harness.physical_mib = 32768 + 8192

    async def other(manager: VmManager) -> None:
        await manager.ensure_vm(ORG, WG_B)
        harness.now += timedelta(hours=2)

    assert run_pass(harness, Checkpoints(harness), before=other) == []
    assert old.id in [item["id"] for item in harness.live()]
    assert [hold["reason"] for hold in upgrades(harness, old.id)[-1]["holds"]] == ["capacity"]
    assert ("vm.golden_switch_failed", {"vm_id": old.id, "reason": "capacity", "code": "capacity_exhausted",
                                        "attempt": 1}) in harness.logs  # fmt: skip


def test_a_failed_clone_keeps_the_old_vm_and_a_request_tries_again_at_once(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    failing = failing_starts(harness)

    async def body(manager: VmManager) -> tuple[list[str], list[str], list[str]]:
        manager.retire = hook.retire()
        switch = manager.golden_switch
        switch.hub_reads_status = True
        failed = await switch.run_pass()
        waiting = await switch.run_pass()  # within the backoff
        failing[0] = False
        switch.request([old.id, old.id])
        return failed, waiting, await switch.run_pass()

    failed, waiting, again = harness.run(body)
    assert (failed, waiting, again) == ([], [], [old.id])
    statuses = upgrades(harness, old.id)
    assert {"reason": "switch_failed", "since": "2026-11-21T12:00:00.000Z"} in statuses[-2]["holds"]
    assert [line[1]["reason"] for line in harness.logs if line[0] == "vm.golden_switch_failed"] == ["switch_failed"]


def test_requested_vms_go_first_and_a_request_no_longer_listed_ends(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first: VmRow = stop_vm_of(harness, WG_A)
    second: VmRow = stop_vm_of(harness, WG_B)
    harness.add_golden("g2", minute=5)
    order: list[str] = []

    async def body(manager: VmManager) -> list[str]:
        manager.retire = Checkpoints(harness).retire()
        switch = manager.golden_switch
        switch.request([max(first.id, second.id)])
        assert switch.requested == [max(first.id, second.id)]
        switch.request([])
        assert switch.requested == []
        switch.request([max(first.id, second.id)])
        found = await switch.run_pass()
        order.extend(found)
        return found

    assert sorted(harness.run(body)) == sorted([first.id, second.id])
    assert order[0] == max(first.id, second.id)


def test_a_vm_that_changed_meanwhile_and_a_stopping_daemon_are_left_alone(harness: Harness) -> None:
    old = outdated_stopped(harness)

    async def changed(manager: VmManager) -> bool:
        manager.retire = Checkpoints(harness).retire()
        golden = await records.ready_golden(manager.env.db)
        assert golden is not None
        harness.sql("UPDATE runner.vm SET state = 'stopping' WHERE id = %s", (old.id,))
        return await manager.golden_switch._visit(old, golden)

    assert harness.run(changed) is False

    async def stopping(manager: VmManager) -> list[str]:
        harness.sql("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (old.id,))
        manager.env.stopping = lambda: True
        return await manager.golden_switch.run_pass()

    assert harness.run(stopping) == []
    assert [item["id"] for item in harness.live()] == [old.id]


def test_a_lima_failure_while_stopping_is_a_failed_switch(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row: VmRow = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A))
    harness.add_golden("g2", minute=5)
    harness.session(row.id, WG_A, "suspended")
    harness.fake.fail["stop"] = (1, "", "stop failed\n")
    assert run_pass(harness, Checkpoints(harness)) == []
    assert [item["state"] for item in harness.live()] == ["running"]
    assert [line[1]["reason"] for line in harness.logs if line[0] == "vm.golden_switch_failed"] == ["switch_failed"]


def test_an_unexpected_refusal_of_a_refresh_or_a_clone_is_a_failed_switch(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)

    async def refused(*_args: Any, **_kwargs: Any) -> VmRow:
        raise RunnerError("vm_start_failed")

    async def body(manager: VmManager) -> list[str]:
        manager.retire = hook.retire()
        manager.golden_switch.hub_reads_status = True
        manager.replacement = refused  # type: ignore[method-assign]
        manager.restart_for_switch = refused  # type: ignore[method-assign]
        found = await manager.golden_switch.run_pass()  # the clone is refused
        hook.reasons[paused] = "checkpoint_due"
        manager.golden_switch.request([old.id])
        return found + await manager.golden_switch.run_pass()  # the refresh is refused

    assert harness.run(body) == []
    assert [line[1]["code"] for line in harness.logs if line[0] == "vm.golden_switch_failed"] == [
        "vm_start_failed",
        "vm_start_failed",
    ]


# -- the start path (05-runner.md section 3.3 level 3) ------------------------------------------------------------


def test_a_start_replaces_a_stopped_vm_whose_paused_sessions_are_checkpointed(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)

    async def body(manager: VmManager) -> VmRow:
        manager.retire = hook.retire()
        return await manager.ensure_vm(ORG, WG_A)

    row = harness.run(body)
    assert row.golden_version == "g2"
    assert harness.sql("SELECT old_vm_id FROM runner.golden_switch") == [{"old_vm_id": old.id}]


def test_a_start_restarts_a_stopped_vm_whose_checkpoint_is_incomplete(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.reasons[paused] = "workspace_incomplete"

    async def body(manager: VmManager) -> VmRow:
        manager.retire = hook.retire()
        return await manager.ensure_vm(ORG, WG_A)

    assert harness.run(body).id == old.id


def test_a_failed_clone_at_a_start_keeps_the_old_vm(harness: Harness) -> None:
    old = outdated_stopped(harness)
    failing_starts(harness)

    async def body(manager: VmManager) -> None:
        manager.retire = Checkpoints(harness).retire()
        await manager.ensure_vm(ORG, WG_A)

    with pytest.raises(RunnerError):
        harness.run(body)  # the new VM does not start, then the old one (restarted instead) does not either
    assert [item["id"] for item in harness.live()] == [old.id]
    assert ("vm.golden_replace_failed", {"vm_id": old.id, "code": "vm_start_failed"}) in harness.logs


def test_a_failed_delete_reuses_the_recorded_successor(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    harness.fake.fail["delete"] = (1, "", "delete failed\n")

    async def body(manager: VmManager) -> tuple[list[str], list[str]]:
        manager.retire = hook.retire()
        switch = manager.golden_switch
        first = await switch.run_pass()
        [successor] = harness.sql("SELECT new_vm_id FROM runner.golden_switch")
        await manager.stop_vm(successor["new_vm_id"])  # it went idle meanwhile: it is started again, not cloned
        switch.request([old.id])
        return first, await switch.run_pass()

    first, second = harness.run(body)
    assert (first, second) == ([], [old.id])
    assert harness.fake.ops().count("clone") == 2  # the first VM of the workgroup and one successor, not two
    [switch_row] = harness.sql("SELECT new_vm_id FROM runner.golden_switch")
    assert [item["id"] for item in harness.live()] == [switch_row["new_vm_id"]]


def test_refreshes_end_after_a_few_attempts_until_a_request(harness: Harness) -> None:
    old = outdated_stopped(harness)
    paused = harness.session(old.id, WG_A, "suspended")
    hook = Checkpoints(harness)
    hook.reasons[paused] = "checkpoint_missing"  # e.g. dropped again and again

    async def body(manager: VmManager) -> None:
        manager.retire = hook.retire()
        for _ in range(6):
            await manager.golden_switch.run_pass()
            harness.now += timedelta(hours=2)
        manager.golden_switch.request([old.id])
        await manager.golden_switch.run_pass()

    harness.run(body)
    attempts = [line[1]["attempt"] for line in harness.logs if line[0] == "vm.golden_refresh"]
    assert attempts == [1, 2, 3, 1]


# -- judge, the retire extension and helpers ----------------------------------------------------------------------


def test_judge_lists_holds_in_session_order(harness: Harness) -> None:
    old = outdated_stopped(harness)
    sessions = sorted(harness.session(old.id, WG_A, "suspended") for _ in range(3))
    hook = Checkpoints(harness)
    hook.reasons.update({sessions[0]: "checkpoint_due", sessions[2]: "checkpoint_missing"})

    async def body(manager: VmManager) -> Any:
        return await judge(manager.env, hook.retire(), old)

    verdict = harness.run(body)
    assert [(hold.reason, hold.session_id) for hold in verdict.holds] == [
        ("checkpoint_due", sessions[0]),
        ("checkpoint_missing", sessions[2]),
    ]
    assert verdict.sessions == tuple(sessions)


def test_retire_checkpoints_combines_hooks() -> None:
    logs: list[tuple[str, dict[str, Any]]] = []

    def hook(name: str, answer: Any) -> RetireHook:
        async def checkpoints(_context: Any, _row: Any, _ids: list[str]) -> Mapping[str, str | None]:
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)

        async def flush(_context: Any, _row: Any) -> None:
            return None

        async def pending(_context: Any, _row: Any) -> bool:
            return False

        return RetireHook(name, flush, pending, checkpoints)

    row = VmRow(**{**_row_fields(), "id": "vm-0000000a"})
    hooks = [
        RetireHook("a.none", hook("x", {}).flush, hook("x", {}).pending),
        hook("b.boom", RuntimeError("boom")),
        hook("c.first", {"s1": "checkpoint_due", "s2": "checkpoint_due"}),
        hook("d.second", {"s1": None, "s2": "workspace_incomplete"}),
    ]
    retire = Retire("ctx", hooks, lambda name, fields: logs.append((name, fields)))
    found = asyncio.run(retire.checkpoints(row, ["s1", "s2", "s3"]))
    assert found == {"s1": None, "s2": "checkpoint_due", "s3": "checkpoint_missing"}
    assert logs == [("vm.retire_failed", {"vm_id": "vm-0000000a", "hook": "b.boom", "error": "RuntimeError"})]
    assert asyncio.run(Retire().checkpoints(row, ["s1"])) == {"s1": "checkpoint_missing"}


def _row_fields() -> dict[str, Any]:
    return {
        "id": "vm-00000001",
        "lima_name": "x",
        "golden_version": "g1",
        "wg_id": WG_A,
        "org_id": ORG,
        "state": "stopped",
        "control_token_hash": "0" * 64,
        "cpus": 4,
        "memory_mib": 8192,
        "created_at": datetime(2026, 11, 21, tzinfo=UTC),
        "assigned_at": None,
        "last_session_at": None,
    }


def test_a_non_callable_checkpoints_hook_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def flush(_context: Any, _row: Any) -> None:
        return None

    async def pending(_context: Any, _row: Any) -> bool:
        return False

    bad = RetireHook("x.bad", flush, pending, "nope")  # type: ignore[arg-type]
    monkeypatch.setattr("agent_runner.vm.extensions.registry.discover", lambda *_args: [("x", [bad])])
    with pytest.raises(RunnerError, match="registry_invalid"):
        retire_hooks()


def test_the_contexts_area_registers_its_checkpoint_judgement() -> None:
    (hook,) = [item for item in retire_hooks() if item.name == "contexts.checkpoint"]
    assert hook.checkpoints is not None


def test_helpers() -> None:
    assert vm_ids(["vm-0000000a", "vm-x", 3, "vm-0000000b"]) == ["vm-0000000a", "vm-0000000b"]
    assert vm_ids({"vm-0000000a": 1}) == []
    assert [retry_delay(count) for count in (1, 2, 3, 5, 20)] == [300.0, 600.0, 1200.0, 3600.0, 3600.0]


# -- the reconcile step `vm_switch` -------------------------------------------------------------------------------


class _Woken:
    def __init__(self) -> None:
        self.woken = 0
        self.golden_switch = GoldenSwitch(self)  # type: ignore[arg-type]

    def wake_refill(self) -> None:
        self.woken += 1


def test_the_reconcile_step_hands_requests_to_the_switch() -> None:
    manager = _Woken()
    context = SimpleNamespace(**{tasks.MANAGER_KEY: manager})

    def run(desired: dict[str, Any] | None) -> None:
        asyncio.run(vm_reconcile.step(Run(context=context, sync=None, book=None, desired=desired)))  # type: ignore[arg-type]

    run(None)  # no desired state (a device pending certification): nothing changes
    assert (manager.golden_switch.hub_reads_status, manager.golden_switch.requested) == (False, [])
    run({"sessions": []})  # an older hub
    assert (manager.golden_switch.hub_reads_status, manager.woken) == (False, 0)
    run({"vm_switch": ["vm-0000000a", "bad"]})
    assert manager.golden_switch.hub_reads_status is True
    assert manager.golden_switch.requested == ["vm-0000000a"]
    assert manager.woken == 1
    assert [(step.order, step.name) for step in vm_reconcile.STEPS] == [(85, "vm_switch")]


def test_a_failed_delete_reuses_a_running_successor(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "suspended")
    harness.fake.fail["delete"] = (1, "", "delete failed\n")

    async def body(manager: VmManager) -> list[str]:
        manager.retire = Checkpoints(harness).retire()
        await manager.golden_switch.run_pass()
        manager.golden_switch.request([old.id])
        return await manager.golden_switch.run_pass()

    assert harness.run(body) == [old.id]
    assert harness.fake.ops().count("clone") == 2


def test_a_vm_gone_meanwhile_is_forgotten_and_a_failure_on_a_deleted_vm_is_not_reported(harness: Harness) -> None:
    old = outdated_stopped(harness)
    harness.session(old.id, WG_A, "working")
    hook = Checkpoints(harness)

    async def body(manager: VmManager) -> None:
        manager.retire = hook.retire()
        switch = manager.golden_switch
        switch.hub_reads_status = True
        await switch.run_pass()  # reported: held
        harness.sql("UPDATE runner.vm SET state = 'deleted' WHERE id = %s", (old.id,))
        await switch.run_pass()  # no longer live: forgotten
        assert old.id not in switch._reported
        harness.sql("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (old.id,))
        harness.sql("UPDATE runner.session SET state = 'suspended'")

        async def gone(*_args: Any, **_kwargs: Any) -> VmRow:
            harness.sql("UPDATE runner.vm SET state = 'deleted' WHERE id = %s", (old.id,))
            raise RunnerError("vm_start_failed")

        manager.replacement = gone  # type: ignore[method-assign]
        sent = len(upgrades(harness, old.id))
        await switch.run_pass()
        # the failure is logged, but nothing is reported for a VM that is gone
        assert [s["state"] for s in upgrades(harness, old.id)[sent:]] == ["switching"]

    harness.run(body)
    assert harness.sql("SELECT * FROM runner.golden_switch_hold WHERE vm_id = %s", (old.id,)) == []
    assert [line[1]["code"] for line in harness.logs if line[0] == "vm.golden_switch_failed"] == ["vm_start_failed"]

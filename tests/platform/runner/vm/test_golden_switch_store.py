"""I-RUN-vm golden switch with the context store's judgement (HOT-24, ADR-093 addendum): the switch of
`vm.golden_switch` over the runner's PostgreSQL, judged by the real `contexts.store` rows - a paused session that
never worked holds nothing, one that can never be checkpointed holds its VM across restarts until a member asks, and
the counters of the switch survive restarts.

The retire hook here answers with the store functions of the contexts area on the harness's own store (the area's
`vm_retire` needs an enrolled device); the export of a guest without the route (`not_found`) and its outcome are
covered with the real `Checkpointer` in `contexts/test_golden_switch_checkpoints.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from agent_runner.contexts import store
from agent_runner.vm.extensions import Retire, RetireHook
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.records import VmRow

from .guests import NOW, ORG, WG_A, Harness
from .test_golden_switch import outdated_stopped, upgrades


class StoreHook:
    """`contexts.checkpoint` of the store: `checkpoints`, `fallbacks`, `pending` from `contexts.store`; `flush` makes
    the sessions due like `Checkpointer.flush_vm` and ends every request like a guest without the export route."""

    def __init__(self) -> None:
        self.flushed: list[str] = []

    async def flush(self, db: Any, row: VmRow) -> None:
        self.flushed.append(row.id)
        async with db.connection() as conn:
            await store.mark_vm_due(conn, row.id, NOW)
            for due in await store.due_on_vm(conn, row.id):
                await store.drop_due(
                    conn,
                    due.session_id,
                    due_at=due.due_at,
                    at=NOW,
                    outcome="impossible",
                    reason="export_refused:not_found",
                )

    async def pending(self, db: Any, row: VmRow) -> bool:
        async with db.connection() as conn:
            return bool(await store.due_on_vm(conn, row.id, worked=True))

    async def checkpoints(self, db: Any, _row: VmRow, ids: list[str]) -> Mapping[str, str | None]:
        async with db.connection() as conn:
            return await store.checkpoint_holds(conn, ids)

    async def fallbacks(self, db: Any, _row: VmRow, ids: list[str]) -> Mapping[str, datetime | None]:
        async with db.connection() as conn:
            return await store.fallback_times(conn, ids)

    def install(self, manager: VmManager, *, hub: bool = True) -> None:
        hook = RetireHook("contexts.checkpoint", self.flush, self.pending, self.checkpoints, self.fallbacks)
        manager.retire = Retire(manager.env.db, [hook])
        manager.golden_switch.hub_reads_status = manager.golden_switch.hub_reads_impossible = hub


def context_row(harness: Harness, session: str, **columns: Any) -> None:
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    harness.sql(
        f"INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, {names})"
        f" SELECT id, task_id, org_id, wg_id, {marks} FROM runner.session WHERE id = %s",
        (*columns.values(), session),
    )


def accept(harness: Harness, session: str) -> None:
    harness.sql(
        "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'accepted')",
        (str(uuid.uuid4()), session),
    )


def switch_pass(harness: Harness, hook: StoreHook, *, requested: dict[str, datetime] | None = None) -> list[str]:
    """One pass of a new daemon (a runner restart)."""

    async def body(manager: VmManager) -> list[str]:
        hook.install(manager)
        if requested is not None:
            await manager.golden_switch.request(list(requested), requested)
        return await manager.golden_switch.run_pass()

    return harness.run(body)


def test_a_stopped_vm_whose_paused_session_never_worked_moves_in_the_next_pass(harness: Harness) -> None:
    """The VM of the operation's private organisation: one paused main session that never worked, every export of
    it dropped (`not_found` before this package), its checkpoint still due."""
    old = outdated_stopped(harness)
    never = harness.session(old.id, WG_A, "suspended")
    context_row(harness, never, checkpoint_state="dropped", due_at=NOW)
    unknown = harness.session(old.id, WG_A, "suspended")  # never even reached the context area
    hook = StoreHook()

    assert switch_pass(harness, hook) == [old.id]
    [new] = harness.live()
    assert new["golden_version"] == "g2"
    assert hook.flushed == []  # nothing to save: never started for it
    switched = upgrades(harness, old.id)[-1]
    assert (switched["state"], switched["holds"], switched["sessions"]) == ("switching", [], sorted([never, unknown]))


def test_a_start_replaces_a_stopped_vm_whose_due_session_never_worked(harness: Harness) -> None:
    old = outdated_stopped(harness)
    never = harness.session(old.id, WG_A, "suspended")
    context_row(harness, never, due_at=NOW)

    async def start(manager: VmManager) -> VmRow:
        StoreHook().install(manager)
        return await manager.ensure_vm(ORG, WG_A)

    assert harness.run(start).golden_version == "g2"
    assert [item["golden_version"] for item in harness.live()] == ["g2"]


def test_a_vm_with_a_never_savable_session_stays_across_restarts_until_a_member_asks(harness: Harness) -> None:
    old = outdated_stopped(harness)
    worked = harness.session(old.id, WG_A, "suspended")
    accept(harness, worked)
    hook = StoreHook()
    # before this package: dropped again and again - the bounded refreshes find out it is never savable
    context_row(harness, worked, checkpoint_state="dropped")
    calls = len(harness.fake.ops())
    for _ in range(4):  # four releases, hours apart
        assert switch_pass(harness, hook) == []
        harness.now += timedelta(hours=2)
    assert hook.flushed == [old.id]  # started once: the export made it `impossible`
    assert harness.fake.ops()[calls:].count("start") == 1
    assert harness.sql(
        "SELECT checkpoint_state, impossible_reason, impossible_turns, due_at FROM runner.context_session"
    ) == [
        {
            "checkpoint_state": "impossible",
            "impossible_reason": "export_refused:not_found",
            "impossible_turns": 1,
            "due_at": None,
        }
    ]
    status = upgrades(harness, old.id)[-1]
    assert status["holds"] == [
        {"reason": "checkpoint_impossible", "session_id": worked, "since": "2026-11-21T12:00:00.000Z"}
    ]  # no checkpoint to continue from: it would start fresh
    assert [item["id"] for item in harness.live()] == [old.id]
    # the switch by hand moves it; the session keeps its VM ID and continues elsewhere with its next prompt
    assert switch_pass(harness, hook, requested={old.id: harness.now}) == [old.id]
    assert [item["golden_version"] for item in harness.live()] == ["g2"]
    assert harness.sql("SELECT vm_id FROM runner.session") == [{"vm_id": old.id}]
    assert harness.sql("SELECT * FROM runner.golden_switch_attempt") == []
    assert hook.flushed == [old.id]

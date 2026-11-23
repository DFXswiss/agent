"""`send_ping` and the reconcile step of the ping area (RUN-14; 05-runner.md sections 2.2 and 9, 03-api.md 6.1/7.1)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.pings import reconcile as ping_reconcile
from agent_runner.pings.send import (
    KINDS,
    PingInvalid,
    check,
    pinger_for,
    send_ping,
    stable_ping_id,
)
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run

from ..reconcile.rig import Rig
from ..sessions.kit import guest_unreachable, session_id
from .kit import ENTRY, TASK_OTHER, WG_OTHER, PKit, ping_id, run

BOOK: Any = None
UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def listed(n: int, sid: str, *, level: str = "task", kind: str = "whiteboard.entry") -> dict[str, Any]:
    """One entry of the `pings` section of `GET /v1/device/state`."""
    return {
        "ping_id": ping_id(n),
        "session_id": sid,
        "kind": kind,
        "level": level,
        "ref": ENTRY,
        "created_at": "2026-11-21T12:00:00.000Z",
    }


def test_stable_ping_ids_are_uuid7_and_stable(repo_root: Path) -> None:
    ref = session_id(3)
    first = stable_ping_id("subagent.finished", ref)
    assert first == stable_ping_id("subagent.finished", ref)
    assert first != stable_ping_id("operation.finished", ref)
    assert first != stable_ping_id("subagent.finished", session_id(4))
    pattern = json.loads((repo_root / "contracts" / "common.json").read_text(encoding="utf-8"))["$defs"]["uuid"][
        "pattern"
    ]
    for kind in KINDS:
        found = stable_ping_id(kind, str(uuid.uuid4()))
        assert re.fullmatch(pattern, found)
        assert UUID7.fullmatch(found)
    # the timestamp bits are the ones of the (UUIDv7) ref
    assert first[:13] == ref[:13]
    with pytest.raises(PingInvalid):
        stable_ping_id("operation.finished", "not-a-uuid")


@pytest.mark.parametrize(
    ("fields"),
    [
        ("whiteboard.entry", None),
        ("subagent.finished", "task"),
        ("whiteboard.entry", "team"),
        ("other", None),
    ],
)
def test_check_refuses_pings_of_the_wrong_shape(fields: tuple[str, str | None]) -> None:
    kind, level = fields
    with pytest.raises(PingInvalid):
        check(session_id(1), kind, level, ENTRY, ping_id(1))


def test_check_refuses_ids_that_are_no_uuids() -> None:
    for sid, ref, pid in (("x", ENTRY, ping_id(1)), (session_id(1), "x", ping_id(1)), (session_id(1), ENTRY, "x")):
        with pytest.raises(PingInvalid):
            check(sid, "whiteboard.entry", "org", ref, pid)
    check(session_id(1), "operation.finished", None, ENTRY, ping_id(1))


def test_send_ping_posts_once_and_is_idempotent(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        assert await send_ping(sid, "subagent.finished", None, session_id(9), ping_id(1), context=p.env.context)
        assert await send_ping(sid, "subagent.finished", None, session_id(9), ping_id(1), context=p.env.context)
        assert p.guest.pings == [(sid, {"ping_id": ping_id(1), "kind": "subagent.finished", "ref": session_id(9)})]
        assert await p.rows() == [(ping_id(1), sid, "subagent.finished", None, session_id(9), True, False)]
        # the same ID with other fields is refused and not sent
        assert not await send_ping(sid, "operation.finished", None, session_id(9), ping_id(1), context=p.env.context)
        assert any(f"id_conflict ping_id={ping_id(1)}" in line for line in p.lines)
        assert len(p.guest.pings) == 1
        # no prompt, no control call: the ping only goes to the pings endpoint
        assert p.guest.names() == ["ping"]

    run(rig, body)


def test_a_whiteboard_ping_carries_its_level(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        assert await send_ping(sid, "whiteboard.entry", "org", ENTRY, ping_id(2), context=p.env.context)
        assert p.guest.pings == [
            (sid, {"ping_id": ping_id(2), "kind": "whiteboard.entry", "level": "org", "ref": ENTRY})
        ]

    run(rig, body)


@pytest.mark.parametrize(
    ("state", "vm"),
    [("requested", True), ("vm_ready", True), ("stopping", True), ("stopped", True), ("failed", True), ("idle", False)],
)
def test_pings_wait_until_the_session_can_take_them(rig: Rig, state: str, vm: bool) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1, state=state, vm=vm)
        assert not await send_ping(sid, "operation.finished", None, ENTRY, ping_id(1), context=p.env.context)
        assert p.guest.pings == []
        assert await p.rows() == [(ping_id(1), sid, "operation.finished", None, ENTRY, False, False)]

    run(rig, body)


def test_unknown_sessions_and_stopped_vms_are_not_pinged(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        assert not await send_ping(session_id(7), "operation.finished", None, ENTRY, ping_id(1), context=p.env.context)
        sid = await p.session(1)
        async with p.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped'")
        assert not await send_ping(sid, "operation.finished", None, ENTRY, ping_id(2), context=p.env.context)
        assert p.guest.pings == []

    run(rig, body)


@pytest.mark.parametrize(
    ("failure", "line"),
    [
        (GuestRefused("pings", 409, "state_conflict"), "refused"),
        (guest_unreachable(), "send_failed"),
    ],
)
def test_guest_failures_leave_the_ping_unsent(rig: Rig, failure: Exception, line: str) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        p.guest.fail["ping"] = failure
        assert not await send_ping(sid, "operation.finished", None, ENTRY, ping_id(1), context=p.env.context)
        assert any(text.startswith(f"runner.pings {line} ping_id={ping_id(1)}") for text in p.lines)
        assert (await p.rows())[0][5] is False
        # the next reconcile run sends it
        del p.guest.fail["ping"]
        assert await p.pinger.flush() == 1
        assert (await p.rows())[0][5] is True

    run(rig, body)


def test_a_client_failure_leaves_the_ping_unsent(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        p.kit.vms.client_error = RunnerError("vm_unreachable", "x")
        assert not await send_ping(sid, "operation.finished", None, ENTRY, ping_id(1), context=p.env.context)
        assert any("send_failed" in text and "code=vm_unreachable" in text for text in p.lines)

    run(rig, body)


def test_send_ping_without_enrolment_sends_nothing() -> None:
    import asyncio  # noqa: PLC0415

    context = SimpleNamespace(identity=None)
    assert pinger_for(context) is None
    assert not asyncio.run(send_ping(session_id(1), "operation.finished", None, ENTRY, ping_id(1), context=context))


# -- reconcile step (scope) ------------------------------------------------------------------------------------------


def test_the_step_pings_only_active_sessions_of_this_device(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        working = await p.session(1)
        idle = await p.session(2, state="idle")
        stopping = await p.session(3, state="stopping")
        stopped = await p.session(4, state="stopped")
        starting = await p.session(5, state="vm_assigning", vm=False)
        foreign = session_id(6)  # not a session of this device
        desired = {
            "pings": [
                listed(1, working),
                listed(2, idle, level="org"),
                listed(3, stopping),
                listed(4, stopped),
                listed(5, starting),
                listed(6, foreign),
            ]
        }
        assert await ping_reconcile.apply_desired(p.pinger, desired) == 3
        assert [sid for sid, _ in p.guest.pings] == [working, idle]
        assert [(row[0], row[5]) for row in await p.rows()] == [
            (ping_id(1), True),
            (ping_id(2), True),
            (ping_id(5), False),
        ]
        for n, sid in ((3, stopping), (4, stopped), (6, foreign)):
            assert f"runner.pings out_of_scope ping_id={ping_id(n)} session_id={sid}" in p.lines
        # idempotent: the same list again sends nothing new
        await ping_reconcile.apply_desired(p.pinger, desired)
        assert len(p.guest.pings) == 2
        # the starting session got its sandbox: the next run sends its ping
        row = await p.vm()
        async with p.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.session SET state = 'sandbox_ready', vm_id = %s WHERE id = %s", (row.id, starting)
            )
        await ping_reconcile.apply_desired(p.pinger, {"pings": []})
        assert [sid for sid, _ in p.guest.pings] == [working, idle, starting]

    run(rig, body)


def test_malformed_entries_are_skipped(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        desired = {
            "pings": [
                "x",
                {**listed(1, sid), "session_id": "nope"},
                {**listed(2, sid), "kind": 5},
                {**listed(3, sid), "level": 5},
                {**listed(4, sid), "level": None},
                listed(5, sid),
            ]
        }
        assert await ping_reconcile.apply_desired(p.pinger, desired) == 1
        assert p.lines.count("runner.pings ping_invalid") == 5
        assert [ping["ping_id"] for _, ping in p.guest.pings] == [ping_id(5)]
        assert await ping_reconcile.apply_desired(p.pinger, {}) == 0
        with pytest.raises(wire.ResponseInvalid):
            await ping_reconcile.apply_desired(p.pinger, {"pings": {}})

    run(rig, body)


def test_a_conflicting_listed_ping_is_not_counted(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        assert await send_ping(sid, "operation.finished", None, ENTRY, ping_id(1), context=p.env.context)
        assert await ping_reconcile.apply_desired(p.pinger, {"pings": [listed(1, sid)]}) == 0

    run(rig, body)


def test_rows_of_long_ended_sessions_are_deleted(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        recent = await p.session(1, state="stopped", ended_ago_s=60)
        old = await p.session(2, state="failed", ended_ago_s=7200, wg_id=WG_OTHER, task_id=TASK_OTHER)
        for n, sid in ((1, recent), (2, old)):
            await send_ping(sid, "operation.finished", None, ENTRY, ping_id(n), context=p.env.context)
        async with p.env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.ping (id, session_id, kind, ref) VALUES (%s, %s, 'operation.finished', %s)",
                (ping_id(3), session_id(9), ENTRY),
            )
        assert await p.pinger.flush() == 0
        assert [row[0] for row in await p.rows()] == [ping_id(1)]

    run(rig, body)


def test_the_step_needs_a_desired_state_and_an_enrolled_device(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        await ping_reconcile.pings_step(Run(context=p.env.context, sync=p.env.sync, book=BOOK, desired=None))
        assert p.guest.pings == []
        run_ = Run(context=p.env.context, sync=p.env.sync, book=BOOK, desired={"pings": [listed(1, sid)]})
        await ping_reconcile.pings_step(run_)
        assert len(p.guest.pings) == 1
        enrolled_not = Run(context=SimpleNamespace(identity=None), sync=p.env.sync, book=BOOK, desired={"pings": []})
        await ping_reconcile.pings_step(enrolled_not)

    run(rig, body)

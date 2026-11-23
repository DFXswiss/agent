"""Journal `ping.delivered` -> outbox audit, the `ping` hint, the notifier registrations and the area's wiring (RUN-14;
03-api.md sections 6.1, 6.2 and 7.2; 05-runner.md sections 9, 12 and 13)."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.operations.extensions import finished_notifier as op_notifier
from agent_runner.pings import hints, journal, op_finished, reconcile, subagent_finished
from agent_runner.pings.send import pinger_for, stable_ping_id
from agent_runner.reconcile.engine import ATTRIBUTE as ENGINE_ATTRIBUTE
from agent_runner.reconcile.engine import Engine as RealEngine
from agent_runner.subagents.controller import ping_id as child_ping_id
from agent_runner.subagents.extensions import FinishedPing
from agent_runner.subagents.extensions import finished_notifier as subagent_notifier

from ..operations.test_wiring import configure
from ..reconcile.rig import Rig
from ..sessions.kit import ORG, WG, guest_unreachable, session_id
from .kit import ENTRY, TASK, TASK_OTHER, WG_OTHER, PKit, ping_id, run


def test_the_area_is_registered() -> None:
    assert registry.journal_handlers()["ping.delivered"] is journal.ping_delivered
    assert registry.hint_handlers()["ping"] is hints.ping
    assert ("pings", 70) in [(step.name, step.order) for step in registry.reconcile_steps()]
    assert reconcile.STEPS[0].run is reconcile.pings_step
    assert subagent_notifier() is subagent_finished.notify
    assert op_notifier() is op_finished.notify


# -- journal ping.delivered ------------------------------------------------------------------------------------------


def test_a_delivered_hub_ping_is_audited_once_and_marks_the_hub_row(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        await p.pinger.send(sid, "whiteboard.entry", "task", ENTRY, ping_id(1))
        await p.journal(
            [
                (sid, "ping.delivered", {"ping_id": ping_id(1), "via": "hook"}),
                (sid, "ping.delivered", {"ping_id": ping_id(1), "via": "tool"}),
            ]
        )
        audits = await p.env.audits()
        assert audits == [
            {
                "action": "ping.delivered",
                "result": "ok",
                "task_id": TASK,
                "session_id": sid,
                "target_kind": "session",
                "target_id": sid,
                "detail": {"ping_id": ping_id(1), "kind": "whiteboard.entry", "via": "hook"},
            }
        ]
        assert (await p.rows())[0][6] is True
        # the contract-checking hub accepts the item
        await p.env.sync.pusher.drain()
        assert [item["body"]["action"] for item in p.env.hub.items() if item["item_kind"] == "audit"] == [
            "ping.delivered"
        ]

    run(rig, body)


def test_runner_pings_are_audited_at_the_hub_too(rig: Rig) -> None:
    """05-runner.md section 9: every delivery is audited; the hub audits a runner ping without a row (KOR-07)."""

    async def body(p: PKit) -> None:
        sid = await p.session(1)
        await p.pinger.send(sid, "operation.finished", None, ENTRY, ping_id(2))
        await p.journal(
            [
                (sid, "ping.delivered", {"ping_id": ping_id(2), "via": "tool"}),
                (sid, "ping.delivered", {"ping_id": ping_id(2), "via": "hook"}),
            ]
        )
        assert [item["detail"] for item in await p.env.audits()] == [
            {"ping_id": ping_id(2), "kind": "operation.finished", "via": "tool"}
        ]
        assert (await p.rows())[0][6] is True
        await p.env.sync.pusher.drain()
        assert [item["body"]["action"] for item in p.env.hub.items() if item["item_kind"] == "audit"] == [
            "ping.delivered"
        ]
        assert f"runner.pings delivered ping_id={ping_id(2)} session_id={sid} kind=operation.finished via=tool" in (
            p.lines
        )

    run(rig, body)


def test_unknown_and_foreign_deliveries_change_nothing(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        other = await p.session(2, wg_id=WG_OTHER, task_id=TASK_OTHER)
        await p.pinger.send(sid, "whiteboard.entry", "org", ENTRY, ping_id(1))
        await p.pinger.send(other, "whiteboard.entry", "org", ENTRY, ping_id(2))
        await p.journal(
            [
                # a ping of another session of the same VM is not this session's
                (sid, "ping.delivered", {"ping_id": ping_id(2), "via": "hook"}),
                (sid, "ping.delivered", {"ping_id": ping_id(3), "via": "hook"}),
                # the session runs on another VM: this guest cannot speak for it
                (other, "ping.delivered", {"ping_id": ping_id(2), "via": "hook"}),
            ]
        )
        assert await p.env.audits() == []
        assert f"runner.pings delivered_unknown ping_id={ping_id(3)} session_id={sid}" in p.lines
        assert any("foreign_session" in line for line in p.lines)

    run(rig, body)


@pytest.mark.parametrize(
    "body",
    [
        {"ping_id": "01930007-0000-4000-8000-000000000001", "via": "hook"},
        {"ping_id": 5, "via": "hook"},
        {"ping_id": "01930007-0000-7000-8000-000000000001", "via": "push"},
        {"ping_id": "01930007-0000-7000-8000-000000000001", "via": "hook", "kind": "whiteboard.entry"},
    ],
)
def test_malformed_records_are_skipped(rig: Rig, body: dict[str, Any]) -> None:
    async def run_body(p: PKit) -> None:
        sid = await p.session(1)
        await p.pinger.send(sid, "whiteboard.entry", "org", ENTRY, ping_id(1))
        reader = await p.journal([(sid, "ping.delivered", body)])
        assert reader.skipped == 1
        assert await p.env.audits() == []

    run(rig, run_body)


# -- hint -----------------------------------------------------------------------------------------------------------


class Engine(RealEngine):
    def __init__(self) -> None:
        super().__init__(SimpleNamespace(), [])
        self.requested = 0

    def request(self) -> None:
        self.requested += 1


def test_a_hint_in_scope_wakes_the_reconcile(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        engine = Engine()
        setattr(p.env.context, ENGINE_ATTRIBUTE, engine)
        p.env.context.log = io.StringIO()
        await p.session(1)
        await p.session(2, state="stopped", wg_id=WG_OTHER, task_id=TASK_OTHER)
        hint_org = {"org_id": ORG, "level": "org", "entry_id": ENTRY}
        hint_wg = {"org_id": ORG, "wg_id": WG, "level": "workgroup", "entry_id": ENTRY}
        hint_task = {"org_id": ORG, "wg_id": WG, "task_id": TASK, "level": "task", "entry_id": ENTRY}
        for hint in (hint_org, hint_wg, hint_task):
            await hints.ping(p.env.context, hint)
        assert engine.requested == 3
        # only the stopped session is in these scopes: nothing to wake
        await hints.ping(p.env.context, {**hint_wg, "wg_id": WG_OTHER})
        await hints.ping(p.env.context, {**hint_task, "wg_id": WG_OTHER, "task_id": TASK_OTHER})
        assert engine.requested == 3
        log = p.env.context.log.getvalue()
        assert log.count("runner.pings hint level=") == 3
        assert "runner.pings hint_out_of_scope level=workgroup" in log
        assert "runner.pings hint_out_of_scope level=task" in log

    run(rig, body)


@pytest.mark.parametrize(
    "hint",
    [
        {"org_id": ORG, "level": "team", "entry_id": ENTRY},
        {"org_id": ORG, "level": 5, "entry_id": ENTRY},
        {"org_id": "x", "level": "org", "entry_id": ENTRY},
        {"org_id": ORG, "level": "org", "entry_id": "x"},
        {"org_id": ORG, "level": "workgroup", "entry_id": ENTRY},
        {"org_id": ORG, "wg_id": WG, "level": "task", "entry_id": ENTRY},
    ],
)
def test_malformed_hints_are_dropped(rig: Rig, hint: dict[str, object]) -> None:
    async def body(p: PKit) -> None:
        engine = Engine()
        setattr(p.env.context, ENGINE_ATTRIBUTE, engine)
        p.env.context.log = io.StringIO()
        await p.session(1)
        await hints.ping(p.env.context, hint)
        assert engine.requested == 0
        assert "runner.pings hint_invalid type=ping" in p.env.context.log.getvalue()

    run(rig, body)


def test_a_hint_on_a_device_that_is_not_enrolled_does_nothing() -> None:
    context = SimpleNamespace(identity=None, log=io.StringIO())
    asyncio.run(hints.ping(context, {"org_id": ORG, "level": "org", "entry_id": ENTRY}))
    assert "hint_out_of_scope level=org" in context.log.getvalue()


def test_a_hint_without_an_engine_only_logs(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        p.env.context.log = io.StringIO()
        identity, p.env.context.identity = p.env.context.identity, None
        try:
            await p.session(1)
            await hints.ping(p.env.context, {"org_id": ORG, "level": "org", "entry_id": ENTRY})
        finally:
            p.env.context.identity = identity
        assert "runner.pings hint level=org sessions=1" in p.env.context.log.getvalue()

    run(rig, body)


# -- notifiers (RUN-07, RUN-11) ---------------------------------------------------------------------------------------


def test_the_subagent_notifier_pings_the_parent(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        parent = await p.session(1)
        child = session_id(2)
        await subagent_finished.notify(p.env.context, FinishedPing(parent, child, child_ping_id(child)))
        assert p.guest.pings == [
            (parent, {"ping_id": child_ping_id(child), "kind": "subagent.finished", "ref": child}),
        ]
        assert child_ping_id(child) == stable_ping_id("subagent.finished", child)

    run(rig, body)


def test_the_operation_notifier_pings_the_session(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        operation = SimpleNamespace(id=ENTRY, session_id=sid)
        await op_finished.notify(p.env.context, operation)  # type: ignore[arg-type]
        await op_finished.notify(p.env.context, operation)  # type: ignore[arg-type]
        expected = stable_ping_id("operation.finished", ENTRY)
        assert p.guest.pings == [(sid, {"ping_id": expected, "kind": "operation.finished", "ref": ENTRY})]
        p.guest.fail["ping"] = guest_unreachable()
        other = SimpleNamespace(id=session_id(8), session_id=sid)
        await op_finished.notify(p.env.context, other)  # type: ignore[arg-type]
        assert len(p.guest.pings) == 1

    run(rig, body)


def test_pinger_for_builds_one_pinger_over_the_session_service(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        from agent_runner.sessions.service import service_for  # noqa: PLC0415

        configure(env.context, tmp_path)
        built = pinger_for(env.context)
        assert built is not None
        assert pinger_for(env.context) is built
        service = service_for(env.context)
        assert service is not None
        assert built.env is service.env

    from ..subagents.kit import run as plain_run  # noqa: PLC0415

    plain_run(rig, body)

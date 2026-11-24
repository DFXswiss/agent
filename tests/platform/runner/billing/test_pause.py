"""U-RUN-billing (pause): the controlled stop of a platform device when an organization's credit is used up - working
main sessions interrupted and suspended with `credit_exhausted`, idle ones suspended, subagents stopped -, follow-ups
deferred, and the continuation after a credit (KOR-31, ADR-062 section 6; AUD-04 W12)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from agent_runner.billing import pause, reconcile
from agent_runner.billing import state as billing
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.prompts.resume import RESUMED
from agent_runner.reconcile.engine import Run
from agent_runner.sessions import records

from ..prompts.kit import PromptGuest, add_prompt, pull, run, setup
from ..reconcile.rig import Rig
from ..sessions.kit import ORG

ACTIVE = {"api": 1, "active": True}


def desired(exhausted: bool, *, active: bool = True) -> dict[str, Any]:
    return {"billing": {**ACTIVE, "active": active, "orgs": {ORG: {"exhausted": exhausted}}}}


def test_the_state_of_the_desired_state() -> None:
    assert not billing.api(None)
    assert not billing.api({"billing": {"api": True}})
    assert not billing.api({"billing": []})
    assert billing.api({"billing": {"api": 1}})
    assert not billing.active({"billing": {"api": 1, "active": "yes"}})
    assert billing.exhausted(desired(True), ORG)
    assert not billing.exhausted(desired(True, active=False), ORG)
    assert not billing.exhausted(desired(True), "other")
    assert not billing.exhausted({"billing": {**ACTIVE, "orgs": []}}, ORG)


def test_an_exhausted_org_is_paused_and_continues_after_a_credit(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        working = await pk.add_session(1, state="working")
        idle = await pk.add_session(2, state="idle")
        child = await pk.add_session(3, state="working", depth=1)
        starting = await pk.add_session(4, state="agent_starting")
        session_env = pk.kit.session_env
        await pause.enforce(pk.deliverer, desired(True))
        assert ("control", (working, "interrupt")) in pk.guest.calls
        assert ("control", (working, "suspend")) in pk.guest.calls
        assert ("control", (idle, "suspend")) in pk.guest.calls
        assert ("control", (idle, "interrupt")) not in pk.guest.calls
        for sid in (working, idle):
            assert (await pk.session(sid)).state == "suspended"
            assert ("suspended", "credit_exhausted") in await pk.kit.transitions(sid)
        assert ("stopping", "credit_exhausted") in await pk.kit.transitions(child)
        assert (await pk.session(starting)).state == "agent_starting"
        # a second run changes nothing; still exhausted: nothing continues
        calls = len(pk.guest.calls)
        await pause.enforce(pk.deliverer, desired(True))
        assert len(pk.guest.calls) == calls
        # a follow-up prompt for the paused session waits while the credit is used up
        session_env.desired = desired(True)
        event_id = add_prompt(env, 1, "follow", text="Weiter?")
        await pull(env)
        await pk.route(idle, 1, event_id)
        await pk.deliverer.advance(idle)
        assert "create_session" not in pk.guest.names()
        # credit is back: the interrupted session is resumed with the continuation, the idle one stays suspended
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts() == [pause.CONTINUATION]
        # the platform's own sender line, never a person's (ADR-083)
        assert pk.guest.speakers() == [pause.CONTINUATION_LINE]
        assert pause.CONTINUATION_LINE.startswith("agent.dfx.swiss instruction from: the platform (not a person): ")
        assert (await pk.session(working)).state == "resuming"
        assert (await pk.session(idle)).state == "suspended"
        rows = await env.rows("SELECT session_id::text FROM runner.credit_pause WHERE resumed_at IS NOT NULL")
        assert sorted(str(row[0]) for row in rows) == sorted([working, idle])
        # done once: another run sends nothing
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts() == [pause.CONTINUATION]

    run(rig, body)


def test_guest_failures_are_retried_on_the_next_run(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, guest=PromptGuest())
        sid = await pk.add_session(1, state="working")
        pk.guest.fail["control"] = GuestUnreachable("control")
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(sid)).state == "working"
        assert any("credit_interrupt_failed" in line for line in pk.lines)
        del pk.guest.fail["control"]
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(sid)).state == "suspended"
        # the continuation fails once, then goes through
        pk.guest.prompt_errors.append(GuestUnreachable("prompts"))
        await pause.enforce(pk.deliverer, desired(False))
        assert any("credit_continue_failed" in line for line in pk.lines)
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts().count(pause.CONTINUATION) == 2  # the same prompt ID both times
        assert len(set(pk.guest.sent())) == 1

    run(rig, body)


def test_an_older_guest_gets_the_continuation_without_the_line(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, guest=PromptGuest())
        await pk.add_session(1, state="working")
        await pause.enforce(pk.deliverer, desired(True))
        pk.guest.prompt_errors.append(GuestRefused("prompts", 400, "request_invalid"))
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.speakers() == [pause.CONTINUATION_LINE, None]
        assert any("speaker_unsupported" in line for line in pk.lines)
        assert not any("credit_continue_failed" in line for line in pk.lines)

    run(rig, body)


def test_a_failing_suspend_leaves_the_session_and_an_ended_session_is_released(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, guest=PromptGuest())
        sid = await pk.add_session(1, state="idle")
        gone = await pk.add_session(2, state="working")
        failing = PromptGuest()
        failing.fail["control"] = GuestUnreachable("control")
        pk.guest.fail["control"] = GuestUnreachable("control")
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(sid)).state == "idle"
        assert any("credit_suspend_failed" in line for line in pk.lines)
        del pk.guest.fail["control"]
        await pause.enforce(pk.deliverer, desired(True))
        # the interrupted one ended meanwhile; a VM that does not run is not asked
        await pk.set_state(gone, "stopped")
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts() == []
        # an interrupted session whose resume must wait (VM not running) keeps its pause
        rows = await env.rows("SELECT session_id::text, resumed_at IS NULL FROM runner.credit_pause ORDER BY 1")
        assert {str(r[0]): r[1] for r in rows} == {sid: False, gone: False}

    run(rig, body)


def test_org_devices_pause_only_sessions_on_the_dfx_key_and_inactive_billing_never_pauses(rig: Rig) -> None:
    """KOR-50, ADR-081 section 7: on own hardware the credit pause acts on the sessions whose access is `platform`
    (the DFX key); sessions on the owner's own access are never touched."""

    async def body(env: Any) -> None:
        pk = await setup(env, device_kind="org")
        own = await pk.add_session(1, state="working")
        dfx = await pk.add_session(2, state="idle")
        async with env.pool.connection() as conn:
            await records.update(conn, own, model_access="api_key")
        await pause.enforce(pk.deliverer, desired(True, active=False))
        assert pk.guest.calls == []
        await pause.enforce(pk.deliverer, desired(True))
        assert ("control", (own, "suspend")) not in pk.guest.calls
        assert ("control", (dfx, "suspend")) in pk.guest.calls
        pk.kit.session_env.desired = desired(True)
        assert not pause.deferred(pk.kit.session_env, await pk.session(own))
        assert pause.deferred(pk.kit.session_env, await pk.session(dfx))

    run(rig, body)


def test_the_reconcile_step(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(1, state="idle")
        assert [(s.order, s.name) for s in reconcile.STEPS] == [(55, "billing")]
        await reconcile.step(Run(context=env.context, sync=None, book=None, desired=None))  # type: ignore[arg-type]
        await reconcile.step(Run(context=env.context, sync=None, book=None, desired=desired(True)))  # type: ignore[arg-type]
        assert (await pk.session(sid)).state == "suspended"

    run(rig, body)


def test_a_suspend_refused_while_the_turn_ends_is_asked_again(rig: Rig, monkeypatch: Any) -> None:
    monkeypatch.setattr(pause, "SUSPEND_WAIT_S", 0.0)

    async def body(env: Any) -> None:
        pk = await setup(env, guest=PromptGuest())
        sid = await pk.add_session(1, state="working")
        refusals = [GuestRefused("control", 409, "state_conflict")] * 2
        original = pk.guest.control

        async def control(session: str, action: str) -> None:
            if action == "suspend" and refusals:
                pk.guest.calls.append(("control", (session, action)))
                raise refusals.pop()
            await original(session, action)

        monkeypatch.setattr(pk.guest, "control", control)
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(sid)).state == "suspended"
        assert pk.guest.calls.count(("control", (sid, "suspend"))) == 3
        # refused for another reason: not asked again
        other = await pk.add_session(2, state="idle")
        refusals.append(GuestRefused("control", 404, "not_found"))
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(other)).state == "idle"
        assert any(f"credit_suspend_failed session_id={other}" in line for line in pk.lines)

    run(rig, body)


def test_no_prompt_reaches_a_session_of_an_exhausted_org(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(1, state="idle")
        pk.kit.session_env.desired = desired(True)
        event_id = add_prompt(env, 1, "paid", text="Noch etwas?")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.texts() == []
        assert (await pk.row(event_id)).state == "pending"
        pk.kit.session_env.desired = desired(False)
        await pk.deliverer.advance(sid)
        assert pk.guest.texts() == ["Noch etwas?"]

    run(rig, body)


def test_a_later_pause_of_an_idle_session_is_not_taken_for_an_interrupted_one(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(1, state="working")
        await pause.enforce(pk.deliverer, desired(True))
        await pause.enforce(pk.deliverer, desired(False))  # resumed with the continuation
        await pk.set_state(sid, "idle")
        await pause.enforce(pk.deliverer, desired(True))
        rows = await env.rows("SELECT interrupted, resumed_at IS NULL FROM runner.credit_pause")
        assert rows == [(False, True)]

    run(rig, body)


def test_waiting_resumes_a_vm_that_stopped_and_a_session_that_changed(rig: Rig, monkeypatch: Any) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(1, state="working")
        other = await pk.add_session(2, state="idle")

        # the session changed while the pause waited for its lock: nothing is asked
        class Changing:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id

            async def __aenter__(self) -> None:
                await pk.set_state(self.session_id, "stopping")

            async def __aexit__(self, *_exc: object) -> None:
                return None

        await pause.stop_for_credit(pk.kit.session_env, Changing, desired(True))
        assert pk.guest.calls == []
        await pk.set_state(sid, "working")
        await pk.set_state(other, "idle")
        await pause.enforce(pk.deliverer, desired(True))
        assert (await pk.session(sid)).state == "suspended"
        # credit back, but the resume must wait (guest or VM not ready): the pause stays open
        monkeypatch.setattr(pk.deliverer.resumer, "resume", AsyncMock(return_value="retry"))
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts() == []
        # resumed, but its VM is not running any more: still open
        monkeypatch.setattr(pk.deliverer.resumer, "resume", AsyncMock(return_value=RESUMED))
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        await pause.enforce(pk.deliverer, desired(False))
        assert pk.guest.texts() == []
        # the idle session's pause ended with the credit; the interrupted one waits for its continuation
        rows = await env.rows("SELECT session_id::text FROM runner.credit_pause WHERE resumed_at IS NULL")
        assert rows == [(sid,)]
        # a pause of a session whose VM does not run: the guest is not asked
        third = await pk.add_session(3, state="idle")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        await pause.enforce(pk.deliverer, desired(True))
        assert ("control", (third, "suspend")) not in pk.guest.calls

    run(rig, body)


def test_the_reconcile_step_without_a_deliverer(monkeypatch: Any) -> None:
    monkeypatch.setattr(reconcile, "deliverer_for", lambda _context: None)
    asyncio.run(reconcile.step(Run(context=object(), sync=None, book=None, desired=desired(True))))  # type: ignore[arg-type]

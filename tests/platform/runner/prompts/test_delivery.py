"""U-RUN-prompts: delivery in event order, waiting while `working`, retries without a second delivery, the store-wipe
case and the journal results (05-runner.md sections 6.2, 2.3; 03-api.md sections 6.1, 7.1, 7.2; 02-data-model.md
section 4 `prompt_delivery`)."""

from __future__ import annotations

from typing import Any

from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.prompts import queue
from agent_runner.prompts.deliver import BACKOFF_MAX_S, TYPED_RETRY_S

from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG, session_id
from .kit import add_prompt, pull, run, setup

AT = "2026-11-21T12:00:02.000Z"


async def accept(pk: Any, sid: str, event_id: str) -> None:
    await pk.journal(sid, state="prompt_accepted", prompt_id=event_id, at=AT)
    await pk.journal(sid, state="idle", claude_session_id="11111111-2222-4333-8444-555555555555", at=AT)


def test_prompts_are_delivered_one_at_a_time_in_event_order(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        ids = [add_prompt(env, seq, f"p{seq}") for seq in (1, 2, 3)]
        await pull(env)
        # routed out of order: delivered in scope order
        for seq in (3, 1, 2):
            await pk.route(sid, seq, ids[seq - 1])
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [ids[0]]
        # the next one waits for the result of the first
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [ids[0]]
        await accept(pk, sid, ids[0])
        assert (await pk.session(sid)).state == "idle"
        await pk.deliverer.advance(sid)
        await accept(pk, sid, ids[1])
        await pk.deliverer.advance(sid)
        await accept(pk, sid, ids[2])
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == ids
        assert await pk.rows(sid) == dict.fromkeys(ids, "accepted")
        assert (await pk.session(sid)).state == "idle"
        assert ("working", None) in await pk.kit.transitions(sid)
        stored = await env.rows("SELECT last_prompt_at FROM runner.session WHERE id = %s", sid)
        assert stored[0][0] is not None

    run(rig, body)


def test_a_prompt_for_a_working_session_waits_until_idle(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="working")
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        for state in ("requested", "vm_ready", "agent_starting"):
            await pk.set_state(sid, state)
            await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        await pk.set_state(sid, "working")
        pk.deliverer.trigger.clear()
        await pk.journal(sid, state="idle", at=AT)
        assert pk.deliverer.trigger.is_set()  # the end of the turn wakes the delivery
        await pk.deliverer.run_once()
        assert pk.guest.sent() == [event_id]
        # ended sessions get nothing
        other = add_prompt(env, 2, "q")
        await pull(env)
        await pk.route(sid, 2, other)
        await pk.set_state(sid, "stopping")
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [event_id]

    run(rig, body)


def test_a_network_error_is_retried_without_a_second_delivery(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        pk.guest.prompt_errors = [GuestUnreachable("prompts"), GuestUnreachable("prompts")]
        sid = await pk.add_session()
        first = add_prompt(env, 1, "p1")
        second = add_prompt(env, 2, "p2")
        await pull(env)
        await pk.route(sid, 1, first)
        await pk.route(sid, 2, second)
        await pk.deliverer.advance(sid)
        # write-ahead: the hand-over is recorded before the call and stays when the call fails
        assert (await pk.row(first)).state == "registered"
        await pk.deliverer.advance(sid)  # within the backoff: nothing
        assert pk.guest.sent() == [first]
        await pk.advance(sid, later=1.0)
        assert pk.guest.sent() == [first, first]
        await pk.advance(sid, later=1.5)  # backoff doubled to 2 s
        assert pk.guest.sent() == [first, first]
        await pk.advance(sid, later=1.0)
        # the same prompt ID every time; the second prompt never overtakes it
        assert pk.guest.sent() == [first, first, first]
        row = await pk.row(first)
        assert (row.state, row.attempts) == ("typed", 3)
        await accept(pk, sid, first)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [first, first, first, second]
        assert pk.deliverer.not_before == {}
        assert any("send_retry" in line for line in pk.lines)

    run(rig, body)


def test_the_backoff_is_capped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        for _ in range(8):
            pk.deliverer._backoff("s")
        assert pk.deliverer.delays["s"] == BACKOFF_MAX_S

    run(rig, body)


def test_a_typed_prompt_without_result_is_sent_again_after_the_guest_lost_it(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [event_id]
        await pk.age(event_id, int(TYPED_RETRY_S) + 1)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [event_id, event_id]
        # while the session works, nothing is sent again
        await pk.age(event_id, int(TYPED_RETRY_S) + 1)
        await pk.set_state(sid, "working")
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [event_id, event_id]

    run(rig, body)


def test_guest_refusals(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        ids = [add_prompt(env, seq, f"p{seq}") for seq in (1, 2, 3)]
        await pull(env)
        for seq, event_id in enumerate(ids, 1):
            await pk.route(sid, seq, event_id)
        pk.guest.prompt_errors = [
            # accepted before (or the guest's session ended): the journal tells
            GuestRefused("prompts", 409, "state_conflict"),
            # the guest does not know the session: routable again, retried later
            GuestRefused("prompts", 404, "not_found"),
            # anything else: never deliverable
            GuestRefused("prompts", 409, "id_conflict"),
        ]
        await pk.deliverer.advance(sid)
        assert (await pk.row(ids[0])).state == "typed"
        await pk.journal(sid, state="prompt_accepted", prompt_id=ids[0], at=AT)
        await pk.journal(sid, state="idle", at=AT)
        await pk.deliverer.advance(sid)
        assert (await pk.row(ids[1])).state == "pending"
        await pk.advance(sid, later=1.0)
        assert (await pk.row(ids[1])).state == "refused"
        assert await pk.audit_codes() == ["prompt_guest_refused"]
        await pk.advance(sid, later=2.0)
        assert pk.guest.sent() == [ids[0], ids[1], ids[1], ids[2]]
        assert (await pk.row(ids[2])).state == "typed"

    run(rig, body)


def test_a_vm_that_does_not_run_or_cannot_be_reached_delays_the_delivery(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        assert (await pk.row(event_id)).state == "registered"
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'running' WHERE id = %s", (row.id,))
        from agent_runner.errors import RunnerError  # noqa: PLC0415

        pk.kit.vms.client_error = RunnerError("vm_unreachable", "x")
        await pk.advance(sid, later=1.0)
        assert pk.guest.sent() == []
        pk.kit.vms.client_error = None
        await pk.advance(sid, later=2.0)
        assert pk.guest.sent() == [event_id]

    run(rig, body)


def test_after_a_store_wipe_delivered_prompts_are_never_delivered_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        ids = [add_prompt(env, seq, f"p{seq}") for seq in (1, 2, 3)]
        await pull(env)
        # the local rows are gone (wipe); the hub still routes the first two as pending (it saw no result yet), but
        # lists them as delivered
        await pk.route(sid, 2, ids[1])
        state = {
            "pending_prompts": [
                {"task_id": TASK, "session_id": sid, "scope_id": WG, "scope_seq": seq, "event_id": ids[seq - 1]}
                for seq in (1, 2, 3)
            ],
            "delivered_prompt_event_ids": {sid: [ids[0], ids[1]]},
        }
        await pk.deliverer.apply_desired(state)
        assert await pk.rows(sid) == {ids[0]: "accepted", ids[1]: "accepted", ids[2]: "pending"}
        await pk.deliverer.run_once()
        assert pk.guest.sent() == [ids[2]]
        await pk.deliverer.apply_desired(state)  # nothing changes on a repeat
        assert any("delivered_adopted count=2" in line for line in pk.lines)

    run(rig, body)


def test_a_prompt_of_an_ended_session_moves_to_the_new_main_session(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        old = await pk.add_session(1)
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(old, 1, event_id)
        new = await pk.add_session(2)
        # the old session still runs: the routing to another session does not move it
        await pk.route(new, 1, event_id)
        assert await pk.rows() == {event_id: "pending"}
        assert (await pk.row(event_id)).session_id == old
        await pk.set_state(old, "failed")
        await pk.route(new, 1, event_id)
        assert (await pk.row(event_id)).session_id == new
        await pk.deliverer.advance(new)
        assert pk.guest.sent() == [event_id]
        # a final row never moves
        await pk.journal(new, state="prompt_accepted", prompt_id=event_id, at=AT)
        await pk.set_state(new, "stopped")
        third = await pk.add_session(3)
        await pk.route(third, 1, event_id)
        assert (await pk.row(event_id)).session_id == new

    run(rig, body)


def test_the_interrupted_prompt_of_a_lost_session_is_delivered_once_more_to_its_successor(rig: Rig) -> None:
    """ADR-086 sections 3.4 and 3.7: the accepted prompt of the lost session goes to the successor the device state
    names in `failovers`, once; any other routing of an accepted prompt stays final."""

    async def body(env: Any) -> None:
        pk = await setup(env)
        lost = await pk.add_session(1)
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(lost, 1, event_id)
        await pk.deliverer.advance(lost)
        await accept(pk, lost, event_id)
        await pk.set_state(lost, "stopped")
        other = await pk.add_session(2)
        successor = await pk.add_session(3)
        failovers = [{"session_id": successor, "from_session": lost, "cancelled_runs": 0}]
        pk.kit.session_env.desired = {"failovers": failovers}
        await pk.route(other, 1, event_id)  # not the successor of the lost session: final
        assert (await pk.row(event_id)).session_id == lost
        await pk.route(successor, 1, event_id)
        moved = await pk.row(event_id)
        assert (moved.session_id, moved.state, moved.attempts) == (successor, "pending", 0)
        await pk.deliverer.advance(successor)
        assert pk.guest.sent() == [event_id, event_id]
        await accept(pk, successor, event_id)
        await pk.route(successor, 1, event_id)  # once: accepted by the successor it is final again
        assert (await pk.row(event_id)).state == "accepted"
        await pk.deliverer.advance(successor)
        assert pk.guest.sent() == [event_id, event_id]

    run(rig, body)


def test_journal_results(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        ids = [add_prompt(env, seq, f"p{seq}") for seq in (1, 2)]
        await pull(env)
        await pk.route(sid, 1, ids[0])
        await pk.route(sid, 2, ids[1])
        await pk.deliverer.advance(sid)
        # the guest could not deliver it into the TUI: failed, a status for the chat, the next one follows
        await pk.journal(sid, state="prompt_refused", prompt_id=ids[0], reason="delivery_failed", at=AT)
        assert (await pk.row(ids[0])).state == "failed"
        statuses = await pk.statuses()
        assert statuses == [{"state": "idle", "reason": "delivery_failed", "details": {"prompt_event_id": ids[0]}}]
        assert ("session.prompt_refused", "rejected") in [
            (item["action"], item["result"]) for item in await env.audits()
        ]
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == ids
        # results of other sessions, of final rows, of unregistered text and unknown IDs change nothing
        await pk.journal(sid, state="prompt_refused", prompt_id=ids[0], reason="delivery_failed", at=AT)
        await pk.journal(sid, state="prompt_refused", prompt_id=session_id(5), reason="unregistered", at=AT)
        await pk.journal(sid, state="prompt_refused", prompt_id=session_id(6), reason="delivery_failed", at=AT)
        other = await pk.add_session(2)
        await pk.journal(other, state="prompt_refused", prompt_id=ids[1], reason="delivery_failed", at=AT)
        await pk.journal(other, state="prompt_accepted", prompt_id=ids[1], at=AT)
        assert (await pk.row(ids[1])).state == "typed"
        assert len(await pk.statuses()) == 1
        # the journal without an installed deliverer still records the results
        del env.context.prompt_deliverer
        await pk.journal(sid, state="prompt_accepted", prompt_id=ids[1], at=AT)
        assert (await pk.row(ids[1])).state == "accepted"

    run(rig, body)


def test_a_session_that_never_accepted_its_first_prompt_fails_delivery_failed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="prompt_delivering")
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)  # a start without initial prompt: sent through the prompt endpoint
        assert pk.guest.sent() == [event_id]
        await pk.journal(sid, state="prompt_refused", prompt_id=event_id, reason="delivery_failed", at=AT)
        stored = await pk.session(sid)
        assert stored.state == "failed"
        assert ("failed", "delivery_failed") in await pk.kit.transitions(sid)
        assert await pk.statuses() == [{"state": "failed", "reason": "delivery_failed"}]
        # its guest session is stopped by the next round
        assert pk.deliverer.stops == {sid}
        await pk.deliverer.run_once()
        assert ("control", (sid, "stop")) in pk.guest.calls
        assert pk.deliverer.stops == set()
        # without an installed deliverer the session still fails
        second = await pk.add_session(2, state="prompt_delivering")
        other = add_prompt(env, 2, "q")
        await pull(env)
        await pk.route(second, 2, other)
        await pk.deliverer.advance(second)
        del env.context.prompt_deliverer
        await pk.journal(second, state="prompt_refused", prompt_id=other, reason="delivery_failed", at=AT)
        assert (await pk.session(second)).state == "failed"

    run(rig, body)


def test_rows_without_a_place_are_refused_as_stale(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'pending')",
                (event_id, sid),
            )
        await pk.deliverer.advance(sid)
        assert (await pk.row(event_id)).state == "refused"
        assert await pk.audit_codes() == ["prompt_stale"]
        # a resend of a row whose event is gone refuses it
        async with env.pool.connection() as conn:
            await queue.set_state(conn, event_id, "registered", channel="prompt")
            await conn.execute("DELETE FROM runner.prompt_event")
        await pk.deliverer.advance(sid)
        assert (await pk.row(event_id)).state == "refused"
        assert pk.guest.sent() == []

    run(rig, body)


def test_unknown_delivery_states_are_refused() -> None:
    import asyncio  # noqa: PLC0415

    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="unknown prompt delivery state"):
        asyncio.run(queue.set_state(None, "x", "lost"))  # type: ignore[arg-type]


def test_a_row_the_journal_ended_meanwhile_is_never_reopened(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        head = await pk.row(event_id)
        session = await pk.session(sid)
        # the journal accepts the prompt between the read of the head and the hand-over
        await pk.journal(sid, state="prompt_accepted", prompt_id=event_id, at=AT)
        await pk.deliverer._send(session, head)
        assert pk.guest.sent() == []
        assert (await pk.row(event_id)).state == "accepted"
        # a refused guest answer does not end a row the journal finished either
        async with env.pool.connection() as conn:
            await queue.set_state(conn, event_id, "failed")
        await pk.deliverer._guest_refused(session, head, "id_conflict")
        await pk.deliverer._guest_refused(session, head, "not_found")
        assert (await pk.row(event_id)).state == "failed"

    run(rig, body)

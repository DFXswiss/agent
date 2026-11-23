"""U-RUN-prompts: only verified prompts of writers reach a session (05-runner.md sections 2.2 step 6 and 2.4,
04-crypto.md section 6.4, 03-api.md sections 6.1 and 10).

Prompt events are sealed with the reference builders and served by the rig's hub; the real inbox verifies them
(E1-E5, author `wg:write` at `lh`) and hands them to the consumer of the prompt area. A forged, unauthorised,
misaddressed, misrouted, stale or replayed prompt is never sent to the guest and is audited
`runner.inbound_rejected`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.inbox.dispatch import VerifiedEvent
from agent_runner.prompts import consumers, queue, verify
from agent_runner.prompts.deliver import HOLD_S

from ..reconcile.rig import Rig
from ..sessions.kit import ORG, TASK, WG, session_id
from .kit import add_prompt, pull, run, setup

OTHER_TASK = "01930004-0000-7000-8000-000000000002"


def test_a_verified_prompt_is_kept_and_delivered_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p1", text="Bitte lies README.md")
        pk.deliverer.trigger.clear()
        assert await pull(env) == 1
        assert pk.deliverer.trigger.is_set()  # a new prompt wakes the delivery
        async with env.pool.connection() as conn:
            stored = await queue.event(conn, event_id)
        assert stored is not None
        assert (stored.task_id, stored.wg_id, stored.scope_seq) == (TASK, WG, 1)
        # the sealed event is stored, never its text
        assert "Bitte" not in str(stored.sealed)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.texts() == ["Bitte lies README.md"]
        assert (await pk.row(event_id)).state == "typed"
        # another pull, another routing and another round deliver nothing again
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [event_id]
        assert await pk.audit_codes() == []

    run(rig, body)


def test_a_forged_prompt_is_rejected_and_never_delivered(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "forged")
        sealed = env.world.events[WG][0][1]
        signature = bytearray(b64u_decode(sealed["sig"]))
        signature[0] ^= 1
        sealed["sig"] = b64u_encode(bytes(signature))
        await pull(env)
        async with env.pool.connection() as conn:
            assert await queue.event(conn, event_id) is None
        # the hub routes it anyway: the inbox's cursor passed it, so the routing is refused as unverified
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        assert await pk.rows() == {}
        assert await pk.audit_codes() == ["signature_invalid", "prompt_unverified"]
        # audited once per prompt and session
        await pk.route(sid, 1, event_id)
        assert await pk.audit_codes() == ["signature_invalid", "prompt_unverified"]

    run(rig, body)


def test_a_prompt_of_an_author_without_write_access_is_rejected(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        # carol is no member of the org: no right in the workgroup at `lh`
        event_id = add_prompt(env, 1, "carol", author="carol")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        codes = await pk.audit_codes()
        assert codes[-1] == "prompt_unverified"
        assert codes[0] != "prompt_unverified"

    run(rig, body)


def test_a_prompt_written_before_the_author_was_a_member_is_rejected(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        # a stale log head: at entry 1 bob was not yet a member of the org
        event_id = add_prompt(env, 1, "early", author="bob", lh=1)
        await pull(env)
        async with env.pool.connection() as conn:
            assert await queue.event(conn, event_id) is None
        await pk.route(sid, 1, event_id)
        assert pk.guest.sent() == []
        assert len(await pk.audit_codes()) == 2

    run(rig, body)


def test_a_prompt_that_addresses_a_session_is_refused(rig: Rig) -> None:
    async def checked(env: Any) -> None:
        pk = await setup(env)
        add_prompt(env, 1, "addressed", session=session_id(1))
        await pull(env)
        assert await env.rows("SELECT * FROM runner.prompt_event") == []
        assert len(await pk.audit_codes()) == 1

    run(rig, checked)


def test_routings_that_do_not_fit_the_event_or_the_session_are_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        child = await pk.add_session(2, depth=1)
        other_task = await pk.add_session(3, task=OTHER_TASK)
        event_id = add_prompt(env, 2, "p")
        await pull(env)
        await pk.route(sid, 2, event_id, task=OTHER_TASK)  # task of the routing differs
        await pk.route(sid, 3, event_id)  # scope_seq differs
        await pk.route(child, 2, event_id)  # a subagent session
        await pk.route(other_task, 2, event_id, task=OTHER_TASK)  # the session of another task
        await pk.route(sid, 2, event_id, scope=session_id(7))  # another scope
        assert await pk.rows() == {}
        # audited once per (event, session)
        assert await pk.audit_codes() == ["prompt_misrouted"] * 3
        # a session that is not known on this device (yet) is no refusal: the start asks for its prompt
        await pk.route(session_id(8), 2, event_id)
        assert await pk.rows() == {}
        # the right routing
        await pk.route(sid, 2, event_id)
        assert await pk.rows() == {event_id: "pending"}

    run(rig, body)


def test_an_event_not_pulled_yet_is_pulled_and_one_beyond_the_hub_waits(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "late")
        # not pulled yet: the routing pulls once
        await pk.route(sid, 1, event_id)
        assert await pk.rows() == {event_id: "pending"}
        # an event the hub does not serve (beyond the cursor) waits without a refusal
        await pk.route(sid, 9, "01930006-0000-7000-8000-000000000009")
        assert await pk.audit_codes() == []

    run(rig, body)


def test_a_stale_prompt_is_refused_and_a_replayed_one_is_never_sent_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        old = add_prompt(env, 1, "old")
        new = add_prompt(env, 2, "new")
        await pull(env)
        await pk.route(sid, 2, new)
        # the older prompt of the task is stored but not routed: the newer one waits for it a while
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        assert any("held session_id" in line for line in pk.lines)
        await pk.age(new, int(HOLD_S) + 1)
        await pk.deliverer.advance(sid)
        await pk.journal(sid, state="prompt_accepted", prompt_id=new, at="2026-11-21T12:00:02.000Z")
        await pk.journal(sid, state="idle", at="2026-11-21T12:00:03.000Z")
        # the older prompt is routed only now: delivering it would break the event order
        await pk.route(sid, 1, old)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [new]
        assert (await pk.row(old)).state == "refused"
        assert await pk.audit_codes() == ["prompt_stale"]
        # the accepted prompt routed again (a replay) stays accepted and is not sent
        await pk.route(sid, 2, new)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [new]
        assert (await pk.row(new)).state == "accepted"

    run(rig, body)


def test_a_prompt_that_does_not_open_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        empty = add_prompt(env, 1, "empty", text="   ")
        await pull(env)
        await pk.route(sid, 1, empty)
        await pk.deliverer.advance(sid)
        assert (await pk.row(empty)).state == "refused"
        # the stored key of the workgroup is gone: the next prompt cannot be opened
        nokey = add_prompt(env, 2, "nokey")
        await pull(env)
        await env.rows("DELETE FROM runner.scope_key WHERE scope = 'wg' RETURNING 1")
        await pk.route(sid, 2, nokey)
        await pk.deliverer.advance(sid)
        assert (await pk.row(nokey)).state == "refused"
        assert pk.guest.sent() == []
        assert await pk.audit_codes() == ["prompt_unreadable", "prompt_unreadable"]

    run(rig, body)


def test_open_text_refuses_everything_that_is_not_the_stored_prompt(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await setup(env)
        event_id = add_prompt(env, 1, "p", text="ok")
        other_id = add_prompt(env, 2, "bob", author="bob")
        await pull(env)
        async with env.pool.connection() as conn:
            stored = await queue.event(conn, event_id)
            other = await queue.event(conn, other_id)
            assert stored is not None
            assert other is not None
            key = env.sync.storage_key
            assert await verify.open_text(conn, key, stored) == "ok"
            header = dict(stored.sealed["header"])
            variants = [
                replace(stored, event_id=other_id),  # another event's ID
                replace(stored, sealed={**stored.sealed, "header": {**header, "session": session_id(1)}}),
                replace(stored, sealed={**stored.sealed, "header": {**header, "epoch": "1"}}),
                replace(stored, sealed={**stored.sealed, "header": {**header, "epoch": 7}}),  # no key held
                replace(stored, author_sig=other.author_sig),  # another author's key
                replace(stored, sealed="junk"),  # type: ignore[arg-type]
            ]
            for variant in variants:
                with pytest.raises(verify.PromptRefused) as refused:
                    await verify.open_text(conn, key, variant)
                assert refused.value.code == verify.UNREADABLE

    run(rig, body)


def test_the_consumer_refuses_a_prompt_that_addresses_a_session_and_keeps_others_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        add_prompt(env, 1, "p")
        await pull(env)
        rows = await env.rows("SELECT sealed FROM runner.prompt_event")
        sealed = rows[0][0]
        header = dict(sealed["header"])

        def verified(**changes: Any) -> VerifiedEvent:
            return VerifiedEvent(
                org_id=ORG,
                scope="wg",
                scope_id=WG,
                scope_seq=1,
                received_at="2026-11-21T12:31:00.000Z",
                header={**header, **changes},
                body={"text": "x", "attachments": []},
                sealed=sealed,
            )

        async with env.pool.connection() as conn:
            await consumers.keep_prompt(env.context, conn, verified(session=session_id(1)))
            await consumers.keep_prompt(env.context, conn, verified(ref="r"))
            # a repeat of a kept prompt (a replay after a crash before the cursor commit) changes nothing
            pk.deliverer.trigger.clear()
            await consumers.keep_prompt(env.context, conn, verified())
            assert not pk.deliverer.trigger.is_set()
        assert await pk.audit_codes() == ["prompt_misrouted", "prompt_misrouted"]
        # without an installed deliverer a new prompt is only kept
        del env.context.prompt_deliverer
        add_prompt(env, 2, "q")
        assert await pull(env) == 1
        assert len(await env.rows("SELECT 1 FROM runner.prompt_event")) == 2

    run(rig, body)


def test_a_prompt_whose_hint_was_lost_is_not_overtaken(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        p2 = add_prompt(env, 2, "p2")
        p3 = add_prompt(env, 3, "p3")
        await pull(env)
        # the hint of p2 was lost, p3 is routed first
        await pk.route(sid, 3, p3)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        # the next reconcile routes p2: delivered first, then p3
        await pk.route(sid, 2, p2)
        await pk.deliverer.advance(sid)
        await pk.journal(sid, state="prompt_accepted", prompt_id=p2, at="2026-11-21T12:00:02.000Z")
        await pk.journal(sid, state="idle", at="2026-11-21T12:00:03.000Z")
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [p2, p3]
        assert await pk.audit_codes() == []

    run(rig, body)


def test_after_a_wipe_an_older_prompt_left_out_of_the_delivered_list_is_not_replayed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        p3 = add_prompt(env, 3, "p3")
        p5 = add_prompt(env, 5, "p5")
        # wiped store: the hub lists only p5 as delivered (before the events are pulled again), then routes p3
        await pk.deliverer.apply_desired({"pending_prompts": [], "delivered_prompt_event_ids": {sid: [p5]}})
        await pull(env)
        await pk.route(sid, 3, p3)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        assert (await pk.row(p3)).state == "refused"
        assert await pk.audit_codes() == ["prompt_stale"]

    run(rig, body)

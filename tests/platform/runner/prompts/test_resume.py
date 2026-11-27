"""U-RUN-prompts: idle -> suspend, follow-up prompts that resume a suspended session with `--resume` on its VM, and a
suspended session whose VM changed (05-runner.md sections 6.3, 3.5; 03-api.md sections 4.6.3, 7.1). The restore and
the emergency path of restarted sessions are tested in `contexts/` (KOR-33)."""

from __future__ import annotations

from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.prompts import resume
from agent_runner.prompts.resume import RESTART_LINE, Resumer, desired_of

from ..reconcile.rig import Rig
from ..sessions.kit import TASK, desired, session_id, wire
from .kit import CLAUDE_ID, MODEL, PromptGuest, add_prompt, pull, run, setup

AT = "2026-11-21T12:00:02.000Z"


def test_an_idle_session_is_suspended_after_the_idle_time(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, idle_seconds=60)
        sid = await pk.add_session(state="idle")
        busy = await pk.add_session(2, state="idle")
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(busy, 1, event_id)
        await pk.set_state(busy, "working")  # its prompt waits
        await pk.set_state(busy, "idle")
        working = await pk.add_session(3, state="working")
        await pk.deliverer.suspend_idle()  # first sight: idle from now
        env.clock.now += 59
        await pk.deliverer.suspend_idle()
        assert ("control", (sid, "suspend")) not in pk.guest.calls
        env.clock.now += 1
        await pk.deliverer.suspend_idle()
        assert ("control", (sid, "suspend")) in pk.guest.calls
        assert (await pk.session(sid)).state == "suspended"
        assert ("suspended", None) in await pk.kit.transitions(sid)
        # a session with an open prompt and a working session are not suspended
        assert ("control", (busy, "suspend")) not in pk.guest.calls
        assert ("control", (working, "suspend")) not in pk.guest.calls
        assert sid not in pk.deliverer.idle_since

    run(rig, body)


def test_the_main_orchestrators_session_is_never_suspended_when_idle(rig: Rig) -> None:
    """A suspend would end the orchestrator's keys in the VM while the hub keeps it active (review KOR-57 b no. 1)."""

    async def body(env: Any) -> None:
        pk = await setup(env, idle_seconds=60)
        sid = await pk.add_session(state="idle")
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.orchestrator (session_id, pairing_id, state, registered_at, handed_over_at)"
                " VALUES (%s, %s, 'handed_over', now(), now())",
                (sid, session_id(9)),
            )
        await pk.deliverer.suspend_idle()
        env.clock.now += 3600
        await pk.deliverer.suspend_idle()
        assert ("control", (sid, "suspend")) not in pk.guest.calls
        assert (await pk.session(sid)).state == "idle"

    run(rig, body)


def test_the_idle_time_restarts_with_every_turn_and_failed_suspends_are_retried(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, idle_seconds=60)
        sid = await pk.add_session(state="working")
        await pk.journal(sid, state="idle", at=AT)
        assert pk.deliverer.idle_since[sid] == env.clock.now
        env.clock.now += 30
        await pk.journal(sid, state="prompt_accepted", prompt_id=session_id(9), at=AT)  # a turn: not idle
        assert sid not in pk.deliverer.idle_since
        await pk.journal(sid, state="idle", at=AT)
        env.clock.now += 61
        # the guest's session started a turn meanwhile: counted again from now
        pk.guest.fail["control"] = GuestRefused("control", 409, "state_conflict")
        await pk.deliverer.suspend_idle()
        assert pk.deliverer.idle_since[sid] == env.clock.now
        env.clock.now += 61
        pk.guest.fail["control"] = GuestUnreachable("control")
        await pk.deliverer.suspend_idle()
        assert (await pk.session(sid)).state == "idle"
        assert any("suspend_retry" in line for line in pk.lines)
        # a VM that does not run is not asked
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        del pk.guest.fail["control"]
        calls = len(pk.guest.calls)
        await pk.deliverer.suspend_idle()
        assert len(pk.guest.calls) == calls
        # an idle time of a session that is no longer idle is dropped
        pk.deliverer.observe(sid, "idle")
        await pk.set_state(sid, "working")
        await pk.deliverer.suspend_idle()
        assert sid not in pk.deliverer.idle_since
        # the session changed while waiting for its lock: nothing
        await pk.set_state(sid, "working")
        await pk.deliverer._suspend(await pk.session(sid))
        assert len(pk.guest.calls) == calls

    run(rig, body)


def test_a_follow_up_prompt_resumes_a_suspended_session_with_resume(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        event_id = add_prompt(env, 1, "follow", text="Und jetzt?")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        names = pk.guest.names()
        assert names.index("create_session") < names.index("put_credential") < names.index("register_prompt")
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert request["session_id"] == sid
        assert request["resume_claude_session_id"] == CLAUDE_ID
        assert (request["task_id"], request["parent_session_id"], request["depth"]) == (TASK, None, 0)
        assert "initial_prompt" not in request
        assert pk.guest.texts() == ["Und jetzt?"]
        # the session's own VM is asked for first (ADR-075 section 6)
        assert pk.kit.vms.calls[-1]["prefer"] == [(await pk.session(sid)).vm_id]
        assert ("resuming", None) in await pk.kit.transitions(sid)
        # the guest's journal: the resumed start, then the acceptance
        for state in ("sandbox_ready", "agent_starting", "prompt_delivering"):
            await pk.journal(sid, state=state, at=AT)
        assert (await pk.session(sid)).state == "resuming"
        await pk.journal(sid, state="prompt_accepted", prompt_id=event_id, at=AT)
        assert (await pk.session(sid)).state == "working"
        assert (await pk.row(event_id)).state == "accepted"

    run(rig, body)


def test_resume_retries_and_failures(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env, guest=PromptGuest())
        sid = await pk.add_session(state="suspended", claude=None)
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        # guest not reachable: the session stays resuming, retried after the backoff
        pk.guest.fail["create_session"] = GuestUnreachable("sessions")
        await pk.deliverer.advance(sid)
        assert (await pk.session(sid)).state == "resuming"
        assert (await pk.row(event_id)).state == "pending"
        # the guest took the first request: a repeat with another context is `id_conflict` and still resumes
        pk.guest.fail["create_session"] = GuestRefused("sessions", 409, "id_conflict")
        await pk.advance(sid, later=1.0)
        request = [value for name, value in pk.guest.calls if name == "create_session"][-1]
        assert "resume_claude_session_id" not in request  # no Claude session ID known: the guest uses its own
        assert pk.guest.sent() == [event_id]

    run(rig, body)


async def resumed(env: Any, **failures: BaseException) -> tuple[Any, str]:
    pk = await setup(env)
    sid = await pk.add_session(state="suspended")
    pk.guest.fail.update(failures)
    return pk, await Resumer(pk.kit.session_env).resume(await pk.session(sid))


def test_resume_failure_reasons(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk, outcome = await resumed(env, create_session=GuestRefused("sessions", 409, "state_conflict"))
        assert outcome == "sandbox_failed"
        sid = session_id(1)
        assert (await pk.session(sid)).state == "failed"
        assert ("control", (sid, "stop")) in pk.guest.calls

    async def protocol(env: Any) -> None:
        from agent_runner.guestlink.errors import GuestProtocolError  # noqa: PLC0415

        _pk, outcome = await resumed(env, create_session=GuestProtocolError("sessions"))
        assert outcome == "sandbox_failed"

    async def wrong_answer(env: Any) -> None:
        pk = await setup(env)
        pk.guest.create_answer = {"state": "working"}
        sid = await pk.add_session(state="suspended")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "sandbox_failed"

    async def capacity(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        sid2 = await pk.add_session(2, state="suspended")
        pk.kit.vms.error = RunnerError("capacity_exhausted", "x")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "capacity_exhausted"
        pk.kit.vms.error = RunnerError("lima_failed", "x")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid2)) == "vm_start_failed"

    async def other_vm(env: Any) -> None:
        # the session's VM is gone: it moves to the workgroup's VM and - without a checkpoint - takes the reported
        # emergency path instead of failing `vm_start_failed` (KOR-33)
        pk = await setup(env)
        sid = await pk.add_session(state="suspended", vm_id="vm-0000ffff")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "resumed"
        assert (await pk.session(sid)).vm_id == (await pk.vm()).id
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert "resume_claude_session_id" not in request
        assert request["harness"]["context_markdown"].endswith(RESTART_LINE.format(task_id=TASK))
        fallback = [item for item in await env.audits() if item["action"] == "session.context_fallback"]
        assert [item["detail"] for item in fallback] == [{"reason": "context_missing"}]
        # its VM row is gone (the session names none any more): the choice of the workgroup applies (ADR-075)
        assert pk.kit.vms.calls[-1] == {"session_id": None, "prefer": [], "new_allowed": True}

    async def unreachable_vm(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        pk.kit.vms.client_error = RunnerError("vm_unreachable", "x")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "retry"

    async def unexpected(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        pk.kit.vms.error = ValueError("boom")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "agent_start_failed"
        assert any("resume_error" in line for line in pk.lines)

    async def no_key(env: Any) -> None:
        pk = await setup(env, key=None)
        sid = await pk.add_session(state="suspended")
        assert await Resumer(pk.kit.session_env).resume(await pk.session(sid)) == "model_access_unavailable"

    for case in (body, protocol, wrong_answer, capacity, other_vm, unreachable_vm, unexpected, no_key):
        run(rig, case)


def test_a_failed_resume_leaves_the_prompt_for_the_next_session(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        pk.guest.fail["create_session"] = GuestRefused("sessions", 409, "state_conflict")
        await pk.deliverer.advance(sid)
        assert (await pk.session(sid)).state == "failed"
        assert (await pk.row(event_id)).state == "pending"
        new = await pk.add_session(2)
        await pk.route(new, 1, event_id)
        del pk.guest.fail["create_session"]
        await pk.deliverer.advance(new)
        assert pk.guest.sent() == [event_id]

    run(rig, body)


def test_a_registered_prompt_of_a_resuming_session_is_resumed_and_sent_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        event_id = add_prompt(env, 1, "p")
        await pull(env)
        await pk.route(sid, 1, event_id)
        pk.guest.prompt_errors = [GuestUnreachable("prompts")]
        await pk.deliverer.advance(sid)
        assert (await pk.row(event_id)).state == "registered"
        # the daemon restarted while resuming: the resume is repeated, then the prompt is sent with the same ID
        await pk.advance(sid, later=1.0)
        assert [name for name in pk.guest.names() if name == "create_session"] == ["create_session"] * 2
        assert pk.guest.sent() == [event_id, event_id]
        # a resume that has to be retried leaves the row
        await pk.set_state(sid, "resuming")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.prompt_delivery SET state = 'registered'")
        pk.guest.fail["create_session"] = GuestUnreachable("sessions")
        await pk.advance(sid, later=5.0)
        assert pk.guest.sent() == [event_id, event_id]

    run(rig, body)


def test_the_desired_entry_gives_the_owner_preferences_of_a_resume(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        session = await pk.session(sid)
        senv = pk.kit.session_env
        assert desired_of(senv, session).model_access_pref == "platform"
        assert desired_of(senv, session).model_id_pref == MODEL
        senv.desired = {"sessions": [wire(desired(1, state="suspended", model_access_pref="auto"))]}
        assert desired_of(senv, session).model_access_pref == "auto"
        # an entry of another task or a malformed one is not used
        senv.desired = {"sessions": [wire(desired(1, task_id=session_id(4)))]}
        assert desired_of(senv, session).model_access_pref == "platform"
        senv.desired = {"sessions": [{"session_id": sid, "depth": "x"}, "junk"]}
        assert desired_of(senv, session).model_access_pref == "platform"

    run(rig, body)


def test_a_session_stopped_before_its_resume_is_left_alone(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        session = await pk.session(sid)
        await pk.set_state(sid, "stopping")  # the stop hint arrived after the delivery read the session
        assert await Resumer(pk.kit.session_env).resume(session) == "ended"
        assert "create_session" not in pk.guest.names()
        assert (await pk.session(sid)).state == "stopping"

    run(rig, body)


def test_an_unhealthy_guest_fails_the_resume_after_the_attempt_limit(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="suspended")
        pk.guest.state = "broken"
        resumer = Resumer(pk.kit.session_env)
        outcomes = [await resumer.resume(await pk.session(sid)) for _ in range(resume.MAX_ATTEMPTS)]
        assert outcomes == ["retry"] * (resume.MAX_ATTEMPTS - 1) + ["guest_unreachable"]
        assert (await pk.session(sid)).state == "failed"
        assert resumer.attempts == {}

    run(rig, body)


def test_after_a_device_restart_the_waiting_prompt_of_a_working_session_is_delivered(rig: Rig) -> None:
    """HOT-25: the device restarted (power cut) while a main session worked. Its VM is `stopped`, the store still says
    `working`, and a follow-up prompt waits for it. The session area suspends it (`device_restart`); the next round of
    the deliverer resumes it on its VM with `--resume` and types the waiting prompt."""
    from agent_runner.sessions.service import SessionService  # noqa: PLC0415

    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="working")
        event_id = add_prompt(env, 1, "follow", text="Weiter nach dem Stromausfall")
        await pull(env)
        await pk.route(sid, 1, event_id)
        await pk.deliverer.advance(sid)
        assert pk.guest.texts() == []  # working: the prompt waits
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))  # the restart
        assert await SessionService(pk.kit.session_env).vms_stopped() == 1
        assert ("suspended", "device_restart") in await pk.kit.transitions(sid)
        await pk.deliverer.run_once()
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert (request["session_id"], request["resume_claude_session_id"]) == (sid, CLAUDE_ID)
        assert pk.kit.vms.calls[-1]["prefer"] == [row.id]  # its own VM, started again
        assert pk.guest.texts() == ["Weiter nach dem Stromausfall"]
        await pk.journal(sid, state="prompt_accepted", prompt_id=event_id, at=AT)
        assert (await pk.row(event_id)).state == "accepted"

    run(rig, body)

"""U-RUN-sessions: start of a main session (05-runner.md section 6.1 steps 1-5, 10.1-10.3; 03-api.md sections 6.1, 7.1).

Real store, real sync runtime and verified logs of the RUN-03 rig, fake guest and fake VMs (`kit.py`). Every guest
request is validated against `contracts/guest/`, every outbox item against `contracts/outbox/` when it is pushed to
the rig's hub.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_guest import harness as guest_harness
from agent_guest.schema import SchemaSet
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestProtocolError, GuestRefused, GuestUnreachable
from agent_runner.inbox.pull import inbox_for
from agent_runner.sessions import harness, records
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.start import Starter, StartTimer

from ..reconcile.rig import Rig, World, seal, standard_world
from .kit import CLAUDE, KEY, ORG, OWNER, TASK, WG, Kit, desired, kit, session_id

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
SCHEMAS = SchemaSet(CONTRACTS)


def profile_world() -> World:
    """Org `a` with profile events of the org, the default workgroup, alice and the task."""
    world = standard_world()
    org = next(iter(world.orgs.values()))
    world.add_event(org.org, 1, seal(org, "alice", "org.profile", scope="org", label="op", body={"name": "Acme\nInc"}))
    world.add_event(
        org.org, 2, seal(org, "alice", "wg.profile", scope="org", label="wp", wg=WG, body={"name": "Default"})
    )
    world.add_event(
        org.org,
        3,
        seal(org, "alice", "member.profile", scope="org", label="mp", ref=OWNER, body={"display_name": "Alice A."}),
    )
    world.add_event(WG, 1, seal(org, "alice", "task.meta", label="tm", task=TASK, body={"title": "Fix `it`"}))
    return world


def run(rig: Rig, body: Any, world: World | None = None) -> None:
    async def main() -> None:
        async with rig.env(world or standard_world()) as env:
            await body(env)

    asyncio.run(main())


def call(found: Kit, name: str) -> Any:
    return next(value for call_name, value in found.guest.calls if call_name == name)


# -- happy path ---------------------------------------------------------------------------------------------------


def test_start_runs_steps_one_to_five_with_transitions_and_contract_requests(rig: Rig) -> None:
    async def body(env: Any) -> None:
        prompt = InitialPrompt(prompt_id="01930006-0000-7000-8000-000000000001", text="What is 2+2?")

        async def refresh() -> None:
            inbox = inbox_for(env.context)
            assert inbox is not None
            await inbox.pull_all()

        found = await kit(env, prompt=prompt, refresh=refresh)
        starter = Starter(found.session_env)
        report = await starter.start(desired())
        assert report.started
        assert report.path == "clone"
        assert report.reason is None
        sid = session_id()
        assert await found.transitions(sid) == [
            ("vm_assigning", None),
            ("vm_ready", None),
            ("sandbox_ready", None),
            ("agent_starting", None),
        ]
        bodies = await found.of_kind("session_transition")
        assert all(b["at"] == "2026-11-21T12:00:00.000Z" for b in bodies)
        assert "model_access" not in bodies[1]
        assert bodies[1]["agent_version"] == CLAUDE
        assert bodies[2]["model_access"] == "platform"
        assert bodies[2]["model_id"] == "deepseek/deepseek-v4.1-flash-1017"
        assert found.guest.names() == ["health", "create_session", "put_credential"]
        request = call(found, "create_session")
        assert SCHEMAS.errors("guest/sessions-create.request.json", request) == []
        assert request["initial_prompt"] == {"prompt_id": prompt.prompt_id, "text": prompt.text}
        assert request["limits"] == {
            "memory_max": 4 * 1024**3,
            "cpu_quota": 200,
            "tasks_max": 256,
            "runtime_max_sec": 28800,
        }
        assert request["model"]["env"]["ANTHROPIC_MODEL"] == "deepseek/deepseek-v4.1-flash-1017"
        expected = guest_harness.context_markdown(
            org=(ORG, "Acme Inc"),
            workgroup=(WG, "Default", True),
            task=(TASK, "Fix 'it'"),
            owner="Alice A.",
            rights="write",
            limits=(2, 3, 10),
        )
        assert request["harness"]["context_markdown"] == expected
        sid_cred, credential = call(found, "put_credential")
        assert sid_cred == sid
        assert SCHEMAS.errors("guest/session-credential.request.json", credential) == []
        assert credential["auth_value"] == "Bearer " + KEY
        local = await found.stored(sid)
        assert (local.state, local.vm_id, local.model_access, local.owner_id) == (
            "agent_starting",
            "vm-00000001",
            "platform",
            OWNER,
        )
        assert found.vms.touched == ["vm-00000001"]
        assert found.first_prompts == [sid]
        # measurements: every step marked, a log line per step with the path
        assert isinstance(report.timer, StartTimer)
        assert set(report.timer.marks) == {"vm", "vm_ready", "model", "sandbox", "agent_starting"}
        assert any(f"start_step session_id={sid} step=vm path=clone" in line for line in found.lines)
        # pushing validates every item against the contracts (rig asserts no violation)
        await env.sync.pusher.drain()
        assert [item["item_kind"] for item in env.hub.items()].count("session_transition") == 4

    run(rig, body, profile_world())


def test_a_known_session_is_never_started_twice(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        starter = Starter(found.session_env)
        assert (await starter.start(desired())).started
        again = await starter.start(desired())
        assert (again.started, again.reason) == (False, "known")
        assert found.guest.names().count("create_session") == 1

    run(rig, body)


def test_missing_names_show_unknown_and_a_refresh_failure_does_not_stop_the_start(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def broken() -> None:
            raise RuntimeError("pull")

        found = await kit(env, refresh=broken)
        report = await Starter(found.session_env).start(desired(model_id_pref="deepseek/deepseek-v4-pro"))
        assert report.started
        request = call(found, "create_session")
        assert "initial_prompt" not in request
        assert request["model"]["model_id"] == "deepseek/deepseek-v4-pro"
        markdown = request["harness"]["context_markdown"]
        assert "- Organization: unknown" in markdown
        assert "- Task owner: unknown" in markdown

    run(rig, body)


def test_budget_of_the_org_goes_into_the_credential(rig: Rig) -> None:
    async def body(env: Any) -> None:
        state = {"platform_budget": {"usd_per_org_day": 4.0, "spent_today_by_org": {ORG: 1.25}}}
        found = await kit(env, desired_state=state)
        assert (await Starter(found.session_env).start(desired())).started
        _sid, credential = call(found, "put_credential")
        assert credential["budget"] == {"usd_per_day": 4.0, "spent_usd": 1.25}

    run(rig, body)


# -- failure reasons ------------------------------------------------------------------------------------------------


async def _fails(found: Kit, expected: str, item: Any = None) -> list[tuple[str, str | None]]:
    report = await Starter(found.session_env).start(item or desired())
    assert (report.started, report.reason) == (False, expected)
    sid = (item or desired()).session_id
    found_transitions = await found.transitions(sid)
    assert found_transitions[-1] == ("failed", expected)
    local = await found.stored(sid)
    assert local.state == "failed"
    assert local.ended_at is not None
    assert any(f"start_failed session_id={sid} reason={expected}" in line for line in found.lines)
    await found.env.sync.pusher.drain()
    return found_transitions


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RunnerError("vm_start_failed"), "vm_start_failed"),
        (RunnerError("capacity_exhausted"), "capacity_exhausted"),
        (RunnerError("vm_exists"), "vm_start_failed"),
        (OSError("boom"), "vm_start_failed"),
    ],
)
def test_vm_failures(rig: Rig, error: Exception, reason: str) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        found.vms.error = error
        assert await _fails(found, reason) == [("vm_assigning", None), ("failed", reason)]
        assert found.guest.calls == []

    run(rig, body)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda k: k.guest.fail.__setitem__("health", GuestUnreachable("health")), "guest_unreachable"),
        (lambda k: setattr(k.guest, "wg_id", "01930002-0000-7000-8000-0000000000ff"), "guest_unreachable"),
        (lambda k: setattr(k.guest, "state", "unassigned"), "guest_unreachable"),
        (lambda k: setattr(k.guest, "org_id", None), "guest_unreachable"),
        (lambda k: setattr(k.vms, "client_error", RunnerError("vm_token_invalid")), "guest_unreachable"),
    ],
)
def test_guest_not_ready_fails_with_guest_unreachable(rig: Rig, setup: Any, reason: str) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        setup(found)
        assert await _fails(found, reason) == [("vm_assigning", None), ("failed", reason)]
        assert "create_session" not in found.guest.names()

    run(rig, body)


@pytest.mark.parametrize(
    ("options", "item"),
    [
        ({"key": None}, None),  # platform device without OPENROUTER_KEY_FILE
        ({"device_kind": "org"}, None),  # org device: no platform key at all
        ({}, desired(model_access_pref="subscription")),  # explicit preference without its strategy
        ({}, desired(model_access_pref="api_key")),
    ],
)
def test_no_model_access_fails_with_model_access_unavailable(rig: Rig, options: dict[str, Any], item: Any) -> None:
    async def body(env: Any) -> None:
        found = await kit(env, **options)
        transitions = await _fails(found, "model_access_unavailable", item)
        assert transitions == [("vm_assigning", None), ("vm_ready", None), ("failed", "model_access_unavailable")]
        assert "create_session" not in found.guest.names()

    run(rig, body)


def test_non_platform_choice_without_credential_support_is_unavailable(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.model.selection import Choice  # noqa: PLC0415

        found = await kit(env)
        found.session_env.strategies = {"subscription": lambda _r, _c: Choice(access="subscription", model_id=None)}
        await _fails(found, "model_access_unavailable")

    run(rig, body)


@pytest.mark.parametrize(
    ("operation", "error", "reason", "stopped"),
    [
        ("create_session", GuestRefused("sessions", 409, "id_conflict"), "sandbox_failed", True),
        ("create_session", GuestUnreachable("sessions"), "guest_unreachable", True),
        ("create_session", GuestProtocolError("sessions"), "sandbox_failed", True),
        ("put_credential", GuestRefused("credential", 400, "credential_invalid"), "sandbox_failed", True),
        ("put_credential", GuestUnreachable("credential"), "guest_unreachable", True),
    ],
)
def test_guest_refusals(rig: Rig, operation: str, error: Exception, reason: str, stopped: bool) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        found.guest.fail[operation] = error
        await _fails(found, reason)
        assert ("control", (session_id(), "stop")) in found.guest.calls

    run(rig, body)


def test_unexpected_create_answer_is_a_sandbox_failure_and_a_failing_stop_is_ignored(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        found.guest.create_answer = {"state": "failed"}
        found.guest.fail["control"] = GuestUnreachable("control")
        transitions = await _fails(found, "sandbox_failed")
        assert transitions == [("vm_assigning", None), ("vm_ready", None), ("failed", "sandbox_failed")]

    run(rig, body)


def test_stop_after_failure_skips_a_vm_that_is_gone(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        found.guest.fail["create_session"] = GuestRefused("sessions", 409, "x")

        async def gone() -> None:
            async with env.pool.connection() as conn:
                await conn.execute("UPDATE runner.vm SET state = 'deleted'")

        # the row disappears between the refusal and the cleanup: no control call
        original = found.guest.create_session

        async def create(request: Any) -> Any:
            await gone()
            async with env.pool.connection() as conn:
                await conn.execute("UPDATE runner.session SET vm_id = NULL")
            return await original(request)

        found.guest.create_session = create  # type: ignore[method-assign]
        await _fails(found, "sandbox_failed")
        assert "control" not in found.guest.names()

    run(rig, body)


# -- measurements ---------------------------------------------------------------------------------------------------


def test_timer_marks_steps_and_budgets() -> None:
    timer = StartTimer(started=10.0, path="pool")
    assert timer.step("vm") is None
    assert not timer.over_budget("vm")
    timer.mark("vm", 10.05)
    timer.mark("vm_ready", 10.06)
    timer.mark("model", 10.2)
    assert timer.mark("vm", 99.0) == pytest.approx(0.05)  # first mark wins
    assert not timer.over_budget("vm")
    assert timer.over_budget("model")  # 0.14 s > 0.05 s
    assert not timer.over_budget("vm_ready")  # no budget
    timer.mark("sandbox", 10.3)
    timer.mark("agent_starting", 10.3)
    timer.mark("prompt_delivering", 13.0)
    timer.mark("working", 13.2)
    assert timer.over_budget("prompt_delivering")
    assert not timer.over_budget("working")
    clone = StartTimer(started=0.0, path="clone")
    clone.mark("vm", 5.0)
    assert not clone.over_budget("vm")
    assert StartTimer(started=0.0).step("model") is None


def test_observe_logs_marks_until_working(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        starter = Starter(found.session_env)
        await starter.start(desired())
        sid = session_id()
        env.clock.now += 3.0
        starter.observe(sid, "prompt_delivering")
        starter.observe(sid, "prompt_delivering")
        starter.observe(sid, "working")
        starter.observe(sid, "working")  # timer gone
        starter.observe("unknown", "working")
        delivering = [line for line in found.lines if "step=prompt_delivering" in line]
        assert len(delivering) == 1
        assert "over_budget=1" in delivering[0]
        assert any(f"started session_id={sid} path=clone total_ms=3000" in line for line in found.lines)

    run(rig, body)


# -- harness block --------------------------------------------------------------------------------------------------


def test_context_markdown_equals_the_guest_format() -> None:
    arguments: dict[str, Any] = {
        "org": ("o", "Org `x`\n# heading"),
        "workgroup": ("w", "  WG  ", False),
        "task": ("t", "Title"),
        "owner": "Bob",
        "rights": "read",
        "limits": (2, 3, 10),
    }
    assert harness.context_markdown(**arguments) == guest_harness.context_markdown(**arguments)
    assert harness.inline("a\n\tb `c`") == guest_harness.inline("a\n\tb `c`")


def test_rights_and_default_flag_without_state() -> None:
    assert harness.rights(None, OWNER, WG) == "none"
    assert harness.is_default(None, WG) is False


def test_records_reject_bad_reasons() -> None:
    with pytest.raises(ValueError, match="failure reason"):
        records.check_reason("failed", "bogus")
    with pytest.raises(ValueError, match="error code"):
        records.check_reason("stopping", "Bad Reason")
    assert records.check_reason("stopping", "unassigned") == "unassigned"


def test_journal_marks_only_the_steps_after_agent_starting(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        starter = Starter(found.session_env)
        timer = StartTimer(started=env.clock())
        starter.timers["s"] = timer
        starter.observe_journal("s", "agent_starting")
        starter.observe_journal("s", "idle")
        assert timer.marks == {}
        starter.observe_journal("s", "prompt_delivering")
        assert set(timer.marks) == {"prompt_delivering"}
        starter.observe_journal("s", "failed")
        assert "s" not in starter.timers
        starter.timers["t"] = StartTimer(started=env.clock())
        starter.observe("t", "idle")  # not a measured state
        assert starter.timers["t"].marks == {}
        starter.observe("t", "stopping")
        assert "t" not in starter.timers

    run(rig, body)


def test_an_unexpected_error_fails_the_start_and_stops_the_guest_session(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)

        async def broken(_context: Any, _session: Any) -> Any:
            raise KeyError("provider")

        found.session_env.first_prompt = broken
        await _fails(found, "agent_start_failed")
        assert any("start_error session_id=" in line and "error=KeyError" in line for line in found.lines)
        found2 = found.guest.names()
        assert "create_session" not in found2  # failed before the guest session existed: nothing to stop
        # after the guest session exists, an unexpected error stops it
        original = found.session_env.vms.touch

        async def touch(_vm_id: str) -> None:
            raise RuntimeError("touch")

        found.session_env.first_prompt = lambda _c, _s: _none()
        found.vms.touch = touch  # type: ignore[method-assign,assignment]
        await _fails(found, "agent_start_failed", desired(2))
        assert ("control", (session_id(2), "stop")) in found.guest.calls
        found.vms.touch = original  # type: ignore[method-assign]

    run(rig, body)


async def _none() -> None:
    return None


def test_a_session_that_ends_during_its_start_is_not_continued(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        original = found.guest.put_credential

        async def ended_meanwhile(sid: str, credential: Any) -> None:
            await original(sid, credential)
            # the guest journal reported the agent's failure before the start pushed `sandbox_ready`
            async with found.session_env.transaction() as conn:
                await records.advance(conn, env.sync, sid, "failed", at="2026-11-21T12:00:00.000Z",
                                      reason="agent_start_failed")  # fmt: skip

        found.guest.put_credential = ended_meanwhile  # type: ignore[method-assign]
        report = await Starter(found.session_env).start(desired())
        assert (report.started, report.reason) == (False, "ended")
        assert ("control", (session_id(), "stop")) in found.guest.calls
        assert (await found.transitions(session_id()))[-1] == ("failed", "agent_start_failed")
        assert any("start_ended" in line for line in found.lines)
        # the journal may report a start state first: that is no end
        found.guest.put_credential = original  # type: ignore[method-assign]
        original_create = found.guest.create_session

        async def journal_first(request: Any) -> Any:
            answer = await original_create(request)
            async with found.session_env.transaction() as conn:
                await records.advance(conn, env.sync, request["session_id"], "agent_starting",
                                      at="2026-11-21T12:00:00.000Z")  # fmt: skip
            return answer

        found.guest.create_session = journal_first  # type: ignore[method-assign]
        again = await Starter(found.session_env).start(desired(2))
        assert again.started
        assert await found.transitions(session_id(2)) == [
            ("vm_assigning", None),
            ("vm_ready", None),
            ("agent_starting", None),
        ]

    run(rig, body)


def test_a_session_gone_from_the_store_ends_the_start(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        original = found.vms.ensure_vm

        async def ensure(org: str, wg: str) -> Any:
            async with env.pool.connection() as conn:
                await conn.execute("DELETE FROM runner.session")
            return await original(org, wg)

        found.vms.ensure_vm = ensure  # type: ignore[method-assign,assignment]
        report = await Starter(found.session_env).start(desired())
        assert report.reason == "ended"
        assert found.guest.names() == ["health"]

    run(rig, body)


def test_a_failure_that_cannot_be_recorded_is_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        starter = Starter(found.session_env)
        found.vms.error = RunnerError("vm_start_failed")
        real = found.session_env.transaction
        calls = 0

        def flaky() -> Any:
            nonlocal calls
            calls += 1
            if calls == 3:  # insert, vm_assigning, then the failure
                raise OSError("store")
            return real()

        found.session_env.transaction = flaky  # type: ignore[method-assign]
        report = await starter.start(desired())
        found.session_env.transaction = real  # type: ignore[method-assign]
        assert report.reason == "vm_start_failed"
        assert any("start_fail_unrecorded" in line and "error=OSError" in line for line in found.lines)
        assert (await found.stored(session_id())).state == "vm_assigning"

    run(rig, body)


def test_a_slow_profile_pull_is_cut_off(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def slow() -> None:
            await asyncio.sleep(30)

        found = await kit(env, refresh=slow)
        started = asyncio.get_running_loop().time()
        assert (await Starter(found.session_env).start(desired())).started
        assert asyncio.get_running_loop().time() - started < 5

    run(rig, body)

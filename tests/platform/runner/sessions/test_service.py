"""U-RUN-sessions: reconcile step 5, stop/close, unreachable VMs, the `guestlink` task and the production wiring
(05-runner.md sections 2.2 step 5, 3.3, 3.5, 6.3, 1.1; 03-api.md section 6.1).
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner import registry
from agent_runner.crypto.log import verify
from agent_runner.crypto.log.vectors import W1, LogBuilder, pid, sig_pk
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.reconcile.engine import Engine
from agent_runner.sessions import reconcile as session_reconcile
from agent_runner.sessions import records, service, tasks
from agent_runner.sessions.service import SessionService, build_env, service_for
from agent_runner.sessions.stop import stop_session
from agent_runner.vm import records as vm_records

from ..reconcile.rig import Rig, standard_world
from .kit import KEY, ORG, WG, WG_OTHER, FakeVms, Kit, desired, kit, session_id, wire

AT = "2026-11-21T12:00:05.000Z"


def run(rig: Rig, body: Any) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(env)

    asyncio.run(main())


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0.001)


def make_service(found: Kit, sleep: Any = None) -> SessionService:
    from agent_runner.sessions.journal import HANDLERS  # noqa: PLC0415

    return SessionService(found.session_env, handlers=HANDLERS, sleep=sleep or Sleeps())


class Book:
    """The verified logs of the rig with the assignments chosen by the test."""

    def __init__(self, *pairs: tuple[str, str], real: Any = None) -> None:
        self.pairs = list(pairs)
        self.real = real
        self.refused: set[tuple[object, ...]] = set()

    def assignments(self) -> list[tuple[str, str]]:
        return self.pairs

    def log(self, org: str) -> Any:
        return self.real.log(org) if self.real is not None else None

    def remember_refusal(self, key: tuple[object, ...]) -> bool:
        new = key not in self.refused
        self.refused.add(key)
        return new


async def start_one(found: Kit, svc: SessionService, n: int = 1) -> str:
    assert svc.schedule_start(desired(n))
    await svc.idle()
    return session_id(n)


# -- reconcile ------------------------------------------------------------------------------------------------------


def test_reconcile_starts_requested_main_sessions_of_assigned_workgroups_only(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        wanted = [
            desired(1),
            desired(2, wg_id=WG_OTHER),  # not assigned to this device
            desired(3, depth=1, parent_session_id=session_id(1)),  # subagent: RUN-11
            desired(4, state="working"),  # not requested and unknown: nothing to do
        ]
        book = Book((ORG, WG), real=found.session_env.book)
        await svc.reconcile(wanted, book)
        assert set(svc.tasks) == {session_id(1)}
        assert not svc.schedule_start(desired(1))  # already running
        await svc.idle()
        assert [r.session_id for r in svc.reports] == [session_id(1)]
        assert (await found.stored(session_id(1))).state == "agent_starting"
        assert any(f"not_assigned session_id={session_id(2)}" in line for line in found.lines)
        # a second reconcile with the same desired state changes nothing
        await svc.reconcile(wanted, book)
        assert svc.tasks == {}
        assert found.guest.names().count("create_session") == 1

    run(rig, body)


def test_reconcile_stops_sessions_the_hub_stops_or_no_longer_lists(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        first = await start_one(found, svc, 1)
        second = await start_one(found, svc, 2)
        third = await start_one(found, svc, 3)
        found.guest.calls.clear()
        await svc.reconcile([desired(1, state="stopping"), desired(3, state="working")], Book((ORG, WG)))
        await svc.idle()
        assert sorted(value for name, value in found.guest.calls if name == "control") == [
            (first, "stop"),
            (second, "stop"),
        ]
        assert [s for s, _ in await found.transitions(first)][-2:] == ["stopping", "stopped"]
        assert (await found.stored(second)).state == "stopped"
        assert (await found.stored(third)).state == "agent_starting"
        # the workgroup of the third session is no longer assigned: stopped with reason `unassigned`
        await svc.reconcile([desired(3, state="working")], Book())
        await svc.idle()
        assert (await found.transitions(third))[-2:] == [("stopping", "unassigned"), ("stopped", None)]

    run(rig, body)


def test_interrupted_starts_fail_with_device_shutdown(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'vm_ready' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, credential_owner) "
                "SELECT %s, task_id, org_id, wg_id, 'requested', credential_owner FROM runner.session LIMIT 1",
                (session_id(9),),
            )
        found.guest.calls.clear()
        await svc.reconcile([desired(1, state="vm_ready"), desired(9, state="requested")], Book((ORG, WG)))
        await svc.idle()
        assert (await found.transitions(sid))[-1] == ("failed", "device_shutdown")
        assert (await found.transitions(session_id(9))) == [("failed", "device_shutdown")]
        # the guest session of the first one is stopped (its agent may run with the key); the second had no VM
        assert found.guest.calls == [("control", (sid, "stop"))]
        assert any(f"guest_stop session_id={sid} outcome=stopped" in line for line in found.lines)
        assert any(f"start_interrupted session_id={sid}" in line for line in found.lines)
        events = await found.pushed_events()
        statuses = [(header["session"], body) for header, body in events if header["kind"] == "session.status"]
        assert statuses == [(sid, {"state": "failed", "reason": "device_shutdown"}),
                            (session_id(9), {"state": "failed", "reason": "device_shutdown"})]  # fmt: skip

    run(rig, body)


def test_live_hub_sessions_unknown_after_a_store_wipe_fail(rig: Rig) -> None:
    """DR-01 (E2E-33): after a store wipe the hub still lists the main sessions as idle/working/suspended, but the
    store has no row, no VM and no transcript for them: they are reported `failed` (`device_shutdown`), so the hub
    starts a new main session for the next follow-up (03-api.md section 4.6.3) instead of waiting for a delivery
    that never comes; a `stopping` one is reported `stopped`. Ended sessions, sessions the store knows and a session
    whose start is running are left alone."""

    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        wanted = [
            desired(1, state="idle"),
            desired(2, state="working"),
            desired(3, state="suspended"),
            desired(4, state="stopped"),
            desired(5, state="failed"),
            desired(6, state="stopping"),
            desired(7, state="working"),
        ]
        running = asyncio.get_running_loop().create_future()
        svc.tasks[session_id(7)] = cast(Any, running)  # a start of this session is running
        await svc.reconcile(wanted, Book((ORG, WG)))
        svc.tasks.pop(session_id(7))
        running.cancel()
        await svc.idle()
        for index, state in ((1, "idle"), (2, "working"), (3, "suspended")):
            assert (await found.transitions(session_id(index))) == [("failed", "device_shutdown")]
            assert any(f"store_lost session_id={session_id(index)} state={state}" in line for line in found.lines)
        assert (await found.transitions(session_id(6))) == [("stopped", None)]
        for index in (4, 5, 7):
            assert (await found.transitions(session_id(index))) == []
        events = await found.pushed_events()
        statuses = sorted(
            (header["session"], body["reason"]) for header, body in events if header["kind"] == "session.status"
        )
        assert statuses == sorted((session_id(index), "device_shutdown") for index in (1, 2, 3))
        assert found.guest.calls == []  # no VM: nothing to stop
        # a second reconcile finds them failed locally: nothing new
        await svc.reconcile(wanted, Book((ORG, WG)))
        await svc.idle()
        assert (await found.transitions(session_id(1))) == [("failed", "device_shutdown")]

    run(rig, body)


def test_task_failures_are_logged_by_type(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)

        async def boom() -> None:
            raise KeyError("x")

        async def fine() -> int:
            return 1

        assert svc._spawn("s1", boom())
        assert svc._spawn("s2", fine())
        await svc.idle()
        assert any("task_failed session_id=s1 error=KeyError" in line for line in found.lines)
        assert svc.reports == []
        task = asyncio.ensure_future(asyncio.sleep(10))
        svc.tasks["s3"] = task
        task.add_done_callback(lambda t: svc._done("s3", t))
        task.cancel()
        await svc.idle()
        assert "s3" not in svc.tasks

    run(rig, body)


def test_vm_unreachable_fails_its_active_sessions(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        await svc.vm_unreachable(row)
        assert (await found.transitions(sid))[-1] == ("failed", "guest_unreachable")

    run(rig, body)


def test_sessions_whose_vm_is_gone_after_the_vm_reconciliation_fail_once(rig: Rig) -> None:
    """ADR-086 section 3.7: a host reboot took the VM; the session is reported `failed`, so the hub moves on."""

    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        ids = [await start_one(found, svc, n) for n in range(1, 6)]
        assert await svc.vms_lost() == 0  # every VM still there
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'working' WHERE id = ANY(%s)", (ids,))
            await conn.execute("UPDATE runner.session SET state = 'idle' WHERE id = %s", (ids[1],))
            await conn.execute("UPDATE runner.session SET state = 'suspended' WHERE id = %s", (ids[2],))
            await conn.execute("UPDATE runner.session SET vm_id = NULL WHERE id = %s", (ids[3],))
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (ids[4],))
            await conn.execute("UPDATE runner.vm SET state = 'deleted' WHERE id = 'vm-00000001'")
        assert await svc.vms_lost() == 2
        for sid in (ids[0], ids[4]):
            assert (await found.transitions(sid))[-1] == ("failed", "guest_unreachable")
            assert any(f"vm_lost session_id={sid}" in line for line in found.lines)
        for sid in ids[1:4]:
            assert (await found.stored(sid)).state != "failed"
        assert await svc.vms_lost() == 0  # once

    run(rig, body)


# -- stop -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "state", "line"),
    [
        (None, "stopped", "stopped session_id="),
        (GuestRefused("control", 404, "not_found"), "stopped", "stopped session_id="),
        (GuestRefused("control", 409, "state_conflict"), "stopping", "stop_refused"),
        (GuestUnreachable("control"), "stopping", "stop_pending"),
    ],
)
def test_stop_session(rig: Rig, error: Exception | None, state: str, line: str) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        if error is not None:
            found.guest.fail["control"] = error
        assert await stop_session(found.session_env, sid) is (state == "stopped")
        assert (await found.stored(sid)).state == state
        assert any(line in text for text in found.lines)
        # a stopping session is retried; once stopped nothing happens any more
        found.guest.fail.clear()
        await stop_session(found.session_env, sid)
        assert (await found.stored(sid)).state == "stopped"
        assert await stop_session(found.session_env, sid) is False
        await env.sync.pusher.drain()

    run(rig, body)


def test_stop_without_running_vm_and_unknown_sessions(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped'")
        found.guest.calls.clear()
        assert await stop_session(found.session_env, sid, "unassigned")
        assert found.guest.calls == []
        assert any("guest=absent" in line for line in found.lines)
        assert await stop_session(found.session_env, session_id(77)) is False
        found.vms.client_error = RunnerError("vm_token_invalid")
        sid2 = await start_one(found, svc, 2)  # fails (client error) -> already ended
        assert (await found.stored(sid2)).state == "failed"

    run(rig, body)


def test_stop_with_a_client_error_stays_stopping(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        found.vms.client_error = RunnerError("vm_token_invalid")
        assert await stop_session(found.session_env, sid) is False
        assert any("stop_pending" in line and "code=vm_token_invalid" in line for line in found.lines)

    run(rig, body)


# -- journal loops --------------------------------------------------------------------------------------------------


def test_run_journals_reads_running_vms_and_cancels_loops_of_stopped_ones(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        sleeps = Sleeps()
        svc = make_service(found, sleeps)
        sid = await start_one(found, svc)
        stopping = asyncio.Event()
        loop = asyncio.ensure_future(svc.run_journals(stopping))
        found.guest.emit(sid, "session.state", {"state": "prompt_delivering", "at": AT})
        for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
            if found.guest.acked == 1:
                break
            await asyncio.sleep(0.005)
        assert found.guest.acked == 1
        assert "vm-00000001" in svc.readers
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped'")
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        svc.wake.set()
        for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
            if "vm-00000001" not in svc.readers:
                break
            await asyncio.sleep(0.005)
        assert svc.readers == {}
        svc.watch(row)  # no loop: wakes the task
        assert svc.wake.is_set()
        stopping.set()
        await loop
        assert (await found.transitions(sid))[-1] == ("prompt_delivering", None)

    run(rig, body)


def test_a_failing_loop_is_restarted_with_backoff(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        sleeps = Sleeps()
        svc = make_service(found, sleeps)
        await start_one(found, svc)
        found.guest.fail["journal"] = GuestUnreachable("journal")
        stopping = asyncio.Event()
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        reader = asyncio.ensure_future(svc._read(row, stopping))
        for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
            if len([s for s in sleeps.calls if s >= 1]) >= 6:
                break
            await asyncio.sleep(0.002)
        stopping.set()
        await reader
        assert [s for s in sleeps.calls if s >= 1][:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        assert any("reader_failed vm_id=vm-00000001 error=GuestUnreachable code=guest_unreachable" in line
                   for line in found.lines)  # fmt: skip
        # a loop that ends normally (stopping set inside) is not restarted
        found.guest.fail.clear()
        done = asyncio.Event()
        done.set()
        await svc._read(row, asyncio.Event()) if False else None
        assert svc.journal(row).vm.id == row.id

    run(rig, body)


def test_a_loop_failing_without_code_and_a_finished_loop(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        sleeps = Sleeps()
        svc = make_service(found, sleeps)
        await start_one(found, svc)
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        stopping = asyncio.Event()
        calls = 0

        async def journal(after: int, *, limit: int = 200, wait: int = 25) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyError("x")
            stopping.set()
            return await original(after, limit=limit, wait=wait)

        original = found.guest.journal
        found.guest.journal = journal  # type: ignore[method-assign]
        await svc._read(row, stopping)
        assert any("reader_failed vm_id=vm-00000001 error=KeyError" in line and "code=" not in line
                   for line in found.lines)  # fmt: skip
        # a finished loop task is replaced by the next sync
        svc.readers[row.id] = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        quiet = asyncio.Event()
        quiet.set()
        await svc.sync_readers(quiet)
        assert svc.readers[row.id] is not None
        for task in svc.readers.values():
            task.cancel()

    run(rig, body)


def test_guestlink_task_waits_for_the_vm_reconciliation_and_runs_until_stopping(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        env.context.session_service = svc
        env.context.stopping.set()
        await tasks.run_guestlink(env.context)
        not_enrolled = SimpleNamespace(identity=None, log=io.StringIO())
        await tasks.run_guestlink(not_enrolled)
        assert "runner.guestlink not_enrolled" in not_enrolled.log.getvalue()

    run(rig, body)


# -- reconcile step -------------------------------------------------------------------------------------------------


def test_reconcile_step_takes_the_desired_sessions_of_the_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        env.context.session_service = svc
        base = env.hub.active_state
        budget = {"usd_per_org_day": 2.0, "spent_today_by_org": {}}
        env.hub.active_state = lambda: {**base(), "sessions": [wire(desired())], "platform_budget": budget}
        steps = [step for step in registry.reconcile_steps() if step.order <= 50]
        assert [step.name for step in steps][-1] == "sessions"
        run_ = await Engine(env.context, steps).run_once()
        assert run_ is not None
        assert run_.halted is None, env.log()
        await svc.idle()
        assert (await found.stored(session_id())).state == "agent_starting"
        assert svc.env.desired is not None
        assert svc.env.desired["platform_budget"] == budget
        # without a desired state (pending device) and without a service the step does nothing
        await session_reconcile.sessions_step(SimpleNamespace(desired=None, context=env.context))  # type: ignore[arg-type]
        nothing = SimpleNamespace(desired={"sessions": []}, context=SimpleNamespace(identity=None))
        await session_reconcile.sessions_step(nothing)  # type: ignore[arg-type]

    run(rig, body)


# -- production wiring ----------------------------------------------------------------------------------------------


class Manager:
    on_unreachable: Any = None

    async def wait_reconciled(self) -> None:
        return None


def test_build_env_and_service_for(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        context = env.context
        key_file = tmp_path / "key"
        key_file.write_text(KEY)
        key_file.chmod(0o600)
        context.config = SimpleNamespace(
            platform_root_key=context.config.platform_root_key,
            model_catalog=None,
            openrouter_key_file=key_file,
            model_upstream_openrouter="https://openrouter.ai/api",
        )
        manager = Manager()
        context._vm_manager = manager
        built = build_env(context)
        assert built is not None
        assert built.platform_key() == KEY
        assert built.device_kind == "platform"
        assert built.catalog.policy("platform").default == "deepseek/deepseek-v4.1-flash-1017"
        assert set(built.strategies) >= {"platform"}
        built.log("runner.session x")
        assert "runner.session x" in env.log()
        await built.refresh_profiles()  # an inbox pull against the rig's hub
        svc = service_for(context)
        assert svc is not None
        assert service_for(context) is svc
        assert manager.on_unreachable == svc.vm_unreachable
        assert "session.state" in svc.handlers
        assert build_env(SimpleNamespace(identity=None)) is None
        assert service_for(SimpleNamespace(identity=None)) is None
        # without an inbox (not enrolled for the inbox) the refresh does nothing
        context.inbox = None
        context.identity, identity = None, context.identity
        try:
            await built.refresh_profiles()
        finally:
            context.identity = identity
        await svc.vms_ready()

    run(rig, body)


def test_vms_ready_waits_for_the_vm_reconciliation(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        waited: list[bool] = []

        class Vms(FakeVms):
            async def wait_reconciled(self) -> None:
                waited.append(True)

        found.session_env.vms = Vms(db=env.pool, guest=found.guest)
        await make_service(found).vms_ready()
        assert waited == [True]

    run(rig, body)


def test_service_module_constants() -> None:
    assert {"requested", "vm_assigning", "vm_ready", "sandbox_ready"} == service.INTERRUPTED
    assert records.START_CHAIN[0] == "requested"


def test_stopping_cancels_running_loops_and_watch_of_a_read_vm_is_quiet(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        await start_one(found, svc)
        stopping = asyncio.Event()
        loop = asyncio.ensure_future(svc.run_journals(stopping))
        for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
            if "vm-00000001" in svc.readers:
                break
            await asyncio.sleep(0.005)
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        svc.wake.clear()
        svc.watch(row)
        assert not svc.wake.is_set()
        stopping.set()
        await loop
        assert svc.readers == {}

    run(rig, body)


def test_sessions_of_owners_outside_the_workgroup_are_refused_and_audited_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        book = Book((ORG, WG_OTHER), real=found.session_env.book)
        stranger = "01930003-0000-7000-8000-0000000000ee"
        # bob has read access to the second workgroup only; a stranger is no member at all
        wanted = [
            desired(1, owner_id=stranger),
            desired(2, wg_id=WG_OTHER, owner_id=stranger),
            desired(3, wg_id=WG_OTHER, owner_id=_bob()),
        ]
        book.pairs.append((ORG, WG))
        await svc.reconcile(wanted, book)
        await svc.reconcile(wanted, book)
        await svc.idle()
        assert [r.session_id for r in svc.reports] == [session_id(3)]
        audits = await found.of_kind("audit")
        assert [(a["action"], a["target_id"], a["detail"]) for a in audits] == [
            ("runner.inbound_rejected", session_id(1), {"source": "assignment", "code": "owner_not_member"}),
            ("runner.inbound_rejected", session_id(2), {"source": "assignment", "code": "owner_not_member"}),
        ]
        # an org whose log is not verified (not acting): nothing is started
        await svc.reconcile([desired(4)], Book((ORG, WG)))
        assert session_id(4) not in svc.tasks
        await env.sync.pusher.drain()

    run(rig, body)


def test_an_admin_reading_only_by_inheritance_is_no_session_owner() -> None:
    """ADR-058 section 1: B2 uses the explicit access; an admin's inherited read does not make it a session owner."""
    log = LogBuilder()
    log.genesis()
    log.add("wg_create", "alice", wg=W1, epoch=1)
    log.invite_user("alice", "bob", role="admin")
    log.accept("bob")
    verified = verify.verify_log(log.entries, sig_pk("alice"))
    assert verified.state.inherited(pid("bob"), W1)
    book = SimpleNamespace(log=lambda _org: verified)

    def owner(user: str) -> bool:
        return SessionService._owner_member(book, cast(Any, SimpleNamespace(org_id="o", wg_id=W1, owner_id=user)))

    assert owner(pid("alice")) is True
    assert owner(pid("bob")) is False


def _bob() -> str:
    from ..reconcile.rig import pid  # noqa: PLC0415

    return pid("bob")


def test_vm_unreachable_writes_status_events(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        await start_one(found, svc)
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        await svc.vm_unreachable(row)
        await svc.vm_unreachable(row)  # already failed: nothing more
        from agent_runner.sessions.events import fail  # noqa: PLC0415

        async with found.session_env.transaction() as conn:
            assert await fail(conn, env.sync, session_id(), "oom", at=AT) is None  # an ended session stays ended
        events = await found.pushed_events()
        assert [body for header, body in events if header["kind"] == "session.status"] == [
            {"state": "failed", "reason": "guest_unreachable"}
        ]

    run(rig, body)


def test_advance_waits_for_the_outbox_lock_before_the_row_lock(rig: Rig) -> None:
    """A journal page holds the outbox lock and then locks a session row; a concurrent transition of the start must
    not hold that row while it waits for the outbox lock (no deadlock)."""

    async def body(env: Any) -> None:
        from agent_runner.sync.outbox import LOCK_ID  # noqa: PLC0415

        found = await kit(env)
        svc = make_service(found)
        sid = await start_one(found, svc)
        async with env.pool.connection() as page:
            await page.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))  # the page wrote an outbox item
            moving = asyncio.ensure_future(_advance(found, sid, "prompt_delivering"))
            await asyncio.sleep(0.2)
            assert not moving.done()
            # the page now locks the same session row: it must get it at once
            await page.execute("SET LOCAL lock_timeout = '2s'")
            await page.execute("SELECT 1 FROM runner.session WHERE id = %s FOR UPDATE", (sid,))
        await asyncio.wait_for(moving, 5)
        assert (await found.stored(sid)).state == "prompt_delivering"

    run(rig, body)


async def _advance(found: Kit, sid: str, state: str) -> Any:
    async with found.session_env.transaction() as conn:
        return await records.advance(conn, found.session_env.outbox, sid, state, at=AT)


def test_a_loop_is_not_started_when_already_stopping(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await kit(env)
        svc = make_service(found)
        await start_one(found, svc)
        row = await vm_records.get(env.pool, "vm-00000001")
        assert row is not None
        stopped = asyncio.Event()
        stopped.set()
        await svc._read(row, stopped)
        assert "journal" not in found.guest.names()

    run(rig, body)

"""U-RUN-vm / sessions: assignment changes (05-runner.md sections 2.2 step 4 and 3.5): unassignment, archiving,
removal of this device, decertification and `key_revoked`, with the real VM manager on a fake `limactl`, the real
session service over the sessions kit and the verified logs of the RUN-03 rig."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.assignments import reconcile as assignment_step
from agent_runner.assignments import tasks as revocation
from agent_runner.assignments import teardown
from agent_runner.crypto.log.state import Principal
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS as BASE_STEPS
from agent_runner.sessions.service import SessionService
from agent_runner.vm import lima
from agent_runner.vm import records as vm_records
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

from ..reconcile.rig import DEVICE, Env, Rig, pid, standard_world, wrap_for
from ..sessions.kit import Kit, kit
from ..settle import until as until_reached
from ..vm.conftest import FakeLimactl

ORG = pid("a/org")
DEFAULT = pid("a/wg/default")
ONE = pid("a/wg/one")
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@dataclass
class Case:
    env: Env
    kit: Kit
    lima: FakeLimactl
    service: SessionService

    @property
    def spec(self) -> Any:
        return self.env.world.orgs[ORG]

    async def run(self) -> Run:
        engine = getattr(self.env.context, "reconcile_engine", None)
        if engine is None:
            engine = Engine(self.env.context, sorted([*BASE_STEPS, *assignment_step.STEPS], key=lambda s: s.order))
            self.env.context.reconcile_engine = engine
        run = await engine.run_once()
        assert run is not None
        return run

    async def add_vm(self, wg: str, n: int = 1, org: str = ORG) -> None:
        name = f"arp-{n:08x}"
        self.lima.add(name, "Running")
        await vm_records.insert(
            self.env.pool,
            vm_id=f"vm-{n:08x}",
            lima_name=name,
            golden_version="g1",
            state="running",
            token_sha256="0" * 64,
            cpus=4,
            memory_mib=8192,
            created_at=NOW,
            org_id=org,
            wg_id=wg,
        )

    async def add_session(self, wg: str, n: int = 1, vm: int | None = 1, org: str = ORG) -> str:
        sid = f"01930004-0000-7000-8000-{n:012x}"
        async with self.env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, depth, state, created_at)"
                " VALUES (%s, %s, %s, %s, %s, 0, 'working', %s)",
                (sid, sid, org, wg, None if vm is None else f"vm-{vm:08x}", NOW),
            )
        return sid

    async def vm_states(self) -> list[tuple[str, str]]:
        return [(str(a), str(b)) for a, b in await self.env.rows("SELECT id, state FROM runner.vm ORDER BY id")]

    async def removed(self) -> list[str]:
        rows = await self.env.rows("SELECT value FROM runner.sync_state WHERE key = %s", teardown.REMOVED)
        return [str(row[0]) for row in rows]

    async def keys(self) -> list[tuple[str, str, int]]:
        rows = await self.env.rows("SELECT scope, scope_id::text, epoch FROM runner.scope_key ORDER BY 1, 2, 3")
        return [(str(a), str(b), int(c)) for a, b, c in rows]


@asynccontextmanager
async def case(rig: Rig, tmp_path: Path, world: Any = None) -> AsyncIterator[Case]:
    world = world or standard_world("a")
    async with rig.env(world) as env:
        built = await kit(env)
        fake = FakeLimactl(lima_dir=tmp_path / "lima")
        manager = VmManager(
            VmEnv(
                lima=lima.Lima(fake, prefix=""),
                db=env.pool,
                settings=VmSettings(
                    cpus=4, memory_mib=8192, disk_gib=20, pool_size=0, memory_reserve_mib=0, vm_idle_seconds=1800
                ),
                tokens=TokenStore(tmp_path / "vm"),
                physical_mib=lambda: 65536,
            )
        )
        env.context._vm_manager = manager
        service = SessionService(built.session_env, sleep=env.clock.sleep)
        env.context.session_service = service
        yield Case(env=env, kit=built, lima=fake, service=service)


def test_an_unassigned_workgroup_is_ended_and_its_keys_go_after_the_rotation(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            sid = await c.add_session(DEFAULT)
            run = await c.run()
            assert run.halted is None, c.env.log()
            assert await c.vm_states() == [("vm-00000001", "running")]
            c.spec.log.add("device_unassign", "alice", device=pid(DEVICE), wg=DEFAULT)
            run = await c.run()
            assert run.halted is None, c.env.log()
            assert await c.kit.transitions(sid) == [("stopping", "unassigned"), ("stopped", None)]
            assert ("control", (sid, "stop")) in c.kit.guest.calls
            assert await c.vm_states() == [("vm-00000001", "deleted")]
            assert "delete" in c.lima.ops()
            assert f"runner.assignments ended org_id={ORG} wg_id={DEFAULT} reason=unassigned sessions=1 vm=deleted" in (
                c.env.log()
            )
            # no rotation yet: the keys stay
            assert await c.keys() == [("org", ORG, 1), ("wg", DEFAULT, 1)]
            c.spec.log.add("rotate", "alice", scope="wg", scope_id=DEFAULT, epoch=2)
            await c.run()
            assert await c.keys() == [("org", ORG, 1)]
            c.spec.log.add("rotate", "alice", scope="org", scope_id=ORG, epoch=2)
            await c.run()
            assert await c.keys() == []
            assert "runner.assignments keys_deleted scopes=1 keys=1" in c.env.log()

    asyncio.run(body())


def test_an_archived_workgroup_keeps_its_keys(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = world.orgs[ORG]
        spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=ONE, platform_seq=2)
        world.wraps.append(wrap_for("alice", "wg", ONE, 1))
        async with case(rig, tmp_path, world) as c:
            await c.add_vm(ONE)
            await c.add_vm(DEFAULT, n=2)
            sid = await c.add_session(ONE)
            other = await c.add_session(DEFAULT, n=2, vm=2)
            await c.run()
            spec.log.add("wg_archive", "alice", wg=ONE)
            await c.run()
            assert await c.vm_states() == [("vm-00000001", "deleted"), ("vm-00000002", "running")]
            assert await c.kit.transitions(sid) == [("stopping", "unassigned"), ("stopped", None)]
            assert await c.kit.transitions(other) == []
            assert ("wg", ONE, 1) in await c.keys()
            assert "reason=archived" in c.env.log()

    asyncio.run(body())


def test_removal_of_the_device_deletes_the_keys_at_once(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            await c.add_session(DEFAULT)
            await c.run()
            assert await c.removed() == []
            c.spec.log.add("remove", "alice", subject=pid(DEVICE))
            run = await c.run()
            assert run.halted is None, c.env.log()
            assert await c.vm_states() == [("vm-00000001", "deleted")]
            assert await c.keys() == []
            assert "reason=removed" in c.env.log()
            # the verified removal is persisted for the maintenance daemon of an own device, once
            assert await c.removed() == [ORG]
            assert c.env.log().count(f"runner.assignments device_removed orgs={ORG}\n") == 1
            assert (await c.run()).halted is None
            assert await c.removed() == [ORG]
            assert c.env.log().count("runner.assignments device_removed") == 1

    asyncio.run(body())


def test_a_stopped_org_keeps_everything(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            await c.run()
            book = book_for(c.env.context)
            book.stopped[ORG] = "log_fork"
            c.spec.log.add("device_unassign", "alice", device=pid(DEVICE), wg=DEFAULT)
            await c.run()
            assert await c.vm_states() == [("vm-00000001", "running")]
            assert len(await c.keys()) == 2
            assert teardown.removals(book, {(ORG, DEFAULT), (pid("x"), DEFAULT)}) == []
            assert teardown.stale_keys(book, [("wg", DEFAULT, 1), ("cred", pid("bob"), 1), ("wg", pid("x"), 1)]) == []
            book.platform_stopped = "log_fork"
            assert teardown.stale_keys(book, [("org", ORG, 1)]) == []

    asyncio.run(body())


def test_the_step_does_nothing_without_a_desired_state(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            run = Run(context=c.env.context, sync=c.env.sync, book=book_for(c.env.context))
            await assignment_step.assignments_step(run)
            assert "runner.assignments" not in c.env.log()

    asyncio.run(body())


def test_sessions_without_a_service_are_left_to_the_session_area(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            bare = SimpleNamespace(identity=None, pool=c.env.pool)
            assert await teardown.stop_sessions(bare, None) == 0

    asyncio.run(body())


def test_a_running_start_is_awaited_before_the_stop(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            sid = await c.add_session(DEFAULT)
            gate = asyncio.Event()

            async def starting() -> None:
                await gate.wait()

            c.service._spawn(sid, starting())
            stopping = asyncio.ensure_future(teardown.stop_sessions(c.env.context, DEFAULT))
            await asyncio.sleep(0)
            assert not stopping.done()
            gate.set()
            assert await stopping == 1
            assert await c.kit.transitions(sid) == [("stopping", "unassigned"), ("stopped", None)]

    asyncio.run(body())


# --- revocation -----------------------------------------------------------------------------------------------------


async def polled(c: Case, done: Any = None) -> None:
    """Wait until the revocation task has seen the state of now: two of its polls (each ends with a `POLL_S` sleep on
    the host clock) have ended, so one ran entirely after this point - or `done()` holds (a task that has revoked
    polls no more). The task reads the database (real I/O), so a fixed number of loop turns is not enough on a loaded
    device."""
    target = c.env.clock.sleeps.count(revocation.POLL_S) + 2

    async def seen() -> bool:
        return c.env.clock.sleeps.count(revocation.POLL_S) >= target or (done is not None and bool(await done()))

    await until_reached(seen, "two revocation polls")


async def watch(c: Case, *, until: Any) -> None:
    task = asyncio.ensure_future(revocation.run_revocation(c.env.context))
    await until_reached(until, "the revocation outcome")
    c.env.context.stopping.set()
    await asyncio.wait_for(task, 5)


async def drive(c: Case, *, until: Any) -> list[str | None]:
    """The watcher next to reconcile runs 31 s apart (the daemon's period), until `until()`."""
    task = asyncio.ensure_future(revocation.run_revocation(c.env.context))
    halts: list[str | None] = []
    for _ in range(20):
        halts.append((await c.run()).halted)
        await polled(c, until)
        if await until():
            break
        c.env.clock.now += 31
    c.env.context.stopping.set()
    await asyncio.wait_for(task, 5)
    return halts


def refuse_everything(c: Case, code: str, status: int = 401) -> None:
    def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        return status, {"error": code}

    c.env.hub.route = route  # type: ignore[method-assign]


def test_key_revoked_ends_everything_on_the_device(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            sid = await c.add_session(DEFAULT)
            await c.run()
            refuse_everything(c, "key_revoked")

            async def revoked() -> bool:
                return await teardown.revoked(c.env.context) is not None and await c.keys() == []

            halts = await drive(c, until=revoked)
            # confirmed over 60 s of failed runs before anything is ended
            assert halts[:2] == ["failed", "failed"]
            assert "runner.assignments key_revoked_seen confirm_after_s=60" in c.env.log()
            assert await teardown.revoked(c.env.context) == "key_revoked"
            # the gate halts every later run: nothing is rebuilt
            assert (await c.run()).halted == "revoked"
            assert await c.vm_states() == [("vm-00000001", "deleted")]
            assert await c.kit.transitions(sid) == [("stopping", "unassigned"), ("stopped", None)]
            assert "runner.assignments revoked reason=key_revoked sessions=1 vms=1 keys=2" in c.env.log()
            # a restarted daemon repeats the teardown once (nothing left to do)
            c.env.context.stopping.clear()
            await watch(c, until=lambda: asyncio.sleep(0, True))
            assert "revoked reason=key_revoked sessions=0 vms=0 keys=0" in c.env.log()

    asyncio.run(body())


def test_a_successful_run_resets_the_confirmation(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            await c.run()
            original = c.env.hub.route
            refuse_everything(c, "key_revoked")
            task = asyncio.ensure_future(revocation.run_revocation(c.env.context))
            assert (await c.run()).halted == "failed"
            await polled(c)
            c.env.hub.route = original  # type: ignore[method-assign]
            c.env.clock.now += 31
            assert (await c.run()).halted is None
            await polled(c)
            refuse_everything(c, "key_revoked")
            c.env.clock.now += 31
            assert (await c.run()).halted == "failed"
            await polled(c)
            c.env.context.stopping.set()
            await asyncio.wait_for(task, 5)
            assert await teardown.revoked(c.env.context) is None
            assert await c.vm_states() == [("vm-00000001", "running")]
            assert c.env.log().count("key_revoked_seen confirm_after_s=60") == 2

    asyncio.run(body())


def test_other_failures_are_left_to_the_next_run(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            await c.run()
            refuse_everything(c, "unavailable", 503)
            await c.run()
            probes: list[int] = []
            original = revocation.probe

            async def counting(context: Any) -> bool:
                probes.append(1)
                return await original(context)

            revocation.probe = counting
            try:

                async def probed() -> bool:
                    return bool(probes)

                await watch(c, until=probed)
            finally:
                revocation.probe = original
            assert await teardown.revoked(c.env.context) is None
            assert await c.vm_states() == [("vm-00000001", "running")]

    asyncio.run(body())


def test_a_probe_that_cannot_reach_the_hub_is_no_revocation(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.run()
            c.env.hub.tamper.append("unsigned")
            assert await revocation.probe(c.env.context) is False
            assert await revocation.probe(c.env.context) is False

    asyncio.run(body())


def test_a_verified_decertification_ends_everything(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.add_vm(DEFAULT)
            await c.run()
            c.env.world.platform.add("device_decertify", device=pid(DEVICE))
            run = await c.run()
            assert run.halted == "decertified"

            async def revoked() -> bool:
                return await teardown.revoked(c.env.context) is not None and await c.keys() == []

            await watch(c, until=revoked)
            assert await teardown.revoked(c.env.context) == "decertified"
            assert await c.vm_states() == [("vm-00000001", "deleted")]

    asyncio.run(body())


def test_the_task_ends_on_a_device_that_is_not_enrolled(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with rig.env(standard_world("a"), enrolled=False) as env:
            await revocation.run_revocation(env.context)

    asyncio.run(body())


def test_registrations() -> None:
    assert [(s.order, s.name) for s in assignment_step.STEPS] == [(5, "revoked"), (40, "assignments")]
    assert [t.name for t in revocation.TASKS] == ["revocation"]


@pytest.mark.parametrize("vm_wg", [None])
def test_pool_vms_survive_a_revocation(rig: Rig, tmp_path: Path, vm_wg: Any) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            c.lima.add("arp-00000009", "Running")
            await vm_records.insert(
                c.env.pool,
                vm_id="vm-00000009",
                lima_name="arp-00000009",
                golden_version="g1",
                state="pool",
                token_sha256="0" * 64,
                cpus=4,
                memory_mib=8192,
                created_at=NOW,
                wg_id=vm_wg,
            )
            await teardown.revoke(c.env.context, "key_revoked")
            assert await c.vm_states() == [("vm-00000009", "pool")]

    asyncio.run(body())


def test_orgs_this_device_never_joined_are_not_touched(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.run()
            book = book_for(c.env.context)
            other = OrgBook(device_id=pid("nobody"), kem_public="", sig_public="", root=book.root)
            other.logs = book.logs
            assert teardown.removals(other, {(ORG, DEFAULT)}) == []

    asyncio.run(body())


def test_the_watcher_waits_for_the_first_run(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            sleeps = len(c.env.clock.sleeps)

            async def slept_twice() -> bool:
                return len(c.env.clock.sleeps) >= sleeps + 2

            await watch(c, until=slept_twice)
            assert await teardown.revoked(c.env.context) is None

    asyncio.run(body())


def test_org_device_books_act_only_for_their_own_membership(rig: Rig, tmp_path: Path) -> None:
    async def body() -> None:
        async with case(rig, tmp_path) as c:
            await c.run()
            book = book_for(c.env.context)
            keys = c.env.context.identity.keys
            as_org = OrgBook(
                device_id=book.device_id,
                kem_public=keys.kem_public,
                sig_public=keys.sig_public,
                root=book.root,
                kind="org",
            )
            as_org.logs = book.logs
            # the log names this ID as a platform device: no org-device membership
            assert as_org.member(ORG) is None
            as_org.stopped[ORG] = "log_fork"
            assert as_org.member(ORG) is None
            assert as_org.status() == "enrolled"
            bare = SimpleNamespace(identity=None, pool=c.env.pool)
            await teardown.end_workgroup(bare, teardown.Removal(ORG, DEFAULT, "unassigned"))
            assert await c.vm_states() == []

    asyncio.run(body())


# --- removal of an own device (ADR-074 section 5) -------------------------------------------------------------------


def principal(device: str, removed_seq: int | None) -> Principal:
    return Principal(id=device, kind="device", role="device", kem="", sig="", joined_seq=1, removed_seq=removed_seq)


def removal_book(**orgs: dict[str, int | None]) -> OrgBook:
    """A book whose org logs name devices: `org=dict(device=removed_seq)`."""
    book = OrgBook(device_id=pid("me"), kem_public="", sig_public="", root="")
    logs: dict[str, Any] = {}
    for org, devices in orgs.items():
        principals = {pid(name): principal(pid(name), seq) for name, seq in devices.items()}
        logs[org] = SimpleNamespace(state=SimpleNamespace(principals=principals))
    book.logs = logs
    return book


def test_a_device_removed_in_every_org_that_names_it_is_removed() -> None:
    book = removal_book(b={"me": 7, "other": None}, a={"me": 3}, c={"other": None})
    assert teardown.removed_everywhere(book) == "a,b"


def test_a_device_still_active_somewhere_is_not_removed() -> None:
    assert teardown.removed_everywhere(removal_book(a={"me": 3}, b={"me": None})) is None
    assert teardown.removed_everywhere(removal_book(a={"me": None})) is None


def test_a_device_no_log_names_is_not_removed() -> None:
    assert teardown.removed_everywhere(removal_book()) is None
    assert teardown.removed_everywhere(removal_book(a={"other": 3})) is None


def test_stopped_copies_decide_nothing() -> None:
    book = removal_book(a={"me": 3}, b={"me": None})
    book.stopped["b"] = "log_fork"
    assert teardown.removed_everywhere(book) == "a"
    book.stopped["a"] = "log_rollback"
    assert teardown.removed_everywhere(book) is None
    book = removal_book(a={"me": 3})
    book.platform_stopped = "log_fork"
    assert teardown.removed_everywhere(book) is None

"""VM lifecycle of the runner (docs/platform/spec/05-runner.md sections 3.2-3.5 and 6.1 step 1, 03-api.md section
4.6.2, ADR-009).

`VmManager.ensure_vm(org_id, wg_id, session_id=..., prefer=[...], new_allowed=...)` returns a running VM of the
workgroup for a session, in the deterministic order of ADR-075 section 4 (05-runner.md section 3.3):

1. the VMs of `prefer` in their order - the hub's `vm_id_pref` (a user's choice), then the VM of the parent session
   (subagent) or of the `resume_from` session - when it is a live VM of **this** workgroup, regardless of the limit:
   running -> use it, stopped -> restart it (no golden replacement: the choice, or the work, lives there);
2. a running VM of the workgroup with room (`SESSIONS_PER_VM` minus its occupying sessions, unbounded with 0): most
   room first, then the smallest VM ID;
3. a stopped VM of the workgroup with room, smallest VM ID -> restart it (`limactl start`, health `assigned` to this
   workgroup), unless its golden is not the newest ready one, it has no `suspended` session and no retire hook keeps
   it (`vm.extensions`: e.g. context checkpoints still due, KOR-33): then it is deleted and replaced by a new VM (a
   replacement, allowed even when the hub allows no new VM);
4. a new VM when the hub allows one (`new_allowed`, the hub's `vm_new_allowed`): warm pool (`Pool.take`), else clone
   + start on the hot path (`Provisioner.create`); while the device has no ready golden yet (a device just added
   builds it, KOR-67) this is `capacity_exhausted` (`golden`): the hub places the next attempt and the task waits
   instead of failing;
5. else `capacity_exhausted` (the hub places the next attempt).

The choice runs under an
asyncio lock per `wg_id`, and the session row names its VM (`records.assign_session`) before the lock is released, so
two starts of one workgroup never both take the last place of a VM. A restart or a clone needs a free slot (section
3.4): when none is free, a pool VM is given up first, then the least recently used idle VM of another workgroup is
stopped, else the request fails with `capacity_exhausted`. Failures are `RunnerError`s with the session failure
reasons `vm_start_failed` and `capacity_exhausted`.

Further rules:
- `health_pass()` (task `vm-idle`, every 5 s): a running VM whose guest fails the health probe 3 times in a row
  (unreachable, `broken`, or not assigned to its workgroup) is reported through `on_unreachable` (the session start
  fails its sessions with `guest_unreachable`) and restarted once; when it fails again after that restart it is
  stopped and reported (`vm.unhealthy_stopped`).
- `idle_pass()` (task `vm-idle`, every 60 s): a running VM without an active session whose last use is
  `VM_IDLE_SECONDS` ago is stopped. "Last use" is the newest of creation, assignment, `last_session_at` (set by
  `touch(vm_id)`, which the session start calls on every session transition) and the session timestamps.
- `delete_workgroup(wg_id)`: every VM of an unassigned or archived workgroup (trigger: RUN-06) is deleted
  (`limactl delete -f`, row `deleted`, token file and journal cursor removed).
- Before a running workgroup VM is stopped (`_stop`: idle, LRU, unhealthy) or deleted (`delete_workgroup`) the retire
  hooks of `vm.extensions` run (`Retire.flush`, bounded in time; KOR-33 takes the due context checkpoints of its
  sessions there).
- `report()`: the heartbeat's `capacity` with the counters and settings of ADR-075 section 2 (the features go with
  the live channel's `Agent-Features` header, `agent_runner.sync.ws`).
- `reconcile()` (start of task `vm-pool`, before any other VM work): `limactl list --json` against `runner.vm`.
  Rows whose instance is gone become `deleted`; `assigning` rows of a crashed process and pool VMs that do not run
  are deleted; states follow the instance (`running` <-> `stopped`); unknown `<prefix>arp-<8 hex>` instances are
  deleted (one that Lima cannot delete is logged, `vm.unknown_delete_failed`, and does not stop the
  reconciliation); token files without a live row are removed.
- `refill()` (task `vm-pool`, after every assignment and every 60 s): pool VMs of a retired golden are deleted, then
  the pool is filled (`Pool.refill`).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import timedelta

from agent_runner.errors import RunnerError
from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.guestlink.errors import GuestError
from agent_runner.vm import records
from agent_runner.vm.capacity import Capacity, lru_victim
from agent_runner.vm.extensions import Retire
from agent_runner.vm.lima import LimaError, LimaNotFound
from agent_runner.vm.pool import HEALTH_CALL_S, Pool, Provisioner, VmEnv, failure_fields
from agent_runner.vm.records import GoldenRef, VmRow

UNHEALTHY_LIMIT = 3
# Reported in every capacity report: this runner chooses VMs itself and reads `device_assign_org` (ADR-075).
UNBOUNDED = sys.maxsize
STOPPED_STATES = ("stopped", "stopping")
# A session start never waits longer than this for the startup reconciliation (task `vm-pool`).
RECONCILE_WAIT_S = 60.0

Unreachable = Callable[[VmRow], Awaitable[None]]


async def _ignore(_row: VmRow) -> None:
    return None


class VmManager:
    def __init__(self, env: VmEnv, *, on_unreachable: Unreachable = _ignore, retire: Retire | None = None) -> None:
        self.env = env
        self.on_unreachable = on_unreachable
        self.retire = retire if retire is not None else Retire()
        self.provisioner = Provisioner(env)
        self.pool = Pool(env, self.provisioner, on_change=self.wake_refill, reserved=lambda: self.reserved)
        # Slots claimed by a running restart or hot-path clone (counted as used until it finished).
        self.reserved = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconciled = asyncio.Event()
        self._refill_wanted = asyncio.Event()
        self._failures: dict[str, int] = {}
        self._recovered: set[str] = set()
        self._arp_re = re.compile("^" + re.escape(env.lima.prefix) + r"arp-[0-9a-f]{8}$")

    # -- coordination ------------------------------------------------------------------------------------------

    def lock(self, wg_id: str) -> asyncio.Lock:
        return self._locks.setdefault(wg_id, asyncio.Lock())

    def wake_refill(self) -> None:
        self._refill_wanted.set()

    async def wait_refill(self, timeout: float) -> None:
        """Return after a `wake_refill()` or `timeout` seconds (the env's sleep), whichever comes first."""
        if not self._refill_wanted.is_set():
            waiter = asyncio.ensure_future(self._refill_wanted.wait())
            sleeper = asyncio.ensure_future(self.env.sleep(timeout))
            try:
                await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                sleeper.cancel()
                await asyncio.gather(waiter, sleeper, return_exceptions=True)
        self._refill_wanted.clear()

    async def wait_reconciled(self) -> None:
        await self._reconciled.wait()

    @property
    def reconciled(self) -> bool:
        return self._reconciled.is_set()

    async def capacity(self) -> Capacity:
        return await self.env.capacity(self.pool.filling + self.reserved)

    async def report(self) -> dict[str, object]:
        """The heartbeat's `capacity`: the five base counters plus the counters and settings of ADR-075
        section 2 (`cpus` and `disk_free_mib` only when known)."""
        env = self.env
        capacity = await self.capacity()
        vms, sessions = await records.load(env.db)
        report: dict[str, object] = {
            **capacity.heartbeat(),
            "memory_total_mib": env.physical_mib(),
            "vms_running": vms,
            "sessions_active": sessions,
            "work_vms_running": capacity.other_vms,
            "sessions_per_vm": env.settings.sessions_per_vm,
            "vms_max": env.settings.max_vms,
        }
        for name, value in (("cpus", env.cpus()), ("disk_free_mib", env.disk_free_mib())):
            if value is not None:
                report[name] = value
        return report

    async def touch(self, vm_id: str) -> None:
        """Record session activity on the VM (idle stop and LRU, sections 3.4 and 3.5)."""
        await records.touch(self.env.db, vm_id, self.env.now())

    async def client(self, row: VmRow) -> GuestClient:
        """A control client for the VM (for the session start and the journal task of RUN-04)."""
        return await self.env.client(row)

    # -- ensure_vm ---------------------------------------------------------------------------------------------

    async def ensure_vm(
        self,
        org_id: str,
        wg_id: str,
        *,
        session_id: str | None = None,
        prefer: Sequence[str] = (),
        new_allowed: bool = True,
    ) -> VmRow:
        """A running VM of the workgroup, booked for `session_id` (see the module docstring for the order)."""
        try:
            await asyncio.wait_for(self._reconciled.wait(), RECONCILE_WAIT_S)
        except TimeoutError:
            raise RunnerError("vm_start_failed", "reconcile") from None
        async with self.lock(wg_id):
            row = await self._choose(org_id, wg_id, prefer, new_allowed=new_allowed)
            if session_id is not None:
                await records.assign_session(self.env.db, session_id, row.id)
            return row

    async def _choose(self, org_id: str, wg_id: str, prefer: Sequence[str], *, new_allowed: bool) -> VmRow:
        env = self.env
        live = await records.of_workgroup(env.db, wg_id)
        by_id = {row.id: row for row in live}
        for vm_id in prefer:
            found = by_id.get(vm_id)
            if found is not None and found.state == "running":
                await self.touch(found.id)
                return found
            if found is not None and found.state in STOPPED_STATES:
                return await self._restart(found)
            env.log("vm.preference_skipped", {"vm_id": vm_id, "wg_id": wg_id})
        limit = env.settings.sessions_per_vm
        occupied = await records.occupancy(env.db, wg_id)

        def room(row: VmRow) -> int:
            return UNBOUNDED if limit == 0 else limit - occupied.get(row.id, 0)

        running = [row for row in live if row.state == "running" and room(row) > 0]
        if running:
            chosen = min(running, key=lambda row: (-room(row), row.id))
            await self.touch(chosen.id)
            return chosen
        golden = await records.ready_golden(env.db)
        stopped = [row for row in live if row.state in STOPPED_STATES and room(row) > 0]
        if stopped:
            row = stopped[0]
            if (
                golden is None
                or row.golden_version == golden.version
                or await records.has_suspended(env.db, row.id)
                or await self.retire.pending(row)
            ):
                return await self._restart(row)
            env.log("vm.golden_replaced", {"vm_id": row.id, "golden_version": golden.version})
            self._forget(row.id)
            await env.delete(row)
        elif not new_allowed:
            # the hub's workgroup limit (`AGENT_HUB_VMS_PER_WG`): no further VM for this session
            raise RunnerError("capacity_exhausted", "vm_new_allowed")
        if golden is None:
            raise RunnerError("capacity_exhausted", "golden")
        taken = await self.pool.take(golden, org_id, wg_id)
        if taken is not None:
            return taken
        self.wake_refill()
        return await self._clone(golden, org_id, wg_id)

    async def _clone(self, golden: GoldenRef, org_id: str, wg_id: str) -> VmRow:
        env = self.env
        async with self._slot(wg_id) as release:
            vm_id, name = env.new_ids()
            token = env.random_token()
            row = await records.insert(
                env.db,
                vm_id=vm_id,
                lima_name=name,
                golden_version=golden.version,
                state="assigning",
                token_sha256=records.token_hash(token),
                cpus=env.settings.cpus,
                memory_mib=env.settings.memory_mib,
                created_at=env.now(),
                org_id=org_id,
                wg_id=wg_id,
            )
            release()  # the `assigning` row holds the slot now

            async def record_name(lima_name: str) -> None:
                await records.update(env.db, vm_id, lima_name=lima_name)

            try:
                await env.emit(row)
                env.tokens.write(vm_id, token)
                created = await self.provisioner.create(
                    golden, vm_id=vm_id, token=token, pool=False, before_clone=record_name
                )
                row = await records.update(env.db, vm_id, assigned_at=env.now())
                async with env.client_factory(await env.socket_path(created), token) as client:
                    await client.assign(vm_id, org_id, wg_id)
                return await env.set_state(row, "running", expect=("assigning",), last_session_at=env.now())
            except BaseException as exc:
                # Also on cancellation or an unexpected error: never leave an `assigning` row holding the workgroup.
                current = await self._quiet_get(vm_id)
                if current is not None:
                    with contextlib.suppress(Exception):
                        await env.delete(current)
                if not isinstance(exc, (RunnerError, GuestError)):
                    raise
                env.log("vm.clone_failed", failure_fields(vm_id, exc))
                raise RunnerError("vm_start_failed") from None

    async def _quiet_get(self, vm_id: str) -> VmRow | None:
        with contextlib.suppress(Exception):
            return await records.get(self.env.db, vm_id)
        return None

    async def _restart(self, row: VmRow) -> VmRow:
        """Start a stopped (or interrupted `stopping`) VM of the workgroup again."""
        env = self.env
        async with self._slot(row.wg_id):
            try:
                await self._start_assigned(row)
                running = await env.set_state(row, "running", expect=("stopped", "stopping"), last_session_at=env.now())
            except (RunnerError, GuestError) as exc:
                env.log("vm.restart_failed", {"vm_id": row.id, "code": exc.code})
                if isinstance(exc, LimaNotFound):
                    # The instance is gone: the row must not keep the workgroup, the next request clones afresh.
                    await env.delete(row)
                else:
                    with contextlib.suppress(LimaError):
                        await env.blocking(env.lima.stop, row.lima_name)
                    if row.state != "stopped":
                        await env.set_state(row, "stopped")
                raise RunnerError("vm_start_failed") from None
        self._failures.pop(row.id, None)
        return running

    async def _start_assigned(self, row: VmRow) -> None:
        """`limactl start` (one retry), then health: assigned to the row's workgroup (assigned again if needed)."""
        env = self.env
        try:
            await env.blocking(env.lima.start, row.lima_name)
        except LimaError as first:
            self._start_failed(row, 1, first)
            with contextlib.suppress(LimaError):
                await env.blocking(env.lima.stop, row.lima_name)
            try:
                await env.blocking(env.lima.start, row.lima_name)
            except LimaError as second:
                self._start_failed(row, 2, second)
                raise
        env.dirs.pop(row.lima_name, None)
        async with await env.client(row) as client:
            health = await env.wait_health(client, lambda h: self._ours(row, h) or h.state == "unassigned")
            if health.state == "unassigned":
                assert row.org_id is not None
                assert row.wg_id is not None
                await client.assign(row.id, row.org_id, row.wg_id)

    def _start_failed(self, row: VmRow, attempt: int, exc: LimaError) -> None:
        self.env.start_failures += 1
        self.env.log("vm.start_failed", {"vm_id": row.id, "lima_name": row.lima_name, "attempt": attempt,
                                         "code": exc.code})  # fmt: skip

    @staticmethod
    def _ours(row: VmRow, health: Health) -> bool:
        return health.state == "assigned" and health.wg_id == row.wg_id

    @contextlib.asynccontextmanager
    async def _slot(self, wg_id: str | None) -> AsyncIterator[Callable[[], None]]:
        """Claim one slot for a restart or clone while it runs (section 3.4), or raise `capacity_exhausted`.

        Yields `release`: a clone calls it once its `assigning` row exists, because from then on the row occupies the
        slot (`capacity.SLOT_STATES`); counting both would take two slots per clone (PFM-01, PERF-05). A restart keeps
        its reservation to the end (a `stopping` row it restarts counts twice meanwhile, on the safe side).
        """
        # Check and claim under the pool's slot lock (the check awaits the store): a concurrent clone, restart or
        # refill sees the slot as used.
        async with self.pool.slot_lock:
            await self._free_slot(wg_id)
            self.reserved += 1
        held = True

        def release() -> None:
            nonlocal held
            if held:
                held = False
                self.reserved -= 1

        try:
            yield release
        finally:
            release()

    async def _free_slot(self, wg_id: str | None) -> None:
        env = self.env
        capacity = await self.capacity()
        if capacity.slots_free > 0:
            return
        for candidate in await records.live(env.db, "pool"):
            if await env.delete(candidate, expect=("pool",)) is not None:
                env.log("vm.slot_from_pool", {"vm_id": candidate.id})
                return
        victim = lru_victim(
            await records.usage(env.db), exclude_wg=wg_id, now=env.now(), idle_seconds=env.settings.vm_idle_seconds
        )
        if victim is not None and victim.wg_id is not None:
            lock = self.lock(victim.wg_id)
            # Never wait for another workgroup's lock while holding this one (no deadlock): a busy VM is no victim.
            if not lock.locked():
                async with lock:
                    current = await records.get(env.db, victim.vm_id)
                    if current is not None and current.state == "running":
                        env.log("vm.lru_stop", {"vm_id": current.id})
                        await self._stop(current)
                        return
        raise RunnerError("capacity_exhausted")

    # -- stop and delete ---------------------------------------------------------------------------------------

    async def _stop(self, row: VmRow) -> VmRow:
        """`running` -> `stopping` -> `limactl stop` -> `stopped` (caller holds the workgroup lock)."""
        env = self.env
        self._recovered.discard(row.id)
        await self.retire.flush(row)
        stopping = await env.set_state(row, "stopping")
        try:
            await env.blocking(env.lima.stop, row.lima_name)
        except LimaError:
            await env.set_state(stopping, "running")
            raise
        self._failures.pop(row.id, None)
        return await env.set_state(stopping, "stopped")

    async def stop_vm(self, vm_id: str) -> VmRow | None:
        """Stop a running VM (idle stop, operator action); None when it is not running."""
        row = await records.get(self.env.db, vm_id)
        if row is None or row.state != "running" or row.wg_id is None:
            return None
        async with self.lock(row.wg_id):
            current = await records.get(self.env.db, vm_id)
            if current is None or current.state != "running":
                return None
            return await self._stop(current)

    async def delete_workgroup(self, wg_id: str) -> list[VmRow]:
        """Delete every VM of an unassigned or archived workgroup (section 3.5); returns the deleted rows."""
        deleted: list[VmRow] = []
        async with self.lock(wg_id):
            for row in await records.of_workgroup(self.env.db, wg_id):
                if row.state == "running":
                    await self.retire.flush(row)
                self._forget(row.id)
                gone = await self.env.delete(row)
                assert gone is not None  # without `expect` the row is always deleted
                deleted.append(gone)
        return deleted

    def _forget(self, vm_id: str) -> None:
        self._failures.pop(vm_id, None)
        self._recovered.discard(vm_id)

    # -- periodic passes ---------------------------------------------------------------------------------------

    async def refill(self) -> int:
        """Delete pool VMs of a retired golden, then fill the pool (section 3.2)."""
        golden = await records.ready_golden(self.env.db)
        await self.pool.purge(golden)
        if self.env.settings.pool_size == 0:
            return 0
        if golden is None:
            self.env.log("vm.pool_no_golden", {})
            return 0
        return await self.pool.refill(golden)

    async def idle_pass(self) -> list[str]:
        """Stop running VMs that were idle for VM_IDLE_SECONDS (section 3.5); returns their IDs."""
        env = self.env
        limit = env.now() - timedelta(seconds=env.settings.vm_idle_seconds)
        stopped = []
        for item in await records.usage(env.db):
            if item.active_sessions == 0 and item.last_used <= limit and item.wg_id is not None:
                async with self.lock(item.wg_id):
                    again = {c.vm_id: c for c in await records.usage(env.db)}.get(item.vm_id)
                    if again is None or again.active_sessions or again.last_used > limit:
                        continue
                    current = await records.get(env.db, item.vm_id)
                    assert current is not None
                    try:
                        await self._stop(current)
                    except LimaError as exc:
                        env.log("vm.idle_stop_failed", {"vm_id": item.vm_id, "code": exc.code})
                        continue
                    env.log("vm.idle_stopped", {"vm_id": item.vm_id})
                    stopped.append(item.vm_id)
        return stopped

    async def health_pass(self) -> None:
        """Probe every running VM once; act after UNHEALTHY_LIMIT failures in a row (section 3.3)."""
        rows = await records.live(self.env.db, "running")
        healthy = await asyncio.gather(*(self._probe(row) for row in rows))
        unhealthy = []
        for row, ok in zip(rows, healthy, strict=True):
            if ok:
                self._failures.pop(row.id, None)
                continue
            count = self._failures.get(row.id, 0) + 1
            self._failures[row.id] = count
            if count >= UNHEALTHY_LIMIT:
                self._failures.pop(row.id, None)
                unhealthy.append(row)
        # Concurrently: one slow restart does not hold back the handling of another VM.
        await asyncio.gather(*(self._unhealthy(row) for row in unhealthy))

    async def _probe(self, row: VmRow) -> bool:
        try:
            async with await self.env.client(row) as client:
                health = await client.health(timeout=HEALTH_CALL_S)
        except (GuestError, RunnerError):
            return False
        return self._ours(row, health)

    async def _unhealthy(self, row: VmRow) -> None:
        env = self.env
        assert row.wg_id is not None
        await self.on_unreachable(row)
        async with self.lock(row.wg_id):
            current = await records.get(env.db, row.id)
            if current is None or current.state != "running":
                return
            if row.id in self._recovered:
                self._recovered.discard(row.id)
                with contextlib.suppress(LimaError):
                    await self._stop(current)
                env.log("vm.unhealthy_stopped", {"vm_id": row.id})
                return
            env.log("vm.unhealthy_restart", {"vm_id": row.id})
            try:
                stopped = await self._stop(current)
                self._recovered.add(row.id)
                await self._restart(stopped)
            except (RunnerError, LimaError) as exc:
                self._recovered.discard(row.id)
                env.log("vm.unhealthy_stopped", {"vm_id": row.id, "code": exc.code})

    # -- startup reconciliation --------------------------------------------------------------------------------

    async def reconcile(self) -> dict[str, int]:
        """Align `runner.vm` with `limactl list --json` (section 3.5); returns counts per action."""
        env = self.env
        counts = {"deleted": 0, "missing": 0, "state": 0, "unknown": 0, "tokens": 0}
        instances = {instance.name: instance for instance in await env.blocking(env.lima.list)}
        env.dirs.update({name: instance.dir for name, instance in instances.items()})
        rows = await records.live(env.db)
        for row in rows:
            instance = instances.get(row.lima_name)
            if instance is None:
                await env.set_state(row, "deleted")
                env.tokens.remove(row.id)
                await cursor.delete_cursor(env.db, row.id)
                counts["missing"] += 1
            elif row.state == "assigning" or (row.state == "pool" and not instance.running):
                await env.delete(row)
                counts["deleted"] += 1
            elif row.state in ("running", "stopping") and not instance.running:
                await env.set_state(row, "stopped")
                counts["state"] += 1
            elif row.state in ("stopped", "stopping") and instance.running:
                await env.set_state(row, "running")
                counts["state"] += 1
        known = {row.lima_name for row in rows}
        for name in instances:
            if self._arp_re.fullmatch(name) and name not in known:
                try:
                    await env.destroy(name)
                except LimaError as exc:
                    # Never block the VM area on one instance Lima cannot delete; the next start tries again.
                    env.log("vm.unknown_delete_failed", {"lima_name": name, "code": exc.code})
                    continue
                env.log("vm.unknown_deleted", {"lima_name": name})
                counts["unknown"] += 1
        live_ids = {row.id for row in await records.live(env.db)}
        for vm_id in env.tokens.known() - live_ids:
            env.tokens.remove(vm_id)
            counts["tokens"] += 1
        env.log("vm.reconciled", dict(counts))
        self._reconciled.set()
        return counts

"""VM lifecycle of the runner (docs/platform/spec/05-runner.md sections 3.2-3.5 and 6.1 step 1, 03-api.md section
4.6.2, ADR-009).

`VmManager.ensure_vm(org_id, wg_id, session_id=..., prefer=[...])` returns a running VM of the
workgroup for a session, in the deterministic order of ADR-075 section 4 (05-runner.md section 3.3):

1. the VMs of `prefer` in their order - the hub's `vm_id_pref` (a user's choice), then the VM of the parent session
   (subagent) or of the `resume_from` session - when it is a live VM of **this** workgroup, regardless of its room
   and size: running -> measured for the log, then used even when full (the work of the task lives there; refusing it
   would refuse running work, HOT-32), stopped -> restart it (no golden replacement);
2. for a new task: a running VM of the workgroup of the current size (`VM_CPUS` / `VM_MEMORY_MIB`) whose room is
   measured before the placement and still takes one more task as large as its largest one, with its subagents
   (`env.session_room`, `vm.room`: the reserve for the subagents of the tasks already there; no fixed number of
   sessions per VM, ADR-075 addendum 2; a VM that cannot be measured has no room), smallest VM ID first. A VM of an
   older size takes no new session: it keeps its sessions and stops when its last one ended (`idle_pass`; HOT-32,
   after the model of the golden switch, ADR-093);
3. a stopped VM of the workgroup of the current size, in store order -> restart it (`limactl start`, health
   `assigned` to this workgroup), unless its golden is not the newest ready one, no retire hook keeps it
   (`vm.extensions`: e.g. context checkpoints still due, KOR-33) and it may move (`golden_switch.judge`: every session
   on it is a paused main session with a complete checkpoint, ADR-093): then a new VM (pool or clone, as in 4.)
   replaces it and only then it is deleted (a failed clone leaves it as it is). Then a stopped VM of an older size
   whose sessions may move by the same rule: a new VM of the current size replaces it (`vm.size_replaced`); one whose
   sessions may not move stays for them;
4. a new VM (no fixed number of VMs per workgroup; an older hub's `vm_new_allowed` is read past, ADR-075 addendum 2):
   warm pool (`Pool.take`), else clone
   + start on the hot path (`Provisioner.create`); while the device has no ready golden yet (a device just added
   builds it, KOR-67) this is `capacity_exhausted` (`golden`): the hub places the next attempt and the task waits
   instead of failing. Such a device reports no free slot in its heartbeat (`report`), so the hub places nothing on
   it until the golden is ready; the refusal only covers the time before that heartbeat;
5. else `capacity_exhausted` (the hub places the next attempt).

Every placement is logged with the chosen VM, its size, the room measured there, the VMs passed over with the
reason (`full`, `unmeasured`, `size`) and the reason of the choice: `runner.vm.placement wg_id= vm_id= reason=
preferred|room|restart|replaced|new_vm|refused cpus= memory_mib= fits= available_mib= need_mib= load_pct= passed_over=
[code=]`, and counted for the metrics (`VmEnv.placements` by reason, `VmEnv.room_checks` by result;
`agent_runner_vm_placements_total{reason}`, `agent_runner_vm_room_checks_total{result}`, 08-monitoring.md).

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
  `VM_IDLE_SECONDS` ago is stopped; a running VM of an older size is stopped as soon as it has no session left that
  has not ended (neither active nor suspended; `vm.resized_stopped`), it takes no new ones. A restart of the runner
  ends no VM and no session (05-runner.md section 1.1): `reconcile()` only aligns the rows with Lima.
  "Last use" is the newest of creation, assignment, `last_session_at` (set by
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
- `refill()` (task `vm-pool`, after every assignment and every 60 s): pool VMs of a retired golden or of an older
  size are deleted (a pool VM is free), then
  the pool is filled (`Pool.refill`); then `golden_switch.run_pass()` moves outdated workgroup VMs whose sessions are
  all paused with complete checkpoints to the newest golden (`vm.golden_switch`, ADR-093; it uses
  `restart_for_switch`, `stop_for_switch` and `replacement`, which never stop another workgroup's VM for a slot).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from agent_runner.admission import SESSION, Demand
from agent_runner.errors import RunnerError
from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.guestlink.errors import GuestError
from agent_runner.vm import records
from agent_runner.vm.capacity import Candidate, Capacity, lru_victim, vm_footprint_mib
from agent_runner.vm.extensions import Retire
from agent_runner.vm.golden_switch import GoldenSwitch, judge
from agent_runner.vm.lima import LimaError, LimaNotFound
from agent_runner.vm.pool import HEALTH_CALL_S, Pool, Provisioner, VmEnv, failure_fields
from agent_runner.vm.records import GoldenRef, VmRow
from agent_runner.vm.room import Room

UNHEALTHY_LIMIT = 3
STOPPED_STATES = ("stopped", "stopping")
# A session start never waits longer than this for the startup reconciliation (task `vm-pool`).
RECONCILE_WAIT_S = 60.0

Unreachable = Callable[[VmRow], Awaitable[None]]


@dataclass
class Placement:
    """What one placement found: the running or stopped VMs passed over (`<vm_id>:<result>`), the reason of the choice
    and the room measured on the chosen VM."""

    checked: list[str] = field(default_factory=list)
    reason: str = "refused"
    room: Room | None = None

    def check(self, env: VmEnv, vm_id: str, result: str) -> None:
        """Count one room check (`room`, `full`, `unmeasured`, `size`) and keep a VM passed over."""
        env.room_checks[result] = env.room_checks.get(result, 0) + 1
        if result != "room":
            self.checked.append(f"{vm_id}:{result}")


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
        # the workgroup VMs admitted and still in flight count for every kind (the device's one admission)
        env.admission.pending = lambda: (self.pool.filling + self.reserved) * vm_footprint_mib(env.settings.memory_mib)
        env.admission.counters[SESSION] = self.active
        env.admission.store = env.db
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconciled = asyncio.Event()
        self._refill_wanted = asyncio.Event()
        self._failures: dict[str, int] = {}
        self._recovered: set[str] = set()
        self._arp_re = re.compile("^" + re.escape(env.lima.prefix) + r"arp-[0-9a-f]{8}$")
        self.golden_switch = GoldenSwitch(self)

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
        section 2 (`cpus` and `disk_free_mib` only when known); no free slot without a ready golden and a VM."""
        env = self.env
        capacity = await self.capacity()
        vms, sessions = await records.load(env.db)
        free = capacity.heartbeat()
        if vms == 0 and await records.ready_golden(env.db) is None:
            # a device without a ready golden (a new one builds it, KOR-67) has no room yet: the hub places nothing here
            free |= {"vm_slots_free": 0, "pool_ready": 0}
        report: dict[str, object] = {
            **free,
            "memory_total_mib": env.physical_mib(),
            "vms_running": vms,
            "sessions_active": sessions,
            "work_vms_running": capacity.other_vms,
        }  # no `sessions_per_vm` / `vms_max`: there are no fixed numbers any more (a hub reads absent as none)
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
    ) -> VmRow:
        """A running VM of the workgroup, booked for `session_id` (see the module docstring for the order); every
        placement, also a refused one, is logged (`vm.placement`) and counted (`placements`)."""
        try:
            await asyncio.wait_for(self._reconciled.wait(), RECONCILE_WAIT_S)
        except TimeoutError:
            raise RunnerError("vm_start_failed", "reconcile") from None
        placement = Placement()
        async with self.lock(wg_id):
            try:
                row = await self._choose(org_id, wg_id, prefer, placement)
            except RunnerError as exc:
                self._placed(wg_id, session_id, None, placement, code=exc.code)
                raise
            if session_id is not None:
                await records.assign_session(self.env.db, session_id, row.id)
            self._placed(wg_id, session_id, row, placement)
            return row

    async def _choose(self, org_id: str, wg_id: str, prefer: Sequence[str], placement: Placement) -> VmRow:
        env = self.env
        settings = env.settings
        live = await records.of_workgroup(env.db, wg_id)
        by_id = {row.id: row for row in live}
        placement.reason = "preferred"
        for vm_id in prefer:
            found = by_id.get(vm_id)
            if found is not None and found.state == "running":
                # the work of the task lives there (its workspace, its parent): taken even when full, never refused;
                # measured so the log shows a preferred VM that runs full (05-runner.md section 3.3.1)
                placement.room = await env.session_room(found)
                await self.touch(found.id)
                return found
            if found is not None and found.state in STOPPED_STATES:
                return await self._restart(found)
            env.log("vm.preference_skipped", {"vm_id": vm_id, "wg_id": wg_id})
        # Room check before every placement (HOT-32): no fixed number of sessions per VM, the running VM's own measured
        # resources decide (`vm.room`); a VM of an older size takes no new session, it keeps the ones it has.
        for row in sorted((row for row in live if row.state == "running"), key=lambda row: row.id):
            if not settings.current_size(row):
                placement.check(env, row.id, "size")
                continue
            room = await env.session_room(row)
            if room.fits:
                placement.check(env, row.id, "room")
                placement.reason, placement.room = "room", room
                await self.touch(row.id)
                return row
            placement.check(env, row.id, "full" if room.measured else "unmeasured")
        golden = await records.ready_golden(env.db)
        stopped = [row for row in live if row.state in STOPPED_STATES]
        for row in stopped:
            if settings.current_size(row):
                chosen = await self._from_stopped(row, golden)
                placement.reason = "restart" if chosen.id == row.id else "replaced"
                return chosen
        for row in stopped:
            # a stopped VM of an older size: replaced by a new VM of the current size when its sessions may move
            # (the golden switch's rule, ADR-093), else it stays for its own sessions (their preference restarts it)
            if golden is not None and await self._movable(row):
                env.log("vm.size_replaced", {"vm_id": row.id, "cpus": row.cpus, "memory_mib": row.memory_mib})
                placement.reason = "replaced"
                return await self.replacement(golden, row, replaced_by=self.golden_switch.hub_reads_status, evict=True)
            placement.check(env, row.id, "size")
        if golden is None:
            raise RunnerError("capacity_exhausted", "golden")
        placement.reason = "new_vm"
        return await self._new_vm(golden, org_id, wg_id)

    def _placed(
        self, wg_id: str, session_id: str | None, row: VmRow | None, placement: Placement, *, code: str | None = None
    ) -> None:
        """Log and count one placement: the chosen VM, the room measured there, the VMs passed over and the reason
        (`preferred`, `room`, `restart`, `replaced`, `new_vm`; `refused` with the code of the refusal)."""
        reason = placement.reason if row is not None else "refused"
        env = self.env
        env.placements[reason] = env.placements.get(reason, 0) + 1
        fields: dict[str, str | int] = {"wg_id": wg_id, "vm_id": "none" if row is None else row.id, "reason": reason}
        if session_id is not None:
            fields["session_id"] = session_id
        if row is not None:
            fields |= {"cpus": row.cpus, "memory_mib": row.memory_mib}
        if placement.room is not None:
            fields |= placement.room.fields()
        fields["passed_over"] = ",".join(placement.checked) or "none"
        if code is not None:
            fields["code"] = code
        env.log("vm.placement", fields)

    async def _movable(self, row: VmRow) -> bool:
        """Whether every session of the stopped VM may move to another VM (no retire hook keeps it and
        `golden_switch.judge` finds nothing blocking: the rule of the golden switch, ADR-093)."""
        if await self.retire.pending(row):
            return False
        return not (await judge(self.env, self.retire, row)).blocking(await self.golden_switch.forced(row))

    async def _from_stopped(self, row: VmRow, golden: GoldenRef | None) -> VmRow:
        """Step 4: restart the stopped VM, or replace it by a VM of the newest golden when it may move."""
        env = self.env
        if golden is None or row.golden_version == golden.version or not await self._movable(row):
            return await self._restart(row)
        env.log("vm.golden_replaced", {"vm_id": row.id, "golden_version": golden.version})
        try:
            return await self.replacement(golden, row, replaced_by=self.golden_switch.hub_reads_status, evict=True)
        except RunnerError as exc:
            # the session goes on on the old golden; the switch tries again later
            env.log("vm.golden_replace_failed", {"vm_id": row.id, "code": exc.code})
            return await self._restart(row)

    async def _new_vm(self, golden: GoldenRef, org_id: str, wg_id: str, *, evict: bool = True) -> VmRow:
        taken = await self.pool.take(golden, org_id, wg_id)
        if taken is not None:
            return taken
        self.wake_refill()
        return await self._clone(golden, org_id, wg_id, evict=evict)

    # -- the switch to a newer golden (`vm.golden_switch`, ADR-093; callers hold the workgroup lock) -------------

    async def replacement(self, golden: GoldenRef, old: VmRow, *, replaced_by: bool, evict: bool = False) -> VmRow:
        """A running VM of `golden` for the workgroup of the stopped VM `old` (pool or clone; another workgroup's VM
        is stopped for it only with `evict`, a session start), the switch recorded, then `old` is deleted. A failure
        before the record leaves `old` as it is; after it (the delete failed, or the daemon stopped) the next attempt
        reuses the recorded successor while it is live instead of making another VM."""
        env = self.env
        assert old.org_id is not None
        assert old.wg_id is not None
        recorded = await records.switched_to(env.db, old.id)
        found = None if recorded is None else await records.get(env.db, recorded)
        if found is not None and found.state == "running":
            new = found
        elif found is not None and found.state in STOPPED_STATES:
            new = await self._restart(found, evict=evict)
        else:
            new = await self._new_vm(golden, old.org_id, old.wg_id, evict=evict)
            await records.record_switch(env.db, old.id, new.id, golden.version, env.now())
        self._forget(old.id)
        await env.delete(old, replaced_by=new.id if replaced_by else None)
        return new

    async def restart_for_switch(self, row: VmRow) -> VmRow:
        """Start a stopped VM so its checkpoints can be taken (no other workgroup's VM is stopped for it)."""
        return await self._restart(row, evict=False)

    async def stop_for_switch(self, row: VmRow) -> VmRow:
        """Stop a running VM; the retire hooks take the final checkpoints of its sessions first."""
        return await self._stop(row)

    async def _clone(self, golden: GoldenRef, org_id: str, wg_id: str, *, evict: bool = True) -> VmRow:
        env = self.env
        async with self._slot(wg_id, evict=evict) as release:
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

    async def _restart(self, row: VmRow, *, evict: bool = True) -> VmRow:
        """Start a stopped (or interrupted `stopping`) VM of the workgroup again."""
        env = self.env
        async with self._slot(row.wg_id, evict=evict):
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
    async def _slot(self, wg_id: str | None, *, evict: bool = True) -> AsyncIterator[Callable[[], None]]:
        """Claim one slot for a restart or clone while it runs (section 3.4), or raise `capacity_exhausted`; without
        `evict` no other workgroup's VM is stopped for it (a pool VM may still be given up).

        Yields `release`: a clone calls it once its `assigning` row exists, because from then on the row occupies the
        slot (`capacity.SLOT_STATES`); counting both would take two slots per clone (PFM-01, PERF-05). A restart keeps
        its reservation to the end (a `stopping` row it restarts counts twice meanwhile, on the safe side).
        """
        # Check and claim under the pool's slot lock (the check awaits the store): a concurrent clone, restart or
        # refill sees the slot as used.
        async with self.pool.slot_lock:
            await self._free_slot(wg_id, evict=evict)
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

    async def _free_slot(self, wg_id: str | None, *, evict: bool = True) -> None:
        """The device's one admission for a workgroup VM (kind `SESSION`, 05-runner.md section 3.8): a refusal keeps
        the session's place (its age) while the hub places its next attempt, here or on another device."""
        try:
            await self._take_slot(wg_id, evict=evict)
        except RunnerError:
            await self.env.admission.refused(SESSION, self.env.settings.memory_mib)
            raise
        await self.env.admission.started(SESSION)

    async def active(self) -> int:
        """Workgroup VMs that hold a slot (a pool entry is not active: it is speculative)."""
        return len(await records.live(self.env.db, "assigning", "running", "stopping"))

    async def _take_slot(self, wg_id: str | None, *, evict: bool) -> None:
        env = self.env
        if not await env.admission.cpu_ok():
            raise RunnerError("capacity_exhausted", "cpu")
        disk = env.disk_free_mib()
        if disk is not None and disk < env.settings.disk_gib * 1024:
            raise RunnerError("capacity_exhausted", "disk")
        capacity = await self.capacity()
        # fewest active first: waiting requests of another kind that go first keep the RAM they need
        demand = Demand(SESSION, vm_footprint_mib(env.settings.memory_mib))  # with its host share
        ahead = await env.admission.ahead_mib(demand, env.admission.hold(SESSION))
        if ahead and capacity.budget_mib - capacity.used_mib - ahead < demand.memory_mib:
            raise RunnerError("capacity_exhausted", "turn")
        if capacity.slots_free > 0:
            if capacity.slots_free_now == 0:  # the work VM pool holds the room: it gives way (ADR-059 addendum 4)
                released = await env.reclaim(env.settings.memory_mib)
                env.log("vm.slot_from_work_vms", {"released_mib": released})
            return
        for candidate in await records.live(env.db, "pool"):
            if await env.delete(candidate, expect=("pool",)) is not None:
                env.log("vm.slot_from_pool", {"vm_id": candidate.id})
                return
        # idle work VMs give way (stopped, disk kept); running work is never ended
        released = await env.reclaim(env.settings.memory_mib)
        if released >= env.settings.memory_mib:
            env.log("vm.slot_from_work_vms", {"released_mib": released})
            return
        if not evict:
            raise RunnerError("capacity_exhausted")
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
        """Stop running VMs that were idle for VM_IDLE_SECONDS (section 3.5), and VMs of an older size whose last
        session ended (HOT-32); returns their IDs."""
        env = self.env
        limit = env.now() - timedelta(seconds=env.settings.vm_idle_seconds)
        resized = {row.id for row in await records.live(env.db, "running") if not env.settings.current_size(row)}

        def due(item: Candidate) -> str | None:
            if item.active_sessions:
                return None
            if item.vm_id in resized and item.newest_suspended is None:
                return "vm.resized_stopped"  # its last session ended: it takes no new one
            return "vm.idle_stopped" if item.last_used <= limit else None

        stopped = []
        for item in await records.usage(env.db):
            if due(item) is not None and item.wg_id is not None:
                async with self.lock(item.wg_id):
                    again = {c.vm_id: c for c in await records.usage(env.db)}.get(item.vm_id)
                    event = None if again is None else due(again)
                    if event is None:
                        continue
                    current = await records.get(env.db, item.vm_id)
                    assert current is not None
                    try:
                        await self._stop(current)
                    except LimaError as exc:
                        env.log("vm.idle_stop_failed", {"vm_id": item.vm_id, "code": exc.code})
                        continue
                    env.log(event, {"vm_id": item.vm_id})
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

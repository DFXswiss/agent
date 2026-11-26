"""Warm pool and clone provisioning (docs/platform/spec/05-runner.md sections 3.2, 3.3, 4.5; ADR-009).

`Provisioner.create(golden, pool=...)` makes one VM from the ready golden:

1. `limactl clone --tty=false --set .cpus=N --set .memory="XGiB" [--set <self-test provisioning>] <golden>
   <prefix>arp-<8 hex>` (APFS clone, ~0.05 s). The Lima name and the `vm-<8 hex>` ID are independent random values.
2. `limactl start -y --set '.param.AGENT_GUEST_CONTROL_TOKEN="<token>"' <name>`: 32 random bytes (b64u) handed to
   the guest before boot; `agent-guest-harden.service` installs it as `/etc/agent-guest/control-token`. Lima writes
   the parameter into the instance's `lima.yaml`; the runner blanks it there right after the start, so the token is
   kept only in memory, in `$AGENT_RUNNER_HOME/vm/<vm_id>.token` (0600) and as hash in `runner.vm`.
3. `GET /v1/health` through the forwarded socket every 100 ms until `state:"unassigned"` (30 s).
4. Pool VMs only: the sandbox self-test (section 4.5). The Lima default user may not run anything as root in a
   hardened clone, so the self-test comes in as a one-time Lima system provisioning step of the pool clone
   (`SELFTEST_SET`): at first boot it starts a transient unit after `agent-guest-api.service` that runs
   `agent_guest.sandbox.selftest` and leaves the JSON result in `/run/agent-pool-selftest/result.json`. The runner
   reads it with `limactl shell`; a failed or missing result deletes the VM, so a broken VM is never assigned. A
   guard file keeps it from running again on a later boot. Clones on the hot path never carry it.

Clone or start failure (section 3.3): the half-created instance is deleted (`limactl delete -f`) and the clone is
retried once under a new name; a second failure raises `vm_start_failed` (`start_failures` counts every failed
attempt for `agent_runner_vm_start_failures_total`).

Interrupted provisioning (daemon stop, decisions KOR-03): an instance being created must never outlive the runner's
control. Once the daemon is stopping (`VmEnv.stopping`) no clone is started and no failed clone is retried; a stop
signal to the process group fails a running `limactl start`, whose instance is then deleted like any failure.
When the provisioning task is cancelled (or fails unexpectedly) while `limactl clone/start` still runs in its worker
thread, the instance is deleted in a worker thread as well. Every `limactl clone/start` of a provisioning and every
deletion (`VmEnv.destroy`, also the hot path's and the pool's own cleanup) take the instance's lock in their worker
thread, so `limactl delete -f` does not race a `limactl start` that is still bringing the instance up, and the deletion
completes even when the task is cancelled again (the interpreter joins the worker threads before it exits). Left over
anyway: a VM of a daemon killed with SIGKILL, or of a clone whose queued work item had not yet taken the lock when the
deletion took it; neither has a row, and the startup reconciliation deletes it before any other VM work
(`VmManager.reconcile`).

`Pool` keeps up to `POOL_SIZE` neutral running clones: `refill()` (asynchronous, at most 2 clones in parallel, only
while a slot is free, never on the hot path), `take(...)` (hot path: the oldest pool VM of the ready golden, `POST
/v1/assign`, row `assigning` -> `running`) and `purge(...)` (pool VMs of a retired golden are deleted).

`VmEnv` bundles every collaborator (Lima, store, settings, token files, guest client factory, clocks, random
sources, the `vm_state` sink, the log, the usage of other runner areas and the host facts of the capacity report), so
the unit tests inject all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runner.admission import Admission
from agent_runner.config import Config
from agent_runner.errors import RunnerError
from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.vm import records
from agent_runner.vm.capacity import NO_USAGE, Capacity, Usage, compute, vm_footprint_mib
from agent_runner.vm.lima import Instance, Lima, LimaError, memory_gib
from agent_runner.vm.records import GoldenRef, TokenStore, VmRow

TOKEN_PARAM = "AGENT_GUEST_CONTROL_TOKEN"
HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.1
HEALTH_CALL_S = 5.0
# A pool VM whose guest hangs must not eat the hot-path budget (0.1-0.5 s): the next pool VM or a clone follows.
ASSIGN_TIMEOUT_S = 2.0
SELFTEST_TIMEOUT_S = 300.0
SELFTEST_POLL_S = 0.5
SELFTEST_RESULT = "/run/agent-pool-selftest/result.json"
MAX_PARALLEL_FILLS = 2
CLONE_ATTEMPTS = 2
_PARAM_LINE_RE = re.compile(r"^(\s+" + TOKEN_PARAM + r":)[^\n]*$", re.MULTILINE)

# Runs as root at the first boot of a pool clone (Lima system provisioning; see the module docstring). The inner
# program waits for the guest API (the self-test checks that port 7700 is reachable), runs the self-test and moves
# its JSON result into place; the runner only reads the result (no secret in it).
_SELFTEST_INNER = """out=/run/agent-pool-selftest
i=0
until curl -fsS --max-time 2 -o /dev/null http://127.0.0.1:7700/v1/health; do
  i=$((i+1)); [ "$i" -ge 300 ] && break; sleep 0.2
done
cd /opt/agent-guest && python3 -E -s -m agent_guest.sandbox.selftest >"$out/result.part" 2>"$out/stderr"
echo "$?" >"$out/exit"
mv -f "$out/result.part" "$out/result.json"
"""
SELFTEST_SCRIPT = (
    "#!/bin/sh\n"
    "set -u\n"
    "guard=/var/lib/agent-guest/pool-selftest.started\n"
    '[ -e "$guard" ] && exit 0\n'
    "install -d -m 0755 /run/agent-pool-selftest\n"
    'install -d -m 0755 "$(dirname "$guard")" && touch "$guard"\n'
    "systemd-run --no-block --quiet --unit=agent-pool-selftest --property=Type=oneshot"
    " --property=After=agent-guest-api.service /bin/sh -c '" + _SELFTEST_INNER.replace("'", "'\\''") + "'\n"
)
SELFTEST_SET = ".provision += [" + json.dumps({"mode": "system", "script": SELFTEST_SCRIPT}) + "]"

Sink = Callable[[dict[str, Any]], Awaitable[None]]
Log = Callable[[str, dict[str, str | int]], None]
ClientFactory = Callable[[str, str], GuestClient]


async def _no_sink(_body: dict[str, Any]) -> None:
    return None


def _no_log(_name: str, _fields: dict[str, str | int]) -> None:
    return None


def _not_stopping() -> bool:
    return False


async def _no_usage() -> Usage:
    return NO_USAGE


def _unknown() -> int | None:
    return None


async def _no_reclaim(_need_mib: int) -> int:
    return 0


async def _room_unmeasured(_row: VmRow) -> bool:
    """Without a measurement (`vm.room.checker`, set by `vm.tasks`) a running VM has room: no fixed number."""
    return True


async def _idle_cpu() -> float:
    return 0.0


def _admission() -> Admission:
    return Admission(cpu_load=_idle_cpu)


def _token() -> str:
    return secrets.token_urlsafe(32)


def format_time(value: datetime) -> str:
    """`2026-11-21T12:00:00.000Z` (contracts `time`)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class VmSettings:
    cpus: int
    memory_mib: int
    disk_gib: int
    pool_size: int
    memory_reserve_mib: int | None
    """The host reserve when set; None: the rule of every device (`capacity.host_reserve_mib`)."""
    vm_idle_seconds: int

    @classmethod
    def from_config(cls, config: Config) -> VmSettings:
        return cls(
            cpus=config.vm_cpus,
            memory_mib=config.vm_memory_mib,
            disk_gib=config.vm_disk_gib,
            pool_size=config.pool_size,
            memory_reserve_mib=config.memory_reserve_mib,
            vm_idle_seconds=config.vm_idle_seconds,
        )


@dataclass
class VmEnv:
    """Collaborators of the VM management (see the module docstring)."""

    lima: Lima
    db: Any
    settings: VmSettings
    tokens: TokenStore
    physical_mib: Callable[[], int]
    sink: Sink = _no_sink
    log: Log = _no_log
    client_factory: ClientFactory = GuestClient
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    random_hex: Callable[[int], str] = secrets.token_hex
    random_token: Callable[[], str] = _token
    blocking: Callable[..., Awaitable[Any]] = asyncio.to_thread
    # True once the daemon is stopping: no clone is started or retried any more.
    stopping: Callable[[], bool] = _not_stopping
    start_failures: int = 0
    # Instance directories by Lima name (the forwarded socket lives there); filled from `limactl list`.
    dirs: dict[str, str] = field(default_factory=dict)
    # Per-instance locks of the provisioning: a deletion waits until the `limactl` call in flight on it returned.
    guards: dict[str, threading.Lock] = field(default_factory=dict)
    # VMs and memory of the other runner areas (`<area>/capacity.py:USAGE`, ADR-075 section 2).
    other_usage: Callable[[], Awaitable[Usage]] = _no_usage
    reclaim: Callable[[int], Awaitable[int]] = _no_reclaim
    """Ask the other areas to give back the MiB a workgroup VM needs (their `RECLAIM`, ADR-059 addendum 4)."""
    session_room: Callable[[VmRow], Awaitable[bool]] = _room_unmeasured
    """Whether a running VM's own free resources take one more session (`vm.room.checker`; default: yes)."""
    admission: Admission = field(default_factory=_admission)
    """The device's one admission (`agent_runner.admission`, shared with the work VMs): its transaction (`lock`, the
    pool's slot lock), the order of the kinds (fewest active first), CPU; default: idle CPU."""
    # Host facts of the capacity report (None: unknown, the counter is left out).
    cpus: Callable[[], int | None] = _unknown
    disk_free_mib: Callable[[], int | None] = _unknown

    # -- helpers shared by pool and lifecycle ------------------------------------------------------------------

    async def emit(self, row: VmRow, **extra: Any) -> None:
        """Report the row's state to the `vm_state` sink (outbox item, 03-api.md section 6.1); `extra` adds the
        optional `upgrade` or `replaced_by` (ADR-093 section 3)."""
        await self.sink(
            {
                "vm_id": row.id,
                "wg_id": row.wg_id,
                "state": row.state,
                "golden_version": row.golden_version,
                "cpus": row.cpus,
                "memory_mib": row.memory_mib,
                "disk_gib": self.settings.disk_gib,
                "at": format_time(self.now()),
                **extra,
            }
        )

    async def set_state(
        self,
        row: VmRow,
        state: str,
        *,
        expect: Sequence[str] = (),
        report: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> VmRow:
        """Set the state (and columns) and report it (with the extra fields `report`); with `expect`, only from those
        states (`vm_state_changed`)."""
        if expect:
            changed = await records.transition(self.db, row.id, expect, state=state, **fields)
            if changed is None:
                raise RunnerError("vm_state_changed")
            updated = changed
        else:
            updated = await records.update(self.db, row.id, state=state, **fields)
        await self.emit(updated, **(report or {}))
        return updated

    async def instance_dir(self, name: str) -> str:
        known = self.dirs.get(name)
        if known is not None:
            return known
        instance: Instance | None = await self.blocking(self.lima.get, name)
        if instance is None:
            raise RunnerError("vm_instance_missing")
        self.dirs[name] = instance.dir
        return instance.dir

    async def socket_path(self, name: str) -> str:
        return os.path.join(await self.instance_dir(name), "sock", "agent-guest.sock")

    async def client(self, row: VmRow) -> GuestClient:
        """A control client for the VM (token from its file, checked against the row's hash)."""
        token = self.tokens.read(row.id, row.control_token_hash)
        return self.client_factory(await self.socket_path(row.lima_name), token)

    def guard(self, name: str) -> threading.Lock:
        """The lock of instance `name` that its provisioning's `limactl` calls and every deletion take."""
        return self.guards.setdefault(name, threading.Lock())

    async def destroy(self, name: str) -> None:
        """`limactl delete -f` (a missing instance is fine), after the `limactl` call in flight on it returned.

        The deletion runs in a worker thread behind the instance's guard: a cancellation of the caller ends the wait,
        never the deletion (the interpreter joins the worker threads before the process exits).
        """
        guard = self.guard(name)

        def delete() -> None:
            with guard:
                self.lima.delete(name)
            self.guards.pop(name, None)

        self.dirs.pop(name, None)
        await self.blocking(delete)

    async def delete(self, row: VmRow, *, expect: Sequence[str] = (), replaced_by: str | None = None) -> VmRow | None:
        """Delete the instance, mark the row `deleted`, remove token file and journal cursor; `replaced_by` names the
        VM that took over (reported with the `deleted` state, ADR-093 section 3).

        With `expect` the row is claimed first (only from those states; None when another task moved it), so a pool
        VM that was just taken for a workgroup is never deleted under it.
        """
        if expect:
            claimed = await records.transition(self.db, row.id, expect, state="deleted")
            if claimed is None:
                return None
            await self.emit(claimed)
            await self.destroy(row.lima_name)
            deleted = claimed
        else:
            await self.destroy(row.lima_name)
            deleted = await self.set_state(
                row, "deleted", report=None if replaced_by is None else {"replaced_by": replaced_by}
            )
        self.tokens.remove(row.id)
        await cursor.delete_cursor(self.db, row.id)
        self.log("vm.deleted", {"vm_id": row.id, "lima_name": row.lima_name})
        return deleted

    async def capacity(self, in_flight: int = 0) -> Capacity:
        return compute(
            physical_mib=self.physical_mib(),
            reserve_mib=self.settings.memory_reserve_mib,
            vm_memory_mib=self.settings.memory_mib,
            vms=await records.slot_rows(self.db),
            in_flight=in_flight,
            other=await self.other_usage(),
        )

    def new_ids(self) -> tuple[str, str]:
        """`(vm_id, lima_name)`: two independent random values."""
        return f"vm-{self.random_hex(4)}", self.lima.name(f"arp-{self.random_hex(4)}")

    async def wait_health(
        self, client: GuestClient, accept: Callable[[Health], bool], *, timeout: float = HEALTH_TIMEOUT_S
    ) -> Health:
        """Poll `GET /v1/health` every 100 ms until `accept(health)`; `vm_health_timeout` after `timeout`.

        A `broken` guest fails at once (`vm_broken`); an answer that is neither accepted nor broken (for example
        assigned to another workgroup) fails with `vm_health_unexpected`.
        """
        deadline = self.clock() + timeout
        while True:
            try:
                health = await client.health(timeout=HEALTH_CALL_S)
            except GuestError:
                health = None
            if health is not None:
                if accept(health):
                    return health
                raise RunnerError("vm_broken" if health.state == "broken" else "vm_health_unexpected")
            if self.clock() >= deadline:
                raise RunnerError("vm_health_timeout")
            await self.sleep(HEALTH_POLL_S)


def failure_fields(vm_id: str, exc: RunnerError) -> dict[str, str | int]:
    """Log fields of a failed VM step: the code, and the guest's own code when the guest refused."""
    fields: dict[str, str | int] = {"vm_id": vm_id, "code": exc.code}
    if isinstance(exc, GuestRefused):
        fields["error"] = exc.error
    return fields


def scrub_param(instance_dir: str) -> bool:
    """Blank the control-token parameter in the instance's `lima.yaml`; True when a value was removed."""
    path = Path(instance_dir) / "lima.yaml"
    text = path.read_text(encoding="utf-8")
    scrubbed = _PARAM_LINE_RE.sub(lambda match: match.group(1) + ' ""', text)
    if scrubbed == text:
        return False
    part = path.with_name("lima.yaml.part")
    part.write_text(scrubbed, encoding="utf-8")
    part.chmod(path.stat().st_mode & 0o777)
    part.replace(path)
    return True


def _unassigned(health: Health) -> bool:
    return health.state == "unassigned"


class Provisioner:
    """Clone + start + health (+ self-test) of one VM (see the module docstring)."""

    def __init__(self, env: VmEnv) -> None:
        self.env = env

    async def create(
        self,
        golden: GoldenRef,
        *,
        vm_id: str,
        token: str,
        pool: bool,
        before_clone: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Provision a VM; returns its Lima name. Retries once under a new name, then `vm_start_failed`.

        `before_clone(name)` runs before each clone (the hot path records the name in the row first, so the row
        always names the instance that may exist).
        """
        env = self.env
        for attempt in range(1, CLONE_ATTEMPTS + 1):
            if env.stopping():
                env.log("vm.start_skipped", {"vm_id": vm_id, "attempt": attempt, "code": "stopping"})
                break
            name = env.lima.name(f"arp-{env.random_hex(4)}")
            if before_clone is not None:
                await before_clone(name)
            guard = env.guard(name)
            try:
                await self._provision(golden, name, token, pool=pool, guard=guard)
            except (RunnerError, OSError) as exc:
                env.start_failures += 1
                code = exc.code if isinstance(exc, RunnerError) else "vm_files"
                env.log("vm.start_failed", {"vm_id": vm_id, "lima_name": name, "attempt": attempt, "code": code})
                if isinstance(exc, LimaError) and exc.detail:
                    env.log("vm.lima_detail", {"lima_name": name, "operation": exc.operation})
                with contextlib.suppress(LimaError):
                    await env.destroy(name)
                continue
            except BaseException:
                # Cancelled (daemon stop) or an unexpected error: `limactl clone/start` may still run in its thread.
                env.log("vm.start_abandoned", {"lima_name": name})
                with contextlib.suppress(LimaError):
                    # Shielded: a second cancellation ends the wait, never the deletion in the worker thread.
                    await asyncio.shield(asyncio.ensure_future(env.destroy(name)))
                raise
            return name
        raise RunnerError("vm_start_failed")

    async def _provision(self, golden: GoldenRef, name: str, token: str, *, pool: bool, guard: threading.Lock) -> None:
        env = self.env

        def locked(call: Callable[..., Any]) -> Callable[..., Any]:
            def run(*args: Any, **kwargs: Any) -> Any:
                with guard:
                    return call(*args, **kwargs)

            return run

        sets = [f".cpus = {env.settings.cpus}", f'.memory = "{memory_gib(env.settings.memory_mib)}GiB"']
        if pool:
            sets.append(SELFTEST_SET)
        await env.blocking(locked(env.lima.clone), golden.lima_name, name, sets=sets)
        await env.blocking(locked(env.lima.start), name, params={TOKEN_PARAM: token})
        directory = await env.instance_dir(name)
        if not await env.blocking(scrub_param, directory):
            raise RunnerError("vm_param_scrub")
        async with env.client_factory(await env.socket_path(name), token) as client:
            await env.wait_health(client, _unassigned)
        if pool:
            await self._selftest(name)

    async def _selftest(self, name: str) -> None:
        env = self.env
        deadline = env.clock() + SELFTEST_TIMEOUT_S
        while True:
            result = await env.blocking(env.lima.shell, name, ["cat", SELFTEST_RESULT], check=False)
            if result.returncode == 0:
                break
            if env.clock() >= deadline:
                raise RunnerError("vm_selftest_failed", "timeout")
            await env.sleep(SELFTEST_POLL_S)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = None
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise RunnerError("vm_selftest_failed", "result")


class Pool:
    """The warm pool (see the module docstring)."""

    def __init__(
        self,
        env: VmEnv,
        provisioner: Provisioner,
        *,
        on_change: Callable[[], None],
        reserved: Callable[[], int],
    ) -> None:
        self.env = env
        self.provisioner = provisioner
        self.filling = 0
        self._on_change = on_change
        self._reserved = reserved
        # Held from a capacity check to its claim (`filling` here, `VmManager.reserved` for clones and restarts): the
        # check awaits the store, so without it two claimants could both take the last free slot. It is the device's
        # one transaction (`Admission.lock`), so the work VMs never claim the same RAM meanwhile.
        self.slot_lock = env.admission.lock

    async def missing(self, golden: GoldenRef | None) -> int:
        """Clones to start now: up to POOL_SIZE, at most 2 in flight, only into free slots."""
        if golden is None or self.env.stopping():
            return 0
        ready = len(await records.live(self.env.db, "pool"))
        wanted = self.env.settings.pool_size - ready - self.filling
        capacity = await self.env.capacity(self.filling + self._reserved())
        # only room nobody holds or waits for: a warm VM is no reason to make work VMs give way (ADR-059 addendum 4),
        # and the waiting requests of the device's admission keep what they need (05-runner.md section 3.8)
        waiting = sum(ticket.memory_mib for ticket in self.env.admission.live())
        room = capacity.slots_free_now - -(-waiting // vm_footprint_mib(self.env.settings.memory_mib))
        return max(min(wanted, MAX_PARALLEL_FILLS - self.filling, room), 0)

    async def purge(self, golden: GoldenRef | None) -> int:
        """Delete pool VMs of another golden (all when there is none or the pool is disabled)."""
        removed = 0
        for row in await records.live(self.env.db, "pool"):
            outdated = golden is None or row.golden_version != golden.version or self.env.settings.pool_size == 0
            if outdated and await self.env.delete(row, expect=("pool",)) is not None:
                removed += 1
        return removed

    async def refill(self, golden: GoldenRef | None) -> int:
        """Fill the pool; returns the number of VMs added. A round with a failure ends the refill (retried later)."""
        added = 0
        while True:
            async with self.slot_lock:
                count = await self.missing(golden)
                if count == 0 or golden is None:
                    return added
                # Claimed under the slot lock, so a concurrent clone or restart sees them as used.
                self.filling += count
            results = await asyncio.gather(
                *(self.fill_one(golden, reserved=True) for _ in range(count)), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, Exception):
                    raise result
            done = sum(1 for result in results if isinstance(result, VmRow))
            added += done
            if done < len(results):
                return added

    async def fill_one(self, golden: GoldenRef, *, reserved: bool = False) -> VmRow:
        """Add one pool VM; `reserved` means the caller already counted it in `filling`."""
        env = self.env
        vm_id = f"vm-{env.random_hex(4)}"
        token = env.random_token()
        if not reserved:
            self.filling += 1
        try:
            name = await self.provisioner.create(golden, vm_id=vm_id, token=token, pool=True)
            written = False
            try:
                env.tokens.write(vm_id, token)
                written = True
                row = await records.insert(
                    env.db,
                    vm_id=vm_id,
                    lima_name=name,
                    golden_version=golden.version,
                    state="pool",
                    token_sha256=records.token_hash(token),
                    cpus=env.settings.cpus,
                    memory_mib=env.settings.memory_mib,
                    created_at=env.now(),
                )
            except BaseException:
                with contextlib.suppress(LimaError):
                    await env.destroy(name)
                if written:
                    env.tokens.remove(vm_id)
                raise
        finally:
            self.filling -= 1
        await env.emit(row)
        env.log("vm.pool_filled", {"vm_id": row.id, "lima_name": row.lima_name})
        return row

    async def take(self, golden: GoldenRef, org_id: str, wg_id: str) -> VmRow | None:
        """Hot path: assign the oldest pool VM of `golden` to the workgroup; None when the pool is empty.

        A pool VM that refuses the assignment or does not answer is deleted and the next one is tried.
        """
        env = self.env
        while True:
            row = await records.take_pool(env.db, golden.version, org_id, wg_id, env.now())
            if row is None:
                return None
            try:
                await env.emit(row)
                async with await env.client(row) as client:
                    await client.assign(row.id, org_id, wg_id, timeout=ASSIGN_TIMEOUT_S)
                running = await env.set_state(row, "running", expect=("assigning",), last_session_at=env.now())
            except BaseException as exc:
                # Also on cancellation or an unexpected error: the half-assigned VM must not hold the workgroup.
                with contextlib.suppress(Exception):
                    await env.delete(row)
                self._changed()
                if not isinstance(exc, (GuestError, RunnerError)):
                    raise
                env.log("vm.assign_failed", failure_fields(row.id, exc))
                continue
            self._changed()
            return running

    def _changed(self) -> None:
        self._on_change()

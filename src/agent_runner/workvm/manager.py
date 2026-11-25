"""Work VMs of this runner (docs/platform/spec/13-work-vm.md sections 2, 3, 7; ADR-059).

`WorkVms` owns every work VM of the device:

- **request** (`workvm.request` executor): the live VM of the scope is returned (`created:false`, started again when it
  was stopped); otherwise the formal limits are checked (size cap, per task / organization / owner), the request waits
  FIFO for a free slot and RAM (at most `WORKVM_QUEUE_SECONDS`, then `timeout`, retryable), and a new VM is cloned from
  the slot's base of the ready work image and started (`.cpus`, `.memory`, `.disk`, `.ssh.localPort` = the slot's
  pinned port, `.dns` = Cloudflare's public resolvers, reached through the tunnel). It is ready when the job helper
  answers. A new VM keeps the person whose turn requested it (`triggered_by`, ADR-083 section 2): the operation's
  acting user, or for a VM obtained implicitly by a tool call the session's acting user at that moment
  (`credentials.acting.stamp`).
- **release** (`workvm.release` executor) and `delete`: `limactl delete -f`, row `deleted`.
- **calls** of the five tools (`journal.py`): the VM is always resolved from the session of the journal that carried
  the call (its own session VM, else the task VM), never from arguments; a stopped VM is started on the way.
- **idle pass**: a VM without a running job and without a call for `WORKVM_IDLE_SECONDS` is deleted, a stopped VM
  (paused session) once it stood that long; a VM whose deletion failed is deleted again, and own `wvm-` instances
  without a row (left by a failed deletion) are swept.
- **paused sessions** (KOR-43, ADR-080 section 4): `pause` stops the session's VM (no RAM, no runtime); a request or
  call of the resumed session starts it again (`_resume`), or deletes it and gets a new one when its RAM does not fit.
- **run VMs** (scope `run`, `obtain_run_vm`): one fresh VM per `test.run` of `agent_runner.workvm.testrun`.
- **browser VMs** (scope `browser`): the VM of a session's platform browser (`agent_runner.workvm.browser`).
- **logs**: a job whose output is longer than its event keeps (`RUN_OUTPUT_LIMIT`) is captured as an encrypted log
  object in the background (`capture`, injected); its final `workvm.run` event names the log, and the teardown of its
  VM waits for the capture (at most `CAPTURE_TIMEOUT`).
- **credit**: on a platform device an exhausted organization gets no new VM (`credit_exhausted`).
- **lease** (ADR-087 section 4): `workvm_exec` and `workvm_browse` (and the implicit VM they start) reach the internet
  without a hub registration; they are refused `unavailable` (`NO_LEASE`) while the runner has not reached the hub for
  longer than the failover time (`Deps.hub_lease`, `sync.lease`). The other tools and running jobs go on.

Availability (`available`): slots configured, a ready work image, and the boundary check (`boundary_ok`, the host
filter; always true in `TEST_MODE` self mode). Lima calls are synchronous and run in threads; one lock per VM
serialises its start, stop and delete. A failed Lima call is logged with its code and Lima's last error line
(`lima="..."`, at most `LIMA_REASON_LIMIT` characters), so the cause is in the runner log (HOT-18).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agent_runner.credentials import acting
from agent_runner.errors import RunnerError
from agent_runner.operations.extensions import OperationFailed
from agent_runner.sessions.records import LocalSession
from agent_runner.vm.lima import Lima, LimaError
from agent_runner.workvm import firewall, jobs, records, sync
from agent_runner.workvm.jobs import JobView, Shell, WorkVmError
from agent_runner.workvm.records import Pooled, WorkVm

SIZES: Mapping[str, tuple[int, int, int]] = {
    "standard": (4, 8192, 50),
    "large": (8, 16384, 100),
    "xlarge": (16, 32768, 200),
}
SIZE_ORDER = ("standard", "large", "xlarge")
READY_TIMEOUT = 60.0
POOL_SIZE = "standard"
"""Pool entries are standard VMs (spec 11 section 7); a larger request is cloned on demand."""
POOL_HEALTH_TIMEOUT = 5.0
POOL_PROBE_ID = "wvm-00000000"
NO_ID = "00000000-0000-0000-0000-000000000000"
QUEUE_POLL = 2.0
CALL_MARGIN = 5.0
# The guest waits for a work VM answer at most `GUEST_MARGIN + wait_seconds`, capped at `GUEST_MAX` seconds
# (`agent_guest.mcp_tools._forward`); the runner keeps `ANSWER_RESERVE` of that budget for its own answer.
GUEST_MARGIN = 5.0
GUEST_MAX = 33.0
LIMA_REASON_LIMIT = 200
_LIMA_MESSAGE = re.compile(r'\bmsg="((?:[^"\\]|\\.)*)"')
_LIMA_PROBLEM = re.compile(r'^(?:time="[^"]*"\s+)?level=(?:fatal|error)\b')
ANSWER_RESERVE = 3.0
BROWSE_RESERVE = 8.0
SYNC_TIMEOUT = 600.0
SYNC_LIMIT = 512 * 1024 * 1024
WORKSPACE = "/workspace"
NOT_ENABLED = (
    "The platform has not switched on work VMs on this execution device yet. This is a state of the platform,"
    " not a limit of the session: tell the user that execution is not available on this device yet and that the"
    " operator switches it on; request it again later."
)
NOT_READY = (
    "The work VMs of this execution device are not ready right now (work image or network boundary). This is a"
    " state of the platform: tell the user, then request it again in a few minutes."
)
NO_VM = "No work VM: request one with request_operation type workvm.request."
IMPLICIT_TOOLS = frozenset({"workvm_exec", "workvm_browse"})
RUN_COMMAND_LIMIT = 4096
RUN_OUTPUT_LIMIT = 65536
BROWSE_COMMAND = "/usr/local/bin/workvm-browse"
BROWSE_TIMEOUT = 120
CAPTURE_TIMEOUT = 300.0
NO_LEASE = (
    "This execution device has not reached the platform hub for longer than the failover time, so the platform may"
    " have moved this session to another device: commands and browsing in the work VM are refused until the hub is"
    " reachable again. Tell the user; try again in a few minutes."
)
NO_CREDIT = (
    "The organization's credit is used up: the platform starts no new work VM or test run until credit is added."
    " Tell the user; the work continues once credit is available."
)
RUN_SCOPE = "run"
BROWSER_SCOPE = "browser"
OWNED_SCOPES = frozenset({RUN_SCOPE, BROWSER_SCOPE})
"""VMs owned by one test run or one browser session (ADR-080, ADR-085): no `workvm.state` events, no per-task limit,
not torn down by the idle pass or stopped by the credit stop of this manager (their owner ends them)."""


def lima_reason(detail: str) -> str:
    """Lima's last error line of a failed call's stderr tail: the message of the last `level=fatal|error` line, else
    the last line; one line of printable characters, at most `LIMA_REASON_LIMIT` characters. The tail may repeat
    arguments of the call; those of the work VMs (instance names, `--set` sizes, port, resolvers) hold no secret."""
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    problems = [line for line in lines if _LIMA_PROBLEM.search(line)]
    line = (problems or lines or [""])[-1]
    if match := _LIMA_MESSAGE.search(line):
        line = re.sub(r"\\(.)", r"\1", match.group(1))
    line = " ".join("".join(char if char.isprintable() else " " for char in line).split())
    return line if len(line) <= LIMA_REASON_LIMIT else line[: LIMA_REASON_LIMIT - 3] + "..."


def failure(error: RunnerError) -> str:
    """`code=<code>` of a failed work VM step, with Lima's last error line when Lima gave one."""
    reason = lima_reason(error.detail) if isinstance(error, LimaError) else ""
    return f"code={error.code}" + (f" lima={json.dumps(reason)}" if reason else "")


@dataclass(frozen=True)
class Settings:
    slots: int
    max_size: str
    idle_seconds: int
    queue_seconds: int
    max_per_task: int
    max_per_org: int
    max_per_owner: int
    ssh_port_base: int
    test_mode: bool
    pool_size: int = 0
    disk_quota_gib: int = 0
    """Allocated disk of all work VMs and pool entries may not exceed it (0: not checked, spec 13 section 7)."""


@dataclass
class Deps:
    """Collaborators (injected; production wiring in `service.py`)."""

    db: Any
    settings: Settings
    now: Callable[[], datetime]
    lima_for: Callable[[int], Lima]
    shell_for: Callable[[WorkVm], Shell]
    base_for: Callable[[int, str], Awaitable[str]]
    """The stopped instance in the slot's Lima home to clone from for the image (created on first use)."""
    boundary_ok: Callable[[], Awaitable[bool]]
    free_memory_mib: Callable[[], Awaitable[int]]
    log: Callable[[str], None]
    emit: Callable[[Any, str, Mapping[str, Any]], Awaitable[None]] = field(default_factory=lambda: _no_emit)
    """Writes an event of the session (`Identity`, kind, body); default: none."""
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    scratch: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    exhausted: Callable[[str], bool] = field(default_factory=lambda: _never)
    """The organization's credit is used up on this (platform) device (`billing.state.exhausted`)."""
    capture: Callable[[WorkVm, Identity, str, int], Awaitable[dict[str, Any] | None]] | None = None
    """Stores the output of a final job (`vm, identity, job_id, output_bytes`) as a log; returns its `log_ref`."""
    hub_lease: Callable[[], bool] = field(default_factory=lambda: _always)
    """The runner's lease of the tools with internet egress (`sync.lease.held`, ADR-087 section 4)."""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _browse_answer(view: JobView) -> dict[str, Any]:
    """The JSON line the browse helper prints last (an object), or an empty answer."""
    for line in reversed(view.output.strip().splitlines()):
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                return cast(dict[str, Any], json.loads(line))  # a JSON text that starts with "{" is an object
    return {}


class CallFailed(Exception):
    """A tool call that ends with a tool error (`mcp/objects.json#/$defs/tool_error`)."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(error)
        self.error = error
        self.message = message


@dataclass(frozen=True)
class Identity:
    """Where an event of a work VM belongs: organization, workgroup, task and session (header of 11 section 8)."""

    org_id: str
    wg_id: str
    task_id: str
    session_id: str


@dataclass
class Run:
    """A job the runner started and has not yet recorded as final."""

    identity: Identity
    workvm_id: str
    kind: str
    command: str
    started_at: datetime


Emit = Callable[[Identity, str, Mapping[str, Any]], Awaitable[None]]


async def _no_emit(_identity: Identity, _kind: str, _body: Mapping[str, Any]) -> None:
    return None


def _never(_org_id: str) -> bool:
    return False


def _always() -> bool:
    return True


class WorkVms:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self.locks: dict[str, asyncio.Lock] = {}
        self.queue: list[str] = []
        self.last_call: dict[str, datetime] = {}
        self.runs: dict[str, Run] = {}
        self.slot_lock = asyncio.Lock()
        """Held from choosing a free slot to inserting its row (work VM or pool entry)."""
        self.pool_wanted = asyncio.Event()
        """Set when a pool entry was assigned: the daemon task refills the pool at once (`tasks.py`)."""
        self.captures: dict[str, set[asyncio.Task[None]]] = {}
        """Running log captures per work VM (its teardown waits for them)."""

    # -- availability ------------------------------------------------------------------------------------------

    async def available(self) -> str | None:
        """The ready image, or None when this device offers no work VMs now."""
        if self.deps.settings.slots == 0:
            return None
        image = await records.ready_image(self.deps.db)
        if image is None or not await self.deps.boundary_ok():
            return None
        return image

    # -- request / release -------------------------------------------------------------------------------------

    async def request(
        self,
        session: LocalSession,
        params: Mapping[str, Any],
        *,
        implicit: bool = False,
        triggered_by: str | None = None,
    ) -> dict[str, Any]:
        size = str(params.get("size", "standard"))
        # a subagent always gets its own VM: the VM's /workspace mirrors one workspace (decisions #19 M7)
        scope = "session" if session.parent_id is not None else str(params.get("scope", "task"))
        started = self.deps.now()
        own = session.id if scope == "session" else None
        existing = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=own)
        if existing is not None and existing.state == "stopped":
            self._check_credit(session)
            existing = await self._resume(existing)
        if existing is not None:
            vm = await self.ensure_running(existing)
            return self._result(vm, created=False, started=started)
        vm = await self.obtain(session, scope=scope, size=size, implicit=implicit, triggered_by=triggered_by)
        return self._result(vm, created=True, started=started)

    def _check_credit(self, session: LocalSession) -> None:
        if self.deps.exhausted(session.org_id):
            raise OperationFailed("credit_exhausted", message=NO_CREDIT)

    async def check_new(self, session: LocalSession, *, scope: str, size: str) -> str:
        """The formal checks of a new VM (credit, size, availability, limits); returns the ready image."""
        self._check_credit(session)
        if SIZE_ORDER.index(size) > SIZE_ORDER.index(self.deps.settings.max_size):
            raise OperationFailed("limit_exceeded", message="This work VM size is above the limit of the device.")
        image = await self.available()
        if image is None:
            raise OperationFailed("workvm_unavailable", message=NOT_READY)
        task, org, owner = await records.counts(
            self.deps.db, task_id=session.task_id, org_id=session.org_id, owner_id=session.owner_id
        )
        settings = self.deps.settings
        over_task = scope not in OWNED_SCOPES and task >= settings.max_per_task  # owned VMs: no per-task limit
        if over_task or org >= settings.max_per_org or owner >= settings.max_per_owner:
            raise OperationFailed("limit_exceeded", message="The limit of work VMs is reached.")
        return image

    async def obtain(
        self,
        session: LocalSession,
        *,
        scope: str,
        size: str,
        implicit: bool = False,
        workvm_id: str | None = None,
        triggered_by: str | None = None,
    ) -> WorkVm:
        """A new running VM of the scope (formal checks, warm pool, else the FIFO queue and a cold clone)."""
        image = await self.check_new(session, scope=scope, size=size)

        async def from_pool() -> WorkVm | None:
            return await self._from_pool(
                session, scope, size, image, implicit=implicit, workvm_id=workvm_id, triggered_by=triggered_by
            )

        if not self.queue:
            pooled = await from_pool()
            if pooled is not None:
                return pooled

        def build(slot: int) -> WorkVm:
            return self._row(
                session,
                scope=scope,
                size=size,
                image=image,
                slot=slot,
                lima_name=self.deps.lima_for(slot).name(_lima_suffix()),
                implicit=implicit,
                workvm_id=workvm_id,
                triggered_by=triggered_by,
            )

        vm = await self._wait_for_capacity(SIZES[size][1], SIZES[size][2], build, from_pool)
        try:
            if vm.state == "starting":
                vm = await self._boot(vm)
        finally:
            self._leave_queue()
        return vm

    async def _wait_for_capacity(
        self,
        memory_mib: int,
        disk_gib: int,
        build: Callable[[int], WorkVm],
        from_pool: Callable[[], Awaitable[WorkVm | None]],
    ) -> WorkVm:
        """Wait FIFO; at the head take a ready pool entry (`running`, standard size only), else wait for a free slot
        and RAM and insert the row `build(slot)` (`starting`). A ready pool entry gives its slot and RAM back to a
        waiting request that does not fit otherwise."""
        ticket = secrets.token_hex(8)
        self.queue.append(ticket)
        deadline = self.deps.now() + timedelta(seconds=self.deps.settings.queue_seconds)
        try:
            while True:
                if self.queue[0] == ticket:
                    pooled = await from_pool()
                    if pooled is not None:
                        return pooled
                    async with self.slot_lock:
                        slot = await self._free_slot()
                        free = await self.deps.free_memory_mib()
                        disk = await self._disk_fits(disk_gib)
                        if slot is not None and free >= memory_mib and disk:
                            return await self._insert(build(slot))
                    if await self._drop_one_pooled(free_mib=free, need_mib=memory_mib, disk_short=not disk):
                        continue
                    if (slot is None or not disk) and await self._drop_one_stopped():
                        continue
                if self.deps.now() >= deadline:
                    raise OperationFailed(
                        "timeout", message="No capacity for a work VM became free in time; request again."
                    )
                await self.deps.sleep(QUEUE_POLL)
        except BaseException:
            self.queue.remove(ticket)  # a time-out, a cancelled operation or a failing check never blocks the queue
            raise

    async def _drop_one_stopped(self) -> bool:
        """Give the slot and disk of the oldest stopped VM (a paused session's) back to a waiting request."""
        stopped = await records.live(self.deps.db, "stopped")
        if not stopped:
            return False
        await self.delete(stopped[0], reason="session_paused")
        return True

    def _leave_queue(self) -> None:
        self.queue.pop(0)  # the request that leaves was the head of the queue (`_wait_for_capacity`)

    async def _free_slot(self) -> int | None:
        used = await records.used_slots(self.deps.db)
        free = [slot for slot in range(self.deps.settings.slots) if slot not in used]
        return free[0] if free else None

    def _row(
        self,
        session: LocalSession,
        *,
        scope: str,
        size: str,
        image: str,
        slot: int,
        lima_name: str,
        implicit: bool,
        triggered_by: str | None = None,
        state: str = "starting",
        workvm_id: str | None = None,
    ) -> WorkVm:
        cpus, memory_mib, disk_gib = SIZES[size]
        now = self.deps.now()
        return WorkVm(
            id=workvm_id or new_workvm_id(),
            lima_name=lima_name,
            slot=slot,
            scope=scope,
            task_id=session.task_id,
            session_id=None if scope == "task" else session.id,
            requested_by=session.id,
            implicit=implicit,
            org_id=session.org_id,
            wg_id=session.wg_id,
            owner_id=session.owner_id,
            size=size,
            cpus=cpus,
            memory_mib=memory_mib,
            disk_gib=disk_gib,
            image=image,
            state=state,
            created_at=now,
            last_used_at=now,
            stopped_at=None,
            triggered_by=triggered_by,
        )

    async def _insert(self, row: WorkVm) -> WorkVm:
        try:
            return await records.insert(self.deps.db, row)
        except RunnerError:
            raise OperationFailed("timeout", retryable=True, message="The work VM slot was taken; try again.") from None

    def _sets(self, size: str, slot: int) -> list[str]:
        """The `limactl clone --set` expressions of a VM of `size` in `slot`."""
        cpus, memory_mib, disk_gib = SIZES[size]
        sets = [f".cpus={cpus}", f'.memory="{memory_mib // 1024}GiB"', f'.disk="{disk_gib}GiB"']
        sets.append(f".ssh.localPort={self.deps.settings.ssh_port_base + slot}")
        sets.append(".dns=[" + ",".join(f'"{address}"' for address in firewall.resolvers()) + "]")
        return sets

    async def _clone_and_start(self, probe: WorkVm, size: str) -> None:
        lima = self.deps.lima_for(probe.slot)
        base = await self.deps.base_for(probe.slot, probe.image)
        await asyncio.to_thread(lima.clone, base, probe.lima_name, sets=self._sets(size, probe.slot))
        await asyncio.to_thread(lima.start, probe.lima_name)
        await self._wait_ready(probe)

    async def _boot(self, vm: WorkVm) -> WorkVm:
        """Clone, start and wait for the `starting` row's VM (the cold path, empty pool or a larger size)."""
        try:
            await self._clone_and_start(vm, vm.size)
        except (LimaError, WorkVmError) as error:
            self.deps.log(f"runner.workvm start_failed workvm_id={vm.id} {failure(error)}")
            await self._remove(vm, reason="failed")
            raise OperationFailed("upstream_error", retryable=True, message="The work VM did not start.") from None
        running = await records.update(self.deps.db, vm.id, state="running", last_used_at=self.deps.now())
        assert running is not None
        self.deps.log(
            f"runner.workvm created workvm_id={vm.id} slot={vm.slot} size={vm.size} scope={vm.scope} pooled=0"
        )
        if vm.scope not in OWNED_SCOPES:
            await self._emit_state(running, "created")
        return running

    # -- warm pool (spec 11 section 7) -------------------------------------------------------------------------

    async def _from_pool(
        self,
        session: LocalSession,
        scope: str,
        size: str,
        image: str,
        *,
        implicit: bool,
        workvm_id: str | None = None,
        triggered_by: str | None = None,
    ) -> WorkVm | None:
        """Assign a ready pool entry (a row update) when the size is the pool's; None: none fits or it is unhealthy
        (then the request continues on the cold path)."""
        if self.deps.settings.pool_size == 0 or size != POOL_SIZE:
            return None

        def build(lima_name: str, slot: int) -> WorkVm:
            return self._row(
                session,
                scope=scope,
                size=size,
                image=image,
                slot=slot,
                lima_name=lima_name,
                implicit=implicit,
                triggered_by=triggered_by,
                state="running",
                workvm_id=workvm_id,
            )

        try:
            vm = await records.pool_assign(self.deps.db, image, SIZES[size][1], build)
        except RunnerError:
            raise OperationFailed("timeout", retryable=True, message="The work VM slot was taken; try again.") from None
        if vm is None:
            return None
        self.pool_wanted.set()
        result = await self.deps.shell_for(vm)(
            jobs.list_argv(), stdin=None, timeout=POOL_HEALTH_TIMEOUT, limit=jobs.LIST_LIMIT
        )
        if result.returncode != 0:
            self.deps.log(f"runner.workvm pool_unhealthy workvm_id={vm.id} slot={vm.slot}")
            await self._remove(vm, reason="failed")
            return None
        self.deps.log(
            f"runner.workvm created workvm_id={vm.id} slot={vm.slot} size={vm.size} scope={vm.scope} pooled=1"
        )
        if vm.scope not in OWNED_SCOPES:
            await self._emit_state(vm, "created")
        return vm

    def _probe(self, entry: Pooled) -> WorkVm:
        """A row-shaped handle of a pool entry for Lima and the shell (no row exists before it is assigned)."""
        cpus, memory_mib, disk_gib = SIZES[POOL_SIZE]
        return WorkVm(
            id=POOL_PROBE_ID,
            lima_name=entry.lima_name,
            slot=entry.slot,
            scope="task",
            task_id=NO_ID,
            session_id=None,
            requested_by=NO_ID,
            implicit=False,
            org_id=NO_ID,
            wg_id=NO_ID,
            owner_id=None,
            size=POOL_SIZE,
            cpus=cpus,
            memory_mib=memory_mib,
            disk_gib=disk_gib,
            image=entry.image,
            state=entry.state,
            created_at=entry.created_at,
            last_used_at=entry.created_at,
            stopped_at=None,
        )

    async def pool_pass(self) -> int:
        """Keep `pool_size` ready standard VMs of the current image (entries of another image, or all when no image
        is offered, go); never while a request waits, never into the RAM of the last free standard VM. Starts at most
        one entry per pass so the boundary guard of the daemon task never waits behind a long refill; when more are
        missing it sets `pool_wanted` for the next pass at once. Returns the number of entries started (0 or 1)."""
        settings = self.deps.settings
        if settings.pool_size == 0:
            return 0
        self.pool_wanted.clear()
        image = await self.available()
        for entry in await records.pool(self.deps.db):
            if entry.image != image:
                await self._drop(entry)
        memory_mib = SIZES[POOL_SIZE][1]
        if image is None or self.queue:
            return 0
        async with self.slot_lock:
            slot = await self._free_slot()
            if (
                len(await records.pool(self.deps.db)) >= settings.pool_size
                or slot is None
                or await self.deps.free_memory_mib() < 2 * memory_mib
                or not await self._disk_fits(2 * SIZES[POOL_SIZE][2])
            ):
                return 0
            lima_name = self.deps.lima_for(slot).name(_lima_suffix())
            entry = Pooled(lima_name, slot, image, memory_mib, "starting", self.deps.now())
            await records.pool_add(self.deps.db, entry)
        begun = time.monotonic()
        try:
            await self._clone_and_start(self._probe(entry), POOL_SIZE)
        except (LimaError, WorkVmError) as error:
            self.deps.log(f"runner.workvm pool_failed slot={slot} {failure(error)}")
            await self._drop(entry)
            return 0
        if not await records.pool_ready(self.deps.db, entry.lima_name, self.deps.now()):
            await self._delete_instance(entry)  # the entry went while it started (guard, restart): its VM goes too
            return 0
        self.deps.log(f"runner.workvm pool_ready slot={slot} start_ms={int((time.monotonic() - begun) * 1000)}")
        if len(await records.pool(self.deps.db)) < settings.pool_size:
            self.pool_wanted.set()
        return 1

    async def _drop(self, entry: Pooled) -> None:
        """Remove the entry, then its VM: only the caller whose removal took the row deletes the instance (a
        concurrent assignment turned it into a work VM)."""
        if await records.pool_remove(self.deps.db, entry.lima_name):
            await self._delete_instance(entry)

    async def _delete_instance(self, entry: Pooled) -> None:
        try:
            await asyncio.to_thread(self.deps.lima_for(entry.slot).delete, entry.lima_name)
        except LimaError as error:
            self.deps.log(f"runner.workvm pool_delete_failed slot={entry.slot} {failure(error)}")

    async def _disk_fits(self, disk_gib: int) -> bool:
        """The disk of a new VM fits the volume quota (always when no quota is configured)."""
        quota = self.deps.settings.disk_quota_gib
        if quota == 0:
            return True
        return await records.disk_in_use(self.deps.db, SIZES[POOL_SIZE][2]) + disk_gib <= quota

    async def _drop_one_pooled(self, *, free_mib: int, need_mib: int, disk_short: bool) -> bool:
        """Give the slot, RAM and disk of the oldest `ready` pool entry back (a `starting` one belongs to the running
        refill) - when the disk is short or the RAM of all ready entries would make the request fit; False otherwise
        or when there is none."""
        ready = [entry for entry in await records.pool(self.deps.db) if entry.state == "ready"]
        if not ready or (not disk_short and free_mib + sum(entry.memory_mib for entry in ready) < need_mib):
            return False
        await self._drop(ready[0])
        return True

    async def _wait_ready(self, vm: WorkVm) -> None:
        shell = self.deps.shell_for(vm)
        deadline = self.deps.now() + timedelta(seconds=READY_TIMEOUT)
        while True:
            result = await shell(jobs.list_argv(), stdin=None, timeout=10.0, limit=jobs.LIST_LIMIT)
            if result.returncode == 0:
                return
            if self.deps.now() >= deadline:
                raise WorkVmError("workvm_not_ready")
            await self.deps.sleep(0.5)

    def _result(self, vm: WorkVm, *, created: bool, started: datetime) -> dict[str, Any]:
        ready_ms = max(0, int((self.deps.now() - started).total_seconds() * 1000))
        return {
            "workvm_id": vm.id,
            "state": "running",
            "scope": vm.scope,
            "size": vm.size,
            "cpus": vm.cpus,
            "memory_mib": vm.memory_mib,
            "disk_gib": vm.disk_gib,
            "created": created,
            "ready_ms": ready_ms,
        }

    async def release(self, session: LocalSession, params: Mapping[str, Any]) -> dict[str, Any]:
        own = session.id if params.get("scope", "task") == "session" else None
        vm = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=own)
        if vm is None:
            return {"released": False}
        await self.delete(vm, reason="released")
        return {"released": True, "workvm_id": vm.id}

    async def delete(self, vm: WorkVm, *, reason: str) -> None:
        """Tear the VM down (temporary VMs, ADR-059 decision 8): record its runs, keep their logs, delete it, record
        its runtime. Shielded: a cancelled caller never leaves a VM half torn down."""
        await asyncio.shield(asyncio.ensure_future(self._delete(vm, reason=reason)))

    async def _delete(self, vm: WorkVm, *, reason: str) -> None:
        async with self._lock(vm.id):
            current = await records.get(self.deps.db, vm.id)
            if current is None or current.state == "deleted":
                return
            await records.update(self.deps.db, vm.id, state="deleting")
            if current.state == "running":
                await self._settle_runs(current, at_teardown=True)
            await self.wait_captures(vm.id)
            if not await self._remove(current, reason=reason):
                return
            if current.scope not in OWNED_SCOPES:
                await self._emit_state(current, "deleted", reason=reason)
        self.deps.log(f"runner.workvm deleted workvm_id={vm.id} reason={reason}")

    async def wait_captures(self, workvm_id: str) -> None:
        """Wait (at most `CAPTURE_TIMEOUT`) for the running log captures of the VM."""
        pending = self.captures.get(workvm_id, set())
        if pending:
            await asyncio.wait(set(pending), timeout=CAPTURE_TIMEOUT)

    async def _remove(self, vm: WorkVm, *, reason: str) -> bool:
        """Delete the instance and mark the row `deleted`; when Lima fails the row stays `deleting` (no slot is given
        back for a VM that may still run) and `sweep` tries again."""
        lima = self.deps.lima_for(vm.slot)
        try:
            await asyncio.to_thread(lima.delete, vm.lima_name)
        except LimaError as error:
            self.deps.log(f"runner.workvm delete_failed workvm_id={vm.id} {failure(error)}")
            await records.update(self.deps.db, vm.id, state="deleting", delete_reason=reason)
            return False
        await records.update(self.deps.db, vm.id, state="deleted", deleted_at=self.deps.now(), delete_reason=reason)
        self.last_call.pop(vm.id, None)
        for job_id in [job_id for job_id, run in self.runs.items() if run.workvm_id == vm.id]:
            del self.runs[job_id]
        return True

    # -- events ------------------------------------------------------------------------------------------------

    async def _emit_state(self, vm: WorkVm, state: str, *, reason: str | None = None) -> None:
        ended = self.deps.now() if state in ("deleted", "stopped") else None
        runtime = 0 if ended is None else max(0, int((ended - vm.created_at).total_seconds() * 1000))
        body = {
            "workvm_id": vm.id,
            "state": state,
            "scope": vm.scope,
            "size": vm.size,
            "cpus": vm.cpus,
            "memory_mib": vm.memory_mib,
            "disk_gib": vm.disk_gib,
            "implicit": vm.implicit,
            "reason": reason,
            "started_at": _iso(vm.created_at),
            "ended_at": None if ended is None else _iso(ended),
            "runtime_ms": runtime,
        }
        identity = Identity(vm.org_id, vm.wg_id, vm.task_id, vm.session_id or vm.requested_by)
        await self._emit(identity, "workvm.state", body)

    async def _emit(self, identity: Identity, kind: str, body: Mapping[str, Any]) -> None:
        try:
            await self.deps.emit(identity, kind, body)
        except Exception as error:  # an event that cannot be written must not break the VM; the log keeps the fact
            self.deps.log(f"runner.workvm event_failed kind={kind} error={type(error).__name__}")

    def _started(self, vm: WorkVm, session: LocalSession, job_id: str, kind: str, command: str) -> Run:
        run = Run(
            identity=Identity(session.org_id, session.wg_id, session.task_id, session.id),
            workvm_id=vm.id,
            kind=kind,
            command=command,
            started_at=self.deps.now(),
        )
        self.runs[job_id] = run
        return run

    async def _record(self, job_id: str, view: JobView, *, state: str | None = None, initial: bool = False) -> None:
        """Write the `workvm.run` event of a known job: `running` at its start (`initial`), its final state once. A
        final job with more output than the event keeps is captured as a log first, in the background; the event then
        names the log."""
        run = self.runs.get(job_id)
        final = (state or view.state) != "running"
        if run is None or (not final and not initial):
            return
        if final:
            del self.runs[job_id]
            if view.output_bytes > RUN_OUTPUT_LIMIT and self.deps.capture is not None:
                task = asyncio.ensure_future(self._captured(run, job_id, view, state))
                self.captures.setdefault(run.workvm_id, set()).add(task)
                task.add_done_callback(lambda done: self.captures.get(run.workvm_id, set()).discard(done))
                return
        await self._write_run(run, job_id, view, state=state, final=final)

    async def _captured(self, run: Run, job_id: str, view: JobView, state: str | None) -> None:
        log: dict[str, Any] | None = None
        vm = await records.get(self.deps.db, run.workvm_id)
        capture = self.deps.capture
        if vm is not None and capture is not None:
            try:
                log = await capture(vm, run.identity, job_id, view.output_bytes)
            except Exception as error:  # the run is recorded without its log; the log line keeps the fact
                self.deps.log(f"runner.workvm capture_failed job_id={job_id} error={type(error).__name__}")
        await self._write_run(run, job_id, view, state=state, final=True, log=log)

    async def _write_run(
        self,
        run: Run,
        job_id: str,
        view: JobView,
        *,
        state: str | None,
        final: bool,
        log: dict[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "workvm_id": run.workvm_id,
            "job_id": job_id,
            "kind": run.kind,
            "command": run.command[:RUN_COMMAND_LIMIT],
            "state": state or view.state,
            "exit_code": view.exit_code,
            "started_at": _iso(run.started_at),
            "ended_at": _iso(self.deps.now()) if final else None,
            "duration_ms": view.duration_ms,
            "output": _tail(view.output, RUN_OUTPUT_LIMIT),
            "output_bytes": view.output_bytes,
        }
        if log is not None:
            body["log"] = log
        await self._emit(run.identity, "workvm.run", body)

    async def _settle_runs(self, vm: WorkVm, *, at_teardown: bool) -> None:
        """Record the final state of the runner's open runs of the VM; at teardown a still running job is `lost`."""
        for job_id in [job_id for job_id, run in self.runs.items() if run.workvm_id == vm.id]:
            try:
                view = await self._wait(vm, {"job_id": job_id, "wait_seconds": 0, "limit": RUN_OUTPUT_LIMIT})
            except WorkVmError:
                view = JobView(job_id, "", "running", None, "", 0, 0, 0)
            if view.state == "running" and at_teardown:
                await self._record(job_id, view, state="lost")
            elif view.state != "running":
                await self._record(job_id, view)

    # -- lifecycle ---------------------------------------------------------------------------------------------

    def _lock(self, workvm_id: str) -> asyncio.Lock:
        return self.locks.setdefault(workvm_id, asyncio.Lock())

    async def ensure_running(self, vm: WorkVm) -> WorkVm:
        """The VM when it is running; a VM that is starting, being deleted or gone cannot be used."""
        current = await records.get(self.deps.db, vm.id)
        if current is None or current.state != "running":
            raise WorkVmError("workvm_gone")
        return current

    async def pause(self, session: LocalSession, *, reason: str, when_idle: bool = False) -> list[str]:
        """Stop the VMs a paused session works in (ADR-080 section 4): its own session VM, and the task VM when no
        other session of the task is active - with `when_idle` only a VM in which no job runs. Their RAM and runtime
        end; they are deleted after `WORKVM_IDLE_SECONDS` stopped unless a call or request of the session starts them
        again. Returns the stopped IDs."""
        found: list[WorkVm] = []
        own = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=session.id)
        if own is not None:
            found.append(own)
        task_vm = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=None)
        if (
            task_vm is not None
            and await records.active_sessions(self.deps.db, session.task_id, except_id=session.id) == 0
        ):
            found.append(task_vm)
        stopped: list[str] = []
        for vm in found:
            if when_idle and await self._busy(vm):
                continue
            if await self.stop(vm, reason=reason):
                stopped.append(vm.id)
        return stopped

    async def _busy(self, vm: WorkVm) -> bool:
        """A job runs in the running VM (an unreadable answer counts as busy: nothing is stopped on a guess)."""
        if vm.state != "running":
            return False
        try:
            return any(job["state"] == "running" for job in await self._jobs(vm))
        except WorkVmError:
            return True

    async def stop(self, vm: WorkVm, *, reason: str) -> bool:
        """`limactl stop` a running VM (its jobs end and are recorded `lost`); False when it does not run."""
        async with self._lock(vm.id):
            current = await records.get(self.deps.db, vm.id)
            if current is None or current.state != "running":
                return False
            await records.update(self.deps.db, vm.id, state="stopping")
            await self._settle_runs(current, at_teardown=True)
            await self.wait_captures(vm.id)
            try:
                await asyncio.to_thread(self.deps.lima_for(vm.slot).stop, vm.lima_name)
            except LimaError as error:
                self.deps.log(f"runner.workvm stop_failed workvm_id={vm.id} {failure(error)}")
                if await self._remove(current, reason="failed"):
                    await self._emit_state(current, "deleted", reason="failed")
                return False
            stopped = await records.update(self.deps.db, vm.id, state="stopped", stopped_at=self.deps.now())
            assert stopped is not None
            await self._emit_state(stopped, "stopped", reason=reason)
        self.deps.log(f"runner.workvm stopped workvm_id={vm.id} reason={reason}")
        return True

    async def _resume(self, vm: WorkVm) -> WorkVm | None:
        """Start a stopped VM again (its disk kept what the session installed); when its RAM does not fit now it is
        deleted and None returned (the caller gets a new VM)."""
        async with self._lock(vm.id):
            current = await records.get(self.deps.db, vm.id)
            if current is None or current.state != "stopped":
                return current if current is not None and current.state != "deleted" else None
            if await self.deps.free_memory_mib() < current.memory_mib:
                fits = False
            else:
                await records.update(self.deps.db, vm.id, state="starting")
                try:
                    await asyncio.to_thread(self.deps.lima_for(vm.slot).start, vm.lima_name)
                    await self._wait_ready(current)
                    fits = True
                except (LimaError, WorkVmError) as error:
                    self.deps.log(f"runner.workvm resume_failed workvm_id={vm.id} {failure(error)}")
                    fits = False
            if fits:
                running = await records.update(
                    self.deps.db, vm.id, state="running", stopped_at=None, last_used_at=self.deps.now()
                )
                assert running is not None
                await self._emit_state(running, "created")
                self.deps.log(f"runner.workvm resumed workvm_id={vm.id}")
                return running
        await self.delete(current, reason="session_paused")
        return None

    async def close_task(self, task_id: str) -> list[str]:
        """Every VM of a closed task goes at once (`task_closed`, ADR-080 section 5); returns their IDs."""
        removed: list[str] = []
        for vm in await records.live(self.deps.db):
            if vm.task_id == task_id and vm.state != "deleting":
                await self.delete(vm, reason="task_closed")
                removed.append(vm.id)
        return removed

    async def sweep(self) -> int:
        """Delete again the VMs whose deletion failed (rows left `deleting` and not being deleted now), and every own
        `wvm-` instance of a slot that has neither a live row nor a pool entry; returns the number deleted."""
        swept = 0
        for vm in await records.live(self.deps.db, "deleting"):
            if self._lock(vm.id).locked():
                continue
            async with self._lock(vm.id):
                current = await records.get(self.deps.db, vm.id)
                if current is None or current.state != "deleting":
                    continue
                reason = await records.delete_reason(self.deps.db, vm.id) or "failed"
                if await self._remove(current, reason=reason):
                    swept += 1
                    if current.scope not in OWNED_SCOPES:
                        await self._emit_state(current, "deleted", reason=reason)
        known = {vm.lima_name for vm in await records.live(self.deps.db)}
        known |= {entry.lima_name for entry in await records.pool(self.deps.db)}
        async with self.slot_lock:
            for slot in range(self.deps.settings.slots):
                lima = self.deps.lima_for(slot)
                marker = lima.prefix + "wvm-"
                for instance in await asyncio.to_thread(lima.list):
                    if instance.name.startswith(marker) and instance.name not in known:
                        try:
                            await asyncio.to_thread(lima.delete, instance.name)
                        except LimaError as error:
                            self.deps.log(f"runner.workvm orphan_delete_failed slot={slot} {failure(error)}")
                            continue
                        swept += 1
                        self.deps.log(f"runner.workvm orphan_deleted slot={slot}")
        return swept

    async def idle_pass(self) -> list[str]:
        """Tear down VMs idle for `WORKVM_IDLE_SECONDS` (no call and no running job) and VMs stopped that long; run
        and browser VMs belong to their run or browser session. Returns their IDs."""
        removed: list[str] = []
        limit = timedelta(seconds=self.deps.settings.idle_seconds)
        for vm in await records.live(self.deps.db, "stopped"):
            if self.deps.now() - (vm.stopped_at or vm.last_used_at) >= limit:
                await self.delete(vm, reason="session_paused")
                removed.append(vm.id)
        for vm in await records.live(self.deps.db, "running"):
            if vm.scope in OWNED_SCOPES:
                continue
            await self._settle_runs(vm, at_teardown=False)
            last = max(vm.last_used_at, self.last_call.get(vm.id, vm.last_used_at))
            if self.deps.now() - last < limit:
                continue
            try:
                running = [job for job in await self._jobs(vm) if job["state"] == "running"]
            except WorkVmError:
                running = []
            if running:
                continue
            await self.delete(vm, reason="idle")
            removed.append(vm.id)
        return removed

    async def guard_pass(self) -> list[str]:
        """Tear down every live VM and pool entry when the boundary check fails (spec 11 section 6.4); returns the
        IDs of the VMs."""
        if self.deps.settings.slots == 0 or await self.deps.boundary_ok():
            return []
        for entry in await records.pool(self.deps.db):
            await self._drop(entry)
        removed: list[str] = []
        for vm in await records.live(self.deps.db, "starting", "running"):
            await self.delete(vm, reason="failed")
            removed.append(vm.id)
        if removed:
            self.deps.log(f"runner.workvm boundary_failed deleted={len(removed)}")
        return removed

    async def session_ended(self, session: LocalSession, *, reason: str = "session_ended") -> None:
        """Its session VM goes; the task VM goes when no other session of the task is active (decision 8); `reason`
        is `task_closed` when the session ended because its task was closed. Its test runs are cancelled by the
        observer (`testrun.TestRuns.cancel`)."""
        own = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=session.id)
        if own is not None:
            await self.delete(own, reason=reason)
        task_vm = await records.for_scope(self.deps.db, task_id=session.task_id, session_id=None)
        if (
            task_vm is not None
            and await records.active_sessions(self.deps.db, session.task_id, except_id=session.id) == 0
        ):
            await self.delete(task_vm, reason=reason)

    async def left_over(self) -> list[WorkVm]:
        """The live VMs at the start of the daemon (taken before the test-run reconciliation, `tasks.py`)."""
        return await records.live(self.deps.db)

    async def reconcile(self, left: Sequence[WorkVm] | None = None) -> None:
        """After a restart nothing is resumed (KONZEPT idea 44): every VM `left` over from before the restart (default:
        every live VM) and every own `wvm-` instance without a live row is deleted. A VM a session obtained while the
        test-run reconciliation kept the logs of the runs is not in `left` and stays."""
        for entry in await records.pool(self.deps.db):
            await self._drop(entry)
        for vm in await records.live(self.deps.db) if left is None else left:
            await self.delete(vm, reason="restart")
        known: dict[int, set[str]] = {}
        for vm in await records.live(self.deps.db):
            known.setdefault(vm.slot, set()).add(vm.lima_name)
        for slot in range(self.deps.settings.slots):
            lima = self.deps.lima_for(slot)
            marker = lima.prefix + "wvm-"
            for instance in await asyncio.to_thread(lima.list):
                if instance.name.startswith(marker) and instance.name not in known.get(slot, set()):
                    await asyncio.to_thread(lima.delete, instance.name)
                    self.deps.log(f"runner.workvm orphan_deleted slot={slot}")

    # -- tool calls --------------------------------------------------------------------------------------------

    async def call(self, env: Any, session: LocalSession, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The result of one tool call (raises `CallFailed` for a tool error)."""
        if tool == "workvm_status":
            return await self._status(session)
        self._check_lease(session, tool)
        begun = time.monotonic()
        vm = await self._vm_of(session)
        if vm is not None and vm.state == "stopped":
            if self.deps.exhausted(session.org_id):
                raise CallFailed("unavailable", NO_CREDIT)
            vm = await self._resume(vm)
        if vm is None and tool in IMPLICIT_TOOLS:
            vm = await self._implicit(session)
        if vm is None:
            raise CallFailed("not_found", NO_VM)
        try:
            vm = await self.ensure_running(vm)
        except WorkVmError:
            raise CallFailed("unavailable", "The work VM is starting or being removed. Try again shortly.") from None
        self.last_call[vm.id] = self.deps.now()
        try:
            if tool == "workvm_exec":
                return await self._exec(env, vm, session, arguments, begun)
            if tool == "workvm_browse":
                return await self._browse(env, vm, session, arguments, begun)
            if tool == "workvm_output":
                wait = _remaining(float(arguments.get("wait_seconds", 30)), begun, ANSWER_RESERVE)
                view = await self._wait(vm, {**arguments, "wait_seconds": wait})
                await self._record(view.job_id, view)
                return view.result()
            if tool == "workvm_kill":
                return await self._kill(vm, arguments)
            return await self._sync(env, vm, session, arguments)
        except WorkVmError as error:
            if error.code == "workvm_job_unknown":
                raise CallFailed("not_found", "There is no job with this job_id in the work VM.") from None
            raise CallFailed("unavailable", "The work VM did not answer as expected. Try again.") from None

    def _check_lease(self, session: LocalSession, tool: str) -> None:
        """The tools with internet egress need the runner's lease (module docstring)."""
        if tool in IMPLICIT_TOOLS and not self.deps.hub_lease():
            self.deps.log(f"runner.workvm lease_lost tool={tool} session_id={session.id}")
            raise CallFailed("unavailable", NO_LEASE)

    async def _vm_of(self, session: LocalSession) -> WorkVm | None:
        """The VM a session works in: a subagent only in its own session VM (its workspace differs from the task's,
        and the VM's `/workspace` mirrors one workspace); a main session in its own VM, else the task VM."""
        if session.parent_id is not None:
            return await records.for_scope(self.deps.db, task_id=session.task_id, session_id=session.id)
        return await records.for_session(self.deps.db, task_id=session.task_id, session_id=session.id)

    async def _implicit(self, session: LocalSession) -> WorkVm | None:
        """A standard VM for a tool call without one; the `workvm.state` `starting` event comes first, so the panel
        shows the countdown as for `workvm.request` (ADR-080 section 5). It keeps the session's acting user at this
        moment as the person who triggered it (ADR-083 section 2)."""
        scope = "session" if session.parent_id is not None else "task"
        workvm_id = new_workvm_id()
        async with self.deps.db.connection() as conn:
            stamp = await acting.stamp(conn, session)
        try:
            await self.check_new(session, scope=scope, size="standard")
            starting = self._starting(session, scope, workvm_id)
            await self._emit_state(starting, "starting")
            try:
                await self.obtain(
                    session, scope=scope, size="standard", implicit=True, workvm_id=workvm_id, triggered_by=stamp
                )
            except OperationFailed:
                await self._emit_state(starting, "deleted", reason="failed")  # the countdown ends
                raise
        except OperationFailed as failed:
            raise CallFailed("unavailable", f"No work VM could be obtained: {failed.message}") from None
        return await self._vm_of(session)

    def _starting(self, session: LocalSession, scope: str, workvm_id: str) -> WorkVm:
        """The shape of the VM about to start (for its `starting` event; no row exists yet)."""
        return self._row(
            session,
            scope=scope,
            size="standard",
            image="",
            slot=0,
            lima_name="",
            implicit=True,
            workvm_id=workvm_id,
        )

    async def _start_job(
        self,
        vm: WorkVm,
        session: LocalSession,
        *,
        kind: str,
        command: str,
        spec: bytes,
        cwd: str,
        timeout: int | None,
    ) -> str:
        job_id = jobs.new_job_id()
        started = await self.deps.shell_for(vm)(
            jobs.start_argv(job_id, cwd, timeout), stdin=spec, timeout=30.0, limit=jobs.HEADER_LIMIT
        )
        if started.returncode != 0:
            raise WorkVmError("workvm_answer_invalid")
        self._started(vm, session, job_id, kind, command)
        await self._record(job_id, JobView(job_id, command, "running", None, "", 0, 0, 0), initial=True)
        return job_id

    async def _exec(
        self, env: Any, vm: WorkVm, session: LocalSession, arguments: Mapping[str, Any], begun: float
    ) -> dict[str, Any]:
        synced: dict[str, int] | None = None
        if arguments.get("sync", "to_vm") == "to_vm":
            pushed = await self.push(env, vm, session, ".")
            synced = {"files": pushed["files"], "bytes": pushed["bytes"], "deleted": pushed["deleted"]}
        command = str(arguments["command"])
        job_id = await self._start_job(
            vm,
            session,
            kind="exec",
            command=command,
            spec=jobs.start_input(command, arguments.get("env", []), arguments.get("stdin")),
            cwd=str(arguments.get("cwd", WORKSPACE)),
            timeout=arguments.get("timeout_seconds"),
        )
        wait = _remaining(float(arguments.get("wait_seconds", 30)), begun, ANSWER_RESERVE)
        view = await self._wait(vm, {"job_id": job_id, "wait_seconds": wait}, command)
        await self._record(job_id, view)
        result = view.result()
        if synced is not None:
            result["synced"] = synced
        return result

    async def _browse(
        self, env: Any, vm: WorkVm, session: LocalSession, arguments: Mapping[str, Any], begun: float
    ) -> dict[str, Any]:
        action = str(arguments.get("action", "read"))
        request = {key: arguments[key] for key in ("url", "selector", "text", "wait_ms") if key in arguments}
        request["action"] = action
        command = "workvm-browse " + json.dumps(request, sort_keys=True)
        job_id = await self._start_job(
            vm,
            session,
            kind="browse",
            command=command[:RUN_COMMAND_LIMIT],
            spec=jobs.start_input(BROWSE_COMMAND, [{"name": "WORKVM_BROWSE", "value": json.dumps(request)}], None),
            cwd="/",
            timeout=BROWSE_TIMEOUT,
        )
        wait = _remaining(30.0, begun, BROWSE_RESERVE)
        view = await self._wait(vm, {"job_id": job_id, "wait_seconds": wait, "limit": jobs.OUTPUT_LIMIT}, command)
        await self._record(job_id, view)
        answer = _browse_answer(view)
        screenshot: str | None = None
        if action == "screenshot" and answer.get("screenshot"):
            screenshot = str(arguments.get("path", f".workvm/screenshots/{job_id}.png"))
            await self.fetch_file(env, vm, session, str(answer["screenshot"]), screenshot)
        return {
            "url": str(arguments["url"]),
            "final_url": str(answer.get("final_url", ""))[:8192],
            "status": answer.get("status")
            if type(answer.get("status")) is int and 0 <= answer["status"] <= 999
            else None,
            "title": str(answer.get("title", ""))[:1024],
            "text": str(answer.get("text", view.output if view.state != "exited" else ""))[: jobs.OUTPUT_LIMIT],
            "screenshot": screenshot,
            "job_id": job_id,
        }

    async def fetch_file(self, env: Any, vm: WorkVm, session: LocalSession, vm_path: str, target: str) -> None:
        """Bring one file of the VM into the workspace at `target` (its directory is merged, nothing else changes)."""
        directory, _, name = target.rpartition("/")
        shell = self.deps.shell_for(vm)
        with tempfile.TemporaryDirectory(dir=self.deps.scratch) as scratch:
            packed, workspace, merged = (Path(scratch) / n for n in ("vm.tar", "ws.tar", "merged.tar"))
            result = await shell(
                ["sudo", "-n", jobs.HELPER, "packfile", vm_path, name],
                stdin=None,
                timeout=60.0,
                limit=SYNC_LIMIT,
                out=packed,
            )
            if result.returncode != 0 or result.truncated:
                raise WorkVmError("workvm_sync_failed")
            root = directory or "."
            await _export(env, session, root, workspace, missing_ok=True)
            if not workspace.exists():
                _empty_export(workspace)
            sync.merge_from_vm(workspace, packed, None, merged)
            await _import(env, session, root, merged)

    async def _wait(self, vm: WorkVm, arguments: Mapping[str, Any], command: str | None = None) -> JobView:
        job_id = str(arguments["job_id"])
        limit = int(arguments.get("limit", jobs.OUTPUT_LIMIT))
        wait = float(arguments.get("wait_seconds", 30 if command is None else 0))
        shell = self.deps.shell_for(vm)
        result = await shell(
            jobs.wait_argv(job_id, arguments.get("offset"), limit, wait),
            stdin=None,
            timeout=wait + CALL_MARGIN,
            limit=jobs.HEADER_LIMIT + limit + 1,
        )
        label = command if command is not None else await self._label(vm, job_id)
        return jobs.parse_wait(result, job_id=job_id, command=label, limit=limit)

    async def _label(self, vm: WorkVm, job_id: str) -> str:
        run = self.runs.get(job_id)
        if run is not None:
            return run.command
        for job in await self._jobs(vm):
            if job["job_id"] == job_id:
                return str(job["command"])
        return ""

    async def _kill(self, vm: WorkVm, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(arguments["job_id"])
        shell = self.deps.shell_for(vm)
        await shell(jobs.kill_argv(job_id, str(arguments.get("signal", "TERM"))), stdin=None, timeout=10.0, limit=4096)
        view = await self._wait(vm, {"job_id": job_id, "wait_seconds": 2})
        await self._record(job_id, view)
        return view.result()

    async def _jobs(self, vm: WorkVm) -> list[dict[str, Any]]:
        shell = self.deps.shell_for(vm)
        return jobs.parse_list(await shell(jobs.list_argv(), stdin=None, timeout=10.0, limit=jobs.LIST_LIMIT))

    async def _status(self, session: LocalSession) -> dict[str, Any]:
        vm = await self._vm_of(session)
        if vm is None:
            return {"workvm": None, "jobs": []}
        found: list[dict[str, Any]] = []
        if vm.state == "running":
            with contextlib.suppress(WorkVmError):
                found = [{**job, "started_at": _time(job["started_at"])} for job in await self._jobs(vm)]
        state = vm.state if vm.state in ("starting", "running") else "stopping"
        last = max(vm.last_used_at, self.last_call.get(vm.id, vm.last_used_at))
        return {
            "workvm": {
                "workvm_id": vm.id,
                "state": state,
                "scope": vm.scope,
                "size": vm.size,
                "cpus": vm.cpus,
                "memory_mib": vm.memory_mib,
                "disk_gib": vm.disk_gib,
                "created_at": _iso(vm.created_at),
                "last_used_at": _iso(last),
            },
            "jobs": found,
        }

    # -- workspace mirror --------------------------------------------------------------------------------------

    async def push(self, env: Any, vm: WorkVm, session: LocalSession, root: str) -> dict[str, int]:
        """Mirror the changed files of the workspace (`root` below it) into `/workspace` (spec 11 section 3)."""
        with tempfile.TemporaryDirectory(dir=self.deps.scratch) as scratch:
            exported = Path(scratch) / "export.tar"
            await _export(env, session, root, exported)
            return await self.push_export(vm, session, root, exported)

    async def push_export(self, vm: WorkVm, session: LocalSession, root: str, exported: Path) -> dict[str, int]:
        """Mirror an exported workspace (`root` below it) into `/workspace`: the files changed since the last mirror
        of this VM and session (all of them into a fresh VM)."""
        export = sync.read_export(exported)
        previous = await records.mirror(self.deps.db, vm.id, session.id, root)
        plan = sync.plan_push(previous, export)
        files, size = len(plan.changed), 0
        if plan.changed or plan.deleted:
            packed = exported.with_name(exported.name + ".push")
            files, size = sync.write_push_tar(exported, plan, packed, "" if root == "." else root)
            shell = self.deps.shell_for(vm)
            applied = await shell(
                ["sudo", "-n", jobs.HELPER, "apply", root], stdin=packed, timeout=SYNC_TIMEOUT, limit=4096
            )
            packed.unlink(missing_ok=True)
            if applied.returncode != 0:
                raise WorkVmError("workvm_sync_failed")
        await records.set_mirror(self.deps.db, vm.id, session.id, root, manifest=plan.manifest, at=self.deps.now())
        return {"files": files, "bytes": size, "deleted": len(plan.deleted)}

    async def _sync(self, env: Any, vm: WorkVm, session: LocalSession, arguments: Mapping[str, Any]) -> dict[str, Any]:
        direction = str(arguments["direction"])
        path = arguments.get("path")
        if direction == "to_vm":
            pushed = await self.push(env, vm, session, str(path) if path is not None else ".")
            return {"direction": direction, "path": path, **pushed, "skipped": 0}
        if path is None:
            raise CallFailed("params_invalid", "from_vm needs path: a directory of the workspace.")
        root = str(path)
        with tempfile.TemporaryDirectory(dir=self.deps.scratch) as scratch:
            packed, workspace, merged = (Path(scratch) / name for name in ("vm.tar", "ws.tar", "merged.tar"))
            shell = self.deps.shell_for(vm)
            result = await shell(
                ["sudo", "-n", jobs.HELPER, "pack", root],
                stdin=None,
                timeout=SYNC_TIMEOUT,
                limit=SYNC_LIMIT,
                out=packed,
            )
            if result.returncode != 0 or result.truncated:
                raise WorkVmError("workvm_sync_failed")
            await _export(env, session, root, workspace, missing_ok=True)
            if not workspace.exists():
                _empty_export(workspace)
            mirrored = await records.mirror(self.deps.db, vm.id, session.id, ".")
            prefixed = None if mirrored is None else _below(mirrored, root)
            outcome = sync.merge_from_vm(workspace, packed, prefixed, merged)
            await _import(env, session, root, merged)
        return {
            "direction": direction,
            "path": root,
            "files": outcome.files,
            "bytes": outcome.bytes,
            "deleted": outcome.deleted,
            "skipped": outcome.skipped,
        }


def _lima_suffix() -> str:
    return "wvm-" + secrets.token_hex(4)


def new_workvm_id() -> str:
    return "wvm-" + secrets.token_hex(4)


def _remaining(requested: float, begun: float, reserve: float) -> float:
    """How long the job may still be waited for so the answer reaches the guest within its budget."""
    budget = min(GUEST_MARGIN + requested, GUEST_MAX) - reserve - (time.monotonic() - begun)
    return max(0.0, min(requested, budget))


def _empty_export(path: Path) -> None:
    """A guest export of an empty directory (manifest only), for a `from_vm` path the workspace does not have yet."""
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    data = b'{"files":[],"skipped":[]}'
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(sync.EXPORT_MANIFEST)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def _below(manifest: Mapping[str, str], root: str) -> dict[str, str]:
    prefix = root.rstrip("/") + "/"
    return {path[len(prefix) :]: sha for path, sha in manifest.items() if path.startswith(prefix)}


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _time(milliseconds: int) -> str:
    from datetime import UTC  # noqa: PLC0415

    return _iso(datetime.fromtimestamp(milliseconds / 1000, UTC))


async def export_workspace(env: Any, session: LocalSession, target: Path) -> None:
    """The guest export of the whole workspace into `target` (raises `CallFailed` when it cannot be read)."""
    await _export(env, session, ".", target)


async def export_directory(env: Any, session: LocalSession, root: str, target: Path) -> None:
    """The guest export of the workspace directory `root` into `target` (raises `CallFailed` when it cannot be
    read, also when it does not exist)."""
    await _export(env, session, root, target)


async def _export(env: Any, session: LocalSession, root: str, target: Path, *, missing_ok: bool = False) -> None:
    from agent_runner.github import snapshot  # noqa: PLC0415 - the github area imports the operation pipeline
    from agent_runner.guestlink.errors import GuestError, GuestRefused  # noqa: PLC0415

    try:
        guest = await snapshot.guest_for(env, session)
        async with guest:
            with open(target, "wb") as sink:
                await guest.export_files(session.id, root, sink.write)
    except GuestRefused as error:
        target.unlink(missing_ok=True)
        if missing_ok and error.error == "not_found":
            return
        code = "params_invalid" if error.error == "path_invalid" else "unavailable"
        raise CallFailed(code, _guest_text(error.error)) from None
    except (GuestError, snapshot.NoGuest):
        raise CallFailed("unavailable", "The workspace could not be read. Try again.") from None


async def _import(env: Any, session: LocalSession, root: str, archive: Path) -> None:
    from agent_runner.github import snapshot  # noqa: PLC0415
    from agent_runner.guestlink.errors import GuestError, GuestRefused  # noqa: PLC0415

    try:
        guest = await snapshot.guest_for(env, session)
        size = archive.stat().st_size
        async with guest:
            await guest.import_files(session.id, root, _chunks(archive), replace=True, size=size)
    except GuestRefused as error:
        code = "params_invalid" if error.error == "path_invalid" else "unavailable"
        raise CallFailed(code, _guest_text(error.error)) from None
    except (GuestError, snapshot.NoGuest):
        raise CallFailed("unavailable", "The workspace could not be written. Try again.") from None


async def _chunks(path: Path) -> Any:
    with open(path, "rb") as source:
        while chunk := source.read(65536):
            yield chunk


def _guest_text(code: str) -> str:
    texts = {
        "path_invalid": "The path is not a valid directory of the workspace.",
        "size_exceeded": "The files are larger than the platform accepts (512 MiB, 100000 entries).",
        "not_found": "The directory does not exist.",
    }
    return texts.get(code, "The workspace could not be accessed. Try again.")


def sizes_fit(sizes: Sequence[str], max_size: str) -> list[str]:
    """The sizes a device allows (for the harness context)."""
    return [size for size in sizes if SIZE_ORDER.index(size) <= SIZE_ORDER.index(max_size)]

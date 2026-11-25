"""Production wiring of the work VMs (docs/platform/spec/13-work-vm.md sections 6 and 7).

`workvms_for(context)` builds the daemon's `WorkVms` once (None on a device without work VM slots or before
enrollment). Slots are the accounts `<prefix>wvm00 …` (`accounts.slot_accounts`), or the runner's own account in
`TEST_MODE` with `WORKVM_SLOTS_SELF`. The boundary check (`boundary_ok`) renders the pf anchor for the slot accounts
and compares it with the live filter (`firewall.check`, read through `sudo -n` with the exact read commands sudoers
allows), cached for `BOUNDARY_TTL` seconds. The boundary holds whether or not the device's Cloudflare client is up;
without it the work VMs have no internet (`internet` false). Self mode needs no filter (refused outside `TEST_MODE`
by the configuration).

The lease of the tools with internet egress (`Deps.hub_lease`, ADR-087 section 4) is `sync.lease.held` over the
daemon's hub client and the last desired state.

Test runs (`testruns_for`, KOR-43): the daemon's `testrun.TestRuns` over the same work VMs; logs of runs and of long
jobs are sealed into the context object cache (`agent_runner.contexts.logs`), at most `LOG_MAX_MIB` per log. On a
platform device an exhausted organization (`billing.state.exhausted` of the last desired state) gets no new VM.

The platform browser (`browsers_for`, KOR-44): the daemon's `browser.Browsers` over the same work VMs with
`BROWSER_IDLE_SECONDS` and `BROWSER_SEARCH_URL`, and its live picture (`browser_live.LiveStreams`, capped by
`BROWSER_LIVE_MAX_KBPS`) that posts sealed frames to the hub with the device's signed client and the workgroup's
current key (`HubLink`) and runs the screencast over the slot's `limactl shell`.

Work images: in self mode the stopped image instance in the runner's Lima home is the clone base of every slot. With
slot accounts each slot keeps its own base `<prefix>wbase-<image hash>` in its Lima home, created from the image's
exported disk in `IMAGES_DIR` (readable by the slot accounts) on first use.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runner.billing import state as billing_state
from agent_runner.errors import RunnerError
from agent_runner.sessions.events import session_event
from agent_runner.sessions.records import LocalSession
from agent_runner.sync import lease
from agent_runner.sync.http import HubClient, HubTransportError
from agent_runner.vm import records as vm_records
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lima import Lima
from agent_runner.workvm import accounts, firewall, records
from agent_runner.workvm.browser import Browsers, BrowserSettings
from agent_runner.workvm.browser_live import Link, LiveStreams, spawn_process
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.manager import Deps, Identity, Settings, WorkVms
from agent_runner.workvm.records import WorkVm
from agent_runner.workvm.shell import SlotCommand, VmShell
from agent_runner.workvm.testrun import Store, TestRuns, read_output

ATTRIBUTE = "_workvms"
RUNS_ATTRIBUTE = "_testruns"
BROWSERS_ATTRIBUTE = "_browsers"
MIB = 1024 * 1024
BOUNDARY_TTL = 60.0
IMAGES_DIR = Path("/var/agent-workvm/images")
BANDWIDTH_MBIT = 500


def existing(context: Any) -> WorkVms | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, WorkVms) else None


def install(context: Any, workvms: WorkVms) -> WorkVms:
    setattr(context, ATTRIBUTE, workvms)
    return workvms


def testruns_for(context: Any) -> TestRuns | None:
    """The daemon's test runs; None where there are no work VMs."""
    workvms = workvms_for(context)
    return None if workvms is None else runs_of(context, workvms)


def runs_of(context: Any, workvms: WorkVms) -> TestRuns:
    """The daemon's test runs over its work VMs (built once)."""
    found = getattr(context, RUNS_ATTRIBUTE, None)
    if isinstance(found, TestRuns):
        return found
    deps = workvms.deps
    runs = TestRuns(
        workvms=workvms,
        store=Store(db=deps.db, emit=deps.emit, now=deps.now, log=deps.log),
        capture=Capturer(context, deps.shell_for),
    )
    setattr(context, RUNS_ATTRIBUTE, runs)
    return runs


def browsers_for(context: Any) -> Browsers | None:
    """The daemon's platform browser; None where there are no work VMs."""
    workvms = workvms_for(context)
    return None if workvms is None else browsers_of(context, workvms)


def existing_browsers(context: Any) -> Browsers | None:
    found = getattr(context, BROWSERS_ATTRIBUTE, None)
    return found if isinstance(found, Browsers) else None


def browsers_of(context: Any, workvms: WorkVms) -> Browsers:
    """The daemon's platform browser over its work VMs, with its live picture (built once)."""
    found = existing_browsers(context)
    if found is not None:
        return found
    config = context.config
    browsers = Browsers(
        workvms=workvms,
        settings=BrowserSettings(idle_seconds=config.browser_idle_seconds, search_url=config.browser_search_url),
    )
    link = HubLink(context)

    async def spawn(vm: WorkVm, argv: Sequence[str]) -> Any:
        return await spawn_process(workvms.deps.shell_for(vm), argv)

    browsers.live = LiveStreams(
        browsers, Link(post=link.post, key=link.key, spawn=spawn), max_kbps=config.browser_live_max_kbps
    )
    setattr(context, BROWSERS_ATTRIBUTE, browsers)
    return browsers


class HubLink:
    """The live picture's way to the hub: the device's signed client and the workgroup's current key."""

    def __init__(self, context: Any) -> None:
        self.context = context

    def _env(self) -> Any:
        from agent_runner.contexts.env import contexts_for  # noqa: PLC0415 - the context area imports the sessions

        env = contexts_for(self.context)
        if env is None:
            raise HubTransportError("not_enrolled")
        return env

    async def post(self, target: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        answer: Mapping[str, Any] = await self._env().hub.request("POST", target, dict(body))
        return answer

    async def key(self, wg_id: str) -> tuple[int, bytes] | None:
        from agent_runner.contexts.checkpoint import current_key  # noqa: PLC0415

        env = self._env()
        async with env.sessions.transaction() as conn:
            return await current_key(conn, env.sessions.storage_key, wg_id)


class Capturer:
    """Stores the output of a job as an encrypted log (`agent_runner.contexts.logs.store_log`)."""

    def __init__(self, context: Any, shell_for: Callable[[WorkVm], Any]) -> None:
        self.context = context
        self.shell_for = shell_for

    async def __call__(
        self, vm: WorkVm, identity: Identity, job_id: str, output_bytes: int, ref: str | None = None
    ) -> dict[str, Any] | None:
        from agent_runner.contexts.env import contexts_for  # noqa: PLC0415 - the context area imports the sessions
        from agent_runner.contexts.logs import Owner, store_log  # noqa: PLC0415

        env = contexts_for(self.context)
        if env is None:
            return None
        owner = Owner(identity.org_id, identity.wg_id, identity.task_id, identity.session_id)
        pieces = read_output(
            self.shell_for(vm), job_id, total=output_bytes, keep=int(self.context.config.log_max_mib) * MIB
        )
        return await store_log(env, owner, ref=ref or job_id, total=output_bytes, pieces=pieces)


def run_command(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(list(argv), 1, "", "")


class Boundary:
    """The cached boundary check of the device (host filter; egress through the device's Cloudflare client)."""

    def __init__(self, config: Any, slots: Sequence[tuple[int, str, int]], run: Any = run_command) -> None:
        self.config = config
        self.slots = slots
        self.run = run
        self.checked_at = -BOUNDARY_TTL
        self.ok = False
        self.internet = False

    async def __call__(self) -> bool:
        if self.config.workvm_slots_self:
            self.internet = bool(self.config.test_mode)
            return self.internet
        if time.monotonic() - self.checked_at < BOUNDARY_TTL:
            return self.ok
        status = await asyncio.to_thread(self._check)
        self.ok, self.internet = status is not None and not status.problems, status is not None and status.internet
        self.checked_at = time.monotonic()
        return self.ok

    def _check(self) -> firewall.Status | None:
        slots: list[firewall.Slot] = []
        for index, account, port in self.slots:
            uid = self.run(["/usr/bin/id", "-u", account], 10.0)
            if uid.returncode != 0 or not uid.stdout.strip().isdigit():
                return None
            slots.append(firewall.Slot(index=index, account=account, uid=int(uid.stdout.strip()), ssh_port=port))
        try:
            return firewall.check(firewall.sudo_run(self.run), slots, BANDWIDTH_MBIT)
        except RunnerError:  # a slot UID or port the anchor refuses: fail closed, never raise
            return None


def image_disk(image: str, images: Path | None = None) -> Path:
    return (IMAGES_DIR if images is None else images) / f"{image}.img"


def base_template(template: str, disk: Path) -> str:
    """The work template with the image location replaced by the exported disk of the image (no digest)."""
    lines, skip = [], False
    for line in template.splitlines():
        if line.startswith("images:"):
            lines += ["images:", f'- location: "{disk}"', "  arch: aarch64"]
            skip = True
            continue
        if skip and (line.startswith("-") or line.startswith("  ")):
            continue
        skip = False
        lines.append(line)
    return "\n".join(lines) + "\n"


class Bases:
    """Clone bases per slot (see the module docstring).

    `images` is the directory of the exported disks (`IMAGES_DIR`); None (self mode) clones the image instance itself.
    A missing exported disk ends the start with `workvm_image_missing` and the line
    `runner.workvm.base.failed slot=<n> path=<disk>` in `log`, before Lima is asked.
    """

    def __init__(
        self,
        command: SlotCommand,
        prefix: str,
        template: Path,
        images: Path | None = IMAGES_DIR,
        log: Callable[[str], None] = lambda _text: None,
    ) -> None:
        self.command = command
        self.prefix = prefix
        self.template = template
        self.images = images
        self.log = log

    async def __call__(self, slot: int, image: str) -> str:
        if self.images is None:
            return image
        lima = Lima(self.command.sync_runner(slot), prefix=self.prefix)
        name = lima.name("wbase-" + hashlib.sha256(image.encode()).hexdigest()[:8])
        if await asyncio.to_thread(lima.get, name) is None:
            disk = image_disk(image, self.images)
            if not disk.is_file():
                self.log(f"runner.workvm.base.failed slot={slot} path={disk}")
                raise WorkVmError("workvm_image_missing", image)
            path = self.images / f"{image}.yaml"
            if not path.exists():
                path.write_text(base_template(self.template.read_text(encoding="utf-8"), disk))
                path.chmod(0o644)
            await asyncio.to_thread(lima.create, name, str(path))
        return name


def _logger(context: Any) -> Any:
    def line(text: str) -> None:
        context.log.write(text + "\n")
        context.log.flush()

    return line


def default_template() -> Path:
    return Path(__file__).resolve().parents[3] / "images" / "workvm.yaml"


def workvms_for(context: Any) -> WorkVms | None:
    """The daemon's work VMs; None on a device without slots or before enrollment."""
    found = existing(context)
    if found is not None:
        return found
    config = context.config
    if config.workvm_slots == 0:
        return None
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points

    service = service_for(context)
    if service is None:
        return None
    slots = accounts.slot_accounts(config.vm_prefix, config.workvm_slots, config.workvm_ssh_port_base)
    command = SlotCommand(() if config.workvm_slots_self else tuple(account for _, account, _ in slots))
    db = service.env.db
    wall = service.env.wall

    async def free_memory_mib() -> int:
        held = ("pool", "assigning", "running", "stopping")
        workgroup = sum(memory for state, memory in await vm_records.slot_rows(db) if state in held)
        work = await records.memory_in_use(db)
        return int(physical_memory_mib()) - int(config.memory_reserve_mib) - workgroup - work

    def shell_for(vm: WorkVm) -> VmShell:
        return VmShell(command, vm.slot, vm.lima_name)

    settings = Settings(
        slots=config.workvm_slots,
        max_size=config.workvm_max_size,
        idle_seconds=config.workvm_idle_seconds,
        queue_seconds=config.workvm_queue_seconds,
        max_per_task=config.workvm_max_per_task,
        max_per_org=config.workvm_max_per_org,
        max_per_owner=config.workvm_max_per_owner,
        ssh_port_base=config.workvm_ssh_port_base,
        test_mode=config.test_mode,
        pool_size=config.workvm_pool_size,
        disk_quota_gib=config.workvm_disk_quota_gib,
    )

    async def emit(identity: Identity, kind: str, body: Mapping[str, Any]) -> None:
        local = LocalSession(
            id=identity.session_id,
            task_id=identity.task_id,
            org_id=identity.org_id,
            wg_id=identity.wg_id,
            vm_id=None,
            parent_id=None,
            depth=0,
            state="working",
            claude_session_id=None,
            model_access=None,
            model_id=None,
            owner_id=None,
            created_at=datetime.fromtimestamp(0, UTC),
            ready_at=None,
            ended_at=None,
        )
        async with service.env.transaction() as conn:
            await session_event(conn, service.env.outbox, local, kind=kind, body=body, ts=service.env.at())

    deps = Deps(
        db=db,
        settings=settings,
        now=lambda: datetime.fromtimestamp(wall(), UTC),
        lima_for=lambda slot: Lima(command.sync_runner(slot), prefix=config.vm_prefix),
        shell_for=shell_for,
        base_for=Bases(
            command,
            config.vm_prefix,
            config.workvm_template or default_template(),
            None if config.workvm_slots_self else IMAGES_DIR,
            _logger(context),
        ),
        boundary_ok=Boundary(config, slots),
        free_memory_mib=free_memory_mib,
        log=_logger(context),
        emit=emit,
        sleep=context.host.sleep,
        scratch=config.home / "tmp",
        exhausted=lambda org_id: (
            service.env.device_kind == "platform" and billing_state.exhausted(service.env.desired, org_id)
        ),
        capture=Capturer(context, shell_for),
        hub_lease=lambda: lease.held(runtime_client(context), service.env.desired),
    )
    return install(context, WorkVms(deps))


def runtime_client(context: Any) -> HubClient | None:
    """The daemon's hub client (None while the device is not enrolled)."""
    from agent_runner.sync.runtime import runtime  # noqa: PLC0415 - the sync runtime imports every area's hooks

    found = runtime(context)
    return None if found is None else found.client

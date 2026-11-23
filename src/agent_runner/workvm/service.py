"""Production wiring of the work VMs (docs/platform/spec/13-work-vm.md sections 6 and 7).

`workvms_for(context)` builds the daemon's `WorkVms` once (None on a device without work VM slots or before
enrollment). Slots are the accounts `<prefix>wvm00 …` (`accounts.slot_accounts`), or the runner's own account in
`TEST_MODE` with `WORKVM_SLOTS_SELF`. The boundary check (`boundary_ok`) renders the pf anchor for the slot accounts
and compares it with the live filter (`firewall.check`, read through `sudo -n` with the exact read commands sudoers
allows), cached for `BOUNDARY_TTL` seconds. The boundary holds whether or not the device's Cloudflare client is up;
without it the work VMs have no internet (`internet` false). Self mode needs no filter (refused outside `TEST_MODE`
by the configuration).

Work images: in self mode the stopped image instance in the runner's Lima home is the clone base of every slot. With
slot accounts each slot keeps its own base `<prefix>wbase-<image hash>` in its Lima home, created from the image's
exported disk in `IMAGES_DIR` (readable by the slot accounts) on first use.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.sessions.events import session_event
from agent_runner.sessions.records import LocalSession
from agent_runner.vm import records as vm_records
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lima import Lima
from agent_runner.workvm import accounts, firewall, records
from agent_runner.workvm.manager import Deps, Identity, Settings, WorkVms
from agent_runner.workvm.records import WorkVm
from agent_runner.workvm.shell import SlotCommand, VmShell

ATTRIBUTE = "_workvms"
BOUNDARY_TTL = 60.0
IMAGES_DIR = Path("/var/agent-workvm/images")
BANDWIDTH_MBIT = 500


def existing(context: Any) -> WorkVms | None:
    found = getattr(context, ATTRIBUTE, None)
    return found if isinstance(found, WorkVms) else None


def install(context: Any, workvms: WorkVms) -> WorkVms:
    setattr(context, ATTRIBUTE, workvms)
    return workvms


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


def image_disk(image: str) -> Path:
    return IMAGES_DIR / f"{image}.img"


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
    """Clone bases per slot (see the module docstring)."""

    def __init__(self, command: SlotCommand, prefix: str, template: Path) -> None:
        self.command = command
        self.prefix = prefix
        self.template = template

    async def __call__(self, slot: int, image: str) -> str:
        if not self.command.accounts:
            return image
        lima = Lima(self.command.sync_runner(slot), prefix=self.prefix)
        name = lima.name("wbase-" + hashlib.sha256(image.encode()).hexdigest()[:8])
        if await asyncio.to_thread(lima.get, name) is None:
            path = IMAGES_DIR / f"{image}.yaml"
            if not path.exists():
                path.write_text(base_template(self.template.read_text(encoding="utf-8"), image_disk(image)))
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
        base_for=Bases(command, config.vm_prefix, config.workvm_template or default_template()),
        boundary_ok=Boundary(config, slots),
        free_memory_mib=free_memory_mib,
        log=_logger(context),
        emit=emit,
        sleep=context.host.sleep,
        scratch=config.home / "tmp",
    )
    return install(context, WorkVms(deps))

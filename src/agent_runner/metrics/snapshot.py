"""One sample of the device, its VMs and its sessions (02-data-model.md section 2.8 entities), the input of both
sinks (`textfile`, `outbox`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from agent_runner.metrics.device import DeviceValues
from agent_runner.metrics.session import SessionEntry
from agent_runner.metrics.workvm import WorkvmEntry
from agent_runner.vm.capacity import Capacity

# VM states (02-data-model.md section 2.4 without `deleted`) and the ones in which the VM must answer its guest probe.
VM_STATES = ("pool", "assigning", "running", "stopping", "stopped")
PROBED_STATES = frozenset({"pool", "assigning", "running"})
SESSION_STATES = (
    "requested",
    "vm_assigning",
    "vm_ready",
    "sandbox_ready",
    "agent_starting",
    "prompt_delivering",
    "working",
    "idle",
    "suspended",
    "resuming",
    "stopping",
    "stopped",
    "failed",
)


@dataclass(frozen=True)
class DeviceEntry:
    values: DeviceValues
    capacity: Capacity
    hub_connected: bool
    sync_fatal: bool
    backlog: int
    counters: Mapping[str, int]
    held: int = 0
    """Items in the scope queues of the outbox (KOR-08), outside the backlog (KOR-10, AUD-02 NB-09)."""
    held_scopes: int = 0
    """Scopes whose queue waits (for a key of the needed epoch or a verified log head)."""


@dataclass(frozen=True)
class VmEntry:
    vm_id: str
    wg_id: str | None
    state: str
    probe_ok: bool | None  # None: not expected to run (stopping, stopped), no probe series
    cpu_ratio: float | None
    memory_bytes: int | None
    memory_limit_bytes: int
    disk_used_bytes: int | None
    disk_limit_bytes: int | None
    uptime_seconds: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None
    sessions: Mapping[str, int]


@dataclass(frozen=True)
class Snapshot:
    wall: float
    at: datetime
    device: DeviceEntry
    vms: tuple[VmEntry, ...]
    sessions: tuple[SessionEntry, ...]
    workvms: tuple[WorkvmEntry, ...] = ()
    """The work VMs of the device (KOR-29, `agent_runner.metrics.workvm`)."""

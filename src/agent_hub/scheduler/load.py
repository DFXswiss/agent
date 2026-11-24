"""The scheduler's inputs from the database (03-api.md section 4.6.2, ADR-060).

Candidates are the devices assigned to the workgroup (`wg_device` without `unassigned_seq`: explicit assignments and
assignments through the organization) that are active members of its org and whose `device.state` is `active`; a
device is online while its `device_presence.last_heartbeat_at` is less than `AGENT_HUB_DEVICE_ONLINE_SECONDS` before
the hub clock (the presence rule of HUB-07). The workgroup's VMs on a device come from `hub.vm` (rows reported by the
runner; every state but `deleted`: `running`, `stopped`, any other counts as `other`), each with its occupying
sessions (`hub.session` with that `vm_id` in a state of `OCCUPYING`); the sessions of the workgroup placed on the
device without a VM yet are its *pending* sessions. A device assigned only through the organization (`org_device`, no
explicit assignment batch delivered its keys) needs the wraps of the workgroup's and the org's current keys for itself
(`key_wrap`) and, for a task, of its owner's current credential key (`with_owner`), else it counts with `keys`
False. The workgroup's allowance of new VMs is `AGENT_HUB_VMS_PER_WG` minus its live VMs on all devices.

Refusals for lack of capacity (KOR-14, ADR-049; a session the device ended `failed` with `capacity_exhausted`):

- a device that refused any session at or after its last heartbeat counts with `vm_slots_free = 0` (and no ready pool
  VM) until it reports
  again (the stored capacity is older than the refusal; several starts in one heartbeat window, pool refill). A
  (re)connect writes the heartbeat time without a capacity report, so until the first heartbeat after it a refusal
  within the online window before the connect counts. A heartbeat measured before the refusal but received after it
  still clears the rule (the refusal limit bounds that race); a heartbeat with unchanged capacity emits no capacity
  signal, so a waiting attempt is then placed by the 15 s sweep;
- `Candidate.refused`: the newest session of the workgroup on the device was refused (no later session of the
  workgroup was placed there), so a stopped VM of the workgroup does not exempt it from needing a free slot.

A chosen device (`Pin`, ADR-060 section 6) restricts the candidates to it while it is one; a chosen VM that is no
longer a live VM there counts as not chosen. Without any candidate the decision is `no_device`, or
`platform_access_required` when the org has no access to the operator's devices and no device of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.scheduler import settings
from agent_hub.scheduler.core import (
    NO_DEVICE,
    OTHER,
    PLATFORM_ACCESS_REQUIRED,
    RUNNING,
    STOPPED,
    Candidate,
    Capacity,
    Decision,
    NoPlacement,
    Vm,
    choose,
)

Connection = psycopg.Connection[Any]
OCCUPYING = (
    "requested",
    "vm_assigning",
    "vm_ready",
    "sandbox_ready",
    "agent_starting",
    "prompt_delivering",
    "working",
    "idle",
    "resuming",
    "stopping",
)
"""Session states that take a place on a VM (the runner's `ACTIVE_SESSION_STATES`; not `suspended`)."""


@dataclass(frozen=True)
class Pin:
    """A chosen device and optionally one of its VMs."""

    device_id: str
    vm_id: str | None = None


@dataclass(frozen=True)
class Inputs:
    """The candidates, the workgroup's allowance of new VMs (None = no limit) and whether a chosen device counts."""

    candidates: list[Candidate]
    allowance: int | None
    pinned: bool = False


def online_after(now: datetime, online_seconds: int) -> datetime:
    """The oldest heartbeat time that still counts as online."""
    return now - timedelta(seconds=online_seconds)


def _state(state: str) -> str:
    return state if state in (RUNNING, STOPPED) else OTHER


def _capacity(stored: Any, *, stale: bool) -> Capacity:
    """The stored capacity; without free slots while a refusal is newer than the last heartbeat."""
    found = Capacity.of(stored)
    return replace(found, vm_slots_free=0, pool_ready=0) if stale else found


def _vms(connection: Connection, wg_id: UUID | str) -> dict[str, list[Vm]]:
    """The live VMs of the workgroup per device with their occupying sessions, ordered by VM ID."""
    found: dict[str, list[Vm]] = {}
    for device, vm_id, state, sessions in connection.execute(
        "SELECT v.device_id, v.id, v.state, (SELECT count(*) FROM hub.session s WHERE s.vm_id = v.id"
        " AND s.state = ANY(%s)) FROM hub.vm v WHERE v.wg_id = %s AND v.state <> 'deleted' ORDER BY v.id",
        (list(OCCUPYING), wg_id),
    ):
        found.setdefault(str(device), []).append(Vm(str(vm_id), _state(str(state)), int(sessions)))
    return found


def _rows(connection: Connection, wg_id: UUID | str, now: datetime, online_seconds: int) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT d.id, d.capacity, p.last_heartbeat_at > %s,"
        " EXISTS (SELECT 1 FROM hub.session r WHERE r.device_id = d.id AND r.state = 'failed'"
        " AND r.state_reason = 'capacity_exhausted' AND r.ended_at >= CASE WHEN p.last_heartbeat_at > p.connected_at"
        " THEN p.last_heartbeat_at ELSE p.connected_at - make_interval(secs => %s) END),"
        " COALESCE((SELECT r.state = 'failed' AND r.state_reason IS NOT DISTINCT FROM 'capacity_exhausted'"
        " FROM hub.session r WHERE r.device_id = d.id AND r.wg_id = a.wg_id"
        " ORDER BY r.created_at DESC, r.id DESC LIMIT 1), false),"
        " (SELECT count(*) FROM hub.session s WHERE s.device_id = d.id AND s.wg_id = a.wg_id AND s.vm_id IS NULL"
        " AND s.state = ANY(%s)),"
        " EXISTS (SELECT 1 FROM hub.org_device o WHERE o.org_id = w.org_id AND o.device_id = d.id"
        " AND o.unassigned_seq IS NULL),"
        " EXISTS (SELECT 1 FROM hub.key_wrap k WHERE k.scope = 'wg' AND k.scope_id = a.wg_id"
        " AND k.recipient_id = d.id AND k.epoch = (SELECT max(e.epoch) FROM hub.scope_epoch e WHERE e.scope = 'wg'"
        " AND e.scope_id = a.wg_id))"
        " AND EXISTS (SELECT 1 FROM hub.key_wrap k WHERE k.scope = 'org' AND k.scope_id = w.org_id"
        " AND k.recipient_id = d.id AND k.epoch = (SELECT max(e.epoch) FROM hub.scope_epoch e WHERE e.scope = 'org'"
        " AND e.scope_id = w.org_id))"
        " FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.device_id AND m.removed_seq IS NULL"
        " JOIN hub.device d ON d.id = a.device_id AND d.state = 'active'"
        " LEFT JOIN hub.device_presence p ON p.device_id = d.id"
        " WHERE a.wg_id = %s AND a.unassigned_seq IS NULL ORDER BY d.id",
        (online_after(now, online_seconds), online_seconds, list(OCCUPYING), wg_id),
    ).fetchall()


def candidates(connection: Connection, wg_id: UUID | str, now: datetime, online_seconds: int) -> list[Candidate]:
    """The active devices assigned to the workgroup with presence, capacity, VMs and pending sessions."""
    vms = _vms(connection, wg_id)
    return [
        Candidate(
            device_id=str(row[0]),
            online=bool(row[2]),
            capacity=_capacity(row[1], stale=bool(row[3])),
            vms=tuple(vms.get(str(row[0]), ())),
            pending=int(row[5]),
            refused=bool(row[4]),
            keys=not row[6] or bool(row[7]),
            org_only=bool(row[6]),
        )
        for row in _rows(connection, wg_id, now, online_seconds)
    ]


def with_owner(connection: Connection, found: list[Candidate], owner: UUID | str | None) -> list[Candidate]:
    """The candidates for a task of `owner`: a device assigned only through the organization also needs the wrap of
    the owner's current credential key (its subscription or API key), else it counts with `keys` False."""
    if owner is None:
        return found
    devices = [item.device_id for item in found if item.org_only and item.keys]
    if not devices:
        return found
    ready = {
        str(row[0])
        for row in connection.execute(
            "SELECT k.recipient_id FROM hub.key_wrap k WHERE k.scope = 'cred' AND k.scope_id = %s"
            " AND k.recipient_id = ANY(%s::uuid[]) AND k.epoch = (SELECT max(c.epoch) FROM hub.key_wrap c"
            " WHERE c.scope = 'cred' AND c.scope_id = %s)",
            (owner, devices, owner),
        )
    }
    return [
        replace(item, keys=False) if item.org_only and item.keys and item.device_id not in ready else item
        for item in found
    ]


def allowance(connection: Connection, wg_id: UUID | str) -> int | None:
    """`AGENT_HUB_VMS_PER_WG` minus the workgroup's live VMs on all devices (None without a limit)."""
    limit = settings.vms_per_wg()
    if limit is None:
        return None
    row = connection.execute("SELECT count(*) FROM hub.vm WHERE wg_id = %s AND state <> 'deleted'", (wg_id,)).fetchone()
    return limit - int(row[0]) if row is not None else limit


def pinned(found: list[Candidate], pin: Pin | None) -> tuple[list[Candidate], bool]:
    """The candidates restricted to a chosen device while it is one (a chosen VM only while it is live there)."""
    if pin is None:
        return found, False
    for item in found:
        if item.device_id == pin.device_id:
            vm = pin.vm_id if any(vm.vm_id == pin.vm_id for vm in item.vms) else None
            return [replace(item, pinned_vm=vm)], True
    return found, False


def inputs(
    connection: Connection,
    wg_id: UUID | str,
    now: datetime,
    online_seconds: int,
    *,
    pin: Pin | None = None,
    owner: UUID | str | None = None,
) -> Inputs:
    """The scheduler's inputs for a new session of the workgroup (module docstring)."""
    found = with_owner(connection, candidates(connection, wg_id, now, online_seconds), owner)
    found, is_pinned = pinned(found, pin)
    return Inputs(found, allowance(connection, wg_id), is_pinned)


def without_device(connection: Connection, wg_id: UUID | str) -> NoPlacement:
    """The reason of a workgroup without an active assigned device: `platform_access_required` when its org has no
    access to the operator's devices and no device of its own, else `no_device`."""
    row = connection.execute(
        "SELECT o.platform_access, EXISTS (SELECT 1 FROM hub.org_member m JOIN hub.device d ON d.id = m.principal_id"
        " WHERE m.org_id = o.id AND m.kind = 'device' AND m.removed_seq IS NULL AND d.kind = 'org'"
        " AND d.state = 'active') FROM hub.wg w JOIN hub.org o ON o.id = w.org_id WHERE w.id = %s",
        (wg_id,),
    ).fetchone()
    if row is not None and not row[0] and not row[1]:
        return NoPlacement(PLATFORM_ACCESS_REQUIRED)
    return NoPlacement(NO_DEVICE)


def task_pin(connection: Connection, task_id: UUID | str) -> Pin | None:
    """The chosen device and VM of a task."""
    row = connection.execute(
        "SELECT placement_device_id, placement_vm_id FROM hub.task WHERE id = %s", (task_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return Pin(str(row[0]), None if row[1] is None else str(row[1]))


def decide(
    connection: Connection,
    wg_id: UUID | str,
    now: datetime,
    online_seconds: int,
    *,
    pin: Pin | None = None,
    owner: UUID | str | None = None,
) -> Decision:
    """The scheduler's decision for a new session of the workgroup (module docstring)."""
    found = inputs(connection, wg_id, now, online_seconds, pin=pin, owner=owner)
    if not found.candidates:
        return without_device(connection, wg_id)
    return choose(found.candidates, found.allowance, pinned=found.pinned)

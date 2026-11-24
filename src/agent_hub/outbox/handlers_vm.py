"""Outbox item `vm_state` (03-api.md section 6.1, 02-data-model.md section 2.4): the `vm` row as the runner reports it.

A VM belongs to the device that reported it first; a report for a VM of another device is `403 forbidden`. A VM with
a workgroup needs `device:wg` (the device is assigned to that workgroup in its verified view), a pool VM
(`wg_id: null`) only the device itself. `created_at` is the hub time of the first report, `state_at` the device time
of the reported state; the row is kept for `deleted` VMs (the scheduler and the monitoring ignore them).

`session.vm_id`: a device with the feature `vm.choice` (ADR-075) names the VM in its `session_transition` items.
A device without it runs one VM per workgroup (05-runner.md section 3) and names none, so a VM reported as
`assigning`/`running` for a workgroup becomes the VM of the open sessions (`ended_at` NULL) of that device and
workgroup; `link_session_vm` covers the opposite order.
"""

from __future__ import annotations

from typing import Any

from agent_hub.device.features import VM_CHOICE, outdated_devices
from agent_hub.errors import HubError
from agent_hub.outbox.base import ItemContext, moment
from agent_hub.outbox.bodies import VmStateBody

ACTIVE_STATES = ("assigning", "running")


def chooses_vms(connection: Any, device: object) -> bool:
    """The device announced the feature `vm.choice` (it names the VM of each session itself)."""
    return not outdated_devices(connection, [str(device)], VM_CHOICE)


def handle(item: ItemContext, body: VmStateBody) -> None:
    """Insert or update the VM row of the device."""
    if body.wg_id is not None and not item.view().reads_wg(body.wg_id):
        raise HubError("forbidden")
    at = moment(body.at)
    row = item.connection.execute(
        "INSERT INTO hub.vm (id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib, created_at,"
        " state_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET"
        " wg_id = EXCLUDED.wg_id, state = EXCLUDED.state, golden_version = EXCLUDED.golden_version,"
        " cpus = EXCLUDED.cpus, memory_mib = EXCLUDED.memory_mib, disk_gib = EXCLUDED.disk_gib,"
        " state_at = EXCLUDED.state_at WHERE hub.vm.device_id = EXCLUDED.device_id RETURNING id",
        (
            body.vm_id,
            item.device,
            body.wg_id,
            body.state,
            body.golden_version,
            body.cpus,
            body.memory_mib,
            body.disk_gib,
            item.now,
            at,
        ),
    ).fetchone()
    if row is None:
        raise HubError("forbidden")
    if body.wg_id is not None and body.state in ACTIVE_STATES and not chooses_vms(item.connection, item.device):
        item.connection.execute(
            "UPDATE hub.session SET vm_id = %s WHERE device_id = %s AND wg_id = %s AND ended_at IS NULL"
            " AND vm_id IS DISTINCT FROM %s",
            (body.vm_id, item.device, body.wg_id, body.vm_id),
        )


def link_session_vm(item: ItemContext, session_id: object) -> None:
    """Give an open session without a VM the active VM of its device and workgroup (module docstring)."""
    item.connection.execute(
        "UPDATE hub.session s SET vm_id = (SELECT v.id FROM hub.vm v WHERE v.device_id = s.device_id"
        " AND v.wg_id = s.wg_id AND v.state = ANY(%s) ORDER BY v.state_at DESC LIMIT 1)"
        " WHERE s.id = %s AND s.vm_id IS NULL AND s.ended_at IS NULL",
        (list(ACTIVE_STATES), session_id),
    )

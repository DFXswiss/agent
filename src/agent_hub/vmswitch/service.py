"""The switch of a workgroup VM to the newest golden (ADR-093 sections 3 and 5; 03-api.md section 4.5).

- `request_switch(connection, user, wg, vm, now)`: `POST /v1/workgroups/{wg}/vms/{vm}/switch`. The caller needs
  `wg:read` and `org:admin` of the workgroup's organization (the member with administration rights; `404 not_found`
  without `wg:read`, `403 forbidden` without `org:admin`). The VM must be a live VM of the workgroup (`404 not_found`
  otherwise). When its device last reported it outdated (`hub.vm.upgrade`), the request is stored
  (`switch_requested_at`, kept when already set; `switch_requested_by`), audited (`vm.switch_requested {vm_id,
  device_id}`) and the device is woken (hint `reconcile`, after the commit); a current VM (`upgrade` NULL) stores
  nothing. The answer is the preview: the VM's status as reported, `requested` and `requested_at`.
- `requested(connection, device)`: the VMs of the device with a request that are still live and outdated, oldest
  request first (the desired state's `vm_switch`).
- `apply_report(connection, body)`: what a `vm_state` report adds (`outbox.py`): `upgrade` when present (NULL ends a
  request), `replaced_by` with the workgroup default (`wg_placement.vm_id`) and task choices (`task.placement_vm_id`)
  that named the replaced VM moved to its successor, so a member's choice survives the switch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.orgs import rights
from agent_hub.outbox.bodies import VmStateBody

Connection = psycopg.Connection[Any]
REQUESTED_MAX = 1000


def request_switch(
    connection: Connection, user: UUID, wg: UUID, vm: str, now: datetime
) -> tuple[dict[str, Any], UUID | None]:
    """The preview and the device to wake (None: nothing was requested); see the module docstring."""
    rights.require(connection, user, rights.WG_READ, wg_id=wg)
    org = connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg,)).fetchone()
    assert org is not None
    rights.require(connection, user, rights.ORG_ADMIN, org_id=org[0], write=True)
    row = connection.execute(
        "SELECT device_id, golden_version, upgrade, switch_requested_at FROM hub.vm WHERE id = %s AND wg_id = %s"
        " AND state <> 'deleted' FOR UPDATE",
        (vm, wg),
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    device, golden, upgrade, requested_at = row
    wake = None
    if upgrade is not None:
        requested_at = requested_at or now
        connection.execute(
            "UPDATE hub.vm SET switch_requested_at = %s, switch_requested_by = %s WHERE id = %s",
            (requested_at, user, vm),
        )
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user,
                action="vm.switch_requested",
                result="ok",
                org_id=org[0],
                wg_id=wg,
                device_id=device,
                target_kind="wg",
                target_id=str(wg),
                detail={"vm_id": vm, "device_id": str(device)},
            ),
        )
        wake = device
    answer = {
        "vm_id": vm,
        "device_id": str(device),
        "golden_version": golden,
        "requested": upgrade is not None,
        "requested_at": None if upgrade is None or requested_at is None else format_time(requested_at),
        "upgrade": upgrade,
    }
    return answer, wake


def requested(connection: Connection, device: UUID) -> list[str]:
    """The desired state's `vm_switch` for `device` (module docstring)."""
    return [
        str(found[0])
        for found in connection.execute(
            "SELECT id FROM hub.vm WHERE device_id = %s AND switch_requested_at IS NOT NULL AND upgrade IS NOT NULL"
            " AND state <> 'deleted' ORDER BY switch_requested_at, id LIMIT %s",
            (device, REQUESTED_MAX),
        )
    ]


def apply_report(connection: Connection, body: VmStateBody) -> None:
    """Store what a `vm_state` report adds (module docstring); the base handler stored the row."""
    reported = body.model_fields_set
    if "upgrade" in reported:
        status = None if body.upgrade is None else Jsonb(body.upgrade.model_dump(mode="json", exclude_unset=True))
        connection.execute(
            "UPDATE hub.vm SET upgrade = %s, switch_requested_at = CASE WHEN %s THEN switch_requested_at END,"
            " switch_requested_by = CASE WHEN %s THEN switch_requested_by END WHERE id = %s",
            (status, status is not None, status is not None, body.vm_id),
        )
    if body.replaced_by is not None:
        old, new = body.vm_id, body.replaced_by
        connection.execute(
            "UPDATE hub.vm SET replaced_by = %s, upgrade = NULL, switch_requested_at = NULL,"
            " switch_requested_by = NULL WHERE id = %s",
            (new, old),
        )
        connection.execute("UPDATE hub.wg_placement SET vm_id = %s WHERE vm_id = %s", (new, old))
        connection.execute("UPDATE hub.task SET placement_vm_id = %s WHERE placement_vm_id = %s", (new, old))

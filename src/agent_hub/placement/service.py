"""Choosing device and VM (03-api.md sections 4.5, 4.6; ADR-060 section 6).

- `check(connection, wg, device, vm)`: a chosen device must be a candidate of the workgroup (assigned to it directly or
  through the organization, an active member of the org, `device.state = active`): else `403 placement_forbidden`;
  nobody chooses a device that is not assigned to the workgroup. A chosen VM must be a live VM (`hub.vm`, state not
  `deleted`) of that workgroup on that device: else `409 placement_invalid`.
- `for_new_task(connection, wg, requested)`: the choice of `POST /v1/tasks` (checked), else the workgroup default while
  it still holds (a default whose device is no longer a candidate is ignored, one whose VM is gone counts as the device
  alone), else None (automatic).
- `set_default` / `default` (`GET`/`PUT /v1/workgroups/{wg}/placement`, audit `wg.placement`) and `set_task`
  (`PUT /v1/tasks/{task}/placement`, audit `task.placement`). A changed choice re-runs the scheduler for the
  workgroup's waiting sessions after the commit (`agent_hub.scheduler.signals`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.errors import HubError
from agent_hub.scheduler.load import Pin

Connection = psycopg.Connection[Any]


def is_candidate(connection: Connection, wg_id: UUID | str, device_id: UUID | str) -> bool:
    """The device is a candidate of the workgroup (module docstring)."""
    row = connection.execute(
        "SELECT 1 FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.device_id AND m.removed_seq IS NULL"
        " JOIN hub.device d ON d.id = a.device_id AND d.state = 'active'"
        " WHERE a.wg_id = %s AND a.device_id = %s AND a.unassigned_seq IS NULL",
        (wg_id, device_id),
    ).fetchone()
    return row is not None


def is_live_vm(connection: Connection, wg_id: UUID | str, device_id: UUID | str, vm_id: str) -> bool:
    """`vm_id` is a live VM of the workgroup on the device."""
    row = connection.execute(
        "SELECT 1 FROM hub.vm WHERE id = %s AND device_id = %s AND wg_id = %s AND state <> 'deleted'",
        (vm_id, device_id, wg_id),
    ).fetchone()
    return row is not None


def check(connection: Connection, wg_id: UUID, device_id: UUID, vm_id: str | None, pointer: str) -> Pin:
    """A choice that holds (module docstring); `pointer` names the object in the request."""
    if not is_candidate(connection, wg_id, device_id):
        raise HubError("placement_forbidden", detail={"pointers": [f"{pointer}/device_id"]})
    if vm_id is not None and not is_live_vm(connection, wg_id, device_id, vm_id):
        raise HubError("placement_invalid", detail={"pointers": [f"{pointer}/vm_id"]})
    return Pin(str(device_id), vm_id)


def default(connection: Connection, wg_id: UUID) -> Pin | None:
    """The stored default of the workgroup (as stored, unchecked)."""
    row = connection.execute("SELECT device_id, vm_id FROM hub.wg_placement WHERE wg_id = %s", (wg_id,)).fetchone()
    return None if row is None else Pin(str(row[0]), row[1])


def for_new_task(connection: Connection, wg_id: UUID, requested: tuple[UUID, str | None] | None) -> Pin | None:
    """The choice of a new task (module docstring)."""
    if requested is not None:
        return check(connection, wg_id, requested[0], requested[1], "/placement")
    found = default(connection, wg_id)
    if found is None or not is_candidate(connection, wg_id, found.device_id):
        return None
    if found.vm_id is not None and not is_live_vm(connection, wg_id, found.device_id, found.vm_id):
        return Pin(found.device_id)
    return found


def json(pin: Pin | None) -> dict[str, Any] | None:
    """The `placement` object of the API."""
    return None if pin is None else {"device_id": pin.device_id, "vm_id": pin.vm_id}


def _audit(
    connection: Connection,
    action: str,
    user: UUID,
    now: datetime,
    *,
    org_id: UUID,
    wg_id: UUID,
    pin: Pin | None,
    task_id: UUID | None = None,
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            org_id=org_id,
            wg_id=wg_id,
            task_id=task_id,
            device_id=None if pin is None else UUID(pin.device_id),
            target_kind="task" if task_id is not None else "wg",
            target_id=str(task_id if task_id is not None else wg_id),
            detail={"device_id": None if pin is None else pin.device_id, "vm_id": None if pin is None else pin.vm_id},
        ),
    )


def set_default(
    connection: Connection, user: UUID, wg_id: UUID, *, device: UUID | None, vm: str | None, now: datetime
) -> Pin | None:
    """Store the workgroup default (None device = automatic) and audit it."""
    if device is None:
        if vm is not None:
            raise HubError("request_invalid", detail={"pointers": ["/vm_id"]})
        connection.execute("DELETE FROM hub.wg_placement WHERE wg_id = %s", (wg_id,))
        pin = None
    else:
        pin = check(connection, wg_id, device, vm, "")
        connection.execute(
            "INSERT INTO hub.wg_placement (wg_id, device_id, vm_id, set_by, set_at) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (wg_id) DO UPDATE SET device_id = EXCLUDED.device_id, vm_id = EXCLUDED.vm_id,"
            " set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at",
            (wg_id, device, vm, user, now),
        )
    _audit(connection, "wg.placement", user, now, org_id=org_of(connection, wg_id), wg_id=wg_id, pin=pin)
    return pin


def set_task(
    connection: Connection,
    user: UUID,
    task: tuple[UUID, UUID, UUID],
    *,
    device: UUID | None,
    vm: str | None,
    now: datetime,
) -> Pin | None:
    """Store the choice of a task (`task` = id, org, workgroup; the caller holds the task row) and audit it."""
    task_id, org_id, wg_id = task
    if device is None and vm is not None:
        raise HubError("request_invalid", detail={"pointers": ["/vm_id"]})
    pin = None if device is None else check(connection, wg_id, device, vm, "")
    connection.execute(
        "UPDATE hub.task SET placement_device_id = %s, placement_vm_id = %s WHERE id = %s",
        (device, None if pin is None else pin.vm_id, task_id),
    )
    _audit(connection, "task.placement", user, now, org_id=org_id, wg_id=wg_id, pin=pin, task_id=task_id)
    return pin


def store_for_new_task(connection: Connection, task_id: UUID, pin: Pin | None) -> None:
    """Store the choice of a just created task."""
    if pin is not None:
        connection.execute(
            "UPDATE hub.task SET placement_device_id = %s, placement_vm_id = %s WHERE id = %s",
            (pin.device_id, pin.vm_id, task_id),
        )


def org_of(connection: Connection, wg_id: UUID) -> UUID:
    """The org of an existing workgroup."""
    return cast(
        UUID,
        cast(tuple[Any, ...], connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg_id,)).fetchone())[0],
    )

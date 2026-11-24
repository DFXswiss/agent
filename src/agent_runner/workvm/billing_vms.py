"""Work VMs and run VMs in the VM runtime meter (`<area>/billing_vms.py:VMS` of `agent_runner.billing.meter`, KOR-31;
KOR-43, ADR-080 section 4; concept idea 46).

Every `running` work VM of this device is billed to the second as `vm_kind` `work` with its size (`standard`, `large`,
`xlarge` of the price list). Who bears the interval:

- a **task VM** is shared by the task's sessions on this device in a holding state (`meter.HOLDING`: never
  `suspended`), split evenly like a workgroup VM; when none holds it, the session that requested it bears it;
- a **session VM** and a **run VM** (one test run) belong to their session.

A stopped VM (paused session) is not running and costs nothing. The meter runs only on platform devices.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.billing.meter import HOLDING, MeteredVm

VM_KIND = "work"


async def work_vms(_context: Any, conn: AsyncConnection[Any]) -> list[MeteredVm]:
    """The running work and run VMs with the sessions that bear them (module docstring)."""
    cursor = await conn.execute(
        "SELECT w.id, w.size, w.scope, COALESCE(w.session_id, w.requested_by)::text,"
        " array_remove(array_agg(s.id::text ORDER BY s.id), NULL) FROM runner.workvm w"
        " LEFT JOIN runner.session s ON w.scope = 'task' AND s.task_id = w.task_id AND s.state = ANY(%s)"
        " WHERE w.state = 'running' GROUP BY w.id ORDER BY w.id",
        (sorted(HOLDING),),
    )
    found: list[MeteredVm] = []
    for vm_id, size, scope, own, holders in await cursor.fetchall():
        sessions = tuple(holders) if scope == "task" and holders else (str(own),)
        found.append(MeteredVm(str(vm_id), VM_KIND, str(size), sessions))
    return found


VMS = work_vms

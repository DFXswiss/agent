"""Work VMs and run VMs in the VM runtime meter (`<area>/billing_vms.py:VMS` of `agent_runner.billing.meter`, KOR-31;
KOR-43, ADR-080 section 4; concept idea 46).

Every `running` work VM of this device is billed to the second as `vm_kind` `work` with its size (`standard`, `large`,
`xlarge` of the price list). Who bears the interval:

- a **task VM** is shared by the task's sessions on this device in a holding state (`meter.HOLDING`: never
  `suspended`), split evenly like a workgroup VM; when none holds it, the session that requested it bears it;
- a **session VM**, a **run VM** (one test run) and a **browser VM** (a platform browser session, KOR-44) belong to
  their session;
- a **CI VM** (one GitHub Actions job, spec 16 section 17) is billed as `vm_kind` `ci` to the session its row names:
  the session that created the job's pull request, or the main session of the repository's fixed task - so the hub
  books it to that task and its workgroup (job -> pull request -> task -> workgroup, decision D-13).

A stopped VM (paused session) is not running and costs nothing. The meter runs only on platform devices.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.billing.meter import HOLDING, MeteredVm

VM_KIND = "work"
CI_KIND = "ci"
CI_SCOPE = "ci"


async def work_vms(_context: Any, conn: AsyncConnection[Any]) -> list[MeteredVm]:
    """The running work, run and browser VMs with the sessions that bear them (module docstring)."""
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
        found.append(MeteredVm(str(vm_id), CI_KIND if scope == CI_SCOPE else VM_KIND, str(size), sessions))
    return found


VMS = work_vms

"""U-RUN-workvm billing: work and run VMs in the VM runtime meter (`workvm/billing_vms.py:VMS`, KOR-43, ADR-078
section 4; KOR-31 meter): only running VMs, size and `vm_kind` `work`; a task VM is shared by the task's holding
sessions or borne by its requester, a session VM and a run VM by their session."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import psycopg

from agent_runner.billing import meter
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.workvm import billing_vms

TASK = "0193000a-0000-7000-8000-0000000000a1"
ORG = "0193000a-0000-7000-8000-0000000000a4"
WG = "0193000a-0000-7000-8000-0000000000a5"
S1 = "0193000a-0000-7000-8000-0000000000b1"
S2 = "0193000a-0000-7000-8000-0000000000b2"
S3 = "0193000a-0000-7000-8000-0000000000b3"
AT = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def vm(
    conn: psycopg.Connection, vm_id: str, slot: int, *, scope: str, state: str, session: str | None, by: str
) -> None:
    conn.execute(
        "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, session_id, requested_by, org_id, wg_id,"
        " size, cpus, memory_mib, disk_gib, image, state, created_at, last_used_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 4, 8192, 50, 'img', %s, %s, %s)",
        (
            vm_id,
            "n-" + vm_id,
            slot,
            scope,
            TASK,
            session,
            by,
            ORG,
            WG,
            "large" if scope == "run" else "standard",
            state,
            AT,
            AT,
        ),
    )


def session(conn: psycopg.Connection, session_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (session_id, TASK, ORG, WG, state, AT),
    )


def test_the_extension_point_is_discovered() -> None:
    assert billing_vms.work_vms in meter.sources()


def test_running_work_and_run_vms_with_who_bears_them(runner_dsn: str) -> None:
    assert migrate(runner_dsn).status == "applied"
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        session(conn, S1, "working")
        session(conn, S2, "idle")
        session(conn, S3, "suspended")
        vm(conn, "wvm-0000000a", 0, scope="task", state="running", session=None, by=S3)
        vm(conn, "wvm-0000000b", 1, scope="session", state="running", session=S2, by=S2)
        vm(conn, "wvm-0000000c", 2, scope="run", state="running", session=S3, by=S3)
        vm(conn, "wvm-0000000d", 3, scope="session", state="stopped", session=S1, by=S1)

    async def run() -> list[meter.MeteredVm]:
        db = await open_pool(runner_dsn)
        try:
            async with db.connection() as conn:
                found = await billing_vms.VMS(None, conn)
                await conn.execute("UPDATE runner.session SET state = 'suspended'")
                found += await billing_vms.VMS(None, conn)
                return found
        finally:
            await db.close()

    shared, own, run_vm, alone, own_again, run_again = asyncio.run(run())
    assert shared == meter.MeteredVm("wvm-0000000a", "work", "standard", (S1, S2))
    assert own == meter.MeteredVm("wvm-0000000b", "work", "standard", (S2,))
    assert run_vm == meter.MeteredVm("wvm-0000000c", "work", "large", (S3,))
    assert alone == meter.MeteredVm("wvm-0000000a", "work", "standard", (S3,))  # nobody holds it: its requester
    assert (own_again, run_again) == (own, run_vm)

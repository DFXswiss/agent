"""Work VM rows of the runner store (docs/platform/spec/13-work-vm.md section 8, migration 0012).

`runner.workvm` holds one row per work VM the runner created (scope `task`, `session`, or `run` for the fresh VM of one
test run, migration 0020); the partial unique indexes make "one live task VM per
task", "one live session VM per session" and "one live VM per slot" database rules. `runner.workvm_mirror` keeps the
SHA-256 manifest of the workspace files last mirrored into a VM (per VM, session and root directory), so the next
mirror sends only changed files. `runner.workvm_image` lists the work images (`ready`: the newest one is cloned).

All queries take the daemon's `psycopg_pool.AsyncConnectionPool` (`db`). Column names in `update` are keyword names
of the code, never input.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_runner.errors import RunnerError

COLUMNS = (
    "id, lima_name, slot, scope, task_id::text AS task_id, session_id::text AS session_id,"
    " requested_by::text AS requested_by, implicit, org_id::text AS org_id,"
    " wg_id::text AS wg_id, owner_id::text AS owner_id, size, cpus, memory_mib, disk_gib, image, state, created_at,"
    " last_used_at, stopped_at"
)
LIVE_STATES = ("starting", "running", "stopping", "stopped", "deleting")
MEMORY_STATES = ("starting", "running", "stopping", "deleting")
"""States in which a work VM holds its RAM (a stopped VM gives it back, 05-runner.md section 3.4)."""


@dataclass(frozen=True)
class WorkVm:
    id: str
    lima_name: str
    slot: int
    scope: str
    task_id: str
    session_id: str | None
    requested_by: str
    implicit: bool
    org_id: str
    wg_id: str
    owner_id: str | None
    size: str
    cpus: int
    memory_mib: int
    disk_gib: int
    image: str
    state: str
    created_at: datetime
    last_used_at: datetime
    stopped_at: datetime | None


async def _rows(db: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with db.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, params)
        return list(await cursor.fetchall())


async def get(db: Any, workvm_id: str) -> WorkVm | None:
    rows = await _rows(db, f"SELECT {COLUMNS} FROM runner.workvm WHERE id = %s", (workvm_id,))
    return WorkVm(**rows[0]) if rows else None


async def for_scope(db: Any, *, task_id: str, session_id: str | None) -> WorkVm | None:
    """The live VM of the session (scope `session`) when `session_id` is given, else the live task VM."""
    if session_id is not None:
        query = (
            f"SELECT {COLUMNS} FROM runner.workvm WHERE scope = 'session' AND session_id = %s AND state <> 'deleted'"
        )
        rows = await _rows(db, query, (session_id,))
    else:
        query = f"SELECT {COLUMNS} FROM runner.workvm WHERE scope = 'task' AND task_id = %s AND state <> 'deleted'"
        rows = await _rows(db, query, (task_id,))
    return WorkVm(**rows[0]) if rows else None


async def for_session(db: Any, *, task_id: str, session_id: str) -> WorkVm | None:
    """The VM a session's commands use: its own session VM when it has one, else the task VM."""
    own = await for_scope(db, task_id=task_id, session_id=session_id)
    return own if own is not None else await for_scope(db, task_id=task_id, session_id=None)


async def live(db: Any, *states: str) -> list[WorkVm]:
    """Work VMs not deleted (only the given states when any), oldest first."""
    query = f"SELECT {COLUMNS} FROM runner.workvm WHERE state <> 'deleted'"
    params: tuple[Any, ...] = ()
    if states:
        query += " AND state = ANY(%s)"
        params = (list(states),)
    rows = await _rows(db, query + " ORDER BY created_at, id", params)
    return [WorkVm(**row) for row in rows]


async def counts(db: Any, *, task_id: str, org_id: str, owner_id: str | None) -> tuple[int, int, int]:
    """Live VMs of the task (without its run VMs), of the organization and of the owner (spec 11 section 2 limits,
    ADR-078 section 2)."""
    rows = await _rows(
        db,
        "SELECT count(*) FILTER (WHERE task_id = %s AND scope <> 'run') AS task,"
        " count(*) FILTER (WHERE org_id = %s) AS org,"
        " count(*) FILTER (WHERE owner_id IS NOT DISTINCT FROM %s) AS owner"
        " FROM runner.workvm WHERE state <> 'deleted'",
        (task_id, org_id, owner_id),
    )
    row = rows[0]
    return int(row["task"]), int(row["org"]), int(row["owner"])


def _values(vm: WorkVm) -> tuple[Any, ...]:
    return (
        vm.id,
        vm.lima_name,
        vm.slot,
        vm.scope,
        vm.task_id,
        vm.session_id,
        vm.requested_by,
        vm.implicit,
        vm.org_id,
        vm.wg_id,
        vm.owner_id,
        vm.size,
        vm.cpus,
        vm.memory_mib,
        vm.disk_gib,
        vm.image,
        vm.state,
        vm.created_at,
        vm.last_used_at,
        vm.stopped_at,
    )


async def insert(db: Any, vm: WorkVm) -> WorkVm:
    """Insert a row; a second live VM of the scope or the slot raises `workvm_exists`."""
    try:
        rows = await _rows(
            db,
            "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, session_id, requested_by, implicit,"
            " org_id, wg_id, owner_id, size, cpus, memory_mib, disk_gib, image, state, created_at, last_used_at,"
            f" stopped_at) VALUES ({', '.join(['%s'] * 20)}) RETURNING {COLUMNS}",
            _values(vm),
        )
    except psycopg.errors.UniqueViolation:
        raise RunnerError("workvm_exists") from None
    return WorkVm(**rows[0])


async def delete_reason(db: Any, workvm_id: str) -> str | None:
    """The teardown reason recorded for the VM (set when its deletion began)."""
    rows = await _rows(db, "SELECT delete_reason FROM runner.workvm WHERE id = %s", (workvm_id,))
    return str(rows[0]["delete_reason"]) if rows and rows[0]["delete_reason"] is not None else None


async def update(db: Any, workvm_id: str, *, expect: Sequence[str] | None = None, **fields: Any) -> WorkVm | None:
    """Set columns of a row (only while it is in one of the `expect` states, when given); None: no such row."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    query = f"UPDATE runner.workvm SET {assignments} WHERE id = %s"
    params: tuple[Any, ...] = (*(fields[name] for name in names), workvm_id)
    if expect is not None:
        query += " AND state = ANY(%s)"
        params = (*params, list(expect))
    rows = await _rows(db, query + f" RETURNING {COLUMNS}", params)
    return WorkVm(**rows[0]) if rows else None


async def used_slots(db: Any) -> set[int]:
    """Slots of live work VMs and of pool entries."""
    rows = await _rows(
        db, "SELECT slot FROM runner.workvm WHERE state <> 'deleted' UNION SELECT slot FROM runner.workvm_pool"
    )
    return {int(row["slot"]) for row in rows}


async def disk_in_use(db: Any, pool_disk_gib: int) -> int:
    """GiB of disk allocated to live work VMs plus `pool_disk_gib` per pool entry (pool entries are standard VMs)."""
    rows = await _rows(
        db,
        "SELECT (SELECT coalesce(sum(disk_gib), 0) FROM runner.workvm WHERE state <> 'deleted')"
        " + (SELECT count(*) FROM runner.workvm_pool) * %s AS gib",
        (pool_disk_gib,),
    )
    return int(rows[0]["gib"])


async def memory_in_use(db: Any) -> int:
    """MiB held by work VMs (states of `MEMORY_STATES`) and by pool entries."""
    rows = await _rows(
        db,
        "SELECT (SELECT coalesce(sum(memory_mib), 0) FROM runner.workvm WHERE state = ANY(%s))"
        " + (SELECT coalesce(sum(memory_mib), 0) FROM runner.workvm_pool) AS mib",
        (list(MEMORY_STATES),),
    )
    return int(rows[0]["mib"])


@dataclass(frozen=True)
class Pooled:
    """A warm pool entry (migration 0012 `runner.workvm_pool`)."""

    lima_name: str
    slot: int
    image: str
    memory_mib: int
    state: str
    created_at: datetime


POOL_COLUMNS = "lima_name, slot, image, memory_mib, state, created_at"


async def pool(db: Any) -> list[Pooled]:
    """Every pool entry, oldest first."""
    rows = await _rows(db, f"SELECT {POOL_COLUMNS} FROM runner.workvm_pool ORDER BY created_at, lima_name")
    return [Pooled(**row) for row in rows]


async def pool_add(db: Any, entry: Pooled) -> None:
    """Insert a `starting` entry; a slot already in the pool raises `workvm_exists`."""
    try:
        async with db.connection() as conn:
            await conn.execute(
                f"INSERT INTO runner.workvm_pool ({POOL_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s)",
                (entry.lima_name, entry.slot, entry.image, entry.memory_mib, entry.state, entry.created_at),
            )
    except psycopg.errors.UniqueViolation:
        raise RunnerError("workvm_exists") from None


async def pool_ready(db: Any, lima_name: str, at: datetime) -> bool:
    """Mark the entry `ready`; False when it no longer exists."""
    rows = await _rows(
        db,
        "UPDATE runner.workvm_pool SET state = 'ready', ready_at = %s WHERE lima_name = %s RETURNING lima_name",
        (at, lima_name),
    )
    return bool(rows)


async def pool_remove(db: Any, lima_name: str) -> bool:
    """Delete the entry; False when it was already gone (assigned or removed by another caller)."""
    rows = await _rows(db, "DELETE FROM runner.workvm_pool WHERE lima_name = %s RETURNING lima_name", (lima_name,))
    return bool(rows)


async def pool_assign(db: Any, image: str, memory_mib: int, build: Callable[[str, int], WorkVm]) -> WorkVm | None:
    """Take the oldest `ready` entry of `image` and size `memory_mib` and turn it into a work VM row in one
    transaction (`build(lima_name, slot)` returns the row); None when the pool has none. A second live VM of the
    scope raises `workvm_exists` and leaves the entry in the pool."""
    try:
        return await _assign(db, image, memory_mib, build)
    except psycopg.errors.UniqueViolation:
        raise RunnerError("workvm_exists") from None


async def _assign(db: Any, image: str, memory_mib: int, build: Callable[[str, int], WorkVm]) -> WorkVm | None:
    async with db.connection() as conn, conn.transaction():
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(
            "DELETE FROM runner.workvm_pool WHERE lima_name = (SELECT lima_name FROM runner.workvm_pool"
            " WHERE state = 'ready' AND image = %s AND memory_mib = %s ORDER BY created_at, lima_name LIMIT 1"
            " FOR UPDATE SKIP LOCKED) RETURNING lima_name, slot",
            (image, memory_mib),
        )
        taken = await cursor.fetchone()
        if taken is None:
            return None
        vm = build(str(taken["lima_name"]), int(taken["slot"]))
        await cursor.execute(
            "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, session_id, requested_by, implicit,"
            " org_id, wg_id, owner_id, size, cpus, memory_mib, disk_gib, image, state, created_at, last_used_at,"
            f" stopped_at) VALUES ({', '.join(['%s'] * 20)}) RETURNING {COLUMNS}",
            _values(vm),
        )
        row = await cursor.fetchone()
        assert row is not None
        return WorkVm(**row)


async def mirror(db: Any, workvm_id: str, session_id: str, root: str) -> dict[str, str] | None:
    rows = await _rows(
        db,
        "SELECT manifest FROM runner.workvm_mirror WHERE workvm_id = %s AND session_id = %s AND root = %s",
        (workvm_id, session_id, root),
    )
    if not rows:
        return None
    manifest = rows[0]["manifest"]
    return {str(path): str(sha) for path, sha in manifest.items()}


async def set_mirror(
    db: Any, workvm_id: str, session_id: str, root: str, *, manifest: Mapping[str, str], at: datetime
) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.workvm_mirror (workvm_id, session_id, root, manifest, updated_at)"
            " VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT (workvm_id, session_id, root)"
            " DO UPDATE SET manifest = excluded.manifest, updated_at = excluded.updated_at",
            (workvm_id, session_id, root, json.dumps(dict(manifest), sort_keys=True), at),
        )


async def ready_image(db: Any) -> str | None:
    rows = await _rows(db, "SELECT name FROM runner.workvm_image WHERE state = 'ready' ORDER BY built_at DESC LIMIT 1")
    return str(rows[0]["name"]) if rows else None


async def set_image(db: Any, name: str, state: str, at: datetime) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.workvm_image (name, state, built_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET state = excluded.state",
            (name, state, at),
        )


async def active_sessions(db: Any, task_id: str, *, except_id: str | None = None) -> int:
    """Sessions of the task on this device that are not final (`stopped`, `failed`)."""
    rows = await _rows(
        db,
        "SELECT count(*) AS n FROM runner.session WHERE task_id = %s AND state NOT IN ('stopped', 'failed')"
        " AND id IS DISTINCT FROM %s",
        (task_id, except_id),
    )
    return int(rows[0]["n"])

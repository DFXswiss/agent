"""VM rows of the runner store and the control-token files (02-data-model.md section 4 `vm`, 05-runner.md section 3.2).

`runner.vm` holds one row per VM the runner created: `id` (`vm-<8 hex>`), `lima_name` (`<prefix>arp-<8 hex>`, an
independent random value), the golden it was cloned from, the workgroup once assigned, the state, the SHA-256 of the
control token and the size. A workgroup may have several live VMs on the device (ADR-075 section 4, migration 0018);
a VM belongs to one workgroup for its life, and the VM of a session is the `vm_id` of its `runner.session` row
(`assign_session`, written by `VmManager.ensure_vm` under the workgroup lock; how many sessions a VM takes is measured
on the VM, `vm.room`).

`runner.golden_switch` records a VM replaced by one of a newer golden while sessions on it were paused (`record_switch`,
`switched_to`; ADR-093), `sessions_on` lists the sessions on a VM that are not stopped or failed. The switch keeps its
attempts (`attempt`, `put_attempt`, `forget_attempt(s)`, `prune_attempts`: `runner.golden_switch_attempt`) and the
requests by hand it took over (`switch_requests`, `take_requests`, `request_time`: `runner.golden_switch_request`)
across restarts; `mark_reported` and `hold_reported`: which holds a hub showed, since when (HOT-24).

The control token itself is kept in `$AGENT_RUNNER_HOME/vm/<vm_id>.token` (0600, directory 0700, written once and
atomically) and in memory; `TokenStore.read` refuses a file whose hash differs from the row (`vm_token_invalid`).

All queries take the daemon's `psycopg_pool.AsyncConnectionPool` (`db`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_runner.errors import RunnerError
from agent_runner.identity import ensure_private_dir, write_new_file
from agent_runner.vm.capacity import Candidate

COLUMNS = (
    "id, lima_name, golden_version, wg_id::text AS wg_id, org_id::text AS org_id, state, control_token_hash, cpus,"
    " memory_mib, created_at, assigned_at, last_session_at"
)
# Session states that keep a VM busy (03-api.md session states without suspended and the terminal ones).
ACTIVE_SESSION_STATES = (
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
_VM_ID_RE = re.compile(r"^vm-[0-9a-f]{8}$")


@dataclass(frozen=True)
class VmRow:
    id: str
    lima_name: str
    golden_version: str
    wg_id: str | None
    org_id: str | None
    state: str
    control_token_hash: str
    cpus: int
    memory_mib: int
    created_at: datetime
    assigned_at: datetime | None
    last_session_at: datetime | None


@dataclass(frozen=True)
class GoldenRef:
    version: str
    lima_name: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _rows(db: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with db.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, params)
        return list(await cursor.fetchall())


async def _execute(db: Any, query: str, params: tuple[Any, ...] = ()) -> None:
    async with db.connection() as conn:
        await conn.execute(query, params)


async def get(db: Any, vm_id: str) -> VmRow | None:
    rows = await _rows(db, f"SELECT {COLUMNS} FROM runner.vm WHERE id = %s", (vm_id,))
    return VmRow(**rows[0]) if rows else None


async def of_workgroup(db: Any, wg_id: str) -> list[VmRow]:
    """The live VMs (state <> deleted) of the workgroup, smallest VM ID first."""
    rows = await _rows(
        db, f"SELECT {COLUMNS} FROM runner.vm WHERE wg_id = %s AND state <> 'deleted' ORDER BY id", (wg_id,)
    )
    return [VmRow(**row) for row in rows]


async def session_tasks(db: Any, vm_id: str) -> dict[str, str]:
    """The task of every session on the VM that has not ended (`vm.room` groups a task's main session and its
    subagents, HOT-32)."""
    rows = await _rows(
        db,
        "SELECT id::text AS id, task_id::text AS task_id FROM runner.session"
        " WHERE vm_id = %s AND state NOT IN ('stopped', 'failed')",
        (vm_id,),
    )
    return {row["id"]: row["task_id"] for row in rows}


async def assign_session(db: Any, session_id: str, vm_id: str) -> None:
    """Name the VM of a session on its local row (the booking of its place, ADR-075 section 4)."""
    await _execute(db, "UPDATE runner.session SET vm_id = %s WHERE id = %s", (vm_id, session_id))


async def load(db: Any) -> tuple[int, int]:
    """`(running workgroup VMs, occupying sessions)` of the device (capacity report, ADR-075 section 2)."""
    rows = await _rows(
        db,
        "SELECT (SELECT COUNT(*) FROM runner.vm WHERE state = 'running') AS vms,"
        " (SELECT COUNT(*) FROM runner.session WHERE state = ANY(%s)) AS sessions",
        (list(ACTIVE_SESSION_STATES),),
    )
    return int(rows[0]["vms"]), int(rows[0]["sessions"])


async def live(db: Any, *states: str) -> list[VmRow]:
    """VMs not deleted (only the given states when any), oldest first."""
    query = f"SELECT {COLUMNS} FROM runner.vm WHERE state <> 'deleted'"
    params: tuple[Any, ...] = ()
    if states:
        query += " AND state = ANY(%s)"
        params = (list(states),)
    rows = await _rows(db, query + " ORDER BY created_at, id", params)
    return [VmRow(**row) for row in rows]


async def insert(
    db: Any,
    *,
    vm_id: str,
    lima_name: str,
    golden_version: str,
    state: str,
    token_sha256: str,
    cpus: int,
    memory_mib: int,
    created_at: datetime,
    org_id: str | None = None,
    wg_id: str | None = None,
) -> VmRow:
    """Insert a row; an ID or Lima name that exists already raises `vm_exists`."""
    try:
        rows = await _rows(
            db,
            "INSERT INTO runner.vm (id, lima_name, golden_version, org_id, wg_id, state, control_token_hash, cpus,"
            " memory_mib, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING " + COLUMNS,
            (vm_id, lima_name, golden_version, org_id, wg_id, state, token_sha256, cpus, memory_mib, created_at),
        )
    except psycopg.errors.UniqueViolation:
        raise RunnerError("vm_exists") from None
    return VmRow(**rows[0])


async def update(db: Any, vm_id: str, **fields: Any) -> VmRow:
    """Set columns of a row (column names are the keyword names; never user input)."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    rows = await _rows(
        db,
        f"UPDATE runner.vm SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
        (*(fields[name] for name in names), vm_id),
    )
    if not rows:
        raise RunnerError("vm_unknown")
    return VmRow(**rows[0])


async def transition(db: Any, vm_id: str, expect: Sequence[str], **fields: Any) -> VmRow | None:
    """Like `update`, but only while the row is in one of the `expect` states (None otherwise): a row another task
    moved meanwhile is never overwritten."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    rows = await _rows(
        db,
        f"UPDATE runner.vm SET {assignments} WHERE id = %s AND state = ANY(%s) RETURNING {COLUMNS}",
        (*(fields[name] for name in names), vm_id, list(expect)),
    )
    return VmRow(**rows[0]) if rows else None


async def take_pool(
    db: Any, golden_version: str, org_id: str, wg_id: str, at: datetime, *, size: tuple[int, int]
) -> VmRow | None:
    """Atomically move the oldest pool VM of the golden and of `size` (`(cpus, memory_mib)`, the size of a new VM) to
    `assigning` for the workgroup (None: no such pool VM)."""
    cpus, memory_mib = size
    rows = await _rows(
        db,
        "UPDATE runner.vm SET state = 'assigning', org_id = %s, wg_id = %s, assigned_at = %s"
        " WHERE id = (SELECT id FROM runner.vm WHERE state = 'pool' AND golden_version = %s"
        " AND cpus = %s AND memory_mib = %s"
        " ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING " + COLUMNS,
        (org_id, wg_id, at, golden_version, cpus, memory_mib),
    )
    return VmRow(**rows[0]) if rows else None


async def slot_rows(db: Any) -> list[tuple[str, int]]:
    rows = await _rows(db, "SELECT state, memory_mib FROM runner.vm WHERE state <> 'deleted'")
    return [(row["state"], int(row["memory_mib"])) for row in rows]


_USAGE = """
SELECT v.id, v.wg_id::text AS wg_id,
       GREATEST(v.created_at, v.assigned_at, v.last_session_at, MAX(s.created_at), MAX(s.last_prompt_at),
                MAX(s.ended_at)) AS last_used,
       COUNT(s.id) FILTER (WHERE s.state = ANY(%s)) AS active_sessions,
       MAX(GREATEST(s.created_at, s.last_prompt_at)) FILTER (WHERE s.state = 'suspended') AS newest_suspended
FROM runner.vm v LEFT JOIN runner.session s ON s.vm_id = v.id
WHERE v.state = 'running'
GROUP BY v.id
ORDER BY v.id
"""


async def usage(db: Any) -> list[Candidate]:
    """Running VMs with their last use, active session count and newest suspended session (05 sections 3.4, 3.5)."""
    rows = await _rows(db, _USAGE, (list(ACTIVE_SESSION_STATES),))
    return [
        Candidate(
            vm_id=row["id"],
            wg_id=row["wg_id"],
            last_used=row["last_used"],
            active_sessions=int(row["active_sessions"]),
            newest_suspended=row["newest_suspended"],
        )
        for row in rows
    ]


async def ready_golden(db: Any) -> GoldenRef | None:
    """The golden new clones start from: the newest `ready` row (VM-01)."""
    rows = await _rows(
        db,
        "SELECT version, lima_name FROM runner.golden WHERE state = 'ready'"
        " ORDER BY built_at DESC, version DESC LIMIT 1",
    )
    return GoldenRef(**rows[0]) if rows else None


async def touch(db: Any, vm_id: str, at: datetime) -> None:
    await _execute(db, "UPDATE runner.vm SET last_session_at = %s WHERE id = %s", (at, vm_id))


@dataclass(frozen=True)
class SessionOnVm:
    id: str
    depth: int
    state: str


async def sessions_on(db: Any, vm_id: str) -> list[SessionOnVm]:
    """The sessions on the VM that are not `stopped` or `failed`, in ID order."""
    rows = await _rows(
        db,
        "SELECT id::text AS id, depth, state FROM runner.session WHERE vm_id = %s"
        " AND state NOT IN ('stopped', 'failed') ORDER BY id",
        (vm_id,),
    )
    return [SessionOnVm(**row) for row in rows]


async def golden_built_at(db: Any, version: str) -> datetime | None:
    rows = await _rows(db, "SELECT built_at FROM runner.golden WHERE version = %s", (version,))
    return rows[0]["built_at"] if rows else None


async def record_switch(db: Any, old_vm_id: str, new_vm_id: str, golden_version: str, at: datetime) -> None:
    await _execute(
        db,
        "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (old_vm_id) DO UPDATE SET new_vm_id = EXCLUDED.new_vm_id, golden_version ="
        " EXCLUDED.golden_version, switched_at = EXCLUDED.switched_at",
        (old_vm_id, new_vm_id, golden_version, at),
    )


async def hold_times(
    db: Any, vm_id: str, keys: Sequence[tuple[str, str]], at: datetime
) -> dict[tuple[str, str], datetime]:
    """Since when each cause `(reason, session_key)` holds the VM (`at` for a new one); the rows of causes that are
    no longer named are deleted (`runner.golden_switch_hold`)."""
    async with db.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM runner.golden_switch_hold WHERE vm_id = %s AND NOT ((reason, session_key) IN"
            " (SELECT * FROM unnest(%s::text[], %s::text[])))",
            (vm_id, [key[0] for key in keys], [key[1] for key in keys]),
        )
        for reason, session_key in keys:
            await conn.execute(
                "INSERT INTO runner.golden_switch_hold (vm_id, reason, session_key, since) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (vm_id, reason, session_key, at),
            )
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(
            "SELECT reason, session_key, since FROM runner.golden_switch_hold WHERE vm_id = %s", (vm_id,)
        )
        return {(row["reason"], row["session_key"]): row["since"] for row in await cursor.fetchall()}


async def forget_holds(db: Any, vm_id: str) -> None:
    await _execute(db, "DELETE FROM runner.golden_switch_hold WHERE vm_id = %s", (vm_id,))


@dataclass(frozen=True)
class Attempt:
    """Attempts of one kind (`refresh`, `failure`) of the switch of a VM, with the reason and the next time."""

    count: int
    reason: str
    next_at: datetime


async def attempt(db: Any, vm_id: str, kind: str) -> Attempt | None:
    rows = await _rows(
        db,
        "SELECT count, reason, next_at FROM runner.golden_switch_attempt WHERE vm_id = %s AND kind = %s",
        (vm_id, kind),
    )
    return Attempt(**rows[0]) if rows else None


async def put_attempt(db: Any, vm_id: str, kind: str, found: Attempt) -> None:
    await _execute(
        db,
        "INSERT INTO runner.golden_switch_attempt (vm_id, kind, count, reason, next_at) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (vm_id, kind) DO UPDATE SET count = EXCLUDED.count, reason = EXCLUDED.reason,"
        " next_at = EXCLUDED.next_at",
        (vm_id, kind, found.count, found.reason, found.next_at),
    )


async def forget_attempt(db: Any, vm_id: str, kind: str) -> None:
    await _execute(db, "DELETE FROM runner.golden_switch_attempt WHERE vm_id = %s AND kind = %s", (vm_id, kind))


async def forget_attempts(db: Any, vm_id: str) -> None:
    await _execute(db, "DELETE FROM runner.golden_switch_attempt WHERE vm_id = %s", (vm_id,))


async def prune_attempts(db: Any, live: Sequence[str]) -> None:
    """The attempts of VMs that are no longer live go."""
    await _execute(db, "DELETE FROM runner.golden_switch_attempt WHERE NOT (vm_id = ANY(%s))", (list(live),))


async def switch_requests(db: Any) -> list[str]:
    """The VMs requested by hand that the switch took over, oldest first."""
    rows = await _rows(db, "SELECT vm_id FROM runner.golden_switch_request ORDER BY requested_at, vm_id")
    return [str(row["vm_id"]) for row in rows]


async def take_requests(db: Any, wanted: Sequence[tuple[str, datetime | None]], now: datetime) -> list[str]:
    """Store the requested VMs with the hub's time of their request (`wanted`; the others are removed); returns the
    ones that are new or requested again (a newer time), whose attempts are forgotten (such a request tries at once).
    A request without a time (a hub before HOT-24) is stored with `now` once and never counts as new again while it
    is listed."""
    async with db.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM runner.golden_switch_request WHERE NOT (vm_id = ANY(%s))", ([vm for vm, _ in wanted],)
        )
        new: list[str] = []
        for vm_id, at in wanted:
            cur = await conn.execute(
                "INSERT INTO runner.golden_switch_request (vm_id, requested_at) VALUES (%s, %s) ON CONFLICT (vm_id)"
                " DO UPDATE SET requested_at = EXCLUDED.requested_at"
                " WHERE %s AND runner.golden_switch_request.requested_at < EXCLUDED.requested_at",
                (vm_id, now if at is None else at, at is not None),
            )
            if cur.rowcount == 1:
                new.append(vm_id)
                await conn.execute("DELETE FROM runner.golden_switch_attempt WHERE vm_id = %s", (vm_id,))
        return new


async def request_time(db: Any, vm_id: str) -> datetime | None:
    """When the VM was requested by hand (the hub's time of the newest request), None when it is not."""
    rows = await _rows(db, "SELECT requested_at FROM runner.golden_switch_request WHERE vm_id = %s", (vm_id,))
    return rows[0]["requested_at"] if rows else None


async def hold_reported(db: Any, vm_id: str) -> dict[tuple[str, str], datetime]:
    """When each recorded cause of the VM was first reported to a hub that shows it as it is (`reported_at`)."""
    rows = await _rows(
        db,
        "SELECT reason, session_key, reported_at FROM runner.golden_switch_hold WHERE vm_id = %s"
        " AND reported_at IS NOT NULL",
        (vm_id,),
    )
    return {(row["reason"], row["session_key"]): row["reported_at"] for row in rows}


async def mark_reported(db: Any, vm_id: str, keys: Sequence[tuple[str, str]], at: datetime) -> None:
    """The causes `keys` of the VM were reported (the first time counts)."""
    if keys:
        await _execute(
            db,
            "UPDATE runner.golden_switch_hold SET reported_at = %s WHERE vm_id = %s AND reported_at IS NULL"
            " AND (reason, session_key) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            (at, vm_id, [key[0] for key in keys], [key[1] for key in keys]),
        )


async def switched_to(db: Any, old_vm_id: str) -> str | None:
    """The VM that replaced `old_vm_id` at a switch to a newer golden, None when it was not switched."""
    rows = await _rows(db, "SELECT new_vm_id FROM runner.golden_switch WHERE old_vm_id = %s", (old_vm_id,))
    return str(rows[0]["new_vm_id"]) if rows else None


class TokenStore:
    """Control tokens in `<directory>/<vm_id>.token` (0600; the directory 0700)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, vm_id: str) -> Path:
        if not _VM_ID_RE.fullmatch(vm_id):
            raise RunnerError("vm_id_invalid")
        return self.directory / f"{vm_id}.token"

    def write(self, vm_id: str, token: str) -> None:
        ensure_private_dir(self.directory)
        try:
            write_new_file(self.path(vm_id), token.encode("ascii"))
        except RunnerError:
            raise RunnerError("vm_token_exists", vm_id) from None

    def read(self, vm_id: str, expected_sha256: str) -> str:
        path = self.path(vm_id)
        try:
            if path.is_symlink():
                raise OSError
            token = path.read_text(encoding="ascii")
        except (OSError, ValueError):
            raise RunnerError("vm_token_invalid", vm_id) from None
        if token_hash(token) != expected_sha256:
            raise RunnerError("vm_token_invalid", vm_id)
        return token

    def remove(self, vm_id: str) -> None:
        self.path(vm_id).unlink(missing_ok=True)

    def known(self) -> set[str]:
        """VM IDs that have a token file."""
        if not self.directory.is_dir():
            return set()
        return {
            path.name.removesuffix(".token")
            for path in self.directory.iterdir()
            if path.name.endswith(".token") and _VM_ID_RE.fullmatch(path.name.removesuffix(".token"))
        }

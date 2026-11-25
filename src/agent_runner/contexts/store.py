"""Rows of the context area (`store/migrations/0017_context.sql`; ADR-072 sections 4 and 7).

- `context_session`: one row per main session of this device that was ever due for a checkpoint (`mark_due`, `due`,
  `taken`, `postpone`, `drop_due`, `pending`, `mark_vm_due`, `due_on_vm`), plus the restore it started with
  (`set_restored`, `take_restore_audit`, `mark_restore_started`, `mark_restore_failed`, `restore_failed`).
- `context_object`: the encrypted checkpoints of the local cache (`insert_object`, `get_object`, `objects_of_task`,
  `uploads`, `uploaded`, `finish_object`, `retry_upload`, `restart_upload`, `delete_object`, `cache_rows`), and the
  logs of test runs and work VM jobs (`kind` `log`, KOR-43, ADR-080 section 3; `objects_of_task` lists only
  checkpoints).
- `context_pr_link`: registrations of pull requests at the hub (`pr_link_work`, `pr_link_state`).

A session of the main orchestrator (a row in `runner.orchestrator`, 05-runner.md section 17.6) never becomes due:
`mark_due`, `mark_periodic` and `mark_vm_due` leave it out, and `due` and `due_on_vm` never name it (also a checkpoint
that was due before the session became the orchestrator).

Every function runs in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class DueSession:
    session_id: str
    task_id: str
    org_id: str
    wg_id: str
    due_at: datetime
    archive_sha256: str | None
    attempts: int


@dataclass(frozen=True)
class StoredObject:
    object_id: str
    task_id: str
    session_id: str
    org_id: str
    wg_id: str
    source: str
    state: str
    manifest: dict[str, Any]
    sig: str
    bytes: int
    uploaded: int
    attempts: int
    created_at: datetime
    kind: str = "context"
    """`context` (a checkpoint) or `log` (ADR-080 section 3)."""
    session_created_at: datetime | None = None
    """When this device created the object's session (`objects_of_task` only; None for a session of another
    device)."""


OBJECT_COLUMNS = (
    "object_id::text AS object_id, task_id::text AS task_id, session_id::text AS session_id, org_id::text AS org_id,"
    " wg_id::text AS wg_id, source, state, manifest, sig, bytes, uploaded, attempts, created_at, kind"
)


_NOT_ORCHESTRATOR = "NOT EXISTS (SELECT 1 FROM runner.orchestrator o WHERE o.session_id = {column})"
"""The session `{column}` is not the main orchestrator (05-runner.md section 17.6)."""


async def mark_due(
    conn: AsyncConnection[Any],
    *,
    session_id: str,
    task_id: str,
    org_id: str,
    wg_id: str,
    at: datetime,
    forget: bool = False,
) -> None:
    """A checkpoint of the session is due (an earlier due time is kept, so a coalesced request is not delayed); with
    `forget` the hash of the last archive is dropped, so an unchanged archive is taken as a new object again."""
    await conn.execute(
        "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, due_at)"
        " SELECT %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::timestamptz"
        f" WHERE {_NOT_ORCHESTRATOR.format(column='%s::uuid')}"
        " ON CONFLICT (session_id) DO UPDATE SET due_at = LEAST(runner.context_session.due_at, EXCLUDED.due_at),"
        " attempts = 0, next_at = NULL,"
        " archive_sha256 = CASE WHEN %s THEN NULL ELSE runner.context_session.archive_sha256 END",
        (session_id, task_id, org_id, wg_id, at, session_id, forget),
    )


async def mark_periodic(conn: AsyncConnection[Any], *, interval_s: float, at: datetime) -> int:
    """Main sessions of this device that are `working` and had no checkpoint for `interval_s` become due (a session
    seen working for the first time counts from now); returns the number of sessions that became due."""
    cur = await conn.execute(
        "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, last_at)"
        " SELECT s.id, s.task_id, s.org_id, s.wg_id, %s FROM runner.session s"
        " WHERE s.depth = 0 AND s.state = 'working' AND NOT EXISTS (SELECT 1 FROM runner.context_session c"
        f" WHERE c.session_id = s.id) AND {_NOT_ORCHESTRATOR.format(column='s.id')}"
        " ON CONFLICT (session_id) DO NOTHING",
        (at,),
    )
    cur = await conn.execute(
        "UPDATE runner.context_session c SET due_at = %s FROM runner.session s WHERE s.id = c.session_id"
        " AND s.depth = 0 AND s.state = 'working' AND c.due_at IS NULL"
        f" AND (c.last_at IS NULL OR c.last_at <= %s) AND {_NOT_ORCHESTRATOR.format(column='s.id')}",
        (at, at - timedelta(seconds=interval_s)),
    )
    return cur.rowcount


DUE_COLUMNS = (
    "c.session_id::text AS session_id, c.task_id::text AS task_id, c.org_id::text AS org_id, c.wg_id::text AS wg_id,"
    " c.due_at, c.archive_sha256, c.attempts"
)
_WAITING_VM = (
    "EXISTS (SELECT 1 FROM runner.session s JOIN runner.vm v ON v.id = s.vm_id WHERE s.id = c.session_id"
    " AND v.state NOT IN ('running', 'deleted'))"
)
"""The session's VM exists but does not run: its checkpoint stays due until the VM runs again or is deleted."""


async def due(conn: AsyncConnection[Any], at: datetime, limit: int = 16) -> list[DueSession]:
    """Due checkpoints whose backoff passed, except those waiting for their VM (`_WAITING_VM`)."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {DUE_COLUMNS} FROM runner.context_session c WHERE c.due_at IS NOT NULL AND c.due_at <= %s"
        f" AND (c.next_at IS NULL OR c.next_at <= %s) AND NOT {_WAITING_VM}"
        f" AND {_NOT_ORCHESTRATOR.format(column='c.session_id')} ORDER BY c.due_at, c.session_id LIMIT %s",
        (at, at, limit),
    )
    return [DueSession(**row) for row in await cursor.fetchall()]


async def mark_vm_due(conn: AsyncConnection[Any], vm_id: str, at: datetime) -> int:
    """Every main session on the VM that has not ended becomes due (before the VM stops or goes away)."""
    cur = await conn.execute(
        "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, due_at)"
        " SELECT id, task_id, org_id, wg_id, %s FROM runner.session WHERE vm_id = %s AND depth = 0"
        " AND state NOT IN ('stopped', 'failed')"
        f" AND {_NOT_ORCHESTRATOR.format(column='runner.session.id')} ON CONFLICT (session_id) DO UPDATE"
        " SET due_at = LEAST(COALESCE(runner.context_session.due_at, EXCLUDED.due_at), EXCLUDED.due_at)",
        (at, vm_id),
    )
    return cur.rowcount


async def due_on_vm(conn: AsyncConnection[Any], vm_id: str) -> list[DueSession]:
    """Due checkpoints of the sessions on the VM, regardless of their backoff."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {DUE_COLUMNS} FROM runner.context_session c JOIN runner.session s ON s.id = c.session_id"
        f" WHERE s.vm_id = %s AND c.due_at IS NOT NULL AND {_NOT_ORCHESTRATOR.format(column='c.session_id')}"
        " ORDER BY c.due_at, c.session_id",
        (vm_id,),
    )
    return [DueSession(**row) for row in await cursor.fetchall()]


async def pending(conn: AsyncConnection[Any], session_id: str) -> bool:
    """Whether a checkpoint of the session is due (not taken, dropped or given up yet)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.context_session WHERE session_id = %s AND due_at IS NOT NULL", (session_id,)
    )
    return await cur.fetchone() is not None


async def taken(conn: AsyncConnection[Any], session_id: str, *, due_at: datetime, sha256: str, at: datetime) -> None:
    """The checkpoint that was due at `due_at` is taken; a request that arrived meanwhile stays due."""
    await conn.execute(
        "UPDATE runner.context_session SET archive_sha256 = %s, last_at = %s, attempts = 0, next_at = NULL,"
        " due_at = CASE WHEN due_at <= %s THEN NULL ELSE due_at END WHERE session_id = %s",
        (sha256, at, due_at, session_id),
    )


async def drop_due(conn: AsyncConnection[Any], session_id: str, *, due_at: datetime, at: datetime) -> None:
    """No checkpoint can be taken for this request (nothing to save, or it failed for good)."""
    await conn.execute(
        "UPDATE runner.context_session SET last_at = %s, attempts = 0, next_at = NULL,"
        " due_at = CASE WHEN due_at <= %s THEN NULL ELSE due_at END WHERE session_id = %s",
        (at, due_at, session_id),
    )


async def postpone(conn: AsyncConnection[Any], session_id: str, *, next_at: datetime) -> None:
    await conn.execute(
        "UPDATE runner.context_session SET attempts = attempts + 1, next_at = %s WHERE session_id = %s",
        (next_at, session_id),
    )


async def set_restored(
    conn: AsyncConnection[Any],
    *,
    session_id: str,
    task_id: str,
    org_id: str,
    wg_id: str,
    object_id: str,
    audit: Mapping[str, object],
) -> None:
    """The session starts from checkpoint `object_id`; `audit` is sent once its agent starts (`take_restore_audit`)."""
    await conn.execute(
        "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, restored_object_id, restore_audit,"
        " last_at) VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (session_id) DO UPDATE"
        " SET restored_object_id = EXCLUDED.restored_object_id, restore_audit = EXCLUDED.restore_audit,"
        " restore_started = false, restore_failed = false, last_at = now()",
        (session_id, task_id, org_id, wg_id, object_id, Jsonb(dict(audit))),
    )


async def take_restore_audit(conn: AsyncConnection[Any], session_id: str) -> dict[str, Any] | None:
    """The pending audit detail of a restored session, cleared (None when there is none)."""
    cur = await conn.execute(
        "UPDATE runner.context_session c SET restore_audit = NULL FROM (SELECT session_id, restore_audit"
        " FROM runner.context_session WHERE session_id = %s AND restore_audit IS NOT NULL FOR UPDATE) old"
        " WHERE c.session_id = old.session_id RETURNING old.restore_audit",
        (session_id,),
    )
    row = await cur.fetchone()
    return None if row is None else dict(row[0])


async def mark_restore_started(conn: AsyncConnection[Any], session_id: str) -> None:
    """The restored agent ran: a later failure is no failed start from its checkpoint."""
    await conn.execute(
        "UPDATE runner.context_session SET restore_started = true WHERE session_id = %s"
        " AND restored_object_id IS NOT NULL AND NOT restore_started",
        (session_id,),
    )


async def mark_restore_failed(conn: AsyncConnection[Any], session_id: str) -> bool:
    """A session that started from a checkpoint failed inside its start window: that checkpoint does not resume."""
    cur = await conn.execute(
        "UPDATE runner.context_session SET restore_failed = true, restore_audit = NULL WHERE session_id = %s"
        " AND restored_object_id IS NOT NULL AND NOT restore_started",
        (session_id,),
    )
    return cur.rowcount == 1


async def restore_failed(conn: AsyncConnection[Any], task_id: str, object_id: str) -> bool:
    """Whether a start from `object_id` already failed on this device."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.context_session WHERE task_id = %s AND restored_object_id = %s AND restore_failed",
        (task_id, object_id),
    )
    return await cur.fetchone() is not None


# -- objects -------------------------------------------------------------------------------------------------------


async def insert_object(
    conn: AsyncConnection[Any],
    *,
    manifest: Mapping[str, Any],
    sig: str,
    source: str,
    state: str,
    size: int,
    at: datetime,
    kind: str = "context",
) -> None:
    await conn.execute(
        "INSERT INTO runner.context_object (object_id, task_id, session_id, org_id, wg_id, source, state, manifest,"
        " sig, bytes, created_at, committed_at, kind) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (object_id) DO NOTHING",
        (
            manifest["object"],
            manifest["task"],
            manifest["session"],
            manifest["org"],
            manifest["wg"],
            source,
            state,
            Jsonb(dict(manifest)),
            sig,
            size,
            at,
            at if state == "committed" else None,
            kind,
        ),
    )


async def get_object(conn: AsyncConnection[Any], object_id: str) -> StoredObject | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(f"SELECT {OBJECT_COLUMNS} FROM runner.context_object WHERE object_id = %s", (object_id,))
    row = await cursor.fetchone()
    return None if row is None else StoredObject(**row)


async def objects_of_task(conn: AsyncConnection[Any], task_id: str) -> list[StoredObject]:
    """Every cached checkpoint of a task (refused ones too: their local copy is complete), with the creation time of
    its session where that session is one of this device's."""
    cursor = conn.cursor(row_factory=dict_row)
    columns = ", ".join(f"o.{part.strip()}" for part in OBJECT_COLUMNS.split(","))
    await cursor.execute(
        f"SELECT {columns}, s.created_at AS session_created_at FROM runner.context_object o"
        " LEFT JOIN runner.session s ON s.id = o.session_id WHERE o.task_id = %s AND o.kind = 'context'"
        " ORDER BY o.object_id",
        (task_id,),
    )
    return [StoredObject(**row) for row in await cursor.fetchall()]


async def uploads(conn: AsyncConnection[Any], at: datetime, limit: int = 8) -> list[StoredObject]:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {OBJECT_COLUMNS} FROM runner.context_object WHERE state = 'sealed'"
        " AND (next_at IS NULL OR next_at <= %s) ORDER BY created_at, object_id LIMIT %s",
        (at, limit),
    )
    return [StoredObject(**row) for row in await cursor.fetchall()]


async def uploaded(conn: AsyncConnection[Any], object_id: str, count: int) -> None:
    await conn.execute(
        "UPDATE runner.context_object SET uploaded = GREATEST(uploaded, %s), attempts = 0 WHERE object_id = %s",
        (count, object_id),
    )


async def finish_object(conn: AsyncConnection[Any], object_id: str, state: str, at: datetime) -> None:
    """`committed` (the hub holds it) or `refused` (for good; the local copy stays)."""
    await conn.execute(
        "UPDATE runner.context_object SET state = %s, committed_at = %s, next_at = NULL WHERE object_id = %s",
        (state, at if state == "committed" else None, object_id),
    )


async def retry_upload(conn: AsyncConnection[Any], object_id: str, next_at: datetime) -> None:
    await conn.execute(
        "UPDATE runner.context_object SET attempts = attempts + 1, next_at = %s WHERE object_id = %s",
        (next_at, object_id),
    )


async def restart_upload(conn: AsyncConnection[Any], object_id: str, next_at: datetime) -> None:
    """Upload the object again from its first chunk (the hub dropped the interrupted upload of a log)."""
    await conn.execute(
        "UPDATE runner.context_object SET uploaded = 0, attempts = attempts + 1, next_at = %s WHERE object_id = %s",
        (next_at, object_id),
    )


async def delete_object(conn: AsyncConnection[Any], object_id: str) -> None:
    await conn.execute("DELETE FROM runner.context_object WHERE object_id = %s", (object_id,))


async def cache_rows(conn: AsyncConnection[Any]) -> list[StoredObject]:
    """Every cached object, newest first."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(f"SELECT {OBJECT_COLUMNS} FROM runner.context_object ORDER BY created_at DESC, object_id DESC")
    return [StoredObject(**row) for row in await cursor.fetchall()]


# -- pull-request links --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequest:
    operation_id: str
    session_id: str
    task_id: str
    wg_id: str
    org_id: str
    repo: str
    number: int
    attempts: int


async def pr_link_work(conn: AsyncConnection[Any], at: datetime, limit: int = 16) -> list[PullRequest]:
    """Succeeded `github.create_pull_request` operations of this device not registered yet (and due)."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        "SELECT o.id::text AS operation_id, o.session_id::text AS session_id, o.task_id::text AS task_id,"
        " o.wg_id::text AS wg_id, o.org_id::text AS org_id, o.params->>'repo' AS repo,"
        " (o.result->>'number')::bigint AS number, COALESCE(l.attempts, 0) AS attempts"
        " FROM runner.operation o LEFT JOIN runner.context_pr_link l ON l.operation_id = o.id"
        " WHERE o.type = 'github.create_pull_request' AND o.state = 'succeeded' AND o.org_id IS NOT NULL"
        " AND jsonb_typeof(o.result->'number') = 'number' AND jsonb_typeof(o.params->'repo') = 'string'"
        " AND (l.operation_id IS NULL OR (l.state = 'pending' AND (l.next_at IS NULL OR l.next_at <= %s)))"
        " ORDER BY o.updated_at, o.id LIMIT %s",
        (at, limit),
    )
    return [PullRequest(**row) for row in await cursor.fetchall()]


async def pr_link_state(
    conn: AsyncConnection[Any], operation_id: str, state: str, *, next_at: datetime | None = None
) -> None:
    await conn.execute(
        "INSERT INTO runner.context_pr_link (operation_id, state, attempts, next_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (operation_id) DO UPDATE SET state = EXCLUDED.state, next_at = EXCLUDED.next_at,"
        " attempts = runner.context_pr_link.attempts + EXCLUDED.attempts, updated_at = now()",
        (operation_id, state, 1 if state == "pending" else 0, next_at),
    )

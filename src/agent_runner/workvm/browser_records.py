"""Browser session rows of the runner store (docs/platform/spec/13-work-vm.md section 13.2, migration 0028).

`runner.browser_session` holds one row per platform browser session; at most one `open` row per session (partial
unique index). A row stays `open` across a lost VM and a runner restart (`workvm_id` then names a VM that is gone)
and is `closed` with the reason of its end. `seq` counts the actions, `frame_seq` the live frames.

All queries take the daemon's `psycopg_pool.AsyncConnectionPool` (`db`).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

COLUMNS = (
    "browser_id, operation_id::text AS operation_id, session_id::text AS session_id, task_id::text AS task_id,"
    " org_id::text AS org_id, wg_id::text AS wg_id, workvm_id, state, seq, frame_seq, idle_seconds, url, ready_ms,"
    " implicit, opened_at, last_call_at, closed_at, close_reason"
)


def new_browser_id() -> str:
    return "br-" + secrets.token_hex(6)


@dataclass(frozen=True)
class BrowserRow:
    browser_id: str
    operation_id: str | None
    session_id: str
    task_id: str
    org_id: str
    wg_id: str
    workvm_id: str | None
    state: str
    seq: int
    frame_seq: int
    idle_seconds: int
    url: str
    ready_ms: int
    implicit: bool
    opened_at: datetime
    last_call_at: datetime
    closed_at: datetime | None
    close_reason: str | None


async def _rows(db: Any, query: str, params: tuple[Any, ...] = ()) -> list[BrowserRow]:
    async with db.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, params)
        return [BrowserRow(**row) for row in await cursor.fetchall()]


async def insert(db: Any, row: BrowserRow) -> BrowserRow:
    found = await _rows(
        db,
        "INSERT INTO runner.browser_session (browser_id, operation_id, session_id, task_id, org_id, wg_id, workvm_id,"
        " state, seq, frame_seq, idle_seconds, url, ready_ms, implicit, opened_at, last_call_at)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', 0, 0, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}",
        (
            row.browser_id,
            row.operation_id,
            row.session_id,
            row.task_id,
            row.org_id,
            row.wg_id,
            row.workvm_id,
            row.idle_seconds,
            row.url,
            row.ready_ms,
            row.implicit,
            row.opened_at,
            row.last_call_at,
        ),
    )
    return found[0]


async def open_of(db: Any, session_id: str) -> BrowserRow | None:
    """The open browser session of a session."""
    found = await _rows(
        db, f"SELECT {COLUMNS} FROM runner.browser_session WHERE session_id = %s AND state = 'open'", (session_id,)
    )
    return found[0] if found else None


async def last_of(db: Any, session_id: str) -> BrowserRow | None:
    """The newest browser session of a session (open or closed)."""
    found = await _rows(
        db,
        f"SELECT {COLUMNS} FROM runner.browser_session WHERE session_id = %s ORDER BY opened_at DESC, browser_id DESC"
        " LIMIT 1",
        (session_id,),
    )
    return found[0] if found else None


async def open_rows(db: Any) -> list[BrowserRow]:
    """Every open browser session, oldest first."""
    return await _rows(
        db, f"SELECT {COLUMNS} FROM runner.browser_session WHERE state = 'open' ORDER BY opened_at, browser_id"
    )


async def next_seq(db: Any, browser_id: str, at: datetime) -> int | None:
    """The `seq` of the next action of an open browser session (its `last_call_at` becomes `at`); None: closed."""
    found = await _rows(
        db,
        "UPDATE runner.browser_session SET seq = seq + 1, last_call_at = %s WHERE browser_id = %s AND state = 'open'"
        f" RETURNING {COLUMNS}",
        (at, browser_id),
    )
    return found[0].seq if found else None


async def next_frame(db: Any, browser_id: str) -> int:
    """The `seq` of the next live frame (a counter of its own, kept across restarts)."""
    found = await _rows(
        db,
        f"UPDATE runner.browser_session SET frame_seq = frame_seq + 1 WHERE browser_id = %s RETURNING {COLUMNS}",
        (browser_id,),
    )
    return found[0].frame_seq


async def update(db: Any, browser_id: str, **fields: Any) -> bool:
    """Set columns of an open row (column names are keyword names of the code, never input); False: it is closed."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    async with db.connection() as conn:
        cursor = await conn.execute(
            f"UPDATE runner.browser_session SET {assignments} WHERE browser_id = %s AND state = 'open'",
            (*(fields[name] for name in names), browser_id),
        )
        return bool(cursor.rowcount)


async def close(db: Any, browser_id: str, *, reason: str, at: datetime) -> BrowserRow | None:
    """Close an open row; None when it was already closed."""
    found = await _rows(
        db,
        "UPDATE runner.browser_session SET state = 'closed', closed_at = %s, close_reason = %s"
        f" WHERE browser_id = %s AND state = 'open' RETURNING {COLUMNS}",
        (at, reason, browser_id),
    )
    return found[0] if found else None

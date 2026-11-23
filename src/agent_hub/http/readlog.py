"""The read log (03-api.md section 1, 02-data-model.md section 2.9): every authenticated `GET` appends an audit row
`read.<resource>`, batched and written within one second.

The request pipeline calls `ReadLog.record` after the response; the writer task wakes on the first queued row, waits
until that row is `max_delay` seconds old, then writes every queued row through `append_audit` in one transaction. A
failed write keeps the rows and retries after `max_delay`; at most `max_rows` rows are kept (the oldest are dropped
with an error line). Rows are validated when recorded, so one malformed row never blocks a batch.

Placement: most reads name only a task, a session, an operation or an event scope (`/v1/tasks/{task}/operations`,
`/v1/operations/{id}`, `/v1/scopes/{scope}/events?task=`), but the organization's audit view lists rows by `org_id`
(03-api.md section 4.11). Before a successful read (`ok`: the reader holds the right, so the resource is one of an
organization it may read) is written, its missing organization, workgroup, task and session are taken from the hub's
row of the most specific ID it names (session, else task, else workgroup, else the operation or the event scope of
the target: a workgroup or an organization). A row is left as it is when it was refused, when the hub does not know
the ID, or when an ID the request named contradicts the hub's row (e.g. an audit filter of one organization naming
another organization's session), so a read never carries another organization's IDs it did not name itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.audit.chain import AuditRow, append_audit, validate_row
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction

LOG = logging.getLogger("agent_hub.readlog")
MAX_DELAY_SECONDS = 1.0
MAX_ROWS = 10_000
UUID_VERSION = 7
ID_PARAMETERS = ("org", "wg", "task", "session")
_TARGET_KIND_RE = re.compile(r"[a-z_]{1,32}")
_TARGET_ID_RE = re.compile(r"[ -~]{1,128}")


def _uuid7(value: object) -> UUID | None:
    try:
        parsed = UUID(str(value))
    except ValueError:
        return None
    return parsed if parsed.version == UUID_VERSION and str(parsed) == str(value) else None


def _result(status: int) -> str:
    if status < 300:
        return "ok"
    if status in (401, 403, 404):
        return "denied"
    if status >= 500:
        return "failed"
    return "rejected"


def read_row(
    principal: Principal,
    *,
    resource: str,
    status: int,
    path_params: Mapping[str, Any],
    query: Mapping[str, str],
    at: datetime,
) -> AuditRow:
    """The audit row of one authenticated read.

    The IDs of the organization, workgroup, task and session come from path parameters of those names, else from
    single query parameters of those names, when they are UUIDv7 text. The target is the last path parameter (kind =
    its name, or the resource for `{id}`); the detail is empty (`contracts/audit/read.*.json`).
    """
    ids = {
        name: _uuid7(path_params[name]) if name in path_params else _uuid7(query.get(name)) for name in ID_PARAMETERS
    }
    target_kind: str | None = None
    target_id: str | None = None
    if path_params:
        name, value = list(path_params.items())[-1]
        text = str(value)
        kind = (resource if name == "id" else name).replace(".", "_")[:32]
        if _TARGET_KIND_RE.fullmatch(kind) and _TARGET_ID_RE.fullmatch(text):
            target_kind, target_id = kind, text
    device_id = principal.principal_id if principal.kind == "device" else None
    return AuditRow(
        at=at,
        actor_kind=principal.actor_kind,
        actor_id=principal.principal_id,
        action=f"read.{resource}",
        result=_result(status),
        device_id=device_id,
        target_kind=target_kind,
        target_id=target_id,
        org_id=ids["org"],
        wg_id=ids["wg"],
        task_id=ids["task"],
        session_id=ids["session"],
    )


_PLACEMENT = {
    "session": "SELECT org_id, wg_id, task_id, id FROM hub.session WHERE id = %s",
    "operation": "SELECT org_id, wg_id, task_id, session_id FROM hub.operation WHERE id = %s",
    "task": "SELECT org_id, wg_id, id, NULL::uuid FROM hub.task WHERE id = %s",
    "wg": "SELECT org_id, id, NULL::uuid, NULL::uuid FROM hub.wg WHERE id = %s",
    "org": "SELECT id, NULL::uuid, NULL::uuid, NULL::uuid FROM hub.org WHERE id = %s",
}
# Targets of `{id}` routes and event scopes that name the read resource itself.
_TARGET_ANCHORS = {"session": ("session",), "operation": ("operation",), "scope": ("wg", "org")}


def _anchor(row: AuditRow) -> tuple[tuple[str, ...], UUID] | None:
    """The tables and the most specific ID of the row to place it by (module docstring)."""
    for kind, value in (("session", row.session_id), ("task", row.task_id), ("wg", row.wg_id)):
        if value is not None:
            return (kind,), value
    tables = _TARGET_ANCHORS.get(row.target_kind or "")
    target = _uuid7(row.target_id)
    return None if tables is None or target is None else (tables, target)


def place(connection: psycopg.Connection[Any], row: AuditRow) -> AuditRow:
    """The row with its missing organization, workgroup, task and session from the hub's rows (module docstring)."""
    anchor = None if row.result != "ok" else _anchor(row)
    if anchor is None:
        return row
    tables, value = anchor
    found = None
    for table in tables:
        found = connection.execute(_PLACEMENT[table], (value,)).fetchone()
        if found is not None:
            break
    if found is None:
        return row
    named = (row.org_id, row.wg_id, row.task_id, row.session_id)
    if any(
        mine is not None and theirs is not None and mine != theirs for mine, theirs in zip(named, found, strict=True)
    ):
        return row
    org, wg, task, session = (mine or theirs for mine, theirs in zip(named, found, strict=True))
    return replace(row, org_id=org, wg_id=wg, task_id=task, session_id=session)


class ReadLog:
    """The batching writer of read-log rows."""

    def __init__(
        self,
        pool: ConnectionPool[psycopg.Connection[Any]],
        *,
        max_delay: float = MAX_DELAY_SECONDS,
        max_rows: int = MAX_ROWS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self.max_delay = max_delay
        self._max_rows = max_rows
        self._monotonic = monotonic
        self._rows: deque[AuditRow] = deque()
        self._first_at: float | None = None
        self._lock = threading.Lock()
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def pending(self) -> int:
        """The number of rows not yet written."""
        with self._lock:
            return len(self._rows)

    def record(self, row: AuditRow) -> None:
        """Queue one row (validated now) and wake the writer."""
        validate_row(row)
        with self._lock:
            self._keep([row])
        self._notify()

    def _keep(self, rows: list[AuditRow]) -> None:
        if self._first_at is None:
            self._first_at = self._monotonic()
        self._rows.extend(rows)
        dropped = 0
        while len(self._rows) > self._max_rows:
            self._rows.popleft()
            dropped += 1
        if dropped:
            LOG.error("read log: %d rows dropped (queue full)", dropped)

    def _notify(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def flush(self) -> int:
        """Write every queued row in one transaction; the number written. On failure the rows stay queued."""
        with self._lock:
            batch = list(self._rows)
            self._rows.clear()
            self._first_at = None
        if not batch:
            return 0
        try:
            with transaction(self._pool) as connection:
                for row in batch:
                    append_audit(connection, place(connection, row))
        except BaseException:
            with self._lock:
                later = list(self._rows)
                self._rows.clear()
                self._first_at = None
                self._keep(batch + later)
            raise
        return len(batch)

    def _delay(self) -> float:
        with self._lock:
            first_at = self._first_at
        return 0.0 if first_at is None else self.max_delay - (self._monotonic() - first_at)

    async def _run(self, wake: asyncio.Event) -> None:
        while True:
            await wake.wait()
            wake.clear()
            delay = self._delay()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(self.flush)
            except Exception:
                LOG.error("read log: writing failed; %d rows kept for the next attempt", self.pending())
                wake.set()

    async def start(self) -> None:
        """Start the writer task on the running loop (rows queued before are written within `max_delay`)."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._wake), name="agent-hub-read-log")
        if self.pending():
            self._wake.set()

    async def stop(self) -> None:
        """Stop the writer and write what is left."""
        task, self._task, self._loop, self._wake = self._task, None, None, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await asyncio.to_thread(self.flush)
        except Exception:
            LOG.error("read log: final write failed; %d rows lost", self.pending())

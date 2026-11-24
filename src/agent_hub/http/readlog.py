"""The read log (03-api.md section 1, 02-data-model.md section 2.9, ADR-063 section 3): every authenticated `GET`
with a `read_resource` is recorded before its answer leaves the hub, and repeats are counted instead of listed. The
rows the request pipeline writes for writes (`agent_hub.http.writelog`) go through the same windows.

A **read key** is everything an audit row of a read says except its time - action, result, actor kind and ID, agent,
device, organization, workgroup, task, session, target and detail - plus a fingerprint of the whole query (`query_key`),
so reading many pages, cursors or filters is many rows. The first read of a key opens a window of `window` seconds (300
by default) and is appended at once (`ReadLog.admit`, a worker thread of the request; the request pipeline answers `503
unavailable` when that fails, so no read leaves without its row). Every further read of the key inside the window waits
until the first row is stored and only counts. When the window ends - the sweeper runs every `sweep` seconds, `flush`
and `stop` end all windows - a key with repeats gets one row `audit.repeat` with the IDs of the first row and the detail
`{action, repeats, first_seq, window_s}`. At most `max_keys` windows are open: beyond that the oldest are ended early.
Nothing is dropped; a process that is killed (not stopped) loses the repeat counts of its open windows, never the first
row of a key.

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
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.audit.chain import AuditRow, append_audit, validate_row
from agent_hub.audit.placement import named_ids, place, target
from agent_hub.auth.keys import Principal
from agent_hub.clock import Clock, system_clock
from agent_hub.db.pool import transaction

LOG = logging.getLogger("agent_hub.readlog")
READ_WINDOW_SECONDS = 300.0
SWEEP_SECONDS = 5.0
MAX_KEYS = 50_000
FIRST_WAIT_SECONDS = 30.0
REPEAT_ACTION = "audit.repeat"


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
    ids = named_ids(path_params, query)
    target_kind, target_id = target(path_params, resource)
    device_id = principal.principal_id if principal.kind == "device" else None
    agent_id = principal.attributes.get("agent_id") if principal.kind == "agent" else None
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
        agent_id=agent_id if isinstance(agent_id, UUID) else None,
    )


Key = tuple[Any, ...]


def query_key(query_string: bytes) -> str:
    """A fingerprint of the whole query (order of the parameters ignored): pages, cursors and filters are different
    reads."""
    if not query_string:
        return ""
    pairs = sorted(parse_qsl(query_string.decode("latin-1"), keep_blank_values=True))
    return hashlib.sha256(urlencode(pairs).encode("latin-1")).hexdigest()[:32]


def read_key(row: AuditRow, query: str = "") -> Key:
    """Everything a row says except its time, and the fingerprint of its query (the detail of a read is empty; the one
    of a pipeline row names route, status and code, so different requests never count as repeats of each other)."""
    return (
        query,
        tuple(sorted(row.detail.items())),
        row.action,
        row.result,
        row.actor_kind,
        row.actor_id,
        row.agent_id,
        row.device_id,
        row.org_id,
        row.wg_id,
        row.task_id,
        row.session_id,
        row.target_kind,
        row.target_id,
    )


class ReadNotStored(RuntimeError):
    """The first row of the read's key is not stored (its write failed or did not finish in time)."""


@dataclass
class _Window:
    """The open window of one read key."""

    opened: float
    first: AuditRow
    first_seq: int | None = None
    repeats: int = 0
    failed: bool = False
    stored: threading.Event = field(default_factory=threading.Event)


class ReadLog:
    """The read log of one hub process (module docstring)."""

    def __init__(
        self,
        pool: ConnectionPool[psycopg.Connection[Any]],
        *,
        window: float = READ_WINDOW_SECONDS,
        sweep: float = SWEEP_SECONDS,
        max_keys: int = MAX_KEYS,
        first_wait: float = FIRST_WAIT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Clock = system_clock,
    ) -> None:
        self._pool = pool
        self.window = window
        self.sweep = sweep
        self._max_keys = max_keys
        self._first_wait = first_wait
        self._monotonic = monotonic
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[Key, _Window] = {}
        self._ended: list[_Window] = []  # windows with repeats whose row is not written yet
        self._task: asyncio.Task[None] | None = None

    def pending(self) -> int:
        """The number of windows whose repeats are not written yet (open or ended)."""
        with self._lock:
            return sum(1 for item in self._open.values() if item.repeats) + len(self._ended)

    def admit(self, row: AuditRow, query: str = "") -> None:
        """Record one read (validated now; `query` is its `query_key`): append the first row of its key, or count a
        repeat once that row is stored. Raises when the read is not recorded (the answer must not leave the hub)."""
        validate_row(row)
        key = read_key(row, query)
        now = self._monotonic()
        with self._lock:
            found = self._open.get(key)
            if found is not None and now - found.opened >= self.window:
                self._end(key, found)
                found = None
            if found is None:
                mine = self._open[key] = _Window(opened=now, first=row)
                self._trim()
            else:
                mine = None
                found.repeats += 1
        if mine is None:
            assert found is not None
            if not found.stored.wait(self._first_wait) or found.failed:
                with self._lock:
                    found.repeats -= 1
                raise ReadNotStored(row.action)
            return
        try:
            with transaction(self._pool) as connection:
                stored = append_audit(connection, place(connection, row))
        except BaseException:
            with self._lock:
                mine.failed = True
                if self._open.get(key) is mine:
                    del self._open[key]
            mine.stored.set()
            raise
        mine.first = stored.row  # placed: the repeats carry exactly the IDs of the first row (review M1)
        mine.first_seq = stored.seq
        mine.stored.set()

    def _end(self, key: Key, window: _Window) -> None:
        """End an open window (under the lock); one with repeats waits for its row in `_ended`."""
        del self._open[key]
        if window.repeats:
            self._ended.append(window)

    def _trim(self) -> None:
        while len(self._open) > self._max_keys:
            oldest = min(self._open, key=lambda key: self._open[key].opened)
            self._end(oldest, self._open[oldest])

    def _repeat_row(self, window: _Window) -> AuditRow:
        return replace(
            window.first,
            at=self._clock(),
            action=REPEAT_ACTION,
            detail={
                "action": window.first.action,
                "repeats": window.repeats,
                "first_seq": window.first_seq,
                "window_s": int(self.window),
            },
        )

    def flush(self, *, all_windows: bool = True) -> int:
        """End the windows (all, or only the expired ones) and write the `audit.repeat` rows of those with repeats in
        one transaction; the number written. On failure the rows wait for the next call."""
        now = self._monotonic()
        with self._lock:
            for key, window in list(self._open.items()):
                if window.first_seq is not None and (all_windows or now - window.opened >= self.window):
                    self._end(key, window)
            # a window ended (beyond `max_keys`) while its first row was still being written waits for that write; one
            # whose first row failed has nothing to repeat (its repeats were refused)
            settled = [window for window in self._ended if window.stored.is_set()]
            self._ended = [window for window in self._ended if not window.stored.is_set()]
            batch = [window for window in settled if not window.failed and window.repeats]
        if not batch:
            return 0
        try:
            with transaction(self._pool) as connection:
                for window in batch:
                    append_audit(connection, self._repeat_row(window))
        except BaseException:
            with self._lock:
                self._ended = batch + self._ended
            raise
        return len(batch)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.sweep)
            try:
                await asyncio.to_thread(self.flush, all_windows=False)
            except Exception:
                LOG.error("read log: writing the repeats failed; %d windows kept for the next attempt", self.pending())

    async def start(self) -> None:
        """Start the sweeper on the running loop."""
        self._task = asyncio.create_task(self._run(), name="agent-hub-read-log")

    async def stop(self) -> None:
        """Stop the sweeper and write the repeats of every window."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await asyncio.to_thread(self.flush)
        except Exception:
            LOG.error("read log: final write failed; the repeats of %d windows are lost", self.pending())

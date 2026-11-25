"""The browser live channel `GET /v1/stream` (03-api.md section 5, 06-web.md section 1.3, ADR-016).

A browser opens one stream per active org with its signed request, read with `fetch` streaming. Query: `org`,
`terminal` (repeatable, at most 3 sessions whose terminal output it watches), `browser` (repeatable, at most 3
sessions whose platform browser it watches, KOR-44) with `browser_kbps` (100-10000, default 1000), `cursors`
(`<scope_id>:<seq>,…`), `monitoring=1`, `account=1`.

**Rights.** The caller must be an active member of the org (else `404 not_found`, like every read). Readable scopes
are the org scope and every workgroup the user reads (`wg_access`, 03-api.md section 3); a watched session must be a
session of the org in a readable workgroup (`task:read`, else `404 not_found`). Every frame is filtered per stream:
`event` only for readable scopes and never a watched kind (`agent_hub.events.watched`); `terminal` and
`browser_action` only for sessions watched with `terminal=` and `browser=`; `browser_live` only for sessions watched
with `browser=`; frames of one
workgroup (task, session and operation rows, or `wg_id=` at `publish`) only for readers of that workgroup - a row
without a workgroup and a `metrics` frame with VM or session samples but without `wg_id=` reach nobody (fail closed);
`metrics` only with `monitoring=1`; user frames (`wraps`, `device_response`, `notification`) only to that user's
streams, a `notification` only to streams of its org that read its workgroup, an account notification (no org,
KOR-24) only to streams opened with `account=1` (a web client of before KOR-24 never receives one). The
rights are read again on every `log` frame of the org and with every heartbeat; a user who left the org loses the
stream, a session in a workgroup the user no longer reads is no longer watched.

**Order of a connect.** The stream is registered (live frames are held back), the heads of all readable scopes are read
and sent as `hello {stream_id, heads, hub_time}`, then the events after each readable cursor are replayed in ascending
order (`event`, and `terminal` of watched sessions; `browser.action` is never replayed, its pictures would fill the
buffer; cursors of unreadable scopes are ignored), then the stored latest live frame of every session watched with
`browser=` (`agent_hub.stream.browser`). If a scope has more than
`REPLAY_LIMIT` (1000) events after its cursor, or all scopes together more than `REPLAY_TOTAL_FRAMES` events or
`REPLAY_TOTAL_BYTES` stored bytes, nothing is replayed and `resync {reason: "replay_limit"}` follows. The held-back live
frames come next; per scope a frame at or below what the stream already sent (the head in `hello` or the last replayed
event) is dropped, so a frame is not sent twice by this stream. Delivery stays at-least-once across reconnects; the
client de-duplicates by `(scope_id, scope_seq)`.

**Live pictures** (13-work-vm.md section 13.6). `browser_live` frames do not queue in the buffer: a stream has one slot
per session watched with `browser=`, a newer frame replaces one not sent yet, and the slots never cause an overflow.
A stream sends a held frame only within its own byte budget of `browser_kbps` kbit/s (a token bucket that holds at most
one second of budget, at least one frame); a frame larger than what is left waits until the budget allows.

**Flow.** A heartbeat comment is sent every 15 s. Frames wait in a per-stream buffer of `BUFFER_FRAMES` frames and
`BUFFER_BYTES` bytes; when a slow reader lets it overflow, the buffer is dropped (except a `hello` not yet sent) and
replaced by `resync {reason: "buffer_overflow"}` (the client re-pulls with its cursors). When the hub shuts down, every
stream receives `resync {reason: "hub_restart"}` and ends; a lost bus connection (`agent_hub.stream.bus`) resyncs every
stream with `buffer_overflow`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.errors import HubError
from agent_hub.events import watched as watched_kinds
from agent_hub.events.read import stored_json
from agent_hub.stream.publish import Published

Connection = psycopg.Connection[Any]
HEARTBEAT_SECONDS = 15.0
HEARTBEAT = b": heartbeat\n\n"
BUFFER_FRAMES = 4096
BUFFER_BYTES = 8 * 1024 * 1024
REPLAY_LIMIT = 1000
REPLAY_TOTAL_FRAMES = 2000
REPLAY_TOTAL_BYTES = 4 * 1024 * 1024
MAX_TERMINALS = 3
MAX_BROWSERS = 3
KBPS_DEFAULT = 1000
KBPS_MIN = 100
KBPS_MAX = 10000
LIVE_FRAME = "browser_live"
MAX_CURSORS = 256
USER_FRAMES = frozenset({"wraps", "notification", "device_response"})
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CURSOR_RE = re.compile(r"(" + _UUID_RE.pattern + r"):(0|[1-9][0-9]{0,15})")
_KBPS_RE = re.compile(r"[1-9][0-9]{2,4}")


@dataclass(frozen=True)
class StreamQuery:
    """The validated query of `GET /v1/stream`."""

    org: UUID
    terminals: frozenset[UUID]
    cursors: Mapping[UUID, int]
    monitoring: bool
    account: bool = False
    """`account=1`: the stream also carries the user's account notifications (no org; KOR-24)."""
    browsers: frozenset[UUID] = frozenset()
    """`browser=`: the sessions whose platform browser the stream watches (KOR-44)."""
    browser_kbps: int = KBPS_DEFAULT

    @property
    def sessions(self) -> frozenset[UUID]:
        """Every session the stream watches, with either parameter."""
        return self.terminals | self.browsers


@dataclass(frozen=True)
class LiveEvent:
    """A committed event as the stream needs it: its place, its kind and session, and the stored form."""

    org_id: UUID
    scope_id: UUID
    scope_seq: int
    kind: str
    session_id: UUID | None
    event: Mapping[str, Any]


def _uuid(text: str) -> UUID:
    if _UUID_RE.fullmatch(text) is None:
        raise HubError("request_invalid")
    return UUID(text)


def _sessions(items: Iterable[str], limit: int) -> frozenset[UUID]:
    found = [_uuid(item) for item in items]
    if len(found) > limit or len(set(found)) != len(found):
        raise HubError("request_invalid")
    return frozenset(found)


def parse_query(
    org: str,
    terminal: Iterable[str],
    cursors: str | None,
    monitoring: str | None,
    account: str | None = None,
    *,
    browser: Iterable[str] = (),
    browser_kbps: str | None = None,
) -> StreamQuery:
    """Check the query (`400 request_invalid` for anything outside contracts/ws/sse/query.json)."""
    watched = _sessions(terminal, MAX_TERMINALS)
    browsers = _sessions(browser, MAX_BROWSERS)
    kbps = KBPS_DEFAULT
    if browser_kbps is not None:
        if _KBPS_RE.fullmatch(browser_kbps) is None or not KBPS_MIN <= int(browser_kbps) <= KBPS_MAX:
            raise HubError("request_invalid")
        kbps = int(browser_kbps)
    positions: dict[UUID, int] = {}
    if cursors is not None:
        parts = cursors.split(",")
        if len(parts) > MAX_CURSORS:
            raise HubError("request_invalid")
        for part in parts:
            match = _CURSOR_RE.fullmatch(part)
            if match is None or UUID(match.group(1)) in positions:
                raise HubError("request_invalid")
            positions[UUID(match.group(1))] = int(match.group(2))
    if monitoring not in (None, "1") or account not in (None, "1"):
        raise HubError("request_invalid")
    return StreamQuery(_uuid(org), watched, positions, monitoring == "1", account == "1", browsers, kbps)


def frame_bytes(frame: str, data: Mapping[str, Any]) -> bytes:
    """One SSE frame `event: <type>\\ndata: <json>\\n\\n` (the JSON is ASCII on one line)."""
    return b"event: " + frame.encode("ascii") + b"\ndata: " + json.dumps(data, separators=(",", ":")).encode() + b"\n\n"


def resync_bytes(reason: str) -> bytes:
    return frame_bytes("resync", {"reason": reason})


# --- database reads (worker threads) ---


def readable_scopes(connection: Connection, org: UUID, user: UUID) -> frozenset[UUID] | None:
    """The org scope and the workgroups the user reads; None when the user is not an active member of the org."""
    member = connection.execute(
        "SELECT 1 FROM hub.org_member WHERE org_id = %s AND principal_id = %s AND removed_seq IS NULL"
        " AND role IN ('admin', 'member')",
        (org, user),
    ).fetchone()
    if member is None:
        return None
    rows = connection.execute(
        "SELECT a.wg_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
        " WHERE w.org_id = %s AND a.user_id = %s AND a.revoked_seq IS NULL",
        (org, user),
    ).fetchall()
    return frozenset({org, *(UUID(str(row[0])) for row in rows)})


def watchable(connection: Connection, org: UUID, sessions: Iterable[UUID], scopes: frozenset[UUID]) -> frozenset[UUID]:
    """The sessions among `sessions` that belong to the org and to a readable workgroup."""
    ids = sorted(str(item) for item in sessions)
    if not ids:
        return frozenset()
    rows = connection.execute(
        "SELECT id, wg_id FROM hub.session WHERE id = ANY(%s::uuid[]) AND org_id = %s", (ids, org)
    ).fetchall()
    return frozenset(UUID(str(row[0])) for row in rows if UUID(str(row[1])) in scopes)


def scope_heads(connection: Connection, scopes: Iterable[UUID]) -> dict[UUID, int]:
    """The current `scope_seq` of every scope (0 without events)."""
    ids = sorted(str(item) for item in scopes)
    rows = connection.execute(
        "SELECT scope_id, seq FROM hub.scope_head WHERE scope_id = ANY(%s::uuid[])", (ids,)
    ).fetchall()
    found = {UUID(str(row[0])): int(row[1]) for row in rows}
    return {UUID(item): found.get(UUID(item), 0) for item in ids}


_EVENT_COLUMNS = "org_id, scope_id, scope_seq, kind, session_id, received_at, header_text, ct, sig"


def live_event(row: tuple[Any, ...]) -> LiveEvent:
    """A `LiveEvent` of a row of `_EVENT_COLUMNS`."""
    org_id, scope_id, scope_seq, kind, session_id, *stored = row
    event = stored_json((scope_seq, *stored))["event"]
    session = None if session_id is None else UUID(str(session_id))
    return LiveEvent(UUID(str(org_id)), UUID(str(scope_id)), int(scope_seq), str(kind), session, event)


def read_event(connection: Connection, scope_id: UUID, scope_seq: int) -> LiveEvent | None:
    """One stored event."""
    row = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM hub.event WHERE scope_id = %s AND scope_seq = %s", (scope_id, scope_seq)
    ).fetchone()
    return None if row is None else live_event(row)


Watching = Mapping[str, frozenset[UUID]]
"""The sessions a stream watches per stream parameter of a watched kind (`terminal`, `browser`)."""


def _replay_filter(scope_id: UUID, after: int, watching: Watching) -> tuple[str, list[Any]]:
    """The rows a replay sends: every kind but the watched ones, and a replayed watched kind (terminal output) only of
    the sessions watched with its parameter (`agent_hub.events.watched`)."""
    clauses = ["scope_id = %s", "scope_seq > %s"]
    params: list[Any] = [scope_id, after]
    for item in watched_kinds.WATCHED:
        if item.replay:
            clauses.append("(kind <> %s OR session_id = ANY(%s::uuid[]))")
            params += [item.kind, sorted(str(session) for session in watching.get(item.param, ()))]
        else:
            clauses.append("kind <> %s")
            params.append(item.kind)
    return "FROM hub.event WHERE " + " AND ".join(clauses), params


def replay_size(connection: Connection, scope_id: UUID, after: int, watching: Watching) -> tuple[int, int]:
    """Number and stored bytes of the events a replay of the scope after `after` would send."""
    where, params = _replay_filter(scope_id, after, watching)
    row = connection.execute(
        f"SELECT count(*), coalesce(sum(octet_length(ct) + octet_length(header_text)), 0) {where}", params
    ).fetchone()
    count, size = cast(tuple[int, int], row)  # an aggregate always returns one row
    return int(count), int(size)


def replay_events(connection: Connection, scope_id: UUID, after: int, watching: Watching) -> list[LiveEvent]:
    """The events of the scope after `after` for this stream (at most `REPLAY_LIMIT`)."""
    where, params = _replay_filter(scope_id, after, watching)
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} {where} ORDER BY scope_seq LIMIT %s", [*params, REPLAY_LIMIT]
    ).fetchall()
    return [live_event(row) for row in rows]


def latest_frames(connection: Connection, sessions: Iterable[UUID]) -> dict[UUID, Mapping[str, Any]]:
    """The stored latest live frame of each of the sessions that has one (`hub.browser_live`)."""
    ids = sorted(str(item) for item in sessions)
    if not ids:
        return {}
    rows = connection.execute(
        "SELECT session_id, frame FROM hub.browser_live WHERE session_id = ANY(%s::uuid[])", (ids,)
    ).fetchall()
    return {UUID(str(row[0])): row[1] for row in rows}


def live_bytes(session: UUID, frame: Mapping[str, Any]) -> bytes:
    """The SSE frame `browser_live {session_id, frame}`."""
    return frame_bytes(LIVE_FRAME, {"session_id": str(session), "frame": frame})


@dataclass(frozen=True)
class Opening:
    """What a new stream sends first: `hello`, the replay and, past the replay limit, a resync; then the stored latest
    live frames of its watched browsers."""

    chunks: list[bytes]
    sent: dict[UUID, int]
    live: dict[UUID, bytes] = field(default_factory=dict)


def opening(
    connection: Connection,
    stream_id: str,
    query: StreamQuery,
    *,
    scopes: frozenset[UUID],
    watching: Watching,
    now: datetime,
) -> Opening:
    """Read the heads, the replay and the latest live frames of a new stream (module docstring)."""
    live = {
        session: live_bytes(session, frame)
        for session, frame in latest_frames(connection, watching.get("browser", ())).items()
    }
    heads = scope_heads(connection, scopes)
    hello = {
        "stream_id": stream_id,
        "heads": {str(key): value for key, value in heads.items()},
        "hub_time": format_time(now),
    }
    chunks = [frame_bytes("hello", hello)]
    sent = dict(heads)
    due = [
        (scope, cursor) for scope, cursor in sorted(query.cursors.items()) if scope in scopes and cursor < heads[scope]
    ]
    frames = size = 0
    for scope, cursor in due:
        count, stored = replay_size(connection, scope, cursor, watching)
        frames, size = frames + count, size + stored
        if count > REPLAY_LIMIT or frames > REPLAY_TOTAL_FRAMES or size > REPLAY_TOTAL_BYTES:
            return Opening([*chunks, resync_bytes("replay_limit")], sent, live)
    replays: list[LiveEvent] = []
    for scope, cursor in due:
        replays.extend(replay_events(connection, scope, cursor, watching))
    for item in replays:
        chunks.append(event_bytes(item))
        sent[item.scope_id] = max(sent[item.scope_id], item.scope_seq)
    return Opening(chunks, sent, live)


def event_bytes(item: LiveEvent) -> bytes:
    frame = watched_kinds.frame_of(item.kind)
    return frame_bytes(frame, {"scope_id": str(item.scope_id), "scope_seq": item.scope_seq, "event": item.event})


ROW_FRAMES = frozenset({"task", "session", "operation", "attention"})
"""Frames of one workgroup's row; `attention` (KOR-39) names its workgroup only by `wg_id=` (its row has none)."""
UNKNOWN = UUID(int=0)  # a frame that must name its workgroup but does not: delivered to nobody (fail closed)


def _wg_value(value: object) -> UUID:
    try:
        return UUID(value) if isinstance(value, str) else UNKNOWN
    except ValueError:
        return UNKNOWN


def frame_wg(item: Published) -> UUID | None:
    """The workgroup an org frame belongs to: `wg_id=` of `publish`; for task, session, operation and attention rows
    the row's `wg_id` (`UNKNOWN` without one); for `metrics` without `wg_id=` None only when every sample is a device
    sample (VM and session samples belong to a workgroup, 03-api.md section 4.11), else `UNKNOWN`; None (org-wide) for
    the other frames."""
    if item.wg_id is not None:
        return item.wg_id
    if item.frame in ROW_FRAMES:
        row = item.data.get("session") if item.frame == "session" else item.data
        return _wg_value(row.get("wg_id")) if isinstance(row, Mapping) else UNKNOWN
    if item.frame == "metrics":
        samples = item.data.get("samples")
        devices_only = isinstance(samples, list) and all(
            isinstance(sample, Mapping) and sample.get("entity_kind") == "device" for sample in samples
        )
        return None if devices_only else UNKNOWN
    return None


# --- one stream (event-loop thread) ---


@dataclass(eq=False)
class BrowserStream:
    """The state of one open stream (module docstring)."""

    user_id: UUID
    query: StreamQuery
    scopes: frozenset[UUID]
    watched: frozenset[UUID]
    """The sessions whose terminal output the stream watches (`terminal=`)."""
    buffer: int = BUFFER_FRAMES
    byte_limit: int = BUFFER_BYTES
    key_id: str | None = None
    """The key that opened the stream: a revoked agent key or an ended session ends it (`CLOSE_KEYS`, KOR-24)."""
    stream_id: str = field(default_factory=uuid7)
    sent: dict[UUID, int] = field(default_factory=dict)
    finished: bool = False
    refresh: bool = False
    _primed: bool = False
    _early: list[LiveEvent | Published] = field(default_factory=list)
    _early_overflow: bool = False
    _items: deque[bytes] = field(default_factory=deque)
    _bytes: int = 0
    _hello_pending: bool = False
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    browsers: frozenset[UUID] = frozenset()
    """The sessions whose platform browser the stream watches (`browser=`)."""
    clock: Callable[[], float] = time.monotonic
    _live: dict[UUID, bytes] = field(default_factory=dict)
    _tokens: float = 0.0
    _filled_at: float | None = None

    @property
    def org(self) -> UUID:
        return self.query.org

    def watching(self) -> dict[str, frozenset[UUID]]:
        """The watched sessions per stream parameter (`agent_hub.events.watched`)."""
        return {"terminal": self.watched, "browser": self.browsers}

    @property
    def rate(self) -> float:
        """The live byte budget per second (`browser_kbps`)."""
        return self.query.browser_kbps * 1000 / 8

    def _enqueue(self, chunk: bytes) -> None:
        if self.finished:
            return
        if len(self._items) >= self.buffer or self._bytes + len(chunk) > self.byte_limit:
            kept = [self._items[0]] if self._hello_pending else []  # the client needs `hello` first
            self._items = deque([*kept, resync_bytes("buffer_overflow")])
            self._bytes = sum(len(item) for item in self._items)
        self._items.append(chunk)
        self._bytes += len(chunk)
        self._wake.set()

    def prime(self, opened: Opening) -> None:
        """Queue the opening frames, then the live frames that arrived meanwhile."""
        self.sent = dict(opened.sent)
        for chunk in opened.chunks:
            self._enqueue(chunk)
        self._hello_pending = bool(opened.chunks)
        for session, chunk in opened.live.items():
            if session in self.browsers:
                self._live.setdefault(session, chunk)  # a frame that arrived meanwhile is newer
        early, self._early = self._early, []
        self._primed = True
        if self._early_overflow:
            self._enqueue(resync_bytes("buffer_overflow"))
        for item in early:
            if isinstance(item, LiveEvent):
                self.offer_event(item)
            else:
                self.offer(item)

    def _hold(self, item: LiveEvent | Published) -> None:
        if len(self._early) >= self.buffer:
            self._early.clear()
            self._early_overflow = True
        self._early.append(item)

    def offer_event(self, item: LiveEvent) -> None:
        """A committed event of the org, filtered for this stream."""
        if not self._primed:
            self._hold(item)
            return
        if item.org_id != self.org or item.scope_id not in self.scopes:
            return
        if item.scope_seq <= self.sent.get(item.scope_id, 0):
            return
        spec = watched_kinds.BY_KIND.get(item.kind)
        if spec is not None and item.session_id not in self.watching()[spec.param]:
            return
        self.sent[item.scope_id] = item.scope_seq
        self._enqueue(event_bytes(item))

    def admits(self, item: Published) -> bool:
        """The rights filter of a published frame (module docstring)."""
        if item.frame == "metrics" and not self.query.monitoring:
            return False
        if item.audience.kind == "org":
            wg = frame_wg(item)
            return wg is None or wg in self.scopes
        if item.frame == "notification":  # a user frame of one org and maybe one workgroup, or of the account
            org, wg = item.data.get("org_id"), item.data.get("wg_id")
            if org is None:
                return self.query.account
            return org == str(self.org) and (wg is None or _wg_value(wg) in self.scopes)
        return True

    def offer(self, item: Published) -> None:
        """A published browser frame of the org or the user, filtered for this stream."""
        if not self._primed:
            self._hold(item)
            return
        if not self.admits(item):
            return
        if item.frame == "log":
            self.refresh = True
        self._enqueue(frame_bytes(item.frame, item.data))

    def offer_live(self, session: UUID, chunk: bytes) -> None:
        """A live frame (`live_bytes`) of a session: it takes the session's slot (replacing a frame not sent yet) when
        the stream watches the session's browser; it waits there for the opening and the byte budget."""
        if self.finished or session not in self.browsers:
            return
        self._live[session] = chunk
        self._wake.set()

    def _live_wait(self, now: float) -> float | None:
        """Seconds until the first held live frame fits the budget (0: now); None without one or before the
        opening."""
        if not self._live or not self._primed:
            return None
        self._fill(now)
        size = min(len(next(iter(self._live.values()))), self.rate)
        return max(0.0, (size - self._tokens) / self.rate)

    def _fill(self, now: float) -> None:
        if self._filled_at is None:
            self._tokens = self.rate  # a new stream may send its first second at once
        else:
            self._tokens = min(self.rate, self._tokens + (now - self._filled_at) * self.rate)
        self._filled_at = now

    def _take_live(self, now: float) -> list[bytes]:
        """The held live frames that fit the budget, oldest slot first (a frame larger than one second of budget goes
        out once the bucket is full and leaves it in debt)."""
        taken: list[bytes] = []
        while (wait := self._live_wait(now)) is not None and wait == 0.0:
            session = next(iter(self._live))
            chunk = self._live.pop(session)
            self._tokens -= len(chunk)
            taken.append(chunk)
        return taken

    def resync_for(self, item: Published) -> None:
        """A frame too large for the bus reached another process (its data is not known here): resync unless the
        frame type or its explicit workgroup excludes this stream (a resync carries no content)."""
        if item.frame == "metrics" and not self.query.monitoring:
            return
        if item.audience.kind == "org" and item.wg_id is not None and item.wg_id not in self.scopes:
            return
        self.resync("buffer_overflow")

    def resync(self, reason: str) -> None:
        """Tell the client to re-pull with its cursors."""
        if self._primed:
            self._enqueue(resync_bytes(reason))
        else:
            self._early_overflow = True

    def close(self, reason: str | None = None) -> None:
        """End the stream after the queued frames (and a resync with `reason`)."""
        if self.finished:
            return
        if reason is not None:
            self._enqueue(resync_bytes(reason))
        self.finished = True
        self._wake.set()

    def update_rights(
        self, scopes: frozenset[UUID] | None, watched: frozenset[UUID], browsers: frozenset[UUID] | None = None
    ) -> None:
        """New rights: None ends the stream (no longer a member); `watched` and `browsers` are the sessions it may still
        watch (`browsers` unchanged when None)."""
        if scopes is None:
            self.close()
            return
        self.scopes = scopes
        self.watched = watched
        if browsers is not None:
            self.browsers = browsers
            self._live = {session: chunk for session, chunk in self._live.items() if session in browsers}

    async def take(self, timeout: float) -> list[bytes]:
        """The queued frames and the live frames the budget allows; an empty list when `timeout` passed without one
        (or a held live frame's wait ended without it fitting yet)."""
        wait = self._live_wait(self.clock())
        if not self._items and not self.finished and wait != 0.0:
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout if wait is None else min(timeout, wait))
        items = list(self._items)
        self._items.clear()
        self._bytes = 0
        self._hello_pending = False
        if not self.finished:
            items += self._take_live(self.clock())
        return items

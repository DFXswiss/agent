"""Watch leases and live frames of the platform browser (13-work-vm.md section 13.6, ADR-085 section 6, KOR-44).

**Leases** (`hub.browser_watch`, unlogged). Each hub process keeps one row per session whose browser at least one of
its streams watches (`browser=` of `GET /v1/stream`): `until` = now + `LEASE_SECONDS`, `kbps` the largest wish of its
local streams. The live service (`agent_hub.stream.service`) writes the row when the first local stream starts
watching, renews it every `RENEW_SECONDS` and deletes it when the last local stream stops watching; a process that
dies leaves a row that ends with its `until`. Every written lease sends the session's device the hint
`browser {session_id}` (a runner that already streams ignores it). When no valid lease of a session remains, its
stored frame is deleted. The renewal also removes expired rows of other processes and the frames they leave behind.

**Frames** (`POST /v1/device/sessions/{session}/browser/live`, `post_live`). The session must run on the calling
device in a workgroup it is assigned to (`403 forbidden`, unknown `404 not_found`); a frame must pass
`agent_hub.crypto.browserframe.check_frame` with the path's session and its workgroup and name an epoch of that
workgroup (`400 request_invalid`; the hub never decrypts); without a valid lease the answer is `409 not_watched`.
A frame replaces the session's row of `hub.browser_live` (unlogged, the only frame the hub keeps) in the transaction
that holds the session's valid leases `FOR SHARE`: a process that ends its last lease meanwhile waits for the commit and
then deletes the frame with it, so no frame stays without a lease; a frame of the same browser session whose `seq` is
not above the stored one is answered but neither stored nor published. At most `FRAMES_PER_SECOND` frame posts per
session and second are taken per hub process (`FrameLimiter`; beyond, `429 rate_limited` with `Retry-After: 1` and
nothing stored). After the commit the reference `{session_id, seq}` is published (`BROWSER_LIVE_REF`, a bus message
carries at most 7900 bytes, a frame up to 512 KiB), and every hub process reads the row once and sends `browser_live
{session_id, frame}` to its streams watching the session. The answer `{until, kbps}` names the latest valid lease and
the largest wish among the valid leases. No audit row per frame (the stream's `read.stream.browser` records who
watches).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.contexts.store import own_session
from agent_hub.crypto import browserframe
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError

Connection = psycopg.Connection[Any]
LEASE_SECONDS = 30
RENEW_SECONDS = 10
HINT = "browser"
FRAMES_PER_SECOND = 10


@dataclass
class FrameLimiter:
    """At most `limit` frame posts per session in any second of `clock` (one per hub process)."""

    limit: int = FRAMES_PER_SECOND
    clock: Callable[[], float] = time.monotonic
    _posts: dict[UUID, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, session: UUID) -> None:
        """Count one frame post of the session (`429 rate_limited` beyond the limit, not counted)."""
        now = self.clock()
        with self._lock:
            for key in [key for key, times in self._posts.items() if times[-1] <= now - 1.0]:
                del self._posts[key]  # sessions without a post in the last second
            times = self._posts.setdefault(session, deque())
            while times and times[0] <= now - 1.0:
                times.popleft()
            if len(times) >= self.limit:
                raise HubError("rate_limited", headers={"Retry-After": "1"})
            times.append(now)


LIMITER = FrameLimiter()


@dataclass(frozen=True)
class Stored:
    """The stored latest frame of a session."""

    browser_id: str
    seq: int
    frame: Mapping[str, Any]


@dataclass(frozen=True)
class Posted:
    """What a post stored: the answer and, with a frame, the reference to publish after the commit."""

    answer: dict[str, Any]
    org_id: UUID
    wg_id: UUID
    seq: int | None


def drop_unwatched(connection: Connection, session: UUID, now: datetime) -> None:
    """Delete the session's frame when no valid lease of it remains."""
    connection.execute(
        "DELETE FROM hub.browser_live WHERE session_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM hub.browser_watch WHERE session_id = %s AND until > %s)",
        (session, session, now),
    )


def write_leases(
    connection: Connection, instance: str, wishes: Mapping[UUID, int | None], now: datetime, *, sweep: bool = False
) -> list[tuple[UUID, UUID]]:
    """Write this process's leases: a wish (kbps) writes or renews the session's row, None deletes it (and the frame
    when no valid lease remains); `sweep` also removes expired rows and frames without a valid lease. Returns the
    `(session, device)` pairs to hint (every written lease of a session placed on a device)."""
    until = now + timedelta(seconds=LEASE_SECONDS)
    written: list[UUID] = []
    for session, kbps in sorted(wishes.items(), key=lambda item: str(item[0])):
        if kbps is None:
            connection.execute(
                "DELETE FROM hub.browser_watch WHERE session_id = %s AND instance = %s", (session, instance)
            )
            drop_unwatched(connection, session, now)
            continue
        connection.execute(
            "INSERT INTO hub.browser_watch (session_id, instance, until, kbps) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (session_id, instance) DO UPDATE SET until = EXCLUDED.until, kbps = EXCLUDED.kbps",
            (session, instance, until, kbps),
        )
        written.append(session)
    if sweep:
        connection.execute("DELETE FROM hub.browser_watch WHERE until <= %s", (now,))
        connection.execute(
            "DELETE FROM hub.browser_live l WHERE NOT EXISTS"
            " (SELECT 1 FROM hub.browser_watch w WHERE w.session_id = l.session_id AND w.until > %s)",
            (now,),
        )
    if not written:
        return []
    rows = connection.execute(
        "SELECT id, device_id FROM hub.session WHERE id = ANY(%s::uuid[]) AND device_id IS NOT NULL ORDER BY id",
        (sorted(str(item) for item in written),),
    ).fetchall()
    return [(UUID(str(row[0])), UUID(str(row[1]))) for row in rows]


def release_instance(connection: Connection, instance: str, now: datetime) -> None:
    """Delete every lease of a stopping process and the frames no valid lease keeps."""
    sessions = connection.execute(
        "DELETE FROM hub.browser_watch WHERE instance = %s RETURNING session_id", (instance,)
    ).fetchall()
    for row in sessions:
        drop_unwatched(connection, row[0], now)


def read_live(connection: Connection, session: UUID) -> Stored | None:
    """The stored latest frame of a session."""
    row = connection.execute(
        "SELECT browser_id, seq, frame FROM hub.browser_live WHERE session_id = %s", (session,)
    ).fetchone()
    return None if row is None else Stored(str(row[0]), int(row[1]), row[2])


def _invalid(pointer: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [pointer]})


def post_live(
    connection: Connection, view: DeviceView, device: UUID, session_id: UUID, *, frame: object | None, now: datetime
) -> Posted:
    """Check and store a posted frame, or only answer the lease without one (module docstring)."""
    session = own_session(connection, view, device, str(session_id), main=False)
    checked: dict[str, object] | None = None
    if frame is not None:
        try:
            checked = browserframe.check_frame(frame, session=str(session.session_id), wg=str(session.wg_id))
        except CryptoError:
            raise _invalid("/frame") from None
        known = connection.execute(
            "SELECT 1 FROM hub.scope_epoch WHERE scope = 'wg' AND scope_id = %s AND epoch = %s",
            (session.wg_id, checked["epoch"]),
        ).fetchone()
        if known is None:
            raise _invalid("/frame/epoch")
    leases = connection.execute(
        "SELECT until, kbps FROM hub.browser_watch WHERE session_id = %s AND until > %s FOR SHARE",
        (session.session_id, now),
    ).fetchall()
    if not leases:
        raise HubError("not_watched")
    answer = {"until": format_time(max(row[0] for row in leases)), "kbps": max(int(row[1]) for row in leases)}
    seq = None if checked is None else int(str(checked["seq"]))
    stored = checked is not None and (
        connection.execute(
            "INSERT INTO hub.browser_live (session_id, browser_id, seq, frame, received_at) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (session_id) DO UPDATE SET browser_id = EXCLUDED.browser_id, seq = EXCLUDED.seq,"
            " frame = EXCLUDED.frame, received_at = EXCLUDED.received_at"
            " WHERE hub.browser_live.browser_id <> EXCLUDED.browser_id OR hub.browser_live.seq < EXCLUDED.seq"
            " RETURNING 1",
            (session.session_id, checked["browser"], seq, Jsonb(checked), now),
        ).fetchone()
        is not None
    )
    return Posted(answer, session.org_id, session.wg_id, seq if stored else None)

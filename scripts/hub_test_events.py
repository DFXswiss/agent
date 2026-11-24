"""Device-authored session events and subagent sessions for the device double of the web suite (WEB-05).

The runner (journal → outbox → `POST /v1/device/outbox`, HUB-10) is not part of the web Playwright suite. To show the
chat items and the terminal, `scripts/hub_test_director.py` writes what the runner would: events of the kinds
`session.output`, `session.tool`, `session.file`, `session.hook`, `session.status` and `session.terminal`, sealed with
the workgroup key the browser wrapped for the device (unwrapped with the device's own X25519 key), signed by the
device and stored through `agent_hub.events.store.append_events` with all hub checks (E1-E9, the session runs on the
author device), then announced like the outbox does (`agent_hub.events.notify.announce`: live `event`/`terminal`
frames over the PostgreSQL bus). Subagent sessions (children by `parent_session_id`, as the subagent operation of
WEB-09/RUN will create them) are inserted as rows on the parent's device and announced with a `session` frame.

Only loopback, only for tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.crypto import event as event_format
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.wrap import unwrap_key
from agent_hub.events.notify import announce
from agent_hub.events.store import append_events
from agent_hub.sessions import rows
from agent_hub.stream.publish import Audience, publish
from agent_hub.wraps.rules import wraps_for

Json = dict[str, Any]
Pool = ConnectionPool[psycopg.Connection[Any]]


def now() -> datetime:
    return datetime.now(UTC)


def _session(connection: psycopg.Connection[Any], session_id: str) -> tuple[str, str, str, str, int]:
    """`(org, wg, task, device, depth)` of a session with a device."""
    found = connection.execute(
        "SELECT org_id, wg_id, task_id, device_id, depth FROM hub.session WHERE id = %s", (session_id,)
    ).fetchone()
    if found is None or found[3] is None:
        raise RuntimeError("the session does not exist or has no device")
    return str(found[0]), str(found[1]), str(found[2]), str(found[3]), int(found[4])


def _workgroup_key(connection: psycopg.Connection[Any], identity: Identity, device: str, wg: str) -> tuple[int, bytes]:
    """The newest workgroup key wrapped for the device: `(epoch, key)`."""
    wraps = wraps_for(connection, device, scope="wg", scope_id=wg)
    if not wraps:
        raise RuntimeError("the device holds no key of the workgroup")
    newest = max(wraps, key=lambda wrap: int(wrap["epoch"]))
    epoch = int(newest["epoch"])
    key = unwrap_key(identity.kem, scope="wg", scope_id=wg, epoch=epoch, recipient=device, wrap=newest["wrap"])
    return epoch, key


def session_events(
    pool: Pool, identity_of: Callable[[str], Identity], root_key: bytes, session_id: str, items: Sequence[Json]
) -> list[int]:
    """Seal and store `items` (`{kind, body, ts_offset_ms?}`) as events of the session's device; their `scope_seq`s.

    `ts_offset_ms` shifts the header time against now (negative: earlier), so a spec can place items in the past.
    """
    with pool.connection() as connection, connection.transaction():
        org, wg, task, device, _depth = _session(connection, session_id)
        identity = identity_of(device)
        epoch, key = _workgroup_key(connection, identity, device, wg)
        head = connection.execute("SELECT log_head_seq FROM hub.org WHERE id = %s", (org,)).fetchone()
        lh = int(head[0]) if head is not None else 0
        moment = now()
        events = []
        for item in items:
            ts = moment + timedelta(milliseconds=int(item.get("ts_offset_ms", 0)))
            fields = event_format.new_header(
                id=str(uuid7()),
                org=org,
                scope="wg",
                scope_id=wg,
                epoch=epoch,
                author=device,
                kind=str(item["kind"]),
                ts=format_time(ts),
                lh=lh,
                wg=wg,
                task=task,
                session=session_id,
            )
            plaintext = json.dumps(item["body"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            events.append(event_format.seal_event(key, fields, plaintext, identity.sig))
        stored = append_events(connection, events, device, now=moment, root_key=root_key)
    announce(stored)
    return [item.scope_seq for item in stored]


def child_session(pool: Pool, parent_id: str, state: str, model_id: str | None) -> str:
    """A subagent session of `parent_id` on the parent's device (depth + 1), announced with a `session` frame."""
    child = str(uuid7())
    with pool.connection() as connection, connection.transaction():
        org, wg, task, device, depth = _session(connection, parent_id)
        owner = connection.execute("SELECT owner_id FROM hub.session WHERE id = %s", (parent_id,)).fetchone()
        moment = now()
        connection.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
            " model_access, model_id, created_at, first_working_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'platform', %s, %s, %s)",
            (
                child,
                task,
                org,
                wg,
                owner[0] if owner else None,
                device,
                parent_id,
                depth + 1,
                state,
                model_id,
                moment,
                moment if state == "working" else None,
            ),
        )
        frame = rows.session_frame(connection, child)
    publish(Audience("org", UUID(org)), "session", frame)
    return child

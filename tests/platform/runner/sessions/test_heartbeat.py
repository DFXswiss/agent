"""Heartbeat field `sessions` (03-api.md section 6.2; KOR-10, AUD-02 NA-09): the sessions that have not ended, oldest
first, checked against the heartbeat contract; only rows the hub accepts; the largest frame under the frame limit."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import heartbeat, records
from agent_runner.store.pool import open_pool
from agent_runner.sync import extensions
from agent_runner.sync.ws import EMPTY_CAPACITY, MAX_FRAME_BYTES
from agent_runner.vm import heartbeat as vm_heartbeat

from ..sync.fakehub import SCHEMAS

T0 = datetime(2026, 11, 22, 12, tzinfo=UTC)


async def _insert(conn: Any, state: str, minute: int, session_id: str | None = None) -> str:
    session_id = session_id or str(uuid7())
    await records.insert(
        conn,
        session_id=session_id,
        task_id=str(uuid7()),
        org_id=str(uuid7()),
        wg_id=str(uuid7()),
        parent_id=None,
        depth=0,
        owner_id=str(uuid7()),
        created_at=T0 + timedelta(minutes=minute),
    )
    await records.update(conn, session_id, state=state)
    return session_id


def test_the_heartbeat_names_the_sessions_that_have_not_ended(store_dsn: str) -> None:
    async def body() -> None:
        pool = await open_pool(store_dsn, max_size=2)
        try:
            context = SimpleNamespace(pool=pool)
            assert await heartbeat.sessions(context) == []
            async with pool.connection() as conn:
                later = await _insert(conn, "working", 2)
                await _insert(conn, "stopped", 0)
                first = await _insert(conn, "suspended", 1)
                await _insert(conn, "failed", 3)
                waiting = await _insert(conn, "requested", 4)
                # rows the hub would refuse (closing the socket) are never sent
                await _insert(conn, "working", 5, str(uuid.uuid4()))
                await _insert(conn, "odd", 6)
            found = await heartbeat.sessions(context)
            assert found == [
                {"session_id": first, "state": "suspended"},
                {"session_id": later, "state": "working"},
                {"session_id": waiting, "state": "requested"},
            ]
            frame = {"type": "heartbeat", "capacity": dict(EMPTY_CAPACITY), "sessions": found, "vms": []}
            assert SCHEMAS.errors("ws/device/heartbeat.json", frame) == []
            heartbeat.MAX_SESSIONS, limit = 2, heartbeat.MAX_SESSIONS
            try:
                assert [item["session_id"] for item in await heartbeat.sessions(context)] == [first, later]
            finally:
                heartbeat.MAX_SESSIONS = limit
        finally:
            await pool.close()

    asyncio.run(body())


def test_the_largest_frame_stays_under_the_frame_limit() -> None:
    """`MAX_SESSIONS` sessions and `MAX_VMS` VMs with the longest IDs and states and the largest counts still fit into
    one frame of 64 KiB (the hub closes the socket for a larger one, 03-api.md section 6.2)."""
    state = max(heartbeat.OPEN_STATES, key=len)
    vm_state = max(("pool", "assigning", "running", "stopping", "stopped"), key=len)
    frame = {
        "type": "heartbeat",
        "capacity": dict.fromkeys(EMPTY_CAPACITY, 2**53 - 1),
        "sessions": [{"session_id": str(uuid7()), "state": state}] * heartbeat.MAX_SESSIONS,
        "vms": [{"vm_id": "vm-0123abcd", "state": vm_state}] * vm_heartbeat.MAX_VMS,
    }
    assert SCHEMAS.errors("ws/device/heartbeat.json", frame) == []
    assert len(json.dumps(frame, separators=(",", ":")).encode()) <= MAX_FRAME_BYTES


def test_the_sessions_area_provides_the_field() -> None:
    assert extensions.heartbeat_sections()["sessions"].provide is heartbeat.sessions

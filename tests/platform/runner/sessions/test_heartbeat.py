"""Heartbeat field `sessions` (03-api.md section 6.2; KOR-10, AUD-02 NA-09): the sessions that have not ended, oldest
first, checked against the heartbeat contract; only rows the hub accepts; every one of them, in frames under the frame
limit (ADR-088 section 3)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from agent_hub.stream.ws import CAPACITY_KEYS, OPTIONAL_CAPACITY_KEYS, parse_heartbeat
from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import heartbeat, records
from agent_runner.store.pool import open_pool
from agent_runner.sync import extensions
from agent_runner.sync.ws import EMPTY_CAPACITY, MAX_FRAME_BYTES, split_heartbeat

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
        finally:
            await pool.close()

    asyncio.run(body())


def test_a_device_with_1500_sessions_and_300_vms_sends_every_one_in_several_frames(store_dsn: str) -> None:
    """No cap (ADR-088 section 3): every open session is read, and the heartbeat is split into complete frames that
    each stay within 64 KiB and 1000 items per list (the hub closes the socket otherwise, 03-api.md section 6.2)."""

    async def body() -> None:
        pool = await open_pool(store_dsn, max_size=2)
        try:
            state = max(heartbeat.OPEN_STATES, key=len)
            async with pool.connection() as conn, conn.transaction():
                created = [await _insert(conn, state, minute) for minute in range(1500)]
            found = await heartbeat.sessions(SimpleNamespace(pool=pool))
        finally:
            await pool.close()
        assert [item["session_id"] for item in found] == created
        vm_state = max(("pool", "assigning", "running", "stopping", "stopped"), key=len)
        vms = [{"vm_id": f"vm-{n:08x}", "state": vm_state} for n in range(300)]
        capacity = dict.fromkeys(sorted(CAPACITY_KEYS | OPTIONAL_CAPACITY_KEYS), 2**53 - 1)
        frames = split_heartbeat({"type": "heartbeat", "capacity": capacity, "sessions": found, "vms": vms})
        assert len(frames) > 1
        sessions: list[Any] = []
        listed_vms: list[Any] = []
        for text in frames:
            assert len(text.encode()) <= MAX_FRAME_BYTES
            assert parse_heartbeat(text) == capacity  # the hub takes every frame on its own
            frame = json.loads(text)
            assert SCHEMAS.errors("ws/device/heartbeat.json", frame) == []
            sessions += frame["sessions"]
            listed_vms += frame["vms"]
        assert (sessions, listed_vms) == (found, vms)

    asyncio.run(body())


def test_the_frame_split_keeps_1000_items_per_list_and_sends_an_empty_heartbeat_whole() -> None:
    empty = {"type": "heartbeat", "capacity": dict(EMPTY_CAPACITY), "sessions": [], "vms": []}
    assert [json.loads(text) for text in split_heartbeat(empty)] == [empty]
    short = [{"vm_id": "vm-0", "state": "pool"}] * 2500
    frames = [json.loads(text) for text in split_heartbeat({**empty, "vms": short})]
    assert [len(frame["vms"]) for frame in frames] == [1000, 1000, 500]
    assert all(frame["sessions"] == [] for frame in frames)


def test_the_sessions_area_provides_the_field() -> None:
    assert extensions.heartbeat_sections()["sessions"].provide is heartbeat.sessions

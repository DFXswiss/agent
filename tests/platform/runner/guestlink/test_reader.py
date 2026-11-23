"""U-RUN-guestlink journal reader and cursor (03-api.md section 7.1, 02-data-model.md section 4 `guest_cursor`)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import GuestClient, JournalPage, JournalRecord
from agent_runner.guestlink.reader import JournalReader, JournalRewound
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool

from .conftest import Guest

CODES = ("gateway_route", "recorder_failed", "scanner_failed", "pending_expired")


def test_run_handles_commits_and_acks(guest: Guest) -> None:
    for code in CODES[:3]:
        guest.context.journal.append("guest.error", {"code": code})
    handled: list[list[str]] = []
    saved: list[int] = []
    stopping = asyncio.Event()

    async def body() -> Any:
        async with GuestClient(guest.socket, guest.token) as client:
            reader = JournalReader(client, after=1, limit=1, wait=0)

            async def handle(records: Sequence[JournalRecord]) -> None:
                handled.append([r.body["code"] for r in records])
                if records[-1].seq == 3:
                    stopping.set()

            async def save(seq: int) -> None:
                saved.append(seq)

            await reader.run(handle, save=save, stopping=stopping)
            return reader.cursor

    assert asyncio.run(body()) == 3
    assert handled == [["recorder_failed"], ["scanner_failed"]]
    assert saved == [2, 3]
    # Acknowledged records are pruned by the guest; the next read after the cursor is empty.
    records, _head = guest.context.journal.read(after=3, limit=10)
    assert records == []


def test_run_without_save_and_empty_polls(guest: Guest) -> None:
    async def body() -> Any:
        async with GuestClient(guest.socket, guest.token) as client:
            reader = JournalReader(client, wait=0)
            assert await reader.poll() == []
            guest.context.journal.append("guest.error", {"code": CODES[3]})
            got: list[int] = []

            async def handle(records: Sequence[JournalRecord]) -> None:
                got.extend(r.seq for r in records)
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                await reader.run(handle)
            await reader.commit(got[-1])
            return reader.cursor, got

    assert asyncio.run(body()) == (1, [1])


def test_empty_page_loops_until_stopped() -> None:
    class Client:
        calls = 0

        async def journal(self, after: int, *, limit: int, wait: int) -> JournalPage:
            Client.calls += 1
            if Client.calls == 2:
                stopping.set()
            return JournalPage(records=(), head=after)

    stopping = asyncio.Event()

    async def body() -> None:
        async def handle(_records: Sequence[JournalRecord]) -> None:
            raise AssertionError

        await JournalReader(Client(), wait=0).run(handle, stopping=stopping)  # type: ignore[arg-type]

    asyncio.run(body())
    assert Client.calls == 2


def test_rewound_journal_and_old_records() -> None:
    record = JournalRecord(seq=4, ts="t", session_id=None, kind="guest.error", body={})

    class Client:
        def __init__(self, head: int) -> None:
            self.head = head

        async def journal(self, after: int, *, limit: int, wait: int) -> JournalPage:
            return JournalPage(records=(record,), head=self.head)

    async def body() -> Any:
        with pytest.raises(JournalRewound) as caught:
            await JournalReader(Client(3), after=5).poll()  # type: ignore[arg-type]
        stale = await JournalReader(Client(9), after=4).poll()  # type: ignore[arg-type]
        return caught.value.code, stale

    assert asyncio.run(body()) == ("guest_journal_rewound", [])


def test_cursor_store(runner_dsn: str) -> None:
    assert migrate(runner_dsn).status == "applied"

    async def body() -> Any:
        db = await open_pool(runner_dsn)
        try:
            first = await cursor.load_cursor(db, "vm-0a1b2c3d")
            await cursor.save_cursor(db, "vm-0a1b2c3d", 5)
            await cursor.save_cursor(db, "vm-0a1b2c3d", 3)  # never backwards
            middle = await cursor.load_cursor(db, "vm-0a1b2c3d")
            await cursor.save_cursor(db, "vm-0a1b2c3d", 9)
            last = await cursor.load_cursor(db, "vm-0a1b2c3d")
            await cursor.delete_cursor(db, "vm-0a1b2c3d")
            return first, middle, last, await cursor.load_cursor(db, "vm-0a1b2c3d")
        finally:
            await db.close()

    assert asyncio.run(body()) == (0, 5, 9, 0)

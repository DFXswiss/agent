"""Journal loop of one VM (05-runner.md sections 1.1 task `guestlink`, 6.4 runner side; 03-api.md section 7.1
`GET /v1/journal`, `POST /v1/journal/ack`; 02-data-model.md section 4 `guest_cursor`; R-6.1).

`VmJournal(env, row, ...).run(stopping)` reads the guest journal of one VM with the long poll (`JournalReader`, at
most 25 s per call) from the stored `guest_cursor`. Each page is handled in **one** store transaction: every record
goes to the handler of its kind (`registry.journal_handlers`, `<area>/journal.py:HANDLERS`) in journal order, each
in its own savepoint, and the cursor is stored with the last sequence number of the page; only after the commit is
the guest acknowledged. So everything a record produced (transitions, events, audit items, usage) is stored locally
before it goes to the network, a crash before the commit re-reads the page, and a crash after it re-reads nothing
(exactly once). Records of a kind without a handler only move the cursor.

A handler that refuses a record (`JournalRecordInvalid`, a `ValueError`/`CryptoError` of the outbox checks) rolls
back its savepoint; the record is skipped with a fact line. Any other error (store) rolls the whole page back and
ends the run (the caller restarts it from the stored cursor). A guest whose journal head fell below the cursor (its
journal was replaced) is read again from the start (`runner.journal rewound`); its records are new records of the new
journal.

The guest is not trusted by the host (00-overview.md section 3, B4): a record names a session only by a canonical
UUID of a session that runs on this VM, and a page must be strictly ascending in `seq` within the store's range, else
it is refused as a protocol error before anything is handled (the loop restarts from the stored cursor with backoff;
a guest can only stall its own VM's journal).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from psycopg import AsyncConnection

from agent_runner.crypto.errors import CryptoError
from agent_runner.guestlink import cursor
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestProtocolError
from agent_runner.guestlink.reader import JournalReader, JournalRewound
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession
from agent_runner.vm.records import VmRow

MAX_SEQ = 2**63 - 1
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def check_page(page: Sequence[JournalRecord], after: int) -> None:
    """A page is strictly ascending, above the cursor and within the store's range (`guest_protocol` otherwise)."""
    previous = after
    for record in page:
        if not previous < record.seq <= MAX_SEQ:
            raise GuestProtocolError("journal")
        previous = record.seq


async def store_cursor(conn: AsyncConnection[Any], vm_id: str, seq: int) -> None:
    await conn.execute(
        "INSERT INTO runner.guest_cursor (vm_id, journal_seq) VALUES (%s, %s) ON CONFLICT (vm_id) DO UPDATE SET"
        " journal_seq = GREATEST(runner.guest_cursor.journal_seq, EXCLUDED.journal_seq)",
        (vm_id, seq),
    )


class VmJournal:
    """The journal consumer of one VM; handlers receive it as their first argument."""

    def __init__(
        self,
        env: SessionEnv,
        vm: VmRow,
        *,
        handlers: Mapping[str, Callable[..., Any]],
        observers: Sequence[StateObserver] = (),
        reached: Callable[[str, str], None] | None = None,
    ) -> None:
        self.env = env
        self.vm = vm
        self.handlers = dict(handlers)
        self.observers = list(observers)
        self._reached = reached
        self.handled = 0
        self.skipped = 0

    def reached(self, session_id: str, state: str) -> None:
        """A transition written from the journal (start measurements)."""
        if self._reached is not None:
            self._reached(session_id, state)

    async def session(self, conn: AsyncConnection[Any], record: JournalRecord) -> LocalSession | None:
        """The local session of the record when it runs on this VM, else None (with a fact line)."""
        sid = record.session_id
        session = None if sid is None or not _UUID_RE.fullmatch(sid) else await records.get(conn, sid)
        if session is None or session.vm_id != self.vm.id:
            self.env.log(f"runner.journal foreign_session vm_id={self.vm.id} kind={record.kind} seq={record.seq}")
            return None
        return session

    async def handle(self, page: Sequence[JournalRecord]) -> None:
        """Handle one page and store the cursor in one transaction (see the module docstring)."""
        async with self.env.transaction() as conn:
            for record in page:
                handler = self.handlers.get(record.kind)
                if handler is None:
                    continue
                try:
                    async with conn.transaction():
                        await handler(self, conn, record)
                    self.handled += 1
                except (ValueError, CryptoError) as exc:
                    self.skipped += 1
                    self.env.log(
                        f"runner.journal record_skipped vm_id={self.vm.id} kind={record.kind} seq={record.seq}"
                        f" error={type(exc).__name__}"
                    )
            await store_cursor(conn, self.vm.id, page[-1].seq)

    async def run(self, stopping: asyncio.Event) -> None:
        """Read, handle and acknowledge pages until `stopping` (guest errors propagate to the caller)."""
        after = await cursor.load_cursor(self.env.db, self.vm.id)
        async with await self.env.client(self.vm) as client:
            reader = JournalReader(client, after=after)
            while not stopping.is_set():
                try:
                    page = await reader.poll()
                except JournalRewound:
                    self.env.log(f"runner.journal rewound vm_id={self.vm.id} cursor={reader.cursor}")
                    await cursor.delete_cursor(self.env.db, self.vm.id)
                    reader.cursor = 0
                    continue
                if not page:
                    continue
                check_page(page, reader.cursor)
                await self.handle(page)
                await reader.commit(page[-1].seq)

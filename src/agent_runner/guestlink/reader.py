"""Long-poll reader of a guest's journal (docs/platform/spec/03-api.md section 7.1, 05-runner.md section 1.1 task
`guestlink`, 02-data-model.md section 4 `guest_cursor`).

`JournalReader(client, after=cursor)` reads `GET /v1/journal?after=<cursor>&wait=25` in a loop. Each non-empty page
is handed to `handle(records)`; only after `handle` returned are the cursor stored (`save`, normally
`cursor.save_cursor` into `runner.guest_cursor`) and the records acknowledged (`POST /v1/journal/ack`), so a crash
re-reads at most the last page and never loses one. The per-VM `guestlink` task of the session start (RUN-04) runs
`reader.run(handle, save=...)` under the supervisor: a raised `GuestError` ends the run and the supervisor restarts
it with backoff from the stored cursor.

A guest whose journal head lies below the cursor has lost records the runner already consumed (a replaced journal);
the reader refuses to continue (`guest_journal_rewound`) instead of silently re-reading or skipping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from agent_runner.guestlink.client import DEFAULT_JOURNAL_LIMIT, JOURNAL_WAIT_MAX_S, GuestClient, JournalRecord
from agent_runner.guestlink.errors import GuestError

Handle = Callable[[Sequence[JournalRecord]], Awaitable[None]]
Save = Callable[[int], Awaitable[None]]


class JournalRewound(GuestError):
    def __init__(self) -> None:
        super().__init__("guest_journal_rewound", "journal")


class JournalReader:
    def __init__(
        self,
        client: GuestClient,
        *,
        after: int = 0,
        limit: int = DEFAULT_JOURNAL_LIMIT,
        wait: int = JOURNAL_WAIT_MAX_S,
    ) -> None:
        self.client = client
        self.cursor = after
        self.limit = limit
        self.wait = wait

    async def poll(self) -> list[JournalRecord]:
        """One long poll: the records after the cursor (empty after `wait` seconds without news)."""
        page = await self.client.journal(self.cursor, limit=self.limit, wait=self.wait)
        if page.head < self.cursor:
            raise JournalRewound()
        return [record for record in page.records if record.seq > self.cursor]

    async def commit(self, through: int, save: Save | None = None) -> None:
        """Store the cursor (when `save` is given), then acknowledge everything up to `through`."""
        if save is not None:
            await save(through)
        await self.client.ack(through)
        self.cursor = through

    async def run(self, handle: Handle, *, save: Save | None = None, stopping: asyncio.Event | None = None) -> None:
        """Read, handle and commit pages until `stopping` is set (or the task is cancelled)."""
        while stopping is None or not stopping.is_set():
            records = await self.poll()
            if not records:
                continue
            await handle(records)
            await self.commit(records[-1].seq, save)

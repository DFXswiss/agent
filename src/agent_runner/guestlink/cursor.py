"""Journal cursor per VM in the runner store (`runner.guest_cursor`, 02-data-model.md section 4).

`load_cursor(db, vm_id)` returns the last stored sequence number (0 for a VM never read), `save_cursor(db, vm_id,
seq)` stores it; the cursor never moves backwards (a late save of an older page keeps the newer value).
`db` is the daemon's `psycopg_pool.AsyncConnectionPool`.
"""

from __future__ import annotations

from typing import Any


async def load_cursor(db: Any, vm_id: str) -> int:
    async with db.connection() as conn:
        cursor = await conn.execute("SELECT journal_seq FROM runner.guest_cursor WHERE vm_id = %s", (vm_id,))
        row = await cursor.fetchone()
    return 0 if row is None else int(row[0])


async def save_cursor(db: Any, vm_id: str, seq: int) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.guest_cursor (vm_id, journal_seq) VALUES (%s, %s)"
            " ON CONFLICT (vm_id) DO UPDATE SET journal_seq = GREATEST(runner.guest_cursor.journal_seq,"
            " EXCLUDED.journal_seq)",
            (vm_id, seq),
        )


async def delete_cursor(db: Any, vm_id: str) -> None:
    async with db.connection() as conn:
        await conn.execute("DELETE FROM runner.guest_cursor WHERE vm_id = %s", (vm_id,))

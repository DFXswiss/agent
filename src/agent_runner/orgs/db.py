"""Store sessions of the reconcile steps and the inbox (02-data-model.md section 4).

Pooled connections are not in autocommit mode: after the first statement an implicit transaction is open, a later
`conn.transaction()` block becomes a savepoint, and everything commits only when the connection goes back to the
pool (or is rolled back if anything raises until then). The steps and the inbox store verified entries, keys,
cursors and audit items one unit at a time and keep in-memory state in step with what is committed, so they use
`session(pool)`: a pooled connection in autocommit mode, in which every `conn.transaction()` block is a real
transaction that commits at its end. The connection is returned to the pool in its default mode.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection


@asynccontextmanager
async def session(pool: Any) -> AsyncIterator[AsyncConnection[Any]]:
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            yield conn
        finally:
            await conn.set_autocommit(False)

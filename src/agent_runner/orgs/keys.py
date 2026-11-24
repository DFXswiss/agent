"""Scope-key store of the runner (02-data-model.md section 4 `scope_key`, 04-crypto.md section 2, ADR-022).

A scope key (`org`, `wg` or `cred`, one per epoch) is kept in `runner.scope_key` only sealed with the storage key
(`crypto.storage.seal_local`, purpose `scope_key`, context `<scope>:<scope_id>:<epoch>`), so a row moved to another
scope or epoch does not open. Keys are opened only into memory, for the duration of one use.

`source` is `wrap` (an unwrapped key wrap, reconcile step 3), `former_agent` (a `CK` wrap a former agent of the owner
made: it opens records, nothing new is sealed under it, ADR-082 section 9.5) or `package` (an invitation package, org
devices, RUN-06). A stored key never changes: storing the same slot again is a no-op (`ON CONFLICT DO NOTHING`), so a
repeated reconcile is idempotent.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.crypto.storage import open_local, scope_key_context, seal_local

FORMER_AGENT = "former_agent"
SOURCES = frozenset({"package", "wrap", FORMER_AGENT})
Slot = tuple[str, str, int]
"""`(scope, scope_id, epoch)`."""


async def store_scope_key(
    conn: AsyncConnection[Any], storage_key: bytes, slot: Slot, key: bytes, *, source: str
) -> bool:
    """Seal and store one scope key; True when it was new, False when the slot already held a key."""
    scope, scope_id, epoch = slot
    if source not in SOURCES:
        raise ValueError(f"unknown scope-key source: {source!r}")
    blob = seal_local(storage_key, "scope_key", scope_key_context(scope, scope_id, epoch), key)
    cur = await conn.execute(
        "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (scope, scope_id, epoch) DO NOTHING",
        (scope, scope_id, epoch, blob, source),
    )
    return cur.rowcount == 1


async def load_scope_key(
    conn: AsyncConnection[Any], storage_key: bytes, scope: str, scope_id: str, epoch: int
) -> bytes | None:
    """The opened key of one slot, None when the device holds no key for it (`decrypt_failed` for a bad row)."""
    cur = await conn.execute(
        "SELECT key_ct FROM runner.scope_key WHERE scope = %s AND scope_id = %s AND epoch = %s",
        (scope, scope_id, epoch),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return open_local(storage_key, "scope_key", scope_key_context(scope, scope_id, epoch), bytes(row[0]))


async def held_slots(conn: AsyncConnection[Any]) -> set[Slot]:
    """Every slot the device holds a key for (the keys stay sealed)."""
    cur = await conn.execute("SELECT scope, scope_id::text, epoch FROM runner.scope_key")
    return {(str(scope), str(scope_id), int(epoch)) for scope, scope_id, epoch in await cur.fetchall()}

"""U-RUN-inbox / orgs: the scope-key store sealed with the storage key (`agent_runner.orgs.keys`)."""

from __future__ import annotations

import asyncio

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import held_slots, load_scope_key, store_scope_key

from ..reconcile.rig import Rig, pid


def test_store_load_and_list(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env, session(env.pool) as conn:
            key, wg = bytes(range(32)), pid("wg")
            sk = env.sync.storage_key
            assert await load_scope_key(conn, sk, "wg", wg, 1) is None
            assert await store_scope_key(conn, sk, ("wg", wg, 1), key, source="wrap") is True
            assert await store_scope_key(conn, sk, ("wg", wg, 1), bytes(32), source="package") is False
            assert await load_scope_key(conn, sk, "wg", wg, 1) == key
            assert await held_slots(conn) == {("wg", wg, 1)}
            rows = await env.rows("SELECT key_ct, source FROM runner.scope_key")
            assert key not in bytes(rows[0][0])
            assert rows[0][1] == "wrap"
            with pytest.raises(ValueError, match="source"):
                await store_scope_key(conn, sk, ("wg", wg, 2), key, source="guess")
            await conn.execute("UPDATE runner.scope_key SET epoch = 2")
            with pytest.raises(CryptoError, match="decrypt_failed"):
                await load_scope_key(conn, sk, "wg", wg, 2)

    asyncio.run(body())

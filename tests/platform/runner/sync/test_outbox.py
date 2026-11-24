"""U-RUN-outbox: enqueue in the caller's transaction, gapless origin_seq under concurrent writers, sealed bodies,
hub-position adoption after a store wipe, sync_state helpers (03-api.md section 6.1, 02-data-model.md section 4)."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.sync import outbox
from agent_runner.sync.codes import ITEM_KINDS

from .conftest import Harness, run, uuid
from .fakehub import CONTRACTS, SCHEMAS

KINDS = ["session_transition", "vm_state", "operation_transition", "usage", "quota", "metrics", "audit", "wrap"]


def fixture_body(kind: str) -> dict[str, Any]:
    body = json.loads((CONTRACTS / "fixtures" / "outbox" / kind / "valid" / "basic.json").read_text())
    assert isinstance(body, dict)
    return body


def draft(**overrides: Any) -> dict[str, Any]:
    wg = overrides.pop("wg", uuid())
    fields: dict[str, Any] = {
        "id": uuid(),
        "org": uuid(),
        "scope": "wg",
        "scope_id": wg,
        "wg": wg,
        "task": uuid(),
        "session": uuid(),
        "kind": "session.output",
        "ts": "2026-11-21T12:00:00.000Z",
        "body": {"message_id": "m1", "role": "assistant", "text": "Grüezi 2+2=4", "turn": 1},
    }
    fields.update(overrides)
    return outbox.event_draft(**fields)


def test_every_item_kind_roundtrips_sealed_with_the_storage_key(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            bodies = {kind: fixture_body(kind) for kind in KINDS}
            for kind in KINDS:
                SCHEMAS.check(f"outbox/{kind}.json", bodies[kind])
                await env.enqueue(kind, bodies[kind])
            event = draft()
            seq = await env.enqueue("event", event)
            assert seq == len(KINDS) + 1
            async with env.pool.connection() as conn:
                items = await outbox.load_batch(conn, env.sync.storage_key, 0, 100)
                cur = await conn.execute("SELECT body_ct FROM runner.outbox ORDER BY origin_seq")
                blobs = [bytes(row[0]) for row in await cur.fetchall()]
            assert [item.origin_seq for item in items] == list(range(1, len(KINDS) + 2))
            assert [item.item_kind for item in items] == [*KINDS, "event"]
            assert [item.body for item in items] == [*(bodies[k] for k in KINDS), event]
            assert all(item.sealed is None for item in items)
            # the plaintext never reaches the table
            assert not any("Grüezi".encode() in blob or b"assistant" in blob for blob in blobs)

    run(body)


def test_gapless_under_concurrent_writers_with_rollbacks(harness: Harness) -> None:
    """M0 acceptance: eight writers on their own connections, every third transaction rolled back."""
    writers, per_writer = 8, 30

    async def writer(env: Any, number: int, committed: list[int]) -> None:
        rng = random.Random(number)
        async with await psycopg.AsyncConnection.connect(harness.dsn) as conn:
            for step in range(per_writer):
                try:
                    async with conn.transaction():
                        seq = await outbox.enqueue(conn, env.sync.storage_key, "metrics", {"samples": []})
                        # hold the lock while others queue up behind it
                        await asyncio.sleep(rng.random() / 500)
                        if step % 3 == 2:
                            raise LookupError
                    committed.append(seq)
                except LookupError:
                    pass

    async def body() -> None:
        async with harness.env() as env:
            committed: list[int] = []
            await asyncio.gather(*(writer(env, number, committed) for number in range(writers)))
            async with env.pool.connection() as conn:
                cur = await conn.execute("SELECT origin_seq FROM runner.outbox ORDER BY origin_seq")
                stored = [row[0] for row in await cur.fetchall()]
            expected = writers * (per_writer - per_writer // 3)
            assert sorted(committed) == stored == list(range(1, expected + 1))

    run(body)


def test_enqueue_in_a_rolled_back_transaction_leaves_no_number(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:

            async def rolled_back() -> None:
                async with env.pool.connection() as conn, conn.transaction():
                    assert await env.sync.enqueue(conn, "metrics", {"samples": []}) == 1
                    raise KeyError

            with pytest.raises(KeyError):
                await rolled_back()
            # a non-autocommit connection without a transaction block is fine: the caller commits
            async with env.pool.connection() as conn:
                assert await env.sync.enqueue(conn, "metrics", {"samples": []}) == 1

    run(body)


def test_enqueue_refusals(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            key = env.sync.storage_key
            async with await psycopg.AsyncConnection.connect(harness.dsn, autocommit=True) as conn:
                with pytest.raises(ValueError, match="open transaction"):
                    await outbox.enqueue(conn, key, "metrics", {"samples": []})
                async with conn.transaction():
                    with pytest.raises(ValueError, match="unknown outbox item kind"):
                        await outbox.enqueue(conn, key, "credential", {})
                    with pytest.raises(ValueError, match="JSON object"):
                        await outbox.enqueue(conn, key, "metrics", [])  # type: ignore[arg-type]
                    shapes: list[dict[str, Any]] = [
                        {"header": {}},
                        {"header": [], "body": {}},
                        {"header": {}, "body": []},
                    ]
                    for bad in shapes:
                        with pytest.raises(ValueError, match="event draft is"):
                            await outbox.enqueue(conn, key, "event", bad)
                    with pytest.raises(ValueError, match="exactly the keys"):
                        await outbox.enqueue(conn, key, "event", {"header": {"id": uuid()}, "body": {}})
                    with pytest.raises(CryptoError, match="header_invalid"):
                        await outbox.enqueue(conn, key, "event", draft(id="not-a-uuid"))
                    with pytest.raises(CryptoError, match="size_exceeded"):
                        await outbox.enqueue(conn, key, "event", draft(body={"text": "x" * (256 * 1024)}))
                    with pytest.raises(CryptoError, match="size_exceeded"):
                        await outbox.enqueue(
                            conn, key, "event", draft(kind="session.terminal", body={"text": "x" * (64 * 1024)})
                        )
                    assert await outbox.last_origin_seq(conn) == 0

    run(body)


def test_tampered_rows_do_not_open(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await env.enqueue("metrics", {"samples": []})
            await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn:
                # swap the ciphertexts of two rows: the AAD binds each to its origin_seq
                await conn.execute(
                    "UPDATE runner.outbox SET body_ct = (SELECT body_ct FROM runner.outbox WHERE origin_seq = 2) "
                    "WHERE origin_seq = 1"
                )
                with pytest.raises(CryptoError, match="decrypt_failed"):
                    await outbox.load_batch(conn, env.sync.storage_key, 0, 10)

    run(body)


def test_adopt_hub_position_renumbers_items_written_before_the_position_was_known(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            bodies = [{"samples": []}, fixture_body("quota"), fixture_body("audit")]
            for index, item in enumerate(bodies):
                await env.enqueue(["metrics", "quota", "audit"][index], item)
            async with env.pool.connection() as conn:
                key = env.sync.storage_key
                assert await outbox.pushed_origin_seq(conn) is None
                assert await outbox.backlog(conn) == 3
                assert await outbox.adopt_hub_position(conn, key, 10) is True
                items = await outbox.load_batch(conn, key, 0, 10)
                assert [item.origin_seq for item in items] == [11, 12, 13]
                assert [item.body for item in items] == bodies
                assert await outbox.pushed_origin_seq(conn) == 10
                assert await outbox.backlog(conn) == 3
                # the known position is authoritative
                assert await outbox.adopt_hub_position(conn, key, 99) is False
                for bad in (-1, True, "3"):
                    with pytest.raises(ValueError, match="non-negative integer"):
                        await outbox.adopt_hub_position(conn, key, bad)  # type: ignore[arg-type]
            assert await env.enqueue("metrics", {"samples": []}) == 14

    run(body)


def test_after_a_wipe_with_an_empty_outbox_the_numbering_continues_behind_the_hub(harness: Harness) -> None:
    """DR-01 (E2E-33): a wiped store adopts the hub's position before it wrote any item; the next item is
    `last_origin_seq + 1`, not 1 (an item numbered below the adopted position would never be pushed)."""

    async def body() -> None:
        async with harness.env() as env:
            async with env.pool.connection() as conn:
                assert await outbox.adopt_hub_position(conn, env.sync.storage_key, 139) is True
                assert await outbox.backlog(conn) == 0
            assert await env.enqueue("metrics", {"samples": []}) == 140
            assert await env.enqueue("metrics", {"samples": []}) == 141
            async with env.pool.connection() as conn:
                items = await outbox.load_batch(conn, env.sync.storage_key, 139, 10)
                assert [item.origin_seq for item in items] == [140, 141]
                assert await outbox.backlog(conn) == 2

    run(body)


def test_adopt_zero_keeps_numbering(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn:
                assert await outbox.adopt_hub_position(conn, env.sync.storage_key, 0) is True
                items = await outbox.load_batch(conn, env.sync.storage_key, 0, 10)
                assert [item.origin_seq for item in items] == [1]
                assert await outbox.pushed_origin_seq(conn) == 0

    run(body)


def test_state_helpers(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            for _ in range(4):
                await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn:
                with pytest.raises(ValueError, match="lower snake case"):
                    await outbox.set_state(conn, "Bad Key", "x")
                await outbox.set_state(conn, outbox.PUSHED, "1")
                await outbox.acknowledge(conn, 3)
                assert await outbox.pushed_origin_seq(conn) == 3
                await outbox.acknowledge(conn, 2)  # never backwards
                assert await outbox.pushed_origin_seq(conn) == 3
                cur = await conn.execute(
                    "SELECT origin_seq FROM runner.outbox WHERE pushed_at IS NOT NULL ORDER BY origin_seq"
                )
                assert [row[0] for row in await cur.fetchall()] == [2, 3]
                await outbox.rewind(conn, 2)
                assert await outbox.pushed_origin_seq(conn) == 1
                assert await outbox.backlog(conn) == 3
                assert await outbox.sync_fatal(conn) is None
                await outbox.set_state(conn, outbox.FATAL, "origin_seq_conflict")
                assert await outbox.sync_fatal(conn) == "origin_seq_conflict"
                await outbox.clear_fatal(conn)
                assert await outbox.sync_fatal(conn) is None
                # store_sealed touches event items only
                await outbox.store_sealed(conn, 1, {"x": 1})
                items = await outbox.load_batch(conn, env.sync.storage_key, 0, 1)
                assert items[0].sealed is None

    run(body)


def test_item_kinds_match_the_table_check() -> None:
    folder = Path(__file__).resolve().parents[4] / "src/agent_runner/store/migrations"
    sql = (folder / "0001_runner.sql").read_text()
    table = sql.split("CREATE TABLE runner.outbox", 1)[1].split(");", 1)[0]
    # the newest form of the check: `runtime` was added by the billing migration (KOR-31), the items of the model
    # fallback and the DFX key by KOR-50
    marker = "ADD CONSTRAINT outbox_item_kind_check"
    newest = [path for path in sorted(folder.glob("*.sql")) if marker in path.read_text()][-1]
    widened = newest.read_text().split(marker, 1)[1]
    later = {"runtime", "operator_quota", "dfx_key_state", "dfx_key_issued", "dfx_key_usage"}
    for kind in ITEM_KINDS:
        assert f"'{kind}'" in widened
        assert f"'{kind}'" in table or kind in later

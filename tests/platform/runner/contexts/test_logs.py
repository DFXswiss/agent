"""U-RUN-contexts (logs): logs of test runs and long work VM jobs as encrypted objects of kind `log` (KOR-43, ADR-079
section 3; 04-crypto.md section 4.12): sealing, upload with `kind`, a re-upload after the hub expired it, the cache
and the restore candidates, which never see a log."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.logs import Owner, store_log
from agent_runner.crypto import runlog
from agent_runner.crypto.encoding import b64u_decode

from ..prompts.kit import run
from ..reconcile.rig import Rig, scope_key
from ..sessions.kit import ORG, TASK, WG
from .kit import CKit, ckit

REF = "tr-00000000000b"


async def pieces(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def objects(ck: CKit) -> list[store.StoredObject]:
    async with ck.pk.env.pool.connection() as conn:
        return await store.cache_rows(conn)


async def setup(env: Any) -> tuple[CKit, Owner]:
    ck = await ckit(env)
    sid = await ck.pk.add_session(state="working")
    return ck, Owner(ORG, WG, TASK, sid)


def test_a_log_is_sealed_uploaded_and_leaves_the_cache(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)
        first, second = b"a" * (300 * 1024), b"b" * (100 * 1024)
        ref = await store_log(ck.contexts, owner, ref=REF, total=1_000_000, pieces=pieces(first, second))
        assert ref is not None
        assert (ref["bytes"], ref["stored_bytes"], ref["truncated"]) == (1_000_000, len(first) + len(second), True)
        assert ck.contexts.upload_wake.is_set()
        [obj] = await objects(ck)
        assert (obj.kind, obj.state, obj.session_id) == ("log", "sealed", owner.session_id)
        assert await Checkpointer(ck.contexts).evict() == []  # a log waiting for the hub is never evicted
        async with env.pool.connection() as conn:
            assert await store.objects_of_task(conn, TASK) == []  # never a restore candidate
        assert await Checkpointer(ck.contexts).upload_round() == 1
        assert await objects(ck) == []  # the hub holds it: it left the cache
        manifest = ck.store.committed[ref["object_id"]]["manifest"]
        assert (manifest["kind"], manifest["ref"], manifest["chunks"]) == ("log", REF, 2)
        assert (manifest["bytes"], manifest["stored"]) == (1_000_000, len(first) + len(second))
        key = runlog.log_key(scope_key("wg", WG, 1), b64u_decode(manifest["salt"]), ref["object_id"])
        opened = [
            runlog.open_chunk(key, ref["object_id"], index, b64u_decode(ck.store.chunks[(ref["object_id"], index)]))
            for index in range(2)
        ]
        assert b"".join(opened) == first + second
        assert len(opened[0]) == runlog.CHUNK_BYTES

    run(rig, body)


def test_a_short_complete_output_is_not_truncated(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)
        ref = await store_log(ck.contexts, owner, ref="j-00000000000c", total=3, pieces=pieces(b"abc"))
        assert ref is not None
        assert (ref["bytes"], ref["stored_bytes"], ref["truncated"]) == (3, 3, False)

    run(rig, body)


def test_no_output_or_no_workgroup_key_keeps_no_log(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)
        assert await store_log(ck.contexts, owner, ref=REF, total=0, pieces=pieces()) is None
        other = Owner(ORG, "01930043-0000-7000-8000-0000000000ee", TASK, owner.session_id)
        assert await store_log(ck.contexts, other, ref=REF, total=5, pieces=pieces(b"hello")) is None
        assert any("log_key_missing" in line for line in ck.lines)
        assert await objects(ck) == []

    run(rig, body)


def test_a_failing_source_leaves_nothing_behind(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)

        async def broken() -> AsyncIterator[bytes]:
            yield b"x" * (runlog.CHUNK_BYTES + 1)
            raise OSError("vm gone")

        with pytest.raises(OSError, match="vm gone"):
            await store_log(ck.contexts, owner, ref=REF, total=10, pieces=broken())
        assert await objects(ck) == []
        assert not list(ck.contexts.cache.root.glob("*/*"))

    run(rig, body)


def test_a_log_the_hub_expired_is_uploaded_again_under_the_same_id(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)
        ref = await store_log(ck.contexts, owner, ref=REF, total=4, pieces=pieces(b"logs"))
        assert ref is not None
        ck.store.refuse["commit"] = (404, "not_found")
        assert await Checkpointer(ck.contexts).upload_round() == 1
        [obj] = await objects(ck)
        assert (obj.state, obj.uploaded) == ("sealed", 0)
        assert any("upload_expired" in line and "again=1" in line for line in ck.lines)
        del ck.store.refuse["commit"]
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_object SET next_at = NULL")
        assert await Checkpointer(ck.contexts).upload_round() == 1
        assert ref["object_id"] in ck.store.committed
        assert await objects(ck) == []

    run(rig, body)


def test_a_log_the_hub_refuses_for_good_leaves_the_cache(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, owner = await setup(env)
        assert await store_log(ck.contexts, owner, ref=REF, total=4, pieces=pieces(b"logs")) is not None
        ck.store.refuse["put"] = (413, "quota_exceeded")
        assert await Checkpointer(ck.contexts).upload_round() == 1
        assert await objects(ck) == []

    run(rig, body)

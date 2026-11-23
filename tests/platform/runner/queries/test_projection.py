"""U-RUN-queries projection: what the consumer keeps and how kept events are opened, paged, replayed and cut
(`queries/projection.py`, `queries/consumers.py`; 04-crypto.md section 5.12, ADR-039)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.orgs.db import session as store_session
from agent_runner.queries import common, device_api, projection
from agent_runner.queries.consumers import keep_read_event
from agent_runner.queries.rights import ReadRefused

from ..reconcile.rig import Rig, device_keys
from .kit import ORG, RDEV, TASK, WG, RKit, run, sealed_event


def test_keepable_needs_the_object_of_the_kind() -> None:
    task = {"kind": "prompt", "task": TASK, "ref": ""}
    assert projection.keepable(task)
    assert not projection.keepable({**task, "task": ""})
    assert projection.keepable({"kind": "whiteboard.edit", "task": "", "ref": TASK})
    assert not projection.keepable({"kind": "whiteboard.edit", "task": TASK, "ref": "entry"})
    assert not projection.keepable({"kind": "task.meta", "task": TASK, "ref": TASK})


def test_the_consumer_ignores_events_without_their_object() -> None:
    # the inbox's E1 already refuses such headers; the consumer does not rely on it (nothing is stored or looked up)
    event = SimpleNamespace(header={"kind": "prompt", "task": "", "ref": ""})
    asyncio.run(keep_read_event(None, None, event))  # type: ignore[arg-type]


def test_the_consumer_skips_events_of_an_org_the_book_does_not_act_for(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        event = SimpleNamespace(org_id=TASK, header={"kind": "prompt", "task": TASK, "ref": "", "lh": 0})
        await keep_read_event(k.env.context, None, event)  # type: ignore[arg-type]
        assert await k.env.rows("SELECT 1 FROM runner.read_event") == []

    run(rig, body, profiles=False)


def test_cut_keeps_the_longest_fitting_prefix() -> None:
    assert projection.cut("abcdef", projection.json_size("abc")) == "abc"
    assert projection.cut("ü" * 10, projection.json_size("ü" * 3) + 5) == "ü" * 3
    assert projection.cut("abc", 1) == ""


def test_the_store_and_the_hub_need_an_enrolled_device() -> None:
    async def body() -> None:
        context = SimpleNamespace(identity=None)
        with pytest.raises(ReadRefused) as refused:
            await common.store(context).__aenter__()
        assert refused.value.error == "unavailable"
        with pytest.raises(ReadRefused):
            await device_api.task(context, None, TASK)  # type: ignore[arg-type]

    asyncio.run(body())


def test_events_without_their_object_are_not_kept(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.add("alice", "prompt", {"text": "no task", "attachments": []})
        k.events.add("alice", "whiteboard.entry", {"level": "org", "text": "no ref"}, scope="org", scope_id=ORG)
        k.events.prompt(TASK, "kept")
        await k.pull()
        rows = await k.env.rows("SELECT kind FROM runner.read_event ORDER BY scope_seq")
        assert rows == [("prompt",)]

    run(rig, body, profiles=False)


def test_a_repeat_is_kept_once(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sealed = sealed_event(
            k.events.spec, "alice", "prompt", scope="wg", scope_id=WG, label="r", body={"text": "x"}, ts=projection_ts()
        )
        async with store_session(k.env.pool) as conn:
            args: dict[str, Any] = {"header": sealed["header"], "author_sig": "x", "sealed": sealed}
            assert await projection.keep(conn, scope_seq=1, received_at="bad", **args)
            assert not await projection.keep(conn, scope_seq=2, received_at="2026-11-21T12:31:00.000Z", **args)
        rows = await k.env.rows("SELECT scope_seq, received_at FROM runner.read_event")
        # a received time that is not a contract time falls back to the signed time
        assert rows == [(1, projection_ts())]

    run(rig, body, profiles=False)


def projection_ts() -> str:
    return "2026-11-21T12:30:00.000Z"


def test_events_that_do_not_open_are_left_out(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        first = k.events.prompt(TASK, "one")
        k.events.prompt(TASK, "two")
        entry = k.events.board("whiteboard.entry", TASK, {"level": "workgroup", "text": "entry"}, level="workgroup")
        k.events.board("whiteboard.entry", RDEV, {"level": "workgroup"}, level="workgroup")  # no text
        k.events.add("alice", "device.profile", {"label": " "}, scope="org", scope_id=ORG, ref=RDEV)
        await k.pull()
        key = k.env.sync.storage_key
        wrong = device_keys().sig_public
        async with store_session(k.env.pool) as conn:
            kept = {row.event_id: row for row in await projection._rows(conn, "true", [])}
            # an epoch the device holds no key for
            row = kept[first]
            no_key = {**row.sealed, "header": {**row.sealed["header"], "epoch": 9}}
            assert await projection.open_body(conn, key, replace(row, sealed=no_key)) is None
            # another signing key than the author's
            await conn.execute(
                "UPDATE runner.read_event SET author_sig = %s WHERE event_id IN (%s, %s)", (wrong, first, entry)
            )
            items, following = await projection.messages(conn, key, wg_id=WG, task_id=TASK, after=0, limit=5)
            assert ([m["text"] for m in items], following) == (["two"], None)
            assert await projection.entries(conn, key, level="workgroup", scope_id=WG) == []
            # a blank label, and a device without profile
            assert await projection.device_label(conn, key, ORG, RDEV) is None
            assert await projection.device_label(conn, key, ORG, TASK) is None
        assert await k.ok(sid, "get_whiteboard", {"level": "workgroup"}) == {"entries": []}

    run(rig, body, profiles=False)


def test_a_single_message_larger_than_the_budget_is_cut(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.session(1)
        k.events.prompt(TASK, "x" * 5000)
        k.events.prompt(TASK, "next")
        await k.pull()
        async with store_session(k.env.pool) as conn:
            key = k.env.sync.storage_key
            items, following = await projection.messages(
                conn, key, wg_id=WG, task_id=TASK, after=0, limit=5, budget=1000
            )
        assert following == 1
        assert len(items) == 1
        assert items[0]["text"] == "x" * len(items[0]["text"])
        assert projection.json_size(items[0]) <= 1000 < projection.json_size(items[0]) + 2

    run(rig, body, profiles=False)


def test_at_most_a_thousand_entries(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projection, "MAX_ENTRIES", 2)

    async def body(k: RKit) -> None:
        await k.session(1)
        ids = [f"01930006-0000-7000-8000-00000000020{n}" for n in range(3)]
        for entry in ids:
            k.events.board("whiteboard.entry", entry, {"level": "workgroup", "text": entry}, level="workgroup")
        await k.pull()
        async with store_session(k.env.pool) as conn:
            found = await projection.entries(conn, k.env.sync.storage_key, level="workgroup", scope_id=WG)
        assert [e["entry_id"] for e in found] == ids[::-1][:2]

    run(rig, body, profiles=False)

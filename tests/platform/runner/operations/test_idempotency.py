"""KOR-28: the content rule of every operation type with dedupe fields applies whether or not the session passes an
idempotency key (KONZEPT section 11; 03-api.md sections 8.3, 9; 04-crypto.md section 4.10; ADR-012).

The key can only mark a repetition as the same request: reused within the type's window for other content of the
same task and type, the request is refused locally (`params_invalid` at `/idempotency_key`) and registered as
`rejected`, so the hub still records and audits it. A key never opens a slot of its own for a type with dedupe
fields, so a new key cannot publish the same comment twice.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ..reconcile.rig import Rig
from .kit import OKit, run
from .slots import SlotHub
from .test_pipeline import REPO, comment_kit

COMMENT = {"repo": REPO, "number": 7, "body": "Looks good."}
OTHER = {"repo": REPO, "number": 7, "body": "Looks even better."}
KEY_CONFLICT = {
    "code": "params_invalid",
    "message": "The idempotency key was already used for another request of this task.",
    "pointers": ["/idempotency_key"],
}


async def slot_kit(env: Any) -> tuple[OKit, list[Any], SlotHub]:
    kit_, calls = await comment_kit(env)
    hub = SlotHub()
    hub.install(kit_)
    return kit_, calls, hub


def test_a_new_key_does_not_publish_the_same_comment_twice(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT)
        assert first.state == "succeeded"
        # the attack: the session rotates the key for every repetition, also keyed first and plain afterwards
        again = [await kit_.run(sid, "github.comment", COMMENT, key=key) for key in ("k-1", "k-2", "k-3")]
        assert [op.state for op in again] == ["duplicate"] * 3
        assert all(op.duplicate_of == first.id for op in again)
        assert all(op.answer is not None and op.answer["status"] == "duplicate" for op in again)
        assert {request["dedupe"]["tag"] for request in kit_.hub.requests} == {first.dedupe_tag}
        assert len(calls) == 1
        # every repetition is recorded and audited by the hub as a duplicate of the first comment
        assert [entry[1:] for entry in hub.audit] == [("accepted", None)] + [("duplicate", first.id)] * 3
        # the key still travels in the encrypted params event (operation record)
        _, params = kit_.hub.params_event(-1)
        assert params["idempotency_key"] == "k-3"

    run(rig, body)


def test_a_keyed_comment_blocks_the_same_comment_without_a_key(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, _hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        plain = await kit_.run(sid, "github.comment", COMMENT)
        assert (first.state, plain.state, plain.duplicate_of) == ("succeeded", "duplicate", first.id)
        assert len(calls) == 1

    run(rig, body)


def test_the_same_key_with_the_same_content_is_the_same_request(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, _hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        again = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        assert (again.state, again.duplicate_of) == ("duplicate", first.id)
        assert len(calls) == 1

    run(rig, body)


def test_a_reused_key_for_other_content_is_refused_and_audited(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        other = await kit_.run(sid, "github.comment", OTHER, key="k-1")
        assert first.state == "succeeded"
        assert (other.state, other.error_code) == ("rejected", "params_invalid")
        assert other.answer == {"operation_id": other.id, "status": "rejected", "error": KEY_CONFLICT}
        registration = kit_.hub.requests[-1]
        assert (registration["local_check"], registration["error_code"], registration["dedupe"]) == (
            "rejected",
            "params_invalid",
            None,
        )
        assert hub.audit[-1] == (other.id, "rejected", "params_invalid")
        assert len(calls) == 1
        # other content without the key is a new request (the key binds nothing it was not given for)
        plain = await kit_.run(sid, "github.comment", OTHER)
        assert plain.state == "succeeded"
        assert len(calls) == 2

    run(rig, body)


def test_a_key_is_free_again_once_its_request_released_its_slot(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'failed' WHERE id = %s", (first.id,))
        hub.release(first.id)
        other = await kit_.run(sid, "github.comment", OTHER, key="k-1")
        assert other.state == "succeeded"
        assert len(calls) == 2

    run(rig, body)


def test_a_key_is_free_again_after_the_window_of_the_type(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, _hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        await kit_.age(first.id, 86400 - 60)
        inside = await kit_.run(sid, "github.comment", OTHER, key="k-1")
        assert inside.state == "rejected"
        await kit_.age(first.id, 86400 + 1)
        after = await kit_.run(sid, "github.comment", OTHER, key="k-1")
        assert after.state == "succeeded"
        assert len(calls) == 2

    run(rig, body)


def test_a_key_of_another_type_or_task_or_epoch_does_not_conflict(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, calls, _hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", COMMENT, key="k-1")
        title = await kit_.run(sid, "task.set_title", {"title": "Review"}, key="k-1")
        assert title.state == "succeeded"
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET registration = jsonb_set(registration, '{dedupe,epoch}', %s)"
                " WHERE id = %s",
                (Jsonb(99), first.id),
            )
        epoch = await kit_.run(sid, "github.comment", OTHER, key="k-1")
        assert epoch.state == "succeeded"
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET task_id = '01930004-0000-7000-8000-0000000000ff' WHERE id <> %s",
                (first.id,),
            )
        task = await kit_.run(sid, "github.comment", {**OTHER, "body": "Third."}, key="k-1")
        assert task.state == "succeeded"
        assert len(calls) == 3

    run(rig, body)


def test_types_without_dedupe_fields_keep_the_key_slot(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _calls, _hub = await slot_kit(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "task.set_title", {"title": "a"}, key="client-1")
        second = await kit_.run(sid, "task.set_title", {"title": "b"}, key="client-1")
        third = await kit_.run(sid, "task.set_title", {"title": "b"}, key="client-2")
        assert (first.state, second.state, second.duplicate_of) == ("succeeded", "duplicate", first.id)
        assert third.state == "succeeded"
        assert kit_.hub.requests[0]["dedupe"] == kit_.hub.requests[1]["dedupe"]

    run(rig, body)

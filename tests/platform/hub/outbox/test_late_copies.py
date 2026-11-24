"""U-HUB-outbox (late copies, ADR-050, KOR-19): a request the runner has given up may still reach the hub after
later pushes were stored. The hub answers a byte-identical copy `replayed`, refuses a copy whose items were stored
meanwhile with other bytes (resealed) with `409 origin_seq_conflict`, and answers a copy of rejected items `replayed`
without a second `outbox.rejected` audit; nothing of it writes anything (03-api.md sections 1 and 6.1). Two
byte-identical pushes arriving together are processed one after the other, the second `replayed`."""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

from agent_hub.outbox.bodies import RawItem
from agent_hub.outbox.process import process
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.outbox.support import NOT_CONTRACT, Outbox, item, new_vm


def written(outbox: Outbox, published: MemoryPublisher) -> tuple[Any, ...]:
    """Everything a push could have written: position, receipts, audit, events, effects and live frames."""
    desk = outbox.desk
    return (
        outbox.position(),
        outbox.receipts(),
        desk.rows("SELECT seq, action, detail::text FROM hub.audit ORDER BY seq"),
        desk.rows("SELECT id, origin_seq FROM hub.event ORDER BY id"),
        desk.rows("SELECT session_id, requests FROM hub.usage_counter ORDER BY session_id"),
        desk.rows("SELECT id, state FROM hub.vm ORDER BY id"),
        [(entry.frame, dict(entry.data)) for entry in published.items()],
    )


def test_a_copy_arriving_after_later_items_were_stored_is_replayed(outbox: Outbox, published: MemoryPublisher) -> None:
    _task, session = outbox.session()
    copy = [outbox.usage(1, session), outbox.output(2, session), outbox.vm(3, new_vm())]
    outbox.ok(*copy)
    outbox.ok(outbox.usage(4, session), outbox.output(5, session))
    before = written(outbox, published)
    assert outbox.ok(*copy) == {
        "acked_through": 5,
        "results": [
            {"origin_seq": 1, "status": "replayed"},
            {"origin_seq": 2, "status": "replayed"},
            {"origin_seq": 3, "status": "replayed"},
        ],
    }
    assert written(outbox, published) == before


def test_a_copy_with_the_old_bytes_of_a_resealed_item_is_refused_without_effect(
    outbox: Outbox, published: MemoryPublisher
) -> None:
    _task, session = outbox.session()
    copy = [outbox.usage(1, session), outbox.output(2, session, epoch=3), outbox.usage(3, session)]
    assert outbox.statuses(*copy) == [(1, "ok", None), (2, "reseal", None)]
    # the runner seals origin_seq 2 anew and stores it with the next push
    assert outbox.statuses(outbox.output(2, session), outbox.usage(3, session)) == [(2, "ok", None), (3, "ok", None)]
    before = written(outbox, published)
    response = outbox.push(*copy)
    assert (response.status_code, response.json()) == (409, {"error": "origin_seq_conflict"})
    assert written(outbox, published) == before


def test_a_copy_of_rejected_items_is_replayed_without_a_second_audit(
    outbox: Outbox, published: MemoryPublisher
) -> None:
    _task, session = outbox.session()
    broken = item(2, "usage", {"session_id": session})
    copy = [outbox.usage(1, session), broken, outbox.usage(3, session)]
    response = outbox.push(*copy, extensions=NOT_CONTRACT)
    assert [(r["origin_seq"], r["status"]) for r in response.json()["results"]] == [
        (1, "ok"),
        (2, "rejected"),
        (3, "ok"),
    ]
    outbox.ok(outbox.usage(4, session))
    assert outbox.rejected_audit() == [{"origin_seq": 2, "item_kind": "usage", "code": "request_invalid"}]
    before = written(outbox, published)
    late = outbox.push(*copy, extensions=NOT_CONTRACT)
    assert late.status_code == 200, late.text
    assert late.json() == {
        "acked_through": 4,
        "results": [
            {"origin_seq": 1, "status": "replayed"},
            {"origin_seq": 2, "status": "replayed"},
            {"origin_seq": 3, "status": "replayed"},
        ],
    }
    assert outbox.rejected_audit() == [{"origin_seq": 2, "item_kind": "usage", "code": "request_invalid"}]
    assert written(outbox, published) == before


def test_a_push_and_its_copy_arriving_together_are_serialised_by_the_row_lock(outbox: Outbox) -> None:
    """The hedge's usual case: the device has pushed before (its `device_stream` row exists), the first request and
    its byte-identical copy arrive at the same time. The row lock (`FOR UPDATE`) puts them one after the other: one
    stores, the other is `replayed`. (`test_rules.py` covers the first push of a device, where the row is created.)"""
    _task, session = outbox.session()
    outbox.ok(outbox.usage(1, session))
    items = [RawItem.model_validate(outbox.usage(seq, session)) for seq in range(2, 5)]
    hub = outbox.hub
    barrier = threading.Barrier(2, timeout=30)  # a thread that fails before it fails the test, never hangs it
    answers: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def push() -> None:
        try:
            with hub.pool.connection() as connection, connection.transaction():
                barrier.wait()
                outcome = process(
                    connection, UUID(outbox.device.id), items, now=hub.clock(), root_key=hub.config.platform_root_key
                )
                answers.append(outcome.answer())
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=push) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted({entry["status"] for entry in answer["results"]}.pop() for answer in answers) == ["ok", "replayed"]
    assert [answer["acked_through"] for answer in answers] == [4, 4]
    assert outbox.position() == 4
    assert [row[0] for row in outbox.receipts()] == [1, 2, 3, 4]
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(4,)]

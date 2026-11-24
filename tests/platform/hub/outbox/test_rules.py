"""U-HUB-outbox (rule set): `POST /v1/device/outbox` in strict `origin_seq` order - gaps, replays, conflicts, reseal,
poison items, the stream position in the same transaction, concurrent pushes of one device and the limits (03-api.md
sections 1 and 6.1, 04-crypto.md sections 5.12 and 6.4, ADR-013, ADR-030)."""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

import psycopg
import pytest

from agent_hub.errors import HubError
from agent_hub.outbox.bodies import RawItem
from agent_hub.outbox.pacing import BURST, MIN_INTERVAL_SECONDS
from agent_hub.outbox.process import process
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.outbox.support import NOT_CONTRACT, Outbox, item, new_vm


def _error(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json()


def test_a_first_push_stores_every_item_and_the_position(outbox: Outbox) -> None:
    _task, session = outbox.session()
    vm = new_vm()
    body = outbox.ok(outbox.vm(1, vm), outbox.usage(2, session), outbox.output(3, session))
    assert body == {
        "acked_through": 3,
        "results": [
            {"origin_seq": 1, "status": "ok"},
            {"origin_seq": 2, "status": "ok"},
            {"origin_seq": 3, "status": "ok"},
        ],
    }
    assert outbox.position() == 3
    assert outbox.receipts() == [(1, "vm_state", "ok", None), (2, "usage", "ok", None), (3, "event", "ok", None)]
    state = outbox.fleet.client(outbox.device).get("/v1/device/state").json()
    assert state["last_origin_seq"] == 3
    # another device has its own stream
    assert outbox.position(outbox.scene.other_device) == 0


def test_a_gap_refuses_the_push_without_any_effect(outbox: Outbox) -> None:
    _task, session = outbox.session()
    assert _error(outbox.push(outbox.usage(2, session))) == (
        409,
        {"error": "origin_seq_gap", "detail": {"expected": 1}},
    )
    # a gap in the middle of the batch refuses the whole batch: nothing of it is stored
    vm = new_vm()
    response = outbox.push(outbox.vm(1, vm), outbox.usage(2, session), outbox.usage(4, session))
    assert _error(response) == (409, {"error": "origin_seq_gap", "detail": {"expected": 1}})
    assert outbox.position() == 0
    assert outbox.receipts() == []
    assert outbox.desk.rows("SELECT count(*) FROM hub.vm WHERE id = %s", vm) == [(0,)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.usage_counter") == [(0,)]
    # the runner resends from detail.expected
    assert outbox.ok(outbox.vm(1, vm), outbox.usage(2, session))["acked_through"] == 2
    response = outbox.push(outbox.usage(4, session))
    assert _error(response) == (409, {"error": "origin_seq_gap", "detail": {"expected": 3}})
    # items out of order are a gap as well
    assert _error(outbox.push(outbox.usage(4, session), outbox.usage(3, session)))[1]["detail"] == {"expected": 3}


def test_replays_are_acknowledged_once_without_a_second_effect(outbox: Outbox) -> None:
    _task, session = outbox.session()
    first = [outbox.usage(1, session), outbox.usage(2, session, requests=2)]
    outbox.ok(*first)
    assert outbox.statuses(*first) == [(1, "replayed", None), (2, "replayed", None)]
    assert outbox.ok(*first)["acked_through"] == 2
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(3,)]
    # replay plus new items in one batch
    assert outbox.statuses(first[1], outbox.usage(3, session), outbox.usage(4, session)) == [
        (2, "replayed", None),
        (3, "ok", None),
        (4, "ok", None),
    ]
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(5,)]
    # an item repeated inside one batch is stored once
    assert outbox.statuses(outbox.usage(5, session), outbox.usage(5, session)) == [
        (5, "ok", None),
        (5, "replayed", None),
    ]
    assert outbox.position() == 5
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(6,)]


def test_a_conflicting_resend_is_refused_without_any_effect(outbox: Outbox) -> None:
    _task, session = outbox.session()
    outbox.ok(outbox.usage(1, session), outbox.usage(2, session))
    other = outbox.usage(2, session, requests=9)
    assert _error(outbox.push(outbox.usage(3, session), other)) == (409, {"error": "origin_seq_conflict"})
    assert outbox.position() == 2
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(2,)]
    # another kind under a stored origin_seq is a conflict as well
    assert _error(outbox.push(outbox.vm(1, new_vm())))[1] == {"error": "origin_seq_conflict"}
    # a conflict inside one batch
    assert _error(outbox.push(outbox.usage(3, session), outbox.usage(3, session, requests=4)))[1] == {
        "error": "origin_seq_conflict"
    }
    assert outbox.position() == 2


def test_reseal_stops_the_batch_and_the_resealed_item_is_accepted(outbox: Outbox, published: MemoryPublisher) -> None:
    _task, session = outbox.session()
    stale = outbox.output(2, session, epoch=3)
    published.clear()
    body = outbox.ok(outbox.usage(1, session), stale, outbox.usage(3, session), outbox.vm(4, new_vm()))
    assert body == {
        "acked_through": 1,
        "results": [{"origin_seq": 1, "status": "ok"}, {"origin_seq": 2, "status": "reseal"}],
    }
    assert outbox.position() == 1
    assert outbox.receipts() == [(1, "usage", "ok", None)]
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(1,)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.event WHERE origin_seq IS NOT NULL") == [(0,)]
    assert [entry.frame for entry in published.items() if entry.frame == "event"] == []
    # the runner re-seals origin_seq 2 with the current epoch: new bytes, never an origin_seq_conflict
    resealed = outbox.output(2, session)
    assert outbox.statuses(resealed, outbox.usage(3, session)) == [(2, "ok", None), (3, "ok", None)]
    assert outbox.desk.rows("SELECT origin_seq FROM hub.event WHERE origin_device_id = %s", outbox.device.id) == [(2,)]
    # a reseal of the first new item answers the position before it
    assert outbox.ok(outbox.usage(3, session), outbox.output(4, session, epoch=3)) == {
        "acked_through": 3,
        "results": [{"origin_seq": 3, "status": "replayed"}, {"origin_seq": 4, "status": "reseal"}],
    }


def test_poison_items_are_rejected_audited_and_the_sequence_continues(outbox: Outbox) -> None:
    _task, session = outbox.session()
    not_allowed = outbox.event(2, outbox.scene.wg_event(outbox.device, "settings.workgroup"))
    refused_step = outbox.transition(3, session, "requested")
    broken = item(4, "usage", {"session_id": session})
    items = [outbox.usage(1, session), not_allowed, refused_step, broken, outbox.usage(5, session)]
    response = outbox.push(*items, extensions=NOT_CONTRACT)
    assert response.status_code == 200, response.text
    assert [(r["origin_seq"], r["status"], r.get("error")) for r in response.json()["results"]] == [
        (1, "ok", None),
        (2, "rejected", "kind_not_allowed"),
        (3, "rejected", "request_invalid"),
        (4, "rejected", "request_invalid"),
        (5, "ok", None),
    ]
    assert response.json()["acked_through"] == 5
    assert outbox.position() == 5
    assert outbox.receipts() == [
        (1, "usage", "ok", None),
        (2, "event", "rejected", "kind_not_allowed"),
        (3, "session_transition", "rejected", "request_invalid"),
        (4, "usage", "rejected", "request_invalid"),
        (5, "usage", "ok", None),
    ]
    assert outbox.rejected_audit() == [
        {"origin_seq": 2, "item_kind": "event", "code": "kind_not_allowed"},
        {"origin_seq": 3, "item_kind": "session_transition", "code": "request_invalid"},
        {"origin_seq": 4, "item_kind": "usage", "code": "request_invalid"},
    ]
    rows = outbox.desk.rows(
        "SELECT actor_kind, actor_id::text, device_id::text, result, target_kind, target_id FROM hub.audit"
        " WHERE action = 'outbox.rejected' ORDER BY seq"
    )
    assert rows[0] == ("device", outbox.device.id, outbox.device.id, "rejected", "outbox_item", "2")
    # the session kept its state, the refused event is not stored
    assert outbox.desk.rows("SELECT state FROM hub.session WHERE id = %s", session) == [("working",)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.event WHERE origin_device_id IS NOT NULL") == [(0,)]
    # a resent rejected item is a replay; other content under its origin_seq a conflict
    replay = outbox.push(*items, extensions=NOT_CONTRACT)
    assert {r["status"] for r in replay.json()["results"]} == {"replayed"}
    assert _error(outbox.push(outbox.usage(4, session)))[1] == {"error": "origin_seq_conflict"}


def test_events_of_one_batch_are_stored_together_or_sorted_out_one_by_one(outbox: Outbox) -> None:
    _task, session = outbox.session()
    other_task, other_session = outbox.session(outbox.scene.other_device)
    good = [outbox.output(1, session), outbox.output(2, session, kind="session.tool")]
    assert outbox.statuses(*good) == [(1, "ok", None), (2, "ok", None)]
    # E7: a session of the other device; E2-E3 refusals and an author other than the device
    foreign = outbox.output(4, other_session)
    of_writer = outbox.event(5, outbox.scene.wg_event(outbox.scene.writer))
    items = [outbox.output(3, session), foreign, of_writer, outbox.output(6, session)]
    response = outbox.push(*items, extensions=NOT_CONTRACT)  # the contract allows only device-authored kinds
    assert [(r["origin_seq"], r["status"], r.get("error")) for r in response.json()["results"]] == [
        (3, "ok", None),
        (4, "rejected", "author_not_writer"),
        (5, "rejected", "forbidden"),
        (6, "ok", None),
    ]
    assert outbox.desk.rows(
        "SELECT origin_seq FROM hub.event WHERE origin_device_id = %s ORDER BY origin_seq", outbox.device.id
    ) == [(1,), (2,), (3,), (6,)]
    assert other_task
    # an event stored before under its id (a store wipe on the device) is accepted again without a second copy
    again = outbox.event(7, good[0]["body"]["event"])
    assert outbox.statuses(again) == [(7, "ok", None)]
    assert outbox.desk.rows(
        "SELECT count(*) FROM hub.event WHERE id = %s", good[0]["body"]["event"]["header"]["id"]
    ) == [(1,)]
    # an event body that breaks its contract
    broken = item(8, "event", {"event": {"header": {}}, "notify": True})
    response = outbox.push(broken, outbox.output(9, session), extensions=NOT_CONTRACT)
    assert [(r["status"], r.get("error")) for r in response.json()["results"]] == [
        ("rejected", "request_invalid"),
        ("ok", None),
    ]


def test_a_server_side_refusal_fails_the_whole_push(outbox: Outbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _task, session = outbox.session()

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise HubError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("agent_hub.outbox.process.append_events", unavailable)
        assert _error(outbox.push(outbox.usage(1, session), outbox.output(2, session)))[0] == 503
    assert outbox.position() == 0
    # in the one-by-one pass as well (the first call refused for content, the second fails on the server side)
    calls: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise HubError("kind_not_allowed")
        raise HubError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("agent_hub.outbox.process.append_events", flaky)
        assert _error(outbox.push(outbox.output(1, session)))[0] == 503
    # a handler's server-side refusal
    with monkeypatch.context() as patch:
        patch.setattr("agent_hub.usage.outbox.session_on_device", unavailable)
        assert _error(outbox.push(outbox.usage(1, session)))[0] == 503
    assert outbox.position() == 0
    assert outbox.statuses(outbox.usage(1, session)) == [(1, "ok", None)]


def test_a_value_the_database_refuses_is_a_poison_item(outbox: Outbox) -> None:
    _task, session = outbox.session()
    outbox.ok(outbox.usage(1, session))
    outbox.desk.execute("UPDATE hub.usage_counter SET requests = 9223372036854775000 WHERE session_id = %s", session)
    huge = 2**53 - 1
    assert outbox.statuses(outbox.usage(2, session, requests=huge), outbox.usage(3, session, requests=0)) == [
        (2, "rejected", "request_invalid"),
        (3, "ok", None),
    ]


def test_two_concurrent_pushes_of_one_device_are_serialised(outbox: Outbox) -> None:
    _task, session = outbox.session()
    items = [RawItem.model_validate(outbox.usage(seq, session)) for seq in range(1, 6)]
    hub = outbox.hub
    barrier = threading.Barrier(2)
    answers: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def push() -> None:
        try:
            with hub.pool.connection() as connection, connection.transaction():
                barrier.wait()
                outcome = process(
                    connection,
                    UUID(outbox.device.id),
                    items,
                    now=hub.clock(),
                    root_key=hub.config.platform_root_key,
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
    statuses = sorted({entry["status"] for entry in answer["results"]}.pop() for answer in answers)
    assert statuses == ["ok", "replayed"]
    assert [answer["acked_through"] for answer in answers] == [5, 5]
    assert outbox.position() == 5
    assert outbox.desk.rows("SELECT requests FROM hub.usage_counter WHERE session_id = %s", session) == [(5,)]


def test_push_limits_items_size_and_rate(outbox: Outbox) -> None:
    _task, session = outbox.session()
    too_many = [outbox.usage(seq, session) for seq in range(1, 502)]
    response = outbox.push(*too_many, extensions=NOT_CONTRACT)
    assert response.status_code == 400
    assert response.json()["error"] == "request_invalid"
    assert outbox.push(extensions=NOT_CONTRACT).status_code == 400
    filler = "a" * (4 * 1024 * 1024)
    oversized = outbox.push(item(1, "audit", {"x": filler}), extensions=NOT_CONTRACT)
    assert _error(oversized) == (413, {"error": "size_exceeded"})
    # 500 items within 4 MiB are one push
    assert outbox.ok(*[outbox.usage(seq, session) for seq in range(1, 501)])["acked_through"] == 500
    # the pace of one device: a reserve of BURST pushes, refilled at one push per MIN_INTERVAL_SECONDS
    outbox.hub.clock.advance(BURST * MIN_INTERVAL_SECONDS)
    for seq in range(501, 501 + BURST):
        assert outbox.push(outbox.usage(seq, session), pace=False).status_code == 200
    early = outbox.push(outbox.usage(501 + BURST, session), pace=False)
    assert _error(early) == (429, {"error": "rate_limited"})
    assert early.headers["Retry-After"] == "1"
    # the other device is not affected, and the first device may push again once a push's worth has refilled
    assert outbox.push(outbox.vm(1, new_vm()), device=outbox.scene.other_device, pace=False).status_code == 200
    outbox.hub.clock.advance(1.01 * MIN_INTERVAL_SECONDS)  # one push's worth (timestamps are floats)
    assert outbox.push(outbox.usage(501 + BURST, session), pace=False).status_code == 200
    assert outbox.position() == 501 + BURST


def test_envelope_refusals_and_device_states(outbox: Outbox) -> None:
    _task, session = outbox.session()
    for bad in (
        {"origin_seq": 0, "item_kind": "usage", "body": {}},
        {"origin_seq": 1, "item_kind": "credential", "body": {}},
        {"origin_seq": "1", "item_kind": "usage", "body": {}},
        {"origin_seq": 1, "item_kind": "usage", "body": []},
        {"origin_seq": 1, "item_kind": "usage", "body": {}, "extra": 1},
    ):
        assert outbox.push(bad, extensions=NOT_CONTRACT).status_code == 400, bad
    assert outbox.position() == 0
    # a pending device may not push; an unsigned request is unauthorized
    outbox.desk.execute("UPDATE hub.device SET state = 'pending_certification' WHERE id = %s", outbox.device.id)
    assert _error(outbox.push(outbox.usage(1, session))) == (403, {"error": "forbidden"})
    unsigned = outbox.hub.anon.post("/v1/device/outbox", json={"items": [outbox.usage(1, session)]})
    assert unsigned.status_code == 401


def test_malformed_values_are_poison_items_not_a_blocked_stream(outbox: Outbox) -> None:
    _task, session = outbox.session()
    client = outbox.fleet.client(outbox.device)
    surrogate = (
        '{"items":[{"origin_seq":1,"item_kind":"audit","body":{"action":"hook.denied","result":"ok",'
        '"target_kind":"session","target_id":"x","detail":{"tool":"\\ud800","rule":3}}}]}'
    )
    infinite = (
        '{"items":[{"origin_seq":2,"item_kind":"metrics","body":{"samples":[{"entity_kind":"device","entity_id":"'
        + outbox.device.id
        + '","ts":"'
        + outbox.now()
        + '","cpu_ratio":Infinity,"mem_used_bytes":null,"mem_limit_bytes":null,"disk_used_bytes":null,'
        '"disk_limit_bytes":null,"net_rx_bytes":null,"net_tx_bytes":null,"uptime_seconds":null,"reachable":true,'
        '"state":"up","free_vm_slots":null,"free_mem_bytes":null}]}}]}'
    )
    for content in (surrogate, infinite):
        outbox.hub.clock.advance(0.3)
        response = client.post(
            "/v1/device/outbox",
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            extensions=NOT_CONTRACT,
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "rejected"
    assert outbox.position() == 2
    # optional fields may be left out but never be null
    nulls = [
        outbox.transition(3, session, "idle", agent_version=None),
        item(
            4,
            "operation_transition",
            {"operation_id": session, "state": "executing", "at": outbox.now(), "retryable": None},
        ),
        outbox.audit(5, "runner.alert", {"alert": "x", "value": None}, task_id=None),
    ]
    response = outbox.push(*nulls, extensions=NOT_CONTRACT)
    assert [(r["status"], r.get("error")) for r in response.json()["results"]] == [("rejected", "request_invalid")] * 3
    # a rejected item repeated in the same push is a replay of it
    broken = item(6, "usage", {"session_id": session})
    response = outbox.push(broken, broken, extensions=NOT_CONTRACT)
    assert [r["status"] for r in response.json()["results"]] == ["rejected", "replayed"]
    for detail in outbox.rejected_audit():
        contracts_registry().validate("audit/outbox.rejected.json", detail)


def test_a_deadlock_victim_is_run_once_more(outbox: Outbox, monkeypatch: pytest.MonkeyPatch) -> None:
    _task, session = outbox.session()
    calls: list[int] = []

    def victim(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) <= failures:
            raise psycopg.errors.DeadlockDetected("deadlock")
        return process(*args, **kwargs)

    monkeypatch.setattr("agent_hub.outbox.routes.process", victim)
    failures = 1
    assert outbox.statuses(outbox.usage(1, session)) == [(1, "ok", None)]
    assert len(calls) == 2
    failures, calls[:] = 2, []
    assert outbox.push(outbox.usage(2, session)).status_code == 503
    assert outbox.position() == 1

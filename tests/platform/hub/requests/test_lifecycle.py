"""U-HUB-requests (R-9.1, R-4.2): device requests between a user and one device (03-api.md sections 4.10, 6.1, 5,
6.2; 02-data-model.md section 2.7; ADR-035).

Creation (eligibility, signature, idempotency), delivery, responses (`seq`, `final`, retries), the user's view, the
replacement of an active request of the same kind, follow-ups, expiry after 15 minutes with the fake clock, failure of
requests whose device is no longer eligible, the `requests` section of `GET /v1/device/state` and the live frames.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.requests import jobs, store
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import SignedClient
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor, new_id

NO_CONTRACT = {"contract": False}
FIFTEEN_MINUTES = 15 * 60


def _send(desk: Desk, user: Actor, device: Actor, kind: str = "github.connect", **options: Any) -> str:
    envelope = desk.request(user, device, kind, **options)
    response = desk.send(user, envelope)
    assert response.status_code == 200, response.text
    assert response.json() == {"request_id": envelope["header"]["id"], "state": "pending"}
    return str(envelope["header"]["id"])


def _respond(client: SignedClient, request_id: str, envelope: Json, **options: Any) -> Any:
    return client.post(f"/v1/device/requests/{request_id}/responses", json={"envelope": envelope}, **options)


def _requests(client: SignedClient) -> list[str]:
    return [item["request_id"] for item in client.get("/v1/device/state").json()["requests"]]


def _expire(desk: Desk) -> int:
    return jobs.expire(desk.hub.app.state.hub)


def test_create_deliver_respond_answer(desk: Desk, published: MemoryPublisher) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _send(desk, writer, device)
    assert [(item.frame, str(item.audience.id), dict(item.data)) for item in published.items()] == [
        ("request", device.id, {"request_id": request_id})
    ]
    client = desk.client(device)
    assert _requests(client) == [request_id]
    fetched = client.get(f"/v1/device/requests/{request_id}")
    assert fetched.status_code == 200
    assert fetched.json()["envelope"]["header"]["id"] == request_id
    assert desk.state(request_id) == "delivered"
    assert client.get(f"/v1/device/requests/{request_id}").status_code == 200
    published.clear()
    first = desk.response(device, writer, request_id, seq=1)
    assert _respond(client, request_id, first).json() == {}
    assert desk.state(request_id) == "delivered"
    second = desk.response(device, writer, request_id, seq=2, final=True, body={"step": "done"})
    assert _respond(client, request_id, second).status_code == 200
    assert desk.state(request_id) == "answered"
    frames = [(item.frame, item.audience.kind, str(item.audience.id), dict(item.data)) for item in published.items()]
    assert frames == [
        ("device_response", "user", writer.id, {"request_id": request_id, "seq": 1}),
        ("device_response", "user", writer.id, {"request_id": request_id, "seq": 2}),
    ]
    view = writer.browser.get(f"/v1/device-requests/{request_id}").json()
    assert view["state"] == "answered"
    assert view["kind"] == "github.connect"
    assert view["device_id"] == device.id
    assert view["expires_at"] == "2026-11-21T12:15:00.123Z"
    assert [item["envelope"] for item in view["responses"]] == [first, second]
    assert [item["seq"] for item in view["responses"]] == [1, 2]
    assert _requests(client) == []
    actions = [
        row[0]
        for row in desk.world.rows("SELECT action FROM hub.audit WHERE action LIKE 'device_request.%%' ORDER BY seq")
    ]
    assert actions == ["device_request.pending", "device_request.delivered", "device_request.answered"]
    [(actor_kind, actor_id, device_id, target_kind, target_id, detail)] = desk.audit("device_request.pending")
    assert (actor_kind, str(actor_id), str(device_id), target_kind, target_id) == (
        "user",
        writer.id,
        device.id,
        "device_request",
        request_id,
    )
    assert detail == {"kind": "github.connect", "error_code": None}


def test_retries_and_conflicts(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    envelope = desk.request(writer, device)
    assert desk.send(writer, envelope).json()["state"] == "pending"
    replay = desk.send(writer, envelope)
    assert replay.json() == {"request_id": envelope["header"]["id"], "state": "pending", "replayed": True}
    other = desk.request(writer, device, request_id=envelope["header"]["id"], body={"note": "other"})
    assert desk.send(writer, other).json() == {"error": "id_conflict"}
    foreign = desk.request(desk.scene.reader, device, request_id=envelope["header"]["id"])
    assert desk.send(desk.scene.reader, foreign).json() == {"error": "id_conflict"}
    assert len(desk.audit("device_request.pending")) == 1


def test_response_sequence_rules(desk: Desk, published: MemoryPublisher) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _send(desk, writer, device)
    client = desk.client(device)
    gap = _respond(client, request_id, desk.response(device, writer, request_id, seq=2))
    assert gap.status_code == 409
    assert gap.json() == {"error": "seq_conflict", "detail": {"expected_seq": 1}}
    first = desk.response(device, writer, request_id, seq=1)
    assert _respond(client, request_id, first).status_code == 200
    assert desk.state(request_id) == "delivered"
    published.clear()
    assert _respond(client, request_id, first).status_code == 200
    assert published.items() == []
    changed = desk.response(device, writer, request_id, seq=1, body={"step": "other"})
    assert _respond(client, request_id, changed).json() == {"error": "seq_conflict", "detail": {"expected_seq": 2}}
    final = desk.response(device, writer, request_id, seq=2, final=True)
    assert _respond(client, request_id, final).status_code == 200
    late = desk.response(device, writer, request_id, seq=3)
    assert _respond(client, request_id, late).json() == {"error": "forbidden"}
    assert _respond(client, request_id, final).status_code == 200


@pytest.mark.parametrize(
    ("case", "status", "error"),
    [
        ("in_reply_to", 400, "header_invalid"),
        ("kind", 400, "header_invalid"),
        ("user", 400, "header_invalid"),
        ("signer", 401, "signature_invalid"),
        ("other_device", 404, "not_found"),
        ("unknown", 404, "not_found"),
        ("bad_id", 404, "not_found"),
        ("form", 400, "header_invalid"),
    ],
)
def test_responses_refused(desk: Desk, case: str, status: int, error: str) -> None:
    writer, device, other = desk.scene.writer, desk.scene.device, desk.scene.other_device
    request_id = _send(desk, writer, device)
    client, target = desk.client(device), request_id
    envelope = desk.response(device, writer, request_id)
    if case == "in_reply_to":
        envelope = desk.response(device, writer, _send(desk, writer, device, "mailbox.check"))
    elif case == "kind":
        envelope = desk.response(device, writer, request_id, "claude.login")
    elif case == "user":
        envelope = desk.response(device, desk.scene.reader, request_id)
    elif case == "signer":
        envelope = desk.response(other, writer, request_id, header={"device": device.id})
    elif case == "other_device":
        client = desk.client(other)
        envelope = desk.response(other, writer, request_id)
    elif case == "unknown":
        target = new_id()
    elif case == "bad_id":
        target = "nope"
    else:
        envelope = {"header": envelope["header"], "ct": envelope["ct"]}
    response = _respond(client, target, envelope, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (status, {"error": error})


@pytest.mark.parametrize(
    ("case", "status", "error"),
    [
        ("stranger_device", 403, "device_not_assigned"),
        ("unknown_device", 403, "device_not_assigned"),
        ("other_user", 403, "forbidden"),
        ("signature", 401, "signature_invalid"),
        ("form", 400, "header_invalid"),
        ("unregistered", 403, "forbidden"),
    ],
)
def test_creation_refused(desk: Desk, case: str, status: int, error: str) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    user = writer
    if case == "stranger_device":
        envelope = desk.request(writer, desk.stranger_device())
    elif case == "unknown_device":
        envelope = desk.request(writer, Actor(new_id(), "device", device.identity))
    elif case == "other_user":
        envelope = desk.request(desk.scene.reader, device, signer=writer)
    elif case == "signature":
        envelope = desk.request(writer, device, signer=desk.scene.reader)
    elif case == "form":
        envelope = {"header": desk.request(writer, device)["header"], "sig": "x"}
    else:
        user = desk.world.user(registered=False)
        envelope = desk.request(user, device)
    response = desk.send(user, envelope, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (status, {"error": error})
    assert desk.world.rows("SELECT 1 FROM hub.device_request") == []


def test_the_user_view_is_private(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _send(desk, writer, device)
    assert desk.scene.reader.browser.get(f"/v1/device-requests/{request_id}").json() == {"error": "not_found"}
    assert writer.browser.get(f"/v1/device-requests/{new_id()}").status_code == 404
    bad = writer.browser.get("/v1/device-requests/nope", extensions=NO_CONTRACT)
    assert bad.status_code == 404
    assert desk.client(desk.scene.other_device).get(f"/v1/device/requests/{request_id}").status_code == 404
    assert desk.client(device).get("/v1/device/requests/nope", extensions=NO_CONTRACT).status_code == 404
    assert desk.state(request_id) == "pending"


def test_a_new_request_replaces_the_active_one_of_its_kind(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    first = _send(desk, writer, device)
    follow_up = _send(desk, writer, device, follows=first)
    other_kind = _send(desk, writer, device, "credential.check")
    other_device = _send(desk, writer, desk.scene.other_device)
    second = _send(desk, writer, device)
    assert desk.state(first) == "failed"
    assert desk.state(follow_up) == "failed"
    assert desk.state(other_kind) == "pending"
    assert desk.state(other_device) == "pending"
    assert desk.state(second) == "pending"
    failed = desk.audit("device_request.failed")
    assert [(row[0], str(row[1]), row[4]) for row in failed] == [
        ("user", writer.id, first),
        ("user", writer.id, follow_up),
    ]
    assert _requests(desk.client(device)) == [other_kind, second]
    response = _respond(desk.client(device), first, desk.response(device, writer, first))
    assert response.json() == {"error": "forbidden"}


def test_roots_of_different_slots_run_side_by_side(desk: Desk, published: MemoryPublisher) -> None:
    """KOR-73, ADR-100 section 2: logins of different subscriptions (`slot`) never replace each other; a new root of
    the same slot replaces the active one; a slot sent with a follow-up is not kept."""
    writer, device = desk.scene.writer, desk.scene.device
    slots = [new_id() for _ in range(5)]

    def send(slot: str | None, **options: Any) -> str:
        envelope = desk.request(writer, device, "claude.login", **options)
        body: Json = {"envelope": envelope} if slot is None else {"envelope": envelope, "slot": slot}
        response = writer.browser.post("/v1/device-requests", json=body)
        assert response.status_code == 200, response.text
        return str(envelope["header"]["id"])

    roots = [send(slot) for slot in slots]
    unslotted = send(None)
    assert [desk.state(root) for root in roots] == ["pending"] * 5
    follow_up = send(slots[0], follows=roots[0], body={"code": "authorization"})
    assert desk.world.rows("SELECT slot FROM hub.device_request WHERE id = %s", follow_up) == [(None,)]
    stored = desk.world.rows("SELECT slot::text FROM hub.device_request WHERE id = %s", roots[1])
    assert stored == [(slots[1],)]
    published.clear()
    again = send(slots[2])
    assert desk.state(roots[2]) == "failed"
    assert [desk.state(root) for root in (roots[0], roots[1], roots[3], roots[4], unslotted, follow_up)] == [
        "pending"
    ] * 6
    assert [(item.frame, dict(item.data)) for item in published.items()] == [
        ("reconcile", {}),
        ("request", {"request_id": again}),
    ]
    second = send(None)
    assert (desk.state(unslotted), desk.state(second), desk.state(roots[0])) == ("failed", "pending", "pending")
    refused = writer.browser.post(
        "/v1/device-requests",
        json={"envelope": desk.request(writer, device, "claude.login"), "slot": "not-a-slot"},
        extensions=NO_CONTRACT,
    )
    assert refused.status_code == 400


def test_a_final_response_to_a_follow_up_answers_the_chain(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    root = _send(desk, writer, device, "claude.login")
    client = desk.client(device)
    assert _respond(client, root, desk.response(device, writer, root, "claude.login")).status_code == 200
    follow_up = _send(desk, writer, device, "claude.login", follows=root, body={"code": "authorization"})
    final = desk.response(device, writer, follow_up, "claude.login", final=True)
    assert _respond(client, follow_up, final).status_code == 200
    assert (desk.state(root), desk.state(follow_up)) == ("answered", "answered")


@pytest.mark.parametrize("case", ["unknown", "answered", "expired", "other_kind", "other_device", "nested", "foreign"])
def test_follow_ups_need_an_open_root(desk: Desk, case: str) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    root = _send(desk, writer, device, "claude.login")
    user, target, follows = writer, device, root
    if case == "unknown":
        follows = new_id()
    elif case == "answered":
        final = desk.response(device, writer, root, "claude.login", final=True)
        assert _respond(desk.client(device), root, final).status_code == 200
    elif case == "expired":
        desk.hub.clock.advance(FIFTEEN_MINUTES)
    elif case == "other_kind":
        follows = _send(desk, writer, device, "mailbox.check")
    elif case == "other_device":
        target = desk.scene.other_device
    elif case == "nested":
        follows = _send(desk, writer, device, "claude.login", follows=root)
    else:
        user = desk.scene.reader
    envelope = desk.request(user, target, "claude.login", follows=follows)
    response = desk.send(user, envelope)
    assert response.status_code == 400
    assert response.json() == {"error": "params_invalid", "detail": {"pointers": ["/envelope/header/follows"]}}


def test_expiry_after_fifteen_minutes(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    root = _send(desk, writer, device, "claude.login")
    follow_up = _send(desk, writer, device, "claude.login", follows=root)
    desk.hub.clock.advance(60)
    lone = _send(desk, writer, device, "mailbox.check")
    lone_follow_up = _send(desk, writer, device, "mailbox.check", follows=lone)
    desk.hub.clock.advance(FIFTEEN_MINUTES - 61)
    assert _expire(desk) == 0
    desk.hub.clock.advance(1)
    view = writer.browser.get(f"/v1/device-requests/{root}").json()
    assert view["state"] == "expired"
    assert desk.state(root) == "pending"
    late = _respond(desk.client(device), root, desk.response(device, writer, root, "claude.login"))
    assert late.json() == {"error": "forbidden"}
    assert _requests(desk.client(device)) == [lone, lone_follow_up]
    assert _expire(desk) == 2
    assert (desk.state(root), desk.state(follow_up)) == ("expired", "expired")
    expired = desk.audit("device_request.expired")
    assert [(row[0], row[1], row[4]) for row in expired] == [("hub", None, root), ("hub", None, follow_up)]
    desk.hub.clock.advance(59)
    assert _expire(desk) == 0
    desk.hub.clock.advance(1)
    assert _expire(desk) == 2
    assert (desk.state(lone), desk.state(lone_follow_up)) == ("expired", "expired")
    assert [job.name for job in jobs.JOBS] == ["requests.expire"]


def test_a_new_request_closes_the_users_expired_ones_first(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    first = _send(desk, writer, device)
    desk.hub.clock.advance(FIFTEEN_MINUTES)
    second = _send(desk, writer, device)
    assert (desk.state(first), desk.state(second)) == ("expired", "pending")
    assert desk.audit("device_request.failed") == []


def test_requests_of_a_device_that_lost_eligibility_fail(desk: Desk) -> None:
    writer, reader, device = desk.scene.writer, desk.scene.reader, desk.scene.device
    lost = _send(desk, writer, device)
    kept = _send(desk, reader, device)
    with desk.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.wg_access SET revoked_seq = 999 WHERE user_id = %s", (writer.id,))
    client = desk.client(device)
    assert _requests(client) == [kept]
    assert client.get(f"/v1/device/requests/{lost}").status_code == 404
    assert _expire(desk) == 1
    assert (desk.state(lost), desk.state(kept)) == ("failed", "pending")
    [(actor_kind, _actor, _device, _kind, target_id, detail)] = desk.audit("device_request.failed")
    assert (actor_kind, target_id, detail) == (
        "hub",
        lost,
        {"kind": "github.connect", "error_code": "device_not_assigned"},
    )


def test_a_racing_insert_of_the_same_id_is_an_id_conflict(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another user's request with the same ID committed between the lookup and the insert (simulated by a lookup
    that misses it)."""
    writer, reader, device = desk.scene.writer, desk.scene.reader, desk.scene.device
    first = _send(desk, writer, device)
    original = store.load

    def missing(connection: Any, request_id: Any, *, lock: bool = False) -> Any:
        return None if str(request_id) == first else original(connection, request_id, lock=lock)

    monkeypatch.setattr(store, "load", missing)
    response = desk.send(reader, desk.request(reader, device, request_id=first))
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


def test_a_follow_up_never_outlives_its_root(desk: Desk) -> None:
    """Review M1: a follow-up expires with its root, also before the job runs."""
    writer, device = desk.scene.writer, desk.scene.device
    root = _send(desk, writer, device, "claude.login")
    desk.hub.clock.advance(10 * 60)
    follow_up = _send(desk, writer, device, "claude.login", follows=root)
    view = writer.browser.get(f"/v1/device-requests/{follow_up}").json()
    assert view["expires_at"] == "2026-11-21T12:15:00.123Z"
    desk.hub.clock.advance(5 * 60 + 5)
    client = desk.client(device)
    assert _requests(client) == []
    final = desk.response(device, writer, follow_up, "claude.login", final=True)
    assert _respond(client, follow_up, final).json() == {"error": "forbidden"}
    record = desk.record(writer, "claude_subscription", author=device)
    put = client.put(
        f"/v1/device/credentials/{writer.id}/claude_subscription",
        json={"record": record, "expected_version": 0, "request_id": follow_up},
    )
    assert put.json() == {"error": "forbidden"}
    assert writer.browser.get(f"/v1/device-requests/{root}").json()["state"] == "expired"
    assert _expire(desk) == 2
    assert (desk.state(root), desk.state(follow_up)) == ("expired", "expired")


def test_a_replacement_sends_a_reconcile_hint(desk: Desk, published: MemoryPublisher) -> None:
    """Review M5: the device learns at once that the replaced flow is gone."""
    writer, device = desk.scene.writer, desk.scene.device
    _send(desk, writer, device)
    published.clear()
    second = _send(desk, writer, device)
    assert [(item.frame, dict(item.data)) for item in published.items()] == [
        ("reconcile", {}),
        ("request", {"request_id": second}),
    ]


def test_kinds_outside_the_closed_list_are_refused(desk: Desk) -> None:
    """Review M2."""
    writer, device = desk.scene.writer, desk.scene.device
    response = desk.send(writer, desk.request(writer, device, "foo.bar"), extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (400, {"error": "header_invalid"})


def test_a_pull_status_request_is_routed_like_every_kind(desk: Desk) -> None:
    """KOR-48, ADR-084 section 2: `github.pull_status` is a kind of the closed list (hub and table)."""
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _send(desk, writer, device, "github.pull_status")
    assert desk.world.rows("SELECT kind FROM hub.device_request WHERE id = %s", request_id) == [("github.pull_status",)]
    body = {"step": "failed", "code": "forbidden"}
    final = desk.response(device, writer, request_id, "github.pull_status", final=True, body=body)
    assert _respond(desk.client(device), request_id, final).status_code == 200
    assert desk.state(request_id) == "answered"


def test_envelopes_and_responses_within_the_contract_limits(desk: Desk) -> None:
    """Review M3: `ct` at most 349526 characters, at most 100 responses per request."""
    writer, device = desk.scene.writer, desk.scene.device
    big = desk.request(writer, device, body={"note": "x" * 300000})
    assert desk.send(writer, big, extensions=NO_CONTRACT).json() == {"error": "size_exceeded"}
    request_id = _send(desk, writer, device)
    client = desk.client(device)
    large = desk.response(device, writer, request_id, body={"step": "x" * 300000})
    assert _respond(client, request_id, large, extensions=NO_CONTRACT).json() == {"error": "size_exceeded"}
    for seq in range(1, 101):
        assert _respond(client, request_id, desk.response(device, writer, request_id, seq=seq)).status_code == 200
    extra = _respond(client, request_id, desk.response(device, writer, request_id, seq=101))
    assert (extra.status_code, extra.json()) == (400, {"error": "size_exceeded"})
    assert len(writer.browser.get(f"/v1/device-requests/{request_id}").json()["responses"]) == 100


def test_a_response_to_a_follow_up_locks_the_root_first(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    root = _send(desk, writer, device, "claude.login")
    follow_up = _send(desk, writer, device, "claude.login", follows=root)
    response = _respond(desk.client(device), follow_up, desk.response(device, writer, follow_up, "claude.login"))
    assert response.status_code == 200
    assert (desk.state(root), desk.state(follow_up)) == ("pending", "delivered")

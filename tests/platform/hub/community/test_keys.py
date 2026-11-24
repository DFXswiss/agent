"""Distribution of the community key (14-community.md sections 3 and 4.2, ADR-069 decision 3)."""

from __future__ import annotations

import secrets
import threading
from typing import Any

import httpx

from agent_hub.community import crypto as fmt
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import x25519_public_raw
from agent_hub.stream.publish import ALL, MemoryPublisher
from tests.platform.hub.community.conftest import Community, Member, frames, unchecked
from tests.platform.hub.orgs.support import new_id


def query(community: Community, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with community.hub.pool.connection() as connection:
        return connection.execute(sql, params).fetchall()


def test_a_fresh_user_sees_no_epoch_and_is_no_member(community: Community) -> None:
    member = community.member(join=False)
    state = member.state()
    assert state == {
        "epoch": None,
        "wrap": None,
        "request": None,
        "member": {"joined": False, "muted": False, "posted": False, "last_read_seq": 0},
        "moderator": False,
        "unread": 0,
        "head_seq": 0,
        "limits": {"text_chars": 2000, "name_chars": 64, "burst": 5, "refill_seconds": 6},
    }
    member.join()
    member.join()  # idempotent
    assert member.state()["member"]["joined"] is True


def test_the_first_member_creates_epoch_one_with_key_check_and_self_wrap(community: Community) -> None:
    founder = community.founder()
    state = founder.state()
    assert state["epoch"] == {"epoch": 1, "key_check": b64u_encode(fmt.key_check(founder.key or b"", 1))}
    assert state["wrap"] is not None
    assert state["wrap"]["epoch"] == 1
    opened = fmt.unwrap_community_key(
        founder.actor.identity.kem, epoch=1, recipient=founder.id, wrap_text=state["wrap"]["wrap"]
    )
    assert opened == founder.key
    assert query(
        community, "SELECT action, actor_id::text, target_id FROM hub.audit WHERE action LIKE 'community.%%'"
    ) == [("community.key_create", founder.id, "1")]


def test_a_second_epoch_creation_is_refused(community: Community) -> None:
    community.founder()
    late = community.member()
    response = late.create_epoch()
    assert (response.status_code, response.json()) == (409, {"error": "community_epoch_exists"})


def test_epoch_creation_needs_membership_epoch_one_and_a_valid_signature(community: Community) -> None:
    outsider = community.member(join=False)
    assert outsider.create_epoch().json() == {"error": "community_not_member"}
    member = community.member()
    key = secrets.token_bytes(32)
    body = {**member.self_wrap(key, epoch=2), "key_check": b64u_encode(fmt.key_check(key, 2))}
    response = member.client.post("/v1/community/epochs", json=body)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")
    other = community.member()
    forged = {**other.self_wrap(key, recipient=member.id), "key_check": b64u_encode(fmt.key_check(key, 1))}
    response = member.client.post("/v1/community/epochs", json=forged)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})


def test_a_user_without_identity_cannot_create_or_request(community: Community) -> None:
    member = community.member()
    with community.hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.user_identity WHERE user_id = %s", (member.id,))
    assert member.create_epoch().json() == {"error": "forbidden"}
    assert member.request().json() == {"error": "forbidden"}


def test_request_wrap_and_self_wrap(community: Community, published: MemoryPublisher) -> None:
    founder = community.founder()
    member = community.member()
    assert member.request().json() == {"id": member.request_id}
    assert member.state()["request"] == {"id": member.request_id, "wrapped": None}
    listed = founder.client.get("/v1/community/requests").json()["requests"]
    assert listed == [{"id": member.request_id, "kem_pub": listed[0]["kem_pub"]}]
    assert member.id not in str(listed)  # holders never see the user ID
    published.clear()
    wraps_answer = founder.serve(founder.key)
    assert wraps_answer.json() == {"stored": 0, "skipped": 0} or wraps_answer.status_code == 200
    # the first listing leased the request to the founder; the lease ends after 30 s
    community.hub.clock.advance(31)
    answer = founder.serve()
    assert answer.json() == {"stored": 1, "skipped": 0}
    assert frames(published) == [("user", member.id, {"change": "key", "seq": 0})]
    key = member.open_request()
    assert key == founder.key
    state = member.state()
    assert state["request"] is None
    assert state["wrap"] is not None
    actions = [
        row[0] for row in query(community, "SELECT action FROM hub.audit WHERE action LIKE 'community.%%' ORDER BY seq")
    ]
    assert actions == ["community.key_create", "community.key_request", "community.key_wrap", "community.key_wrap"]
    targets = query(
        community, "SELECT target_kind, target_id FROM hub.audit WHERE action = 'community.key_wrap' ORDER BY seq"
    )
    assert targets == [("community_request", member.request_id), ("user", member.id)]


def test_requests_are_leased_to_one_holder_and_never_offered_to_their_owner(community: Community) -> None:
    founder = community.founder()
    second = community.holder(founder)
    waiting = community.member()
    waiting.request()
    first = founder.client.get("/v1/community/requests").json()["requests"]
    assert [item["id"] for item in first] == [waiting.request_id]
    assert second.client.get("/v1/community/requests").json() == {"requests": []}
    community.hub.clock.advance(31)
    assert [i["id"] for i in second.client.get("/v1/community/requests").json()["requests"]] == [waiting.request_id]


def test_a_non_holder_cannot_list_or_wrap(community: Community) -> None:
    founder = community.founder()
    member = community.member()
    member.request()
    assert member.client.get("/v1/community/requests").json() == {"error": "community_no_key"}
    wraps = [{"request": member.request_id, "epoch": 1, "wrap": b64u_encode(bytes(80)), "sig": b64u_encode(bytes(64))}]
    response = member.client.post("/v1/community/requests/wraps", json={"wraps": wraps})
    assert (response.status_code, response.json()) == (409, {"error": "community_no_key"})
    assert founder.state()["wrap"] is not None


def test_a_wrap_of_another_epoch_or_with_a_bad_signature_is_refused(community: Community) -> None:
    founder = community.founder()
    member = community.member()
    member.request()
    stale = [{"request": member.request_id, "epoch": 2, "wrap": b64u_encode(bytes(80)), "sig": b64u_encode(bytes(64))}]
    assert founder.client.post("/v1/community/requests/wraps", json={"wraps": stale}).json() == {"error": "epoch_stale"}
    bad = [{**stale[0], "epoch": 1}]
    response = founder.client.post("/v1/community/requests/wraps", json={"wraps": bad})
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})


def test_a_wrong_key_is_rejected_and_the_wrapper_is_not_accepted_again(
    community: Community, published: MemoryPublisher
) -> None:
    founder = community.founder()
    cheat = community.holder(founder)
    member = community.member()
    member.request()
    community.hub.clock.advance(31)
    assert cheat.serve(secrets.token_bytes(32)).json() == {"stored": 1, "skipped": 0}
    state = member.state()
    assert member.request_key is not None
    wrong = fmt.unwrap_community_key(
        member.request_key, epoch=1, recipient=member.request_id or "", wrap_text=state["request"]["wrapped"]["wrap"]
    )
    assert b64u_encode(fmt.key_check(wrong, 1)) != state["epoch"]["key_check"]
    published.clear()
    assert member.client.post("/v1/community/request/reject", json={}).json() == {"rejected": True}
    assert frames(published) == [("all", str(ALL), {"change": "request", "seq": 0})]
    assert member.state()["request"] == {"id": member.request_id, "wrapped": None}
    assert member.client.post("/v1/community/request/reject", json={}).json() == {"error": "not_found"}
    community.hub.clock.advance(31)
    # the cheat is never offered this request again, and its wrap is skipped
    assert cheat.client.get("/v1/community/requests").json() == {"requests": []}
    assert member.request_key is not None
    wrap_text = fmt.wrap_community_key(
        x25519_public_raw(member.request_key),
        epoch=1,
        recipient=member.request_id or "",
        key=secrets.token_bytes(32),
    )
    sig = fmt.sign_community_wrap(
        cheat.actor.identity.sig, epoch=1, recipient=member.request_id or "", wrap_text=wrap_text
    )
    item = {"request": member.request_id, "epoch": 1, "wrap": wrap_text, "sig": sig}
    assert cheat.client.post("/v1/community/requests/wraps", json={"wraps": [item]}).json() == {
        "stored": 0,
        "skipped": 1,
    }
    assert founder.serve().json() == {"stored": 1, "skipped": 0}
    assert member.open_request() == founder.key
    rows = query(community, "SELECT actor_id::text, target_id FROM hub.audit WHERE action = 'community.key_reject'")
    assert rows == [(member.id, cheat.id)]


def test_rejections_bar_a_wrapper_only_for_the_rejecting_users(community: Community) -> None:
    """Rejecting cannot shut the key distribution down: any number of rejections leave the wrapper serving others."""
    founder = community.founder()
    holder = community.holder(founder)
    with community.hub.pool.connection() as connection:
        for _ in range(5):
            rejecting = community.member()
            connection.execute(
                "INSERT INTO hub.community_reject (wrapper_id, user_id, rejected_at) VALUES (%s, %s, now())",
                (holder.id, rejecting.id),
            )
    newcomer = community.member()
    newcomer.request()
    assert [item["id"] for item in holder.client.get("/v1/community/requests").json()["requests"]] == [
        newcomer.request_id
    ]


def test_a_muted_holder_does_not_serve_requests(community: Community) -> None:
    founder = community.founder()
    holder = community.holder(founder)
    community.moderator(founder)
    assert founder.client.put(f"/v1/community/mutes/{holder.id}", json={}).json() == {"muted": True}
    newcomer = community.member()
    newcomer.request()
    assert holder.client.get("/v1/community/requests").json() == {"error": "forbidden"}
    item = {"request": newcomer.request_id, "epoch": 1, "wrap": b64u_encode(bytes(80)), "sig": b64u_encode(bytes(64))}
    assert holder.client.post("/v1/community/requests/wraps", json={"wraps": [item]}).json() == {"error": "forbidden"}
    assert founder.client.delete(f"/v1/community/mutes/{holder.id}").json() == {"muted": False}
    assert holder.serve().json() == {"stored": 1, "skipped": 0}


def test_new_key_requests_are_rate_limited_per_user(community: Community) -> None:
    community.founder()
    member = community.member()
    for _ in range(3):
        assert member.request().status_code == 200
    response = member.request()
    assert (response.status_code, response.json(), response.headers["retry-after"]) == (
        429,
        {"error": "rate_limited"},
        "20",
    )
    community.hub.clock.advance(20)
    assert member.request().status_code == 200


def test_concurrent_epoch_creations_make_one_epoch(community: Community) -> None:
    first, second = community.member(), community.member()
    start = threading.Barrier(2)
    answers: list[httpx.Response] = []

    def attempt(member: Member) -> None:
        start.wait()
        answers.append(member.create_epoch())

    threads = [threading.Thread(target=attempt, args=(member,)) for member in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    assert sorted(str(answer.json()) for answer in answers) == ["{'epoch': 1}", "{'error': 'community_epoch_exists'}"]
    assert query(community, "SELECT count(*) FROM hub.community_epoch") == [(1,)]
    assert len(query(community, "SELECT user_id FROM hub.community_wrap")) == 1


def test_request_rules(community: Community, published: MemoryPublisher) -> None:
    founder = community.founder()
    assert founder.request().json() == {"error": "forbidden"}  # a holder does not request
    outsider = community.member(join=False)
    assert outsider.request().json() == {"error": "community_not_member"}
    member = community.member()
    published.clear()
    assert member.request().status_code == 200
    first_id, first_key = member.request_id, member.request_key
    assert first_key is not None
    # an identical repeat is no new request (no second frame), a replacement is
    kem = b64u_encode(x25519_public_raw(first_key))
    sig = fmt.sign_request(member.actor.identity.sig, id=first_id or "", kem_pub=kem, user=member.id)
    again = member.client.put("/v1/community/request", json={"id": first_id, "kem_pub": kem, "sig": sig})
    assert again.json() == {"id": first_id}
    assert len(frames(published)) == 1
    other = community.member()
    taken = other.client.put(
        "/v1/community/request",
        json={
            "id": first_id,
            "kem_pub": kem,
            "sig": fmt.sign_request(other.actor.identity.sig, id=first_id or "", kem_pub=kem, user=other.id),
        },
    )
    assert (taken.status_code, taken.json()) == (409, {"error": "id_conflict"})
    forged = member.client.put("/v1/community/request", json={"id": new_id(), "kem_pub": kem, "sig": sig})
    assert (forged.status_code, forged.json()) == (401, {"error": "signature_invalid"})
    community.hub.clock.advance(20)  # past the frame window, and a token of the request limit is back
    assert member.request().status_code == 200  # replaces the first
    assert query(community, "SELECT id::text FROM hub.community_request WHERE user_id = %s", member.id) == [
        (member.request_id,)
    ]
    assert len(frames(published)) == 2
    assert member.request().status_code == 200  # within 5 s: no further frame
    assert len(frames(published)) == 2


def test_a_disabled_user_is_not_offered(community: Community) -> None:
    founder = community.founder()
    member = community.member()
    member.request()
    with community.hub.pool.connection() as connection:
        connection.execute("UPDATE hub.principal SET disabled_at = now() WHERE id = %s", (member.id,))
    assert founder.client.get("/v1/community/requests").json() == {"requests": []}


def test_self_wrap_rules(community: Community) -> None:
    member = community.member()
    key = secrets.token_bytes(32)
    assert member.client.post("/v1/community/wraps", json=member.self_wrap(key)).json() == {"error": "community_no_key"}
    founder = community.founder()
    assert member.client.post("/v1/community/wraps", json=member.self_wrap(key, epoch=2)).json() == {
        "error": "epoch_stale"
    }
    forged = member.self_wrap(key, recipient=founder.id)
    assert member.client.post("/v1/community/wraps", json=forged).json() == {"error": "signature_invalid"}
    outsider = community.member(join=False)
    assert outsider.client.post("/v1/community/wraps", json=outsider.self_wrap(key)).json() == {
        "error": "community_not_member"
    }
    with community.hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.user_identity WHERE user_id = %s", (member.id,))
    assert member.client.post("/v1/community/wraps", json=member.self_wrap(key)).json() == {"error": "forbidden"}


def test_a_self_wrap_needs_an_opened_request_wrap(community: Community) -> None:
    """C1: a member without a self-wrap cannot declare itself a holder of a key it drew itself."""
    community.founder()
    member = community.member()
    own_key = secrets.token_bytes(32)
    response = member.client.post("/v1/community/wraps", json=member.self_wrap(own_key))
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    member.request()
    response = member.client.post("/v1/community/wraps", json=member.self_wrap(own_key))
    assert response.json() == {"error": "forbidden"}
    assert member.state()["wrap"] is None
    assert member.client.get("/v1/community/requests").json() == {"error": "community_no_key"}


def test_a_self_wrap_is_idempotent(community: Community) -> None:
    founder = community.founder()
    member = community.holder(founder)
    body = member.self_wrap(member.key or b"")
    assert member.client.post("/v1/community/wraps", json=body).json() == {"epoch": 1}


def test_the_org_wrap_endpoint_refuses_the_community_scope(community: Community) -> None:
    founder = community.founder()
    item = {
        "scope": "community",
        "scope_id": fmt.SCOPE_ID,
        "epoch": 1,
        "recipient_id": founder.id,
        **{k: v for k, v in founder.self_wrap(founder.key or b"").items() if k != "epoch"},
    }
    with unchecked(community.hub):
        response = founder.client.post("/v1/wraps", json={"wraps": [item]})
    assert response.status_code == 400
    assert response.json()["error"] in ("request_invalid", "header_invalid", "wrap_unauthorized")
    with unchecked(community.hub):
        listed = founder.client.get("/v1/wraps", params={"scope": "community"})
    assert listed.json() == {"error": "request_invalid"}


def test_a_member_may_request_before_any_epoch_exists(community: Community) -> None:
    early = community.member()
    assert early.request().json() == {"id": early.request_id}
    founder = community.founder()
    assert [item["id"] for item in founder.client.get("/v1/community/requests").json()["requests"]] == [
        early.request_id
    ]


def test_a_waiting_request_is_announced_again_when_its_owner_reads_the_state(
    community: Community, published: MemoryPublisher
) -> None:
    """A request made inside the coalescing window is announced on the requester's next read of `GET /v1/community`
    once the window is over, unless a holder holds its lease or it is wrapped."""
    founder = community.founder()
    first, second = community.member(), community.member()
    published.clear()
    first.request()
    second.request()  # inside the window: no frame of its own
    assert frames(published) == [("all", str(ALL), {"change": "request", "seq": 0})]
    second.state()  # still inside the window
    assert len(frames(published)) == 1
    community.hub.clock.advance(6)
    second.state()
    assert frames(published)[1:] == [("all", str(ALL), {"change": "request", "seq": 0})]
    # leased to a holder: no announcement while the lease runs
    assert len(founder.client.get("/v1/community/requests").json()["requests"]) == 2
    community.hub.clock.advance(6)
    second.state()
    assert len(frames(published)) == 2
    # wrapped: nothing to announce
    community.hub.clock.advance(31)
    assert founder.serve().json() == {"stored": 2, "skipped": 0}
    published.clear()
    community.hub.clock.advance(6)
    first.state()
    founder.state()
    assert [f for f in frames(published) if f[2]["change"] == "request"] == []

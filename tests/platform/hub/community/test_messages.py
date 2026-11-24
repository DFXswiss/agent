"""Messages of the community: post, read, delete, read position, limits, frames (14-community.md sections 4 and 5)."""

from __future__ import annotations

import secrets
import threading
from typing import Any
from uuid import UUID

import pytest

from agent_hub.community import crypto as fmt
from agent_hub.community import routes, store
from agent_hub.community.limits import PostLimiter
from agent_hub.crypto import signed
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import Identity
from agent_hub.stream.publish import ALL, MemoryPublisher
from tests.platform.hub.community.conftest import Community, frames, unchecked
from tests.platform.hub.conftest import FixedClock


def query(community: Community, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with community.hub.pool.connection() as connection:
        return connection.execute(sql, params).fetchall()


def test_post_and_read_back(community: Community, published: MemoryPublisher) -> None:
    founder = community.founder()
    reader = community.holder(founder)
    published.clear()
    response = founder.post(text="Grüezi mitenand - https://dfx.swiss", name="Anna Müller")
    assert response.json() == {"seq": 1}
    assert frames(published) == [("all", str(ALL), {"change": "message", "seq": 1})]
    page = reader.client.get("/v1/community/messages").json()
    assert page["next_before"] is None
    [row] = page["messages"]
    assert (row["seq"], row["author"], row["deleted"]) == (1, founder.id, None)
    assert row["created_at"] == "2026-11-21T12:00:00.000Z" or row["created_at"].endswith("Z")
    assert page["authors"] == {
        founder.id: {"sig_pub": b64u_encode(founder.actor.identity.sig_public()), "moderator": False}
    }
    body = fmt.open_message(reader.key or b"", row["message"], founder.actor.identity.sig_public())
    assert body == {"name": "Anna Müller", "text": "Grüezi mitenand - https://dfx.swiss", "v": 1}
    state = reader.state()
    assert (state["unread"], state["head_seq"], founder.state()["unread"]) == (1, 1, 0)
    assert founder.state()["member"]["posted"] is True


def test_paging_newest_first_and_catch_up_after(community: Community) -> None:
    founder = community.founder()
    for number in range(5):
        community.hub.clock.advance(7)
        assert founder.post(text=f"Nachricht {number}").status_code == 200
    first = founder.client.get("/v1/community/messages", params={"limit": "2"}).json()
    assert [row["seq"] for row in first["messages"]] == [5, 4]
    assert first["next_before"] == "4"
    second = founder.client.get("/v1/community/messages", params={"limit": "2", "before": "4"}).json()
    assert [row["seq"] for row in second["messages"]] == [3, 2]
    last = founder.client.get("/v1/community/messages", params={"limit": "2", "before": "2"}).json()
    assert ([row["seq"] for row in last["messages"]], last["next_before"]) == ([1], None)
    after = founder.client.get("/v1/community/messages", params={"after": "3"}).json()
    assert ([row["seq"] for row in after["messages"]], after["next_before"]) == ([4, 5], None)
    both = founder.client.get("/v1/community/messages", params={"after": "3", "before": "4"})
    assert both.json() == {"error": "request_invalid"}
    empty = founder.client.get("/v1/community/messages", params={"after": "9"}).json()
    assert empty == {"messages": [], "authors": {}, "next_before": None}


def test_an_identical_repeat_is_replayed_and_another_body_conflicts(community: Community) -> None:
    founder = community.founder()
    sealed = founder.message(text="einmal")
    assert founder.post(sealed).json() == {"seq": 1}
    assert founder.post(sealed).json() == {"seq": 1, "replayed": True}
    other = founder.message(text="anders", message_id=sealed["header"]["id"])
    assert founder.post(other).json() == {"error": "id_conflict"}
    assert query(community, "SELECT count(*) FROM hub.audit WHERE action = 'community.post'") == [(1,)]


def test_another_author_cannot_reuse_an_id(community: Community) -> None:
    founder = community.founder()
    second = community.holder(founder)
    sealed = founder.message()
    founder.post(sealed)
    copy = second.message(message_id=sealed["header"]["id"])
    assert second.post(copy).json() == {"error": "id_conflict"}


def test_post_checks_in_order(community: Community) -> None:
    founder = community.founder()
    outsider = community.member(join=False)
    assert outsider.post(founder.message()).json() == {"error": "community_not_member"}
    waiting = community.member()
    assert waiting.post().json() == {"error": "community_no_key"}
    assert founder.post(founder.message(author=waiting.id)).json() == {"error": "forbidden"}
    assert founder.post(founder.message(epoch=2)).json() == {"error": "epoch_stale"}
    sealed = founder.message()
    sealed["sig"] = b64u_encode(bytes(64))
    response = founder.post(sealed)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    broken = founder.message()
    broken["header"] = {**broken["header"], "v": 2}
    with unchecked(community.hub):
        assert founder.post(broken).json() == {"error": "header_invalid"}


def test_oversized_ciphertext_is_refused_before_decoding(community: Community) -> None:
    founder = community.founder()
    sealed = founder.message()
    sealed["ct"] = "A" * 21848
    with unchecked(community.hub):
        response = founder.client.post("/v1/community/messages", json={"message": sealed})
    assert (response.status_code, response.json()) == (413, {"error": "size_exceeded"})


def test_a_message_id_that_is_no_uuid_is_a_format_error(community: Community) -> None:
    founder = community.founder()
    with community.hub.pool.connection() as connection:
        assert store.existing(connection, UUID(founder.id), {"header": {"id": "x"}}) is None
        assert store.existing(connection, UUID(founder.id), {"header": None}) is None


def test_delete_by_author_and_by_moderator(community: Community, published: MemoryPublisher) -> None:
    founder = community.founder()
    second = community.holder(founder)
    first = founder.message(text="weg damit")
    founder.post(first)
    community.hub.clock.advance(7)
    other = second.message(text="auch weg")
    second.post(other)
    assert second.client.delete(f"/v1/community/messages/{first['header']['id']}").json() == {"error": "forbidden"}
    published.clear()
    assert founder.client.delete(f"/v1/community/messages/{first['header']['id']}").json() == {"deleted": "author"}
    assert frames(published) == [("all", str(ALL), {"change": "delete", "seq": 1})]
    again = founder.client.delete(f"/v1/community/messages/{first['header']['id']}")
    assert again.json() == {"deleted": "author"}
    community.moderator(founder)
    assert founder.client.delete(f"/v1/community/messages/{other['header']['id']}").json() == {"deleted": "moderator"}
    page = second.client.get("/v1/community/messages").json()
    assert [(row["seq"], row["deleted"], row["message"]) for row in page["messages"]] == [
        (2, "moderator", None),
        (1, "author", None),
    ]
    assert page["authors"] == {}
    stored = query(community, "SELECT header, ct, sig, deleted_as FROM hub.community_message ORDER BY seq")
    assert stored == [(None, None, None, "author"), (None, None, None, "moderator")]
    # a deleted ID can never be posted again
    assert founder.post(first).json() == {"error": "id_conflict"}
    assert second.state()["unread"] == 0
    rows = query(
        community, "SELECT actor_id::text, detail FROM hub.audit WHERE action = 'community.delete' ORDER BY seq"
    )
    assert rows == [(founder.id, {"as": "author", "seq": 1}), (founder.id, {"as": "moderator", "seq": 2})]
    assert founder.client.delete("/v1/community/messages/not-an-id").json() == {"error": "not_found"}
    unknown = "0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
    assert founder.client.delete(f"/v1/community/messages/{unknown}").json() == {"error": "not_found"}


def test_read_position_moves_forward_only_and_needs_membership(community: Community) -> None:
    founder = community.founder()
    reader = community.holder(founder)
    for _ in range(3):
        community.hub.clock.advance(7)
        founder.post()
    assert reader.state()["unread"] == 3
    assert reader.client.put("/v1/community/read", json={"seq": 2}).json() == {"last_read_seq": 2}
    assert reader.state()["unread"] == 1
    assert reader.client.put("/v1/community/read", json={"seq": 1}).json() == {"last_read_seq": 2}
    assert reader.client.put("/v1/community/read", json={"seq": 99}).json() == {"last_read_seq": 3}
    outsider = community.member(join=False)
    assert outsider.client.put("/v1/community/read", json={"seq": 1}).json() == {"error": "community_not_member"}


def test_the_rate_limit_counts_accepted_and_refused_posts(community: Community) -> None:
    founder = community.founder()
    for _ in range(4):
        assert founder.post().status_code == 200
    broken = founder.message()
    broken["sig"] = b64u_encode(bytes(64))
    assert founder.post(broken).status_code == 401  # the fifth token
    response = founder.post()
    assert (response.status_code, response.json(), response.headers["retry-after"]) == (
        429,
        {"error": "rate_limited"},
        "6",
    )
    community.hub.clock.advance(6)
    assert founder.post().status_code == 200
    replay = founder.sent[0]
    assert founder.post(replay).json()["replayed"] is True  # a repeat neither needs nor takes a token


def test_post_limiter_bounds_its_buckets() -> None:
    clock = FixedClock()
    limiter = PostLimiter(clock, burst=2, refill_seconds=10, max_users=2)
    for user in ("a", "b", "c"):
        assert limiter.acquire(user) is None
    assert len(limiter) == 2  # "a" was dropped first
    assert limiter.acquire("c") is None
    assert limiter.acquire("c") == 10
    clock.advance(5)
    assert limiter.acquire("c") == 5
    clock.advance(5)
    assert limiter.acquire("c") is None


def test_concurrent_posts_of_one_user_never_exceed_the_burst() -> None:
    clock = FixedClock()
    limiter = PostLimiter(clock, burst=5, refill_seconds=6)
    start = threading.Barrier(20)
    results: list[int | None] = []

    def attempt() -> None:
        start.wait()
        results.append(limiter.acquire("a"))

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(None) == 5


def test_another_message_under_a_used_id_costs_a_token(community: Community) -> None:
    founder = community.founder()
    first = founder.message()
    assert founder.post(first).status_code == 200
    other = founder.message(text="Etwas anderes", message_id=first["header"]["id"])
    for _ in range(4):
        assert founder.post(other).json() == {"error": "id_conflict"}
    response = founder.post(other)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})


def test_a_failing_fan_out_does_not_fail_the_post(community: Community, monkeypatch: Any) -> None:
    founder = community.founder()

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(routes, "publish", broken)
    assert founder.post().json() == {"seq": 1}


BAD_BODIES = [
    {"name": "Anna", "text": "x", "v": 2},
    {"name": "Anna", "text": "x"},
    [],
    {"name": " Anna", "text": "x", "v": 1},
    {"name": "", "text": "x", "v": 1},
    {"name": "A" * 65, "text": "x", "v": 1},
    {"name": "Ann\u200ba", "text": "x", "v": 1},
    {"name": "Anna\u202e", "text": "x", "v": 1},
    {"name": "Zoe\u0308", "text": "x", "v": 1},
    {"name": "Anna", "text": "a\u202eb", "v": 1},
    {"name": "Anna", "text": "a\u0007b", "v": 1},
    {"name": "Anna", "text": "x" * 2001, "v": 1},
    {"name": 1, "text": "x", "v": 1},
]


def test_text_and_name_rules() -> None:
    good = {"name": "Zo\u00eb", "text": "Zeile 1\nZeile 2\tmit Tab", "v": 1}
    assert fmt.check_body(good) == good


@pytest.mark.parametrize("bad", BAD_BODIES)
def test_bodies_breaking_a_rule_are_refused(bad: object) -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        fmt.check_body(bad)


def test_open_message_refuses_a_body_that_is_no_json() -> None:
    identity = Identity.generate()
    key = secrets.token_bytes(32)
    header = fmt.new_header(
        id="0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b",
        author="0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2c",
        epoch=1,
        ts="2026-11-23T12:00:00.000Z",
    )
    sealed = signed.seal(fmt.FORMAT, key, header, b"\xff", identity.sig)
    with pytest.raises(CryptoError, match="header_invalid"):
        fmt.open_message(key, sealed, identity.sig_public())


def test_concurrent_identical_posts_store_one_message(community: Community) -> None:
    """Two transactions post the same message at once: the lock orders them, the second is a repeat."""
    founder = community.founder()
    sealed = founder.message()
    now = community.hub.clock()
    start = threading.Barrier(2)
    results: list[store.Posted] = []

    def attempt() -> None:
        with community.hub.pool.connection() as connection:
            start.wait()
            results.append(store.post(connection, UUID(founder.id), sealed, now))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item.replayed for item in results) == [False, True]
    assert results[0].seq == results[1].seq
    assert query(community, "SELECT count(*) FROM hub.community_message") == [(1,)]


def test_a_post_that_loses_the_race_to_an_identical_one_is_a_repeat(community: Community, monkeypatch: Any) -> None:
    """The early repeat check missed a concurrent identical post: `store.post` finds it under the lock, the answer is a
    repeat and the request declares that nothing changed (no audit defect)."""
    founder = community.founder()
    sealed = founder.message()
    assert founder.post(sealed).json() == {"seq": 1}
    original, calls = store.existing, []

    def missed_once(*args: Any) -> store.Posted | None:  # the route's early check misses, the locked one finds it
        calls.append(args)
        return None if len(calls) == 1 else original(*args)

    monkeypatch.setattr(store, "existing", missed_once)
    audit_rows = query(community, "SELECT count(*) FROM hub.audit WHERE action = 'community.post'")
    assert founder.post(sealed).json() == {"seq": 1, "replayed": True}
    assert query(community, "SELECT count(*) FROM hub.audit WHERE action = 'community.post'") == audit_rows

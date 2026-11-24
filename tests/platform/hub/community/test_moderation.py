"""Moderation of the community: moderators, mutes, audit, and the zero-knowledge scan (ADR-069 decisions 1, 5)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.community.conftest import Community, frames


def query(community: Community, text: str, *params: Any) -> list[tuple[Any, ...]]:
    with community.hub.pool.connection() as connection:
        return connection.execute(text, params).fetchall()


def test_the_operator_grants_and_revokes_moderators(community: Community) -> None:
    member = community.member()
    root = community.hub.root
    assert root.get("/v1/community/moderators").json() == {"moderators": []}
    assert root.post("/v1/community/moderators", json={"user": member.id, "granted": True}).json() == {
        "user": member.id,
        "moderator": True,
    }
    root.post("/v1/community/moderators", json={"user": member.id, "granted": True})  # unchanged, no second audit
    listed = root.get("/v1/community/moderators").json()["moderators"]
    assert [item["user"] for item in listed] == [member.id]
    assert member.state()["moderator"] is True
    assert root.post("/v1/community/moderators", json={"user": member.id, "granted": False}).json() == {
        "user": member.id,
        "moderator": False,
    }
    root.post("/v1/community/moderators", json={"user": member.id, "granted": False})
    assert member.state()["moderator"] is False
    unknown = "0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
    assert root.post("/v1/community/moderators", json={"user": unknown, "granted": True}).json() == {
        "error": "not_found"
    }
    rows = query(
        community,
        "SELECT action, actor_kind, actor_id, target_id FROM hub.audit WHERE action LIKE"
        " 'community.moderator%%' ORDER BY seq",
    )
    assert rows == [
        ("community.moderator_grant", "operator", None, member.id),
        ("community.moderator_revoke", "operator", None, member.id),
    ]
    assert member.client.get("/v1/community/moderators").status_code in (401, 403)


def test_mute_and_unmute(community: Community, published: MemoryPublisher) -> None:
    founder = community.founder()
    noisy = community.holder(founder)
    assert founder.client.get("/v1/community/mutes").json() == {"error": "forbidden"}
    assert founder.client.put(f"/v1/community/mutes/{noisy.id}", json={}).json() == {"error": "forbidden"}
    community.moderator(founder)
    published.clear()
    assert founder.client.put(f"/v1/community/mutes/{noisy.id}", json={}).json() == {"muted": True}
    assert frames(published) == [("user", noisy.id, {"change": "mute", "seq": 0})]
    assert founder.client.put(f"/v1/community/mutes/{noisy.id}", json={}).json() == {"muted": True}
    assert len(frames(published)) == 1  # unchanged: no second frame
    assert noisy.state()["member"]["muted"] is True
    sealed = noisy.message(text="spam")
    assert noisy.post(sealed).json() == {"error": "community_muted"}
    mutes = founder.client.get("/v1/community/mutes").json()["mutes"]
    assert [item["user"] for item in mutes] == [noisy.id]
    assert founder.client.delete(f"/v1/community/mutes/{noisy.id}").json() == {"muted": False}
    assert noisy.post(sealed).json() == {"seq": 1}
    rows = query(
        community,
        "SELECT action, actor_id::text, target_id FROM hub.audit WHERE action LIKE 'community.%%mute' ORDER BY seq",
    )
    assert rows == [("community.mute", founder.id, noisy.id), ("community.unmute", founder.id, noisy.id)]


def test_moderators_cannot_be_muted_and_only_members(community: Community) -> None:
    founder = community.founder()
    other = community.holder(founder)
    community.moderator(founder)
    community.moderator(other)
    assert founder.client.put(f"/v1/community/mutes/{other.id}", json={}).json() == {"error": "forbidden"}
    assert founder.client.put(f"/v1/community/mutes/{founder.id}", json={}).json() == {"error": "forbidden"}
    outsider = community.member(join=False)
    assert founder.client.put(f"/v1/community/mutes/{outsider.id}", json={}).json() == {"error": "not_found"}
    assert founder.client.delete(f"/v1/community/mutes/{other.id}").json() == {"muted": False}


def test_moderators_are_marked_on_pages(community: Community) -> None:
    founder = community.founder()
    community.moderator(founder)
    founder.post()
    page = founder.client.get("/v1/community/messages").json()
    assert page["authors"][founder.id]["moderator"] is True


def _every_text_value(connection: psycopg.Connection[Any]) -> str:
    tables = connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'hub' ORDER BY table_name"
    ).fetchall()
    parts: list[str] = []
    for (table,) in tables:
        rows = connection.execute(
            sql.SQL("SELECT row_to_json(t)::text FROM hub.{} t").format(sql.Identifier(table))
        ).fetchall()
        parts.extend(row[0] for row in rows)
        for (raw,) in connection.execute(
            sql.SQL(
                "SELECT string_agg(encode(convert_to(row_to_json(t)::text, 'UTF8'), 'escape'), '') FROM hub.{} t"
            ).format(sql.Identifier(table))
        ).fetchall():
            parts.append(raw or "")
    return "\n".join(parts)


def test_the_hub_database_and_the_frames_hold_no_name_and_no_text(
    community: Community, published: MemoryPublisher
) -> None:
    founder = community.founder()
    reader = community.holder(founder)
    community.moderator(founder)
    canary_name, canary_text = "Kanarienname Zebra", "kanarientext-community-7Q4K"
    founder.post(text=canary_text, name=canary_name)
    community.hub.clock.advance(7)
    deleted = reader.message(text=canary_text + "-weg", name=canary_name + " B")
    reader.post(deleted)
    founder.client.delete(f"/v1/community/messages/{deleted['header']['id']}")
    reader.client.put("/v1/community/read", json={"seq": 2})
    founder.client.put(f"/v1/community/mutes/{reader.id}", json={})
    community.hub.flush_read_log()
    with community.hub.pool.connection() as connection:
        everything = _every_text_value(connection)
    for canary in (canary_name, canary_text, "Kanarienname", "kanarientext", "Zebra"):
        assert canary not in everything
    for item in published.items():
        assert "kanarien" not in str(item.data).lower()


def test_a_muted_user_cannot_repeat_an_earlier_post(community: Community) -> None:
    """The mute check comes before the idempotent repeat: a retry of an accepted post is refused while muted."""
    founder = community.founder()
    noisy = community.holder(founder)
    sealed = noisy.message()
    assert noisy.post(sealed).json() == {"seq": 1}
    community.moderator(founder)
    assert founder.client.put(f"/v1/community/mutes/{noisy.id}", json={}).json() == {"muted": True}
    response = noisy.post(sealed)
    assert (response.status_code, response.json()) == (403, {"error": "community_muted"})

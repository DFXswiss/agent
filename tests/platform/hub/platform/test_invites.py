"""Platform invitations of inviters (03-api.md section 4.2, ADR-005, ADR-026): the right `platform:inviter` from the
platform log, create (idempotent, expiry bounds, ID conflicts), list (states, newest first) and revoke."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.platform.invites import state_of
from tests.platform.hub.auth.authenticator import INVITES, new_invite
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.platform.conftest import Platform, new_org


def new_invite_id() -> str:
    return new_invite().invite_id


def inviter(plat: Platform, hub: HubHarness) -> SignedClient:
    client = hub.browser()
    assert client.key is not None
    plat.append("role_grant", subject=str(client.key.principal.principal_id), role="inviter")
    return client


def create(
    client: SignedClient, hub: HubHarness, invite_id: str, hours: float = 72, pk_inv: str | None = None, **kwargs: Any
) -> Any:
    """`POST /v1/platform/invites` with the `pk_inv` of the remembered code (or the given key)."""
    expires = format_time(hub.clock() + timedelta(hours=hours))
    key = pk_inv or (INVITES[invite_id].pk_inv if invite_id in INVITES else new_invite().pk_inv)
    return client.post(
        "/v1/platform/invites",
        json={"invite_id": invite_id, "pk_inv": key, "kind": "user", "expires_at": expires},
        **kwargs,
    )


def test_an_inviter_creates_lists_and_revokes(plat: Platform, hub: HubHarness) -> None:
    client = inviter(plat, hub)
    assert client.key is not None
    user = str(client.key.principal.principal_id)
    first = new_invite_id()
    response = create(client, hub, first)
    assert (response.status_code, response.json()) == (200, {})
    assert create(client, hub, first).status_code == 200  # identical repeat
    hub.clock.advance(1)
    second = new_invite_id()
    create(client, hub, second, hours=1)
    listed = client.get("/v1/platform/invites").json()["invites"]
    assert [item["invite_id"] for item in listed] == [second, first]
    assert listed[1] == {
        "invite_id": first,
        "kind": "user",
        "state": "open",
        "created_by": user,
        "created_at": "2026-11-21T12:00:00.123Z",
        "expires_at": "2026-11-24T12:00:00.123Z",
        "registered": False,
        "used_at": None,
        "revoked_at": None,
    }
    assert client.post(f"/v1/platform/invites/{first}/revoke", json={}).json() == {}
    assert client.post(f"/v1/platform/invites/{first}/revoke", json={}).status_code == 200
    hub.clock.advance(3600)
    states = {item["invite_id"]: item["state"] for item in client.get("/v1/platform/invites").json()["invites"]}
    assert states == {first: "revoked", second: "expired"}
    actions = plat.query(
        "SELECT action, actor_id::text, target_id, detail FROM hub.audit WHERE action LIKE 'invite.%%'"
    )
    assert actions == [
        ("invite.create", user, first, {"invite_id": first, "kind": "user"}),
        ("invite.create", user, second, {"invite_id": second, "kind": "user"}),
        ("invite.revoke", user, first, {"invite_id": first, "kind": "user"}),
    ]
    hub.flush_read_log()
    # the same read twice in one window: one row and its repeat (ADR-063 section 3)
    assert plat.query("SELECT count(*) FROM hub.audit WHERE action = 'read.platform.invites'") == [(1,)]
    assert plat.query("SELECT detail->>'repeats' FROM hub.audit WHERE action = 'audit.repeat'") == [("1",)]


def test_operator_invitations_are_listed_and_used_ones_cannot_be_revoked(plat: Platform, hub: HubHarness) -> None:
    client = inviter(plat, hub)
    operator = plat.invite_user()
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.invite SET registered_user = %s, registered_at = %s, used_at = %s WHERE id = %s",
            (plat.hub.keys.session().principal.principal_id, hub.clock(), hub.clock(), operator),
        )
    listed = client.get("/v1/platform/invites").json()["invites"]
    assert [(item["created_by"], item["state"], item["registered"]) for item in listed] == [(None, "used", True)]
    response = client.post(f"/v1/platform/invites/{operator}/revoke", json={})
    assert (response.status_code, response.json()["error"]) == (400, "invite_consumed")


def test_without_the_role_everything_is_forbidden(plat: Platform, hub: HubHarness) -> None:
    client = inviter(plat, hub)
    assert client.key is not None
    plat.append("role_revoke", subject=str(client.key.principal.principal_id), role="inviter")
    invite_id = new_invite_id()
    for response in (
        create(client, hub, invite_id),
        client.get("/v1/platform/invites"),
        client.post(f"/v1/platform/invites/{invite_id}/revoke", json={}),
        create(hub.browser(), hub, invite_id),
    ):
        assert (response.status_code, response.json()["error"]) == (403, "forbidden")
    assert create(hub.device(), hub, invite_id).status_code == 403
    assert create(hub.anon, hub, invite_id).status_code == 401


def test_the_open_switch_lets_every_user_invite_with_their_own_list(plat: Platform, hub: HubHarness) -> None:
    """ADR-077 section 3: with `open_platform_invites` on, a user without the role creates invitations and lists and
    revokes only its own; the role still sees and revokes every invitation; switched off, the user is refused again."""
    holder = inviter(plat, hub)
    plat.append("platform_policy", name="open_platform_invites", on=True)
    alice, bob = hub.browser(), hub.browser()
    mine, theirs = new_invite_id(), new_invite_id()
    assert create(alice, hub, mine).status_code == 200
    hub.clock.advance(1)
    assert create(bob, hub, theirs).status_code == 200
    assert [item["invite_id"] for item in alice.get("/v1/platform/invites").json()["invites"]] == [mine]
    assert [item["invite_id"] for item in holder.get("/v1/platform/invites").json()["invites"]] == [theirs, mine]
    refused = alice.post(f"/v1/platform/invites/{theirs}/revoke", json={})
    assert (refused.status_code, refused.json()["error"]) == (404, "not_found")  # another user's invitation
    assert alice.post(f"/v1/platform/invites/{mine}/revoke", json={}).status_code == 200
    assert holder.post(f"/v1/platform/invites/{theirs}/revoke", json={}).status_code == 200
    plat.append("platform_policy", name="open_platform_invites", on=False)
    closed = create(alice, hub, new_invite_id())
    assert (closed.status_code, closed.json()["error"]) == (403, "forbidden")


@pytest.mark.parametrize("hours", [-1, 0, 30 * 24 + 0.01])
def test_expiry_must_be_within_thirty_days(plat: Platform, hub: HubHarness, hours: float) -> None:
    response = create(inviter(plat, hub), hub, new_invite_id(), hours=hours)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/expires_at"]}},
    )


def test_an_existing_id_with_other_content_conflicts(plat: Platform, hub: HubHarness) -> None:
    client = inviter(plat, hub)
    invite_id = new_invite_id()
    create(client, hub, invite_id)
    assert create(client, hub, invite_id, hours=10).status_code == 409
    assert create(client, hub, invite_id, pk_inv=new_invite().pk_inv).status_code == 409
    assert create(client, hub, invite_id).status_code == 200
    other = inviter(plat, hub)
    assert create(other, hub, invite_id).json()["error"] == "id_conflict"
    org_invite = new_invite_id()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, org_id, created_at, expires_at)"
            " VALUES (%s, 'org', 'user', %s, %s, %s)",
            (org_invite, new_org(hub), hub.clock(), hub.clock() + timedelta(days=1)),
        )
    assert create(client, hub, org_invite).json()["error"] == "id_conflict"
    assert client.post(f"/v1/platform/invites/{org_invite}/revoke", json={}).json()["error"] == "not_found"


def test_the_invitation_key_must_be_an_ed25519_public_key(plat: Platform, hub: HubHarness) -> None:
    """ADR-043: a key that can verify nothing would close the invitation for good; refused up front."""
    client = inviter(plat, hub)
    invite_id = new_invite_id()
    response = create(client, hub, invite_id, pk_inv=b64u_encode(bytes(32)))
    assert (response.status_code, response.json()) == (
        400,
        {"error": "params_invalid", "detail": {"pointers": ["/pk_inv"]}},
    )
    with hub.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM hub.invite WHERE id = %s", (invite_id,)).fetchone() == (0,)


def test_the_stored_invitation_carries_the_key(plat: Platform, hub: HubHarness) -> None:
    found = new_invite()
    assert create(inviter(plat, hub), hub, found.invite_id).status_code == 200
    with hub.pool.connection() as connection:
        row = connection.execute("SELECT pk_inv FROM hub.invite WHERE id = %s", (found.invite_id,)).fetchone()
    assert row is not None
    assert b64u_encode(bytes(row[0])) == found.pk_inv


KEY = "AgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.mark.parametrize(
    "body",
    [
        {"invite_id": "short", "pk_inv": KEY, "kind": "user", "expires_at": "2026-11-22T12:00:00.000Z"},
        {
            "invite_id": "AAAAAAAAAAAAAAAAAAAAAA",
            "pk_inv": KEY,
            "kind": "device",
            "expires_at": "2026-11-22T12:00:00.000Z",
        },
        {"invite_id": "AAAAAAAAAAAAAAAAAAAAAA", "pk_inv": KEY, "kind": "user", "expires_at": "2026-11-22"},
        {
            "invite_id": "AAAAAAAAAAAAAAAAAAAAAA",
            "pk_inv": KEY,
            "kind": "user",
            "expires_at": "2026-11-22T12:00:00.000Z",
            "x": 1,
        },
        {"invite_id": "AAAAAAAAAAAAAAAAAAAAAA", "kind": "user", "expires_at": "2026-11-22T12:00:00.000Z"},
        {
            "invite_id": "AAAAAAAAAAAAAAAAAAAAAA",
            "pk_inv": "AA",
            "kind": "user",
            "expires_at": "2026-11-22T12:00:00.000Z",
        },
    ],
)
def test_malformed_requests_are_refused(plat: Platform, hub: HubHarness, body: dict[str, Any]) -> None:
    response = inviter(plat, hub).post("/v1/platform/invites", json=body, extensions={"contract": False})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_invitation_states_have_a_precedence() -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert state_of(t, t, t, t) == "used"
    assert state_of(None, t, t, t) == "revoked"
    assert state_of(None, None, t, t) == "expired"
    assert state_of(None, None, t + timedelta(seconds=1), t) == "open"

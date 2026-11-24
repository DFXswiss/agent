"""`POST /v1/invites/lookup` (anon, 03-api.md section 4.1, ADR-005): the platform and the org form, the single refusal
`invite_unknown` for unknown, used, revoked and expired invitations (no oracle), the audit row and the rate limit.
Org invitation rows are created by HUB-11; the tests seed them directly."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_encode, canonical
from agent_hub.errors import HubError
from agent_hub.invites.lookup import lookup
from agent_hub.log.entries import entry_hash
from agent_hub.log.vectors import main_builder
from tests.platform.hub.conftest import HubHarness, uuid7


def new_invite_id() -> str:
    return invite_codes.invite_secrets(invite_codes.generate_code()).invite_id


def seed_invite(
    hub: HubHarness, *, scope: str = "platform", kind: str = "user", org: UUID | None = None, **columns: Any
) -> str:
    invite_id = new_invite_id()
    values = {
        "id": invite_id,
        "scope": scope,
        "kind": kind,
        "org_id": org,
        "created_at": hub.clock(),
        "expires_at": hub.clock() + timedelta(hours=72),
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    with hub.pool.connection() as connection:
        connection.execute(f"INSERT INTO hub.invite ({names}) VALUES ({marks})", tuple(values.values()))
    return invite_id


def seed_org_log(hub: HubHarness, count: int) -> tuple[UUID, list[dict[str, object]]]:
    """An org with the first `count` entries of the vector main log stored as HUB-05 stores them."""
    entries = main_builder().entries[:count]
    org = UUID(str(entries[0]["body"]["org"]))
    head = entries[-1]
    with hub.pool.connection() as connection, connection.transaction():
        body0 = entries[0]["body"]
        connection.execute(
            "INSERT INTO hub.org (id, private, default_wg, created_by, created_at, log_head_seq, log_head_hash)"
            " VALUES (%s, false, %s, %s, %s, %s, %s)",
            (org, body0["default_wg"], body0["by"], hub.clock(), count - 1, entry_hash(head)),
        )
        for seq, entry in enumerate(entries):
            body = entry["body"]
            connection.execute(
                "INSERT INTO hub.membership_entry (org_id, seq, entry_text, entry_hash, type, by_id, received_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (org, seq, canonical(entry).decode("ascii"), entry_hash(entry), body["type"], body["by"], hub.clock()),
            )
    return org, entries


def test_a_platform_invitation_answers_the_platform_form(hub: HubHarness) -> None:
    invite_id = seed_invite(hub)
    response = hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id})
    assert response.status_code == 200
    assert response.json() == {
        "kind": "user",
        "scope": "platform",
        "expires_at": "2026-11-24T12:00:00.123Z",
        "registered": False,
    }
    registered = seed_invite(hub, registered_user=uuid7(), registered_at=hub.clock())
    assert hub.anon.post("/v1/invites/lookup", json={"invite_id": registered}).json()["registered"] is True
    device = seed_invite(hub, kind="device")
    assert hub.anon.post("/v1/invites/lookup", json={"invite_id": device}).json()["kind"] == "device"


@pytest.mark.parametrize("kind", ["user", "device"])
def test_an_org_invitation_answers_package_head_and_the_whole_log(hub: HubHarness, kind: str) -> None:
    org, entries = seed_org_log(hub, 5)
    package = secrets.token_bytes(60)
    invite_id = seed_invite(hub, scope="org", kind=kind, org=org, log_seq=2, package_ct=package)
    answer = hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id}).json()
    assert answer == {
        "kind": kind,
        "scope": "org",
        "org_id": str(org),
        "expires_at": "2026-11-24T12:00:00.123Z",
        "registered": False,
        "package_ct": b64u_encode(package),
        "log_head": {"seq": 4, "hash": entry_hash(entries[-1])},
        "log": [json.loads(canonical(entry)) for entry in entries],
    }


def unknown_cases(hub: HubHarness) -> dict[str, str]:
    org, _entries = seed_org_log(hub, 3)
    package = secrets.token_bytes(40)
    later = hub.clock() + timedelta(minutes=1)
    return {
        "unknown": new_invite_id(),
        "used": seed_invite(hub, used_at=hub.clock(), used_by=uuid7()),
        "revoked": seed_invite(hub, revoked_at=hub.clock()),
        "expired": seed_invite(hub, created_at=hub.clock() - timedelta(days=2), expires_at=hub.clock()),
        "org_used": seed_invite(hub, scope="org", org=org, package_ct=package, used_at=later, used_by=uuid7()),
        "org_package_gone": seed_invite(hub, scope="org", org=org),
        "org_missing": seed_invite(hub, scope="org", org=uuid7(), package_ct=package),
    }


def test_unknown_used_revoked_and_expired_answer_the_same_refusal(hub: HubHarness) -> None:
    bodies = set()
    for label, invite_id in unknown_cases(hub).items():
        response = hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id})
        assert response.status_code == 404, label
        bodies.add(response.content)
    assert bodies == {b'{"error":"invite_unknown"}'}


def test_an_org_log_with_a_gap_is_not_answered(hub: HubHarness) -> None:
    org, _entries = seed_org_log(hub, 3)
    invite_id = seed_invite(hub, scope="org", org=org, package_ct=secrets.token_bytes(40))
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.org SET log_head_seq = 5 WHERE id = %s", (org,))
        with pytest.raises(HubError) as raised:
            lookup(connection, invite_id, hub.clock())
    assert raised.value.code == "invite_unknown"


def test_answered_lookups_are_audited_without_content(hub: HubHarness) -> None:
    invite_id = seed_invite(hub)
    hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id})
    hub.anon.post("/v1/invites/lookup", json={"invite_id": new_invite_id()})
    with hub.pool.connection() as connection:
        rows = connection.execute("SELECT action, actor_kind, target_id, detail FROM hub.audit").fetchall()
    assert rows == [
        ("invite.lookup", "anonymous", invite_id, {"invite_id": invite_id, "kind": "user"}),
        # the refused lookup leaves the pipeline's refusal, still without content (ADR-063 section 1)
        (
            "request.refused",
            "anonymous",
            None,
            {"method": "POST", "route": "/v1/invites/lookup", "status": 404, "code": "invite_unknown"},
        ),
    ]


@pytest.mark.parametrize(
    "body", [{}, {"invite_id": "x"}, {"invite_id": new_invite_id(), "code": "ABCDE"}, {"invite_id": 7}]
)
def test_malformed_lookups_are_refused(hub: HubHarness, body: dict[str, Any]) -> None:
    response = hub.anon.post("/v1/invites/lookup", json=body, extensions={"contract": False})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_lookups_are_rate_limited_per_source(hub: HubHarness) -> None:
    invite_id = new_invite_id()
    statuses = [hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id}).status_code for _ in range(31)]
    assert statuses[:30] == [404] * 30
    assert statuses[30] == 429
    limited = hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id})
    assert limited.json() == {"error": "rate_limited"}
    assert int(limited.headers["retry-after"]) > 0
    hub.clock.advance(61)
    assert hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id}).status_code == 404

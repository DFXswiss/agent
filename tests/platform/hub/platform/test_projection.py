"""Projections of the platform log (02-data-model.md sections 1.3, 2.1, 2.2): roles, platform access, device state,
invitation rows, the decertification effects (state, `rotation_pending`, hint, signal) and rebuild = incremental."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg

from agent_hub import signals
from agent_hub.platform import projection
from agent_hub.platform.projections import REBUILDERS, rebuild
from agent_hub.registry import PROJECTION_REBUILDERS, merge_mappings
from agent_hub.stream import publish as publish_module
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.platform.conftest import CAPACITY, Platform, new_org, new_user


def assign(hub: HubHarness, org: str, device: str, *, wgs: int = 1, member: bool = True) -> list[str]:
    """Workgroups of `org` with the device assigned (rows as HUB-05 projects them)."""
    created = []
    with hub.pool.connection() as connection, connection.transaction():
        for index in range(wgs):
            wg = str(uuid7())
            connection.execute(
                "INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, %s, 1)", (wg, org, index == 0)
            )
            connection.execute(
                "INSERT INTO hub.wg_device (wg_id, device_id, assigned_seq) VALUES (%s, %s, 2)", (wg, device)
            )
            created.append(wg)
        if member:
            connection.execute(
                "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq)"
                " VALUES (%s, %s, 'device', 'device', 2)",
                (org, device),
            )
    return created


def test_role_grant_and_revoke_project_platform_role(plat: Platform, hub: HubHarness) -> None:
    user = new_user(hub)
    grant = plat.append("role_grant", subject=str(user), role="inviter")
    assert plat.query("SELECT user_id, role, granted_seq, revoked_seq FROM hub.platform_role") == [
        (user, "inviter", grant, None)
    ]
    revoke = plat.append("role_revoke", subject=str(user), role="inviter")
    assert plat.query("SELECT granted_seq, revoked_seq FROM hub.platform_role") == [(grant, revoke)]
    again = plat.append("role_grant", subject=str(user), role="inviter")
    assert plat.query("SELECT granted_seq, revoked_seq FROM hub.platform_role") == [(again, None)]


def test_org_platform_access_follows_the_latest_entry(plat: Platform, hub: HubHarness) -> None:
    org = new_org(hub)
    private = new_org(hub, private=True)
    plat.append("org_platform_access", org=org, granted=True)
    assert plat.query("SELECT platform_access FROM hub.org WHERE id = %s", (org,)) == [(True,)]
    plat.append("org_platform_access", org=org, granted=False)
    plat.append("org_platform_access", org=private, granted=False)
    assert plat.query("SELECT id::text, platform_access FROM hub.org ORDER BY private") == [
        (org, False),
        (private, True),
    ]


def test_invitations_of_the_log_become_invite_rows(plat: Platform, hub: HubHarness) -> None:
    device = plat.invite_device()
    user = plat.invite_user()
    rows = plat.query("SELECT id, scope, kind, created_by, created_at, expires_at FROM hub.invite ORDER BY kind")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (device.invite_id, "platform", "device", None),
        (user, "platform", "user", None),
    ]
    assert rows[0][4] == hub.clock()


def test_certification_activates_the_enrolled_device(plat: Platform, hub: HubHarness) -> None:
    enrolled = plat.enroll()
    assert plat.query("SELECT state FROM hub.device") == [("pending_certification",)]
    hub.clock.advance(60)
    seq = plat.certify(enrolled)
    row = plat.query("SELECT state, certified_seq, sig_pub, kind, updated_at FROM hub.device")[0]
    assert row[:4] == ("active", seq, bytes(enrolled.sig.public_key().public_bytes_raw()), "platform")
    assert row[4] == hub.clock()
    assert plat.query("SELECT certified_at FROM hub.device_enrollment") == [(hub.clock(),)]
    assert plat.query("SELECT used_at, used_by FROM hub.invite") == [(hub.clock(), uuid.UUID(enrolled.device_id))]


def test_certification_without_a_stored_enrollment_creates_the_device(plat: Platform, hub: HubHarness) -> None:
    enrolled = plat.enroll(store=False)
    seq = plat.certify(enrolled)
    row = plat.query("SELECT state, certified_seq, agent_version, os, arch, capacity, owner_org FROM hub.device")
    assert row == [("active", seq, "0.1.0", "macos", "arm64", CAPACITY, None)]
    assert plat.query("SELECT kind FROM hub.principal WHERE id = %s", (enrolled.device_id,)) == [("device",)]


def test_decertification_refuses_the_device_and_marks_rotation(
    plat: Platform, hub: HubHarness, published: MemoryPublisher
) -> None:
    enrolled = plat.enroll()
    plat.certify(enrolled)
    org = new_org(hub)
    other = new_org(hub)
    wgs = assign(hub, org, enrolled.device_id, wgs=2)
    assign(hub, other, enrolled.device_id, wgs=0)
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.org SET rotation_pending = %s WHERE id = %s",
            (psycopg.types.json.Jsonb([{"scope": "wg", "scope_id": wgs[0]}]), org),
        )
    received: list[tuple[str, dict[str, Any]]] = []
    signals.configure({"device.decertified": [lambda name, ids: received.append((name, dict(ids)))]})
    try:
        hub.clock.advance(5)
        plat.append("device_decertify", device=enrolled.device_id)
    finally:
        signals.configure()
    assert plat.query("SELECT state, updated_at FROM hub.device") == [("decertified", hub.clock())]
    pending = dict(plat.query("SELECT id::text, rotation_pending FROM hub.org"))
    expected = sorted(
        [{"scope": "org", "scope_id": org}] + [{"scope": "wg", "scope_id": wg} for wg in wgs],
        key=lambda i: (i["scope"], i["scope_id"]),
    )
    assert pending[org] == expected
    assert pending[other] == [{"scope": "org", "scope_id": other}]
    hints = [item for item in published.items() if item.frame == "decertified"]
    assert [(item.audience, dict(item.data)) for item in hints] == [
        (Audience(kind="device", id=uuid.UUID(enrolled.device_id)), {})
    ]
    assert received == [("device.decertified", {"device": uuid.UUID(enrolled.device_id)})]


def test_unassigned_and_removed_rows_and_missing_orgs_are_skipped(plat: Platform, hub: HubHarness) -> None:
    enrolled = plat.enroll()
    plat.certify(enrolled)
    org = new_org(hub)
    wgs = assign(hub, org, enrolled.device_id, member=False)
    ghost = str(uuid7())
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.wg_device SET unassigned_seq = 5 WHERE wg_id = %s", (wgs[0],))
        connection.execute(
            "INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, true, 1)", (str(uuid7()), ghost)
        )
        connection.execute(
            "INSERT INTO hub.wg_device (wg_id, device_id, assigned_seq) SELECT id, %s, 2 FROM hub.wg WHERE org_id = %s",
            (enrolled.device_id, ghost),
        )
    with hub.pool.connection() as connection:
        assert projection.affected_scopes(connection, enrolled.device_id).keys() == {ghost}
        assert projection.mark_rotation_pending(connection, enrolled.device_id) == [ghost]
    assert plat.query("SELECT rotation_pending FROM hub.org") == [([],)]


def snapshot(hub: HubHarness) -> dict[str, list[tuple[Any, ...]]]:
    queries = {
        "roles": "SELECT user_id, role, granted_seq, revoked_seq FROM hub.platform_role ORDER BY user_id",
        "orgs": "SELECT id, platform_access FROM hub.org ORDER BY id",
        "devices": "SELECT id, kind, state, certified_seq, kem_pub, sig_pub FROM hub.device ORDER BY id",
        "invites": "SELECT id, scope, kind, created_at, expires_at, used_at, used_by FROM hub.invite ORDER BY id",
        "enrollments": "SELECT invite_id, certified_at FROM hub.device_enrollment ORDER BY invite_id",
    }
    with hub.pool.connection() as connection:
        return {name: list(connection.execute(sql).fetchall()) for name, sql in queries.items()}


def test_rebuild_equals_the_incremental_state(plat: Platform, hub: HubHarness) -> None:
    orgs = [new_org(hub), new_org(hub), new_org(hub, private=True)]
    users = [str(new_user(hub)) for _ in range(3)]
    first = plat.enroll()
    plat.certify(first)
    second = plat.enroll()
    hub.clock.advance(1)
    plat.certify(second)
    plat.append("device_decertify", device=first.device_id)
    again = plat.enroll(device_id=first.device_id, store=False)
    plat.store_enrollment(again)
    plat.certify(again)
    plat.enroll()
    plat.invite_user()
    for user in users:
        plat.append("role_grant", subject=user, role="inviter")
    plat.append("role_revoke", subject=users[1], role="inviter")
    plat.append("org_platform_access", org=orgs[0], granted=True)
    plat.append("org_platform_access", org=orgs[1], granted=True)
    plat.append("org_platform_access", org=orgs[1], granted=False)
    incremental = snapshot(hub)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("DELETE FROM hub.platform_role")
        connection.execute("UPDATE hub.platform_role SET revoked_seq = NULL")
        connection.execute("UPDATE hub.org SET platform_access = NOT platform_access")
        connection.execute("UPDATE hub.device SET state = 'pending_certification', certified_seq = NULL")
        connection.execute(
            "INSERT INTO hub.platform_role (user_id, role, granted_seq) VALUES (%s, 'inviter', 0)", (uuid7(),)
        )
    assert snapshot(hub) != incremental
    with hub.pool.connection() as connection, connection.transaction():
        assert rebuild(connection) == len(plat.entries)
    assert snapshot(hub) == incremental


def test_the_rebuilder_is_discovered() -> None:
    assert merge_mappings(PROJECTION_REBUILDERS)["platform"] is REBUILDERS["platform"] is rebuild


def test_a_failing_hint_does_not_fail_the_stored_append(plat: Platform, hub: HubHarness) -> None:
    class Broken:
        def publish(self, item: object) -> None:
            raise RuntimeError("fan-out down")

    enrolled = plat.enroll()
    plat.certify(enrolled)
    previous = publish_module.set_publisher(Broken())
    try:
        plat.append("device_decertify", device=enrolled.device_id)
    finally:
        publish_module.set_publisher(previous)
    assert plat.query("SELECT state FROM hub.device") == [("decertified",)]

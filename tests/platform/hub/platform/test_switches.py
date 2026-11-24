"""The switches and lists of the platform log at the hub (KOR-49, ADR-079 sections 2 and 3): `platform_policy`,
`org_operator_subscriptions` and the role `operator` are appended only after every active device - platform and
organization devices - announced `log.policy` (`409 peer_outdated`); a list entry needs a known organization; the
switch `open_platform_access` derives `org.platform_access` of every organization without an explicit
`org_platform_access` (projection `org.platform_access_grant`, table `platform_policy`), also in the rebuild."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from agent_hub.platform import projections
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World
from tests.platform.hub.platform.conftest import Platform, new_org, new_user

POLICY = "log.policy"


def _post(plat: Platform, entry_type: str, **fields: object) -> Any:
    return plat.post(plat.chain(plat.body(entry_type, **fields)))


def _access(plat: Platform) -> dict[str, tuple[bool, bool | None]]:
    rows = plat.query("SELECT id::text, platform_access, platform_access_grant FROM hub.org")
    return {org: (access, grant) for org, access, grant in rows}


def test_the_switch_gives_access_to_every_org_without_its_own_entry(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    open_org, excluded, private = new_org(hub), new_org(hub), new_org(hub, private=True)
    plat.append("org_platform_access", org=excluded, granted=False)
    plat.append("platform_policy", name="open_platform_access", on=True)
    assert _access(plat) == {open_org: (True, None), excluded: (False, False), private: (True, None)}
    assert plat.query('SELECT name, "on", seq FROM hub.platform_policy') == [("open_platform_access", True, 2)]
    plat.append("platform_policy", name="open_platform_invites", on=True)  # another switch changes no access
    assert _access(plat)[open_org] == (True, None)
    plat.append("platform_policy", name="open_platform_access", on=False)
    assert _access(plat) == {open_org: (False, None), excluded: (False, False), private: (True, None)}
    plat.append("platform_policy", name="open_platform_access", on=True)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.org SET platform_access = false, platform_access_grant = true")
        connection.execute('UPDATE hub.platform_policy SET "on" = false')
        assert projections.rebuild(connection) == 6
    assert _access(plat) == {open_org: (True, None), excluded: (False, False), private: (True, None)}
    assert sorted(plat.query('SELECT name, "on" FROM hub.platform_policy')) == [
        ("open_platform_access", True),
        ("open_platform_invites", True),
    ]


def test_the_operator_list_needs_a_known_org_and_changes_no_projection(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    org = new_org(hub)
    missing = _post(plat, "org_operator_subscriptions", org=str(uuid7()), granted=True)
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")
    plat.append("org_operator_subscriptions", org=org, granted=True)
    assert _access(plat)[org] == (False, None)
    assert plat.query("SELECT type, subject_id::text FROM hub.platform_entry ORDER BY seq DESC LIMIT 1") == [
        ("org_operator_subscriptions", org)
    ]
    assert plat.query("SELECT action FROM hub.audit WHERE action = 'platform.org_operator_subscriptions'") == [
        ("platform.org_operator_subscriptions",)
    ]


def _features(hub: HubHarness, device: str, features: list[str]) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.device SET features = %s WHERE id = %s", (features, device))


def test_new_entries_wait_until_every_active_device_announced_the_feature(plat: Platform, hub: HubHarness) -> None:
    """ADR-079 section 2: an older runner would stop at the new types; organization devices verify the platform log
    too, so they count as well. `role_grant` of `inviter` and `user_certify` are not held back by this feature."""
    device = plat.enroll()
    plat.append("device_certify", **device.certify_fields())
    world = World(hub)
    org_device = Fleet(world).org_device(world.public_org())
    org, user = new_org(hub), str(new_user(hub))
    _features(hub, device.device_id, ["log.admit"])
    _features(hub, org_device.id, ["log.admit"])
    held = (
        ("platform_policy", {"name": "open_platform_access", "on": True}),
        ("org_operator_subscriptions", {"org": org, "granted": True}),
        ("role_grant", {"subject": user, "role": "operator"}),
    )
    for entry_type, fields in held:
        refused = _post(plat, entry_type, **fields)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"] == "peer_outdated"
        assert sorted(refused.json()["detail"]["devices"]) == sorted([device.device_id, org_device.id])
    assert _post(plat, "role_grant", subject=user, role="inviter").status_code == 200  # not gated
    _features(hub, device.device_id, ["log.admit", POLICY])
    refused = _post(plat, "platform_policy", name="open_platform_access", on=True)
    assert refused.json()["detail"]["devices"] == [org_device.id]  # the organization device still holds it back
    _features(hub, org_device.id, [POLICY])
    for entry_type, fields in held:
        assert _post(plat, entry_type, **fields).status_code == 200, entry_type
    assert plat.query("SELECT role FROM hub.platform_role WHERE user_id = %s ORDER BY role", (user,)) == [
        ("inviter",),
        ("operator",),
    ]
    revoked = _post(plat, "role_revoke", subject=user, role="operator")
    assert revoked.status_code == 200, revoked.text


def test_the_operator_role_is_stored_beside_inviter(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    user = str(new_user(hub))
    plat.append("role_grant", subject=user, role="operator")
    with hub.pool.connection() as connection:
        rows: list[tuple[Any, ...]] = connection.execute(
            "SELECT role, revoked_seq FROM hub.platform_role WHERE user_id = %s", (user,)
        ).fetchall()
    assert rows == [("operator", None)]
    with pytest.raises(psycopg.errors.CheckViolation), hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.platform_role (user_id, role, granted_seq) VALUES (%s, 'admin', 1)", (user,)
        )

"""U-HUB-orgs (assignment to the whole organization, ADR-075 section 5): `device_assign_org` makes a device a candidate
of every workgroup, also of a later one, the projection keeps `org_device` and `wg_device`, a new workgroup wraps its
key for the device, ending the assignment owes the rotation of everything the device read, and older runners are
kept out (`peer_outdated`)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_hub.errors import HubError
from agent_hub.log.state import OrgState
from agent_hub.orgs import projection
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id


@dataclass
class Org:
    draft: OrgDraft
    admin: Actor
    device: Actor


def org_with_device(world: World) -> Org:
    draft = world.public_org()
    device = world.device()
    world.join(draft, device)
    return Org(draft, draft.founder, device)


def execute(world: World, query: str, *params: object) -> None:
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute(query, params)


def rows(world: World, query: str, *params: object) -> list[tuple[object, ...]]:
    return [tuple(str(value) if value is not None else None for value in row) for row in world.rows(query, *params)]


def test_a_device_for_the_whole_organization(world: World) -> None:
    org = org_with_device(world)
    draft, admin, device = org.draft, org.admin, org.device
    assert rows(world, "SELECT wg_id FROM hub.wg_device WHERE device_id = %s", device.id) == []
    draft.add("device_assign_org", admin, device=device.id)
    draft.append(admin)
    assert rows(world, "SELECT org_id, unassigned_seq FROM hub.org_device WHERE device_id = %s", device.id) == [
        (draft.id, None)
    ]
    assert rows(world, "SELECT wg_id FROM hub.wg_device WHERE device_id = %s", device.id) == [(draft.default_wg,)]
    audit = rows(
        world, "SELECT target_kind, target_id, device_id FROM hub.audit WHERE action = 'log.device_assign_org'"
    )
    assert audit == [("device", device.id, device.id)]
    # a later workgroup: its key is wrapped for the device in the same batch, and the device is assigned to it
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    with pytest.raises(HubError) as refused:
        draft.append(admin, draft.wraps(admin, "wg", wg, 1, [admin]))
    assert refused.value.code == "rotation_required"
    draft.drop_pending()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    draft.append(admin, draft.wraps(admin, "wg", wg, 1, [admin, device]))
    assert sorted(rows(world, "SELECT wg_id FROM hub.wg_device WHERE device_id = %s", device.id)) == sorted(
        [(draft.default_wg,), (wg,)]
    )
    # ending it owes the rotation of every workgroup and of the org scope
    draft.add("device_unassign_org", admin, device=device.id)
    for scope, scope_id in (("org", draft.id), ("wg", draft.default_wg), ("wg", wg)):
        draft.add("rotate", admin, scope=scope, scope_id=scope_id, epoch=2)
    wraps = [
        *draft.wraps(admin, "org", draft.id, 2, [admin]),
        *draft.wraps(admin, "wg", draft.default_wg, 2, [admin]),
        *draft.wraps(admin, "wg", wg, 2, [admin]),
    ]
    draft.append(admin, wraps)
    assert rows(world, "SELECT unassigned_seq IS NULL FROM hub.org_device WHERE device_id = %s", device.id) == [
        ("False",)
    ]
    assert rows(
        world, "SELECT count(*) FROM hub.wg_device WHERE device_id = %s AND unassigned_seq IS NULL", device.id
    ) == [("0",)]


def test_older_runners_keep_the_organization_assignment_out(world: World) -> None:
    org = org_with_device(world)
    draft, admin, device = org.draft, org.admin, org.device
    execute(world, "UPDATE hub.device SET features = '{}' WHERE id = %s", device.id)
    draft.add("device_assign_org", admin, device=device.id)
    with pytest.raises(HubError) as refused:
        draft.append(admin)
    assert refused.value.code == "peer_outdated"
    draft.drop_pending()
    execute(world, "UPDATE hub.device SET features = '{log.org_assign}' WHERE id = %s", device.id)
    draft.add("device_assign_org", admin, device=device.id)
    draft.append(admin)
    # a device joins by its own `accept` (its runner verified the log before it accepted): not refused
    late = world.device()
    execute(world, "UPDATE hub.device SET features = '{}' WHERE id = %s", late.id)
    found = draft.invite(admin, kind="device")
    draft.append(admin)
    draft.accept(late, found)
    draft.append(late)
    # ending the assignment is never blocked by an older device
    draft.add("device_unassign_org", admin, device=device.id)
    for scope, scope_id in (("org", draft.id), ("wg", draft.default_wg)):
        draft.add("rotate", admin, scope=scope, scope_id=scope_id, epoch=2)
    readers = [admin]
    draft.append(
        admin,
        [*draft.wraps(admin, "org", draft.id, 2, readers), *draft.wraps(admin, "wg", draft.default_wg, 2, [admin])],
    )
    # a second organization assignment while an older device is a member: refused
    draft.add("device_assign_org", admin, device=device.id)
    with pytest.raises(HubError) as refused:
        draft.append(admin)
    assert refused.value.code == "peer_outdated"


class Excluded:
    """A platform state stub whose certification of `device` has ended."""

    def __init__(self, device: str) -> None:
        self.device = device

    def excluded(self, _state: OrgState) -> set[str]:
        return {self.device}


def test_a_decertified_organization_device_owes_every_rotation(world: World) -> None:
    org = org_with_device(world)
    org.draft.add("device_assign_org", org.admin, device=org.device.id)
    result = org.draft.append(org.admin)
    state = result.log.state
    owed = projection.pending_rotation(state, Excluded(org.device.id))  # type: ignore[arg-type]
    assert owed == sorted([("org", org.draft.id), ("wg", org.draft.default_wg)])

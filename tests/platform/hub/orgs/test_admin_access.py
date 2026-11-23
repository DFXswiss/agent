"""U-HUB-rights through the projections (KOR-34, ADR-058): a second admin without any workgroup membership reads every
workgroup at the hub (rights API, workgroup list), cannot write it, never makes devices eligible for its credentials,
and loses the read with its key at a demotion (rotation in the same batch); the hub refuses a demotion without it."""

from __future__ import annotations

import uuid

import pytest

from agent_hub.credentials.eligibility import eligible_devices
from agent_hub.orgs import rights
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, World


@pytest.fixture
def scene(world: World) -> Scene:
    """An org with an admin (creator of a workgroup), a writer, a reader and two devices on that workgroup."""
    return Scene.build(world)


def _access(world: World, wg: str, user: Actor) -> tuple[object, ...] | None:
    with world.hub.pool.connection() as connection:
        return connection.execute(
            "SELECT access, inherited, revoked_seq IS NULL FROM hub.wg_access WHERE wg_id = %s AND user_id = %s",
            (wg, user.id),
        ).fetchone()


def _can(world: World, user: Actor, right: str, wg: str) -> bool:
    with world.hub.pool.connection() as connection:
        return rights.can(connection, user.id, right, wg_id=wg)


def test_a_second_admin_reads_every_workgroup_and_loses_it_at_a_demotion(world: World, scene: Scene) -> None:
    draft = scene.draft
    second = world.user()
    world.join(draft, second, role="admin")
    assert _access(world, scene.wg, second) == ("read", True, True)
    assert _can(world, second, rights.WG_READ, scene.wg)
    assert _can(world, second, rights.TASK_READ, scene.wg)
    assert not _can(world, second, rights.WG_WRITE, scene.wg)
    with world.hub.pool.connection() as connection:
        assert rights.wg_access(connection, scene.wg, second.id) == "read"
        # the device of the workgroup is eligible for the reader's credentials, never for the inheriting admin's
        assert [item.device_id for item in eligible_devices(connection, second.id)] == []
        assert uuid.UUID(scene.device.id) in [item.device_id for item in eligible_devices(connection, scene.reader.id)]
    listed = second.browser.get(f"/v1/orgs/{scene.org}")
    assert listed.status_code == 200, listed.text
    assert {wg["wg_id"]: wg["my_access"] for wg in listed.json()["workgroups"]}[scene.wg] == "read"

    # a demotion without the rotation of the workgroup the subject read only as admin is refused
    draft.add("role", scene.admin, subject=second.id, role="member")
    refused = draft.post(scene.admin)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "rotation_required"
    draft.add("rotate", scene.admin, scope="wg", scope_id=scene.wg, epoch=2)
    readers = [scene.admin, scene.writer, scene.reader, scene.device, scene.other_device]
    accepted = draft.post(scene.admin, draft.wraps(scene.admin, "wg", scene.wg, 2, readers))
    assert accepted.status_code == 200, accepted.text
    assert _access(world, scene.wg, second) == ("read", True, False)  # revoked
    assert not _can(world, second, rights.WG_READ, scene.wg)

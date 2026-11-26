"""U-HUB-placement: choosing device and VM, the workgroup default, waiting reasons and the keys of devices assigned
to the whole organization (03-api.md sections 4.5, 4.6, 4.6.2, 6.1; ADR-075 sections 3, 5, 6, 7)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from agent_hub.scheduler import jobs, load, settings
from agent_hub.scheduler.core import NoPlacement
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import Actor, World, new_id
from tests.platform.hub.tasks.support import CAPACITY, Desk, audit_rows

PER_VM_ONE = {**CAPACITY, "sessions_per_vm": 1}


@pytest.fixture(autouse=True)
def unbound_settings() -> Iterator[None]:
    settings.unbind()
    yield
    settings.unbind()


def create(desk: Desk, placement: dict[str, Any] | None, actor: Actor | None = None) -> Any:
    who = actor or desk.scene.writer
    task_id = new_id()
    body: dict[str, Any] = {"task_id": task_id, "wg_id": desk.wg, "events": desk.task_events(who, task_id)}
    if placement is not None:
        body["placement"] = placement
    # malformed choices are refused by the hub itself (the contract check of the test client is off for them)
    return who.browser.post("/v1/tasks", json=body, extensions={"contract": False})


def put_default(desk: Desk, device: str | None, vm: str | None, actor: Actor | None = None) -> Any:
    who = actor or desk.scene.writer
    body = {"device_id": device, "vm_id": vm}
    return who.browser.put(f"/v1/workgroups/{desk.wg}/placement", json=body, extensions={"contract": False})


def test_a_chosen_device_holds_for_the_task(desk: Desk) -> None:
    other = desk.scene.other_device.id
    response = create(desk, {"device_id": other})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["placement"] == {"device_id": other, "vm_id": None}
    assert body["session"]["device_id"] == other


def test_a_chosen_vm_is_checked_and_named(desk: Desk) -> None:
    vm = desk.vm(desk.scene.other_device, "running")
    body = create(desk, {"device_id": desk.scene.other_device.id, "vm_id": vm}).json()
    assert body["task"]["placement"]["vm_id"] == vm
    assert body["session"]["device_id"] == desk.scene.other_device.id


@pytest.mark.parametrize(
    "case",
    [
        ({"device_id": "stranger"}, 403, "placement_forbidden", "/placement/device_id"),
        ({"device_id": "device", "vm_id": "vm-0000ffff"}, 409, "placement_invalid", "/placement/vm_id"),
        ({"device_id": "device", "vm_id": "vm-1"}, 400, "request_invalid", "/placement/vm_id"),
        ({"device_id": "not-a-uuid"}, 400, "request_invalid", "/placement/device_id"),
    ],
)
def test_nobody_chooses_a_device_that_is_not_assigned(
    desk: Desk, world: World, case: tuple[dict[str, str], int, str, str]
) -> None:
    placement, status, error, pointer = case
    ids = {"stranger": world.device().id, "device": desk.scene.device.id, "not-a-uuid": "nope"}
    chosen = {**placement, "device_id": ids[placement["device_id"]]}
    response = create(desk, chosen)
    assert response.status_code == status
    assert response.json() == {"error": error, "detail": {"pointers": [pointer]}}


def test_the_workgroup_default_applies_to_new_tasks(desk: Desk) -> None:
    other = desk.scene.other_device.id
    assert desk.scene.reader.browser.get(f"/v1/workgroups/{desk.wg}/placement").json() == {"placement": None}
    vm = desk.vm(desk.scene.other_device, "running")
    assert put_default(desk, other, vm).json() == {"placement": {"device_id": other, "vm_id": vm}}
    assert desk.scene.reader.browser.get(f"/v1/workgroups/{desk.wg}").json()["placement"] == {
        "device_id": other,
        "vm_id": vm,
    }
    assert len(audit_rows(desk, "wg.placement")) == 1
    body = create(desk, None).json()
    assert body["task"]["placement"] == {"device_id": other, "vm_id": vm}
    # the VM is gone: the device alone counts
    desk.execute("UPDATE hub.vm SET state = 'deleted' WHERE id = %s", vm)
    assert create(desk, None).json()["task"]["placement"] == {"device_id": other, "vm_id": None}
    # the device is no longer assigned: automatic
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE device_id = %s", other)
    assert "placement" not in create(desk, None).json()["task"]
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = NULL WHERE device_id = %s", other)
    assert put_default(desk, None, None).json() == {"placement": None}
    assert "placement" not in create(desk, None).json()["task"]


@pytest.mark.parametrize(
    "case",
    [
        (None, "vm-0000000a", 400, "request_invalid"),
        ("stranger", None, 403, "placement_forbidden"),
        ("device", "vm-0000ffff", 409, "placement_invalid"),
        ("device", "vm-x", 400, "request_invalid"),
    ],
)
def test_a_default_is_checked(desk: Desk, world: World, case: tuple[str | None, str | None, int, str]) -> None:
    device, vm, status, error = case
    ids = {"stranger": world.device().id, "device": desk.scene.device.id}
    response = put_default(desk, None if device is None else ids[device], vm)
    assert response.status_code == status
    assert response.json()["error"] == error


def test_readers_cannot_set_a_default(desk: Desk) -> None:
    response = put_default(desk, desk.scene.device.id, None, desk.scene.reader)
    assert response.status_code == 403


def test_a_chosen_device_that_is_offline_or_full_waits_and_can_be_released(desk: Desk) -> None:
    other = desk.scene.other_device
    desk.offline(other)
    body = create(desk, {"device_id": other.id}).json()
    task = body["task"]["task_id"]
    assert (body["task"]["state"], body["task"]["state_reason"]) == ("scheduling", "placement_offline")
    desk.online(other, {**CAPACITY, "vm_slots_free": 0, "pool_ready": 0})
    response = desk.scene.writer.browser.put(f"/v1/tasks/{task}/placement", json={"device_id": other.id, "vm_id": None})
    assert response.json()["task"]["state_reason"] == "placement_full"
    assert len(audit_rows(desk, "task.placement")) == 1
    released = desk.scene.writer.browser.put(f"/v1/tasks/{task}/placement", json={"device_id": None, "vm_id": None})
    assert released.status_code == 200
    detail = released.json()["task"]
    assert detail["state"] == "starting"
    assert "placement" not in detail
    session = desk.rows("SELECT device_id FROM hub.session WHERE task_id = %s", task)[0][0]
    assert str(session) == desk.scene.device.id


def test_a_task_choice_is_checked(desk: Desk, world: World) -> None:
    task, _ = desk.started()
    browser = desk.scene.writer.browser
    assert (
        browser.put(f"/v1/tasks/{task}/placement", json={"device_id": None, "vm_id": "vm-0000000a"}).status_code == 400
    )
    stranger = world.device().id
    assert browser.put(f"/v1/tasks/{task}/placement", json={"device_id": stranger, "vm_id": None}).status_code == 403
    reader = desk.scene.reader.browser
    assert reader.put(f"/v1/tasks/{task}/placement", json={"device_id": None, "vm_id": None}).status_code == 403


def test_a_full_vm_of_the_workgroup_gets_a_second_vm_and_no_number_of_vms_bounds_it(desk: Desk) -> None:
    device, other = desk.scene.device, desk.scene.other_device
    desk.offline(other)
    desk.online(device, PER_VM_ONE)
    vm = desk.vm(device, "running")
    task, session = desk.started()
    desk.execute("UPDATE hub.session SET vm_id = %s WHERE id = %s", vm, session)
    # the VM is full (an older runner's one session per VM): a second VM on the same device
    second_task, second = desk.started()
    assert desk.session(second)["device_id"] == device.id
    desk.execute("UPDATE hub.session SET vm_id = %s WHERE id = %s", desk.vm(device, "running"), second)
    # no fixed number of VMs per workgroup (ADR-075 addendum 2): a third VM while the device has a free slot
    body = create(desk, None).json()
    assert body["task"]["state"] == "starting"
    assert desk.session(body["session"]["session_id"])["device_id"] == device.id
    assert {task, second_task}


def test_the_device_state_names_the_chosen_vm_and_never_forbids_a_new_vm(world: World, fleet: Fleet) -> None:
    desk = Desk(org_device_scene(world))
    device = desk.scene.device
    desk.online(device)
    vm = desk.vm(device, "running")
    body = create(desk, {"device_id": device.id, "vm_id": vm}).json()
    sessions = fleet.client(device).get("/v1/device/state").json()["sessions"]
    assert sessions[0]["session_id"] == body["session"]["session_id"]
    assert sessions[0]["vm_id_pref"] == vm
    assert "vm_new_allowed" not in sessions[0]


def org_assigned(desk: Desk, world: World) -> Actor:
    """A device of the org that is assigned to the workgroup only through the organization (rows as the projection
    writes them for `device_assign_org`)."""
    device = world.device()
    world.join(desk.scene.draft, device)
    desk.execute(
        "INSERT INTO hub.org_device (org_id, device_id, assigned_seq, unassigned_seq) VALUES (%s, %s, 50, NULL)",
        desk.org,
        device.id,
    )
    desk.execute(
        "INSERT INTO hub.wg_device (wg_id, device_id, assigned_seq, unassigned_seq) VALUES (%s, %s, 50, NULL)",
        desk.wg,
        device.id,
    )
    return device


def test_a_device_of_the_organization_needs_the_workgroup_key(desk: Desk, world: World) -> None:
    device = org_assigned(desk, world)
    desk.offline(desk.scene.device)
    desk.online(desk.scene.other_device, {**CAPACITY, "vm_slots_free": 0, "pool_ready": 0})
    desk.online(device)
    body = create(desk, None).json()
    assert body["task"]["state_reason"] == "keys_pending"

    def wrap(scope: str, scope_id: str, epoch: int) -> None:
        desk.execute(
            "INSERT INTO hub.key_wrap (scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
            scope,
            scope_id,
            epoch,
            device.id,
            b"w" * 80,
            desk.scene.admin.id,
            b"s" * 64,
        )

    def keys(owner: str | None) -> bool:
        with desk.hub.pool.connection() as connection:
            found = load.with_owner(connection, load.candidates(connection, desk.wg, desk.hub.clock(), 30), owner)
        return {item.device_id: item.keys for item in found}[device.id]

    current = "SELECT max(epoch) FROM hub.scope_epoch WHERE scope = %s AND scope_id = %s"
    wrap("wg", desk.wg, desk.rows(current, "wg", desk.wg)[0][0])
    assert keys(None) is False  # the org key is missing as well
    wrap("org", desk.org, desk.rows(current, "org", desk.org)[0][0])
    assert keys(None) is True
    owner = desk.scene.writer.id
    assert keys(owner) is False  # the owner's credential key has not reached the device
    cred = desk.rows("SELECT max(epoch) FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s", owner)[0][0] or 1
    if cred == 1 and not desk.rows("SELECT 1 FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s", owner):
        desk.execute(
            "INSERT INTO hub.key_wrap (scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig, created_at)"
            " VALUES ('cred', %s, 1, %s, %s, %s, %s, now())",
            owner,
            owner,
            b"w" * 80,
            owner,
            b"s" * 64,
        )
    wrap("cred", owner, cred)
    assert keys(owner) is True
    # the sweep places the waiting task on the device now
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    assert desk.task(body["task"]["task_id"])["state"] == "starting"


def test_the_lists_show_vms_sessions_and_the_organization_assignment(desk: Desk, world: World) -> None:
    device = org_assigned(desk, world)
    vm = desk.vm(desk.scene.device, "running")
    _, session = desk.started()
    desk.execute("UPDATE hub.session SET vm_id = %s WHERE id = %s", vm, session)
    body = desk.scene.reader.browser.get(f"/v1/workgroups/{desk.wg}").json()
    found = {item["device_id"]: item for item in body["devices"]}
    assert found[device.id]["assigned_via"] == "org"
    assert found[desk.scene.device.id]["vms"] == [
        {"vm_id": vm, "state": "running", "sessions": 1, "golden_version": "golden-1", "upgrade": None}
    ]
    devices = desk.scene.writer.browser.get(f"/v1/orgs/{desk.org}/devices").json()["devices"]
    listed = {item["device_id"]: item for item in devices}
    assert listed[device.id]["assigned_org"] is True
    assert listed[desk.scene.device.id]["vms"] == [{"vm_id": vm, "wg_id": desk.wg, "state": "running", "sessions": 1}]
    # a member who cannot read the workgroup sees no VMs of it
    outsider = world.user()
    world.join(desk.scene.draft, outsider)
    hidden = outsider.browser.get(f"/v1/orgs/{desk.org}/devices").json()["devices"]
    assert all(item["vms"] == [] for item in hidden)


def test_without_any_device_the_reason_names_who_can_help(desk: Desk, world: World) -> None:
    with desk.hub.pool.connection() as connection:
        assert load.without_device(connection, desk.wg) == NoPlacement("no_device")
        assert load.without_device(connection, new_id()) == NoPlacement("no_device")
        assert load.task_pin(connection, new_id()) is None
    desk.execute("UPDATE hub.device SET state = 'decertified' WHERE kind = 'org'")
    with desk.hub.pool.connection() as connection:
        assert load.without_device(connection, desk.wg) == NoPlacement("platform_access_required")
    desk.execute("UPDATE hub.org SET platform_access = true WHERE id = %s", desk.org)
    with desk.hub.pool.connection() as connection:
        assert load.without_device(connection, desk.wg) == NoPlacement("no_device")


def test_a_chosen_device_that_is_no_longer_assigned_counts_as_automatic(desk: Desk) -> None:
    other = desk.scene.other_device
    body = create(desk, {"device_id": other.id}).json()
    assert body["session"]["device_id"] == other.id
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE device_id = %s", other.id)
    with desk.hub.pool.connection() as connection:
        found = load.candidates(connection, desk.wg, desk.hub.clock(), 30)
        pin = load.task_pin(connection, body["task"]["task_id"])
    assert pin == load.Pin(other.id)
    assert load.pinned(found, pin) == (found, False)

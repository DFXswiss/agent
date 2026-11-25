"""U-HUB-vmswitch: the switch of workgroup VMs to the newest golden (ADR-093 sections 3-5; 03-api.md sections 4.5,
6.1): the status a device reports with `vm_state`, the workgroup view, the request by hand of a member with
administration rights, the desired state's `vm_switch`, a member's VM choice that follows the switch and the restored
session's `context_reason`."""

from __future__ import annotations

from typing import Any

from agent_hub.outbox.bodies import VmStateBody
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import Outbox, new_vm

SESSION = "01930003-0000-7000-8000-000000000001"


def upgrade(outbox: Outbox, *holds: dict[str, Any], state: str = "waiting") -> dict[str, Any]:
    return {
        "golden_version": "golden-2",
        "golden_built_at": outbox.now(-60),
        "state": state,
        "holds": list(holds),
        "sessions": [SESSION],
    }


def stored(outbox: Outbox, vm: str) -> tuple[Any, ...]:
    [row] = outbox.desk.rows(
        "SELECT upgrade, replaced_by, switch_requested_at, switch_requested_by FROM hub.vm WHERE id = %s", vm
    )
    return tuple(row)


def view_vms(outbox: Outbox, actor: Actor) -> list[dict[str, Any]]:
    response = actor.browser.get(f"/v1/workgroups/{outbox.desk.wg}")
    assert response.status_code == 200, response.text
    return [vm for device in response.json()["devices"] for vm in device["vms"]]


def switch(outbox: Outbox, actor: Actor, vm: str, wg: str | None = None) -> Any:
    return actor.browser.post(
        f"/v1/workgroups/{wg or outbox.desk.wg}/vms/{vm}/switch", json={}, extensions={"contract": False}
    )


def desired(fleet: Fleet, device: Actor) -> dict[str, Any]:
    response = fleet.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_the_reported_status_is_stored_shown_kept_and_cleared(outbox: Outbox) -> None:
    vm = new_vm()
    hold = {"reason": "session_active", "session_id": SESSION, "since": outbox.now(-30)}
    status = upgrade(outbox, hold)
    assert outbox.statuses(outbox.vm(1, vm, upgrade=status)) == [(1, "ok", None)]
    assert stored(outbox, vm)[0] == status
    [shown] = view_vms(outbox, outbox.scene.reader)
    assert (shown["vm_id"], shown["golden_version"], shown["upgrade"]) == (vm, "golden-1", status)
    # a report without the field leaves the status as it was
    assert outbox.statuses(outbox.vm(2, vm, state="stopped")) == [(2, "ok", None)]
    assert stored(outbox, vm)[0] == status
    # null: the VM is current
    assert outbox.statuses(outbox.vm(3, vm, state="stopped", upgrade=None)) == [(3, "ok", None)]
    assert stored(outbox, vm)[0] is None
    assert view_vms(outbox, outbox.scene.reader)[0]["upgrade"] is None


def test_a_member_with_administration_rights_asks_for_the_switch(
    outbox: Outbox, fleet: Fleet, published: MemoryPublisher
) -> None:
    scene = outbox.scene
    vm = new_vm()
    outbox.ok(outbox.vm(1, vm, state="stopped", upgrade=upgrade(outbox)))
    assert desired(fleet, scene.device)["vm_switch"] == []
    # a member without administration rights, a stranger, an unknown VM and a malformed ID
    assert switch(outbox, scene.writer, vm).status_code == 403
    assert switch(outbox, scene.stranger, vm).status_code == 404
    assert switch(outbox, scene.admin, new_vm()).status_code == 404
    assert switch(outbox, scene.admin, "vm-x").status_code == 404
    published.clear()
    response = switch(outbox, scene.admin, vm)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["requested"] is True
    assert answer["upgrade"]["sessions"] == [SESSION]
    assert (answer["vm_id"], answer["device_id"], answer["golden_version"]) == (vm, scene.device.id, "golden-1")
    first = answer["requested_at"]
    assert [str(entry.audience.id) for entry in published.items() if entry.frame == "reconcile"] == [scene.device.id]
    [audit] = outbox.desk.rows(
        "SELECT actor_id, detail, wg_id FROM hub.audit WHERE action = 'vm.switch_requested' ORDER BY seq"
    )
    assert (str(audit[0]), str(audit[2])) == (scene.admin.id, outbox.desk.wg)
    assert audit[1] == {"vm_id": vm, "device_id": scene.device.id, "requested": True}
    assert desired(fleet, scene.device)["vm_switch"] == [vm]
    assert desired(fleet, scene.other_device)["vm_switch"] == []
    # asked again: the first time stays
    outbox.hub.clock.advance(5)
    assert switch(outbox, scene.admin, vm).json()["requested_at"] == first
    # the VM is current: the request ends
    outbox.ok(outbox.vm(2, vm, state="stopped", upgrade=None))
    assert stored(outbox, vm)[2:] == (None, None)
    assert desired(fleet, scene.device)["vm_switch"] == []


def test_a_current_vm_stores_no_request(outbox: Outbox, published: MemoryPublisher) -> None:
    vm = new_vm()
    outbox.ok(outbox.vm(1, vm))
    published.clear()
    answer = switch(outbox, outbox.scene.admin, vm).json()
    assert (answer["requested"], answer["requested_at"], answer["upgrade"]) == (False, None, None)
    assert [entry for entry in published.items() if entry.frame == "reconcile"] == []
    assert [row[0]["requested"] for row in outbox.desk.rows(
        "SELECT detail FROM hub.audit WHERE action = 'vm.switch_requested'"
    )] == [False]  # fmt: skip


def test_a_switched_vm_hands_the_members_choice_to_its_successor(outbox: Outbox, fleet: Fleet) -> None:
    desk, scene = outbox.desk, outbox.scene
    old, new = new_vm(), new_vm()
    outbox.ok(outbox.vm(1, old, state="stopped", upgrade=upgrade(outbox)))
    switch(outbox, scene.admin, old)
    desk.execute(
        "INSERT INTO hub.wg_placement (wg_id, device_id, vm_id, set_by, set_at) VALUES (%s, %s, %s, %s, now())",
        desk.wg,
        scene.device.id,
        old,
        scene.admin.id,
    )
    task = desk.create(scene.writer).json()["task"]["task_id"]
    desk.execute(
        "UPDATE hub.task SET placement_device_id = %s, placement_vm_id = %s WHERE id = %s", scene.device.id, old, task
    )
    outbox.ok(outbox.vm(2, new, state="running"), outbox.vm(3, old, state="deleted", replaced_by=new))
    assert stored(outbox, old) == (None, new, None, None)
    assert desk.rows("SELECT vm_id FROM hub.wg_placement WHERE wg_id = %s", desk.wg) == [(new,)]
    assert desk.rows("SELECT placement_vm_id FROM hub.task WHERE id = %s", task) == [(new,)]
    assert desired(fleet, scene.device)["vm_switch"] == []


def test_malformed_reports_are_rejected() -> None:
    base = {
        "vm_id": "vm-0000000a",
        "wg_id": None,
        "state": "deleted",
        "golden_version": "g1",
        "cpus": 4,
        "memory_mib": 8192,
        "disk_gib": 20,
        "at": "2026-11-21T12:00:00.000Z",
    }
    VmStateBody.model_validate(base)
    for bad in (
        {"replaced_by": None},
        {"upgrade": {"golden_version": "g2", "golden_built_at": None, "state": "waiting", "holds": [
            {"reason": "session_active", "session_id": None, "since": "2026-11-21T12:00:00.000Z"}], "sessions": []}},
    ):  # fmt: skip
        try:
            VmStateBody.model_validate({**base, **bad})
        except ValueError:
            continue
        raise AssertionError(bad)


def test_a_restore_after_a_switch_is_named_on_the_session(outbox: Outbox) -> None:
    _task, session = outbox.session()
    detail = {"object_id": new_id(), "workspace": "full", "stale": False, "moved": "golden"}
    assert outbox.statuses(
        outbox.audit(1, "session.context_restored", detail, session_id=session, target_id=session)
    ) == [(1, "ok", None)]
    assert outbox.desk.rows("SELECT context_restore, context_reason FROM hub.session WHERE id = %s", session) == [
        ("restored", "golden_switch")
    ]
    partial = {**detail, "workspace": "partial"}
    outbox.ok(outbox.audit(2, "session.context_restored", partial, session_id=session, target_id=session))
    assert outbox.desk.rows("SELECT context_reason FROM hub.session WHERE id = %s", session) == [("workspace_partial",)]

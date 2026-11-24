"""U-HUB-outbox (session VM): `session.vm_id` (02-data-model.md section 2.4) follows the VM the device reports for the
session's workgroup. The contract carries no VM in `session_transition` (03-api.md section 6.1); a device runs one VM
per workgroup (05-runner.md section 3), so the hub links the open sessions of a device and workgroup to the VM that
`vm_state` reports as `assigning`/`running` there, whichever of the two items arrives first."""

from __future__ import annotations

from tests.platform.hub.outbox.support import Outbox, new_vm


def _vm_of(outbox: Outbox, session: str) -> str | None:
    rows = outbox.desk.rows("SELECT vm_id FROM hub.session WHERE id = %s", session)
    return None if rows[0][0] is None else str(rows[0][0])


def test_vm_state_links_the_open_sessions_of_its_workgroup(outbox: Outbox) -> None:
    _task, session = outbox.session()
    assert _vm_of(outbox, session) is None
    pool, vm = new_vm(), new_vm()
    outbox.ok(outbox.vm(1, pool, "pool", wg=None))
    assert _vm_of(outbox, session) is None
    outbox.ok(outbox.vm(2, vm, "assigning"))
    assert _vm_of(outbox, session) == vm
    outbox.ok(outbox.vm(3, vm, "stopped"))
    assert _vm_of(outbox, session) == vm  # a stopped VM keeps the link until another VM runs the workgroup
    replacement = new_vm()
    outbox.ok(outbox.vm(4, replacement, "running"))
    assert _vm_of(outbox, session) == replacement


def test_ended_sessions_and_other_devices_keep_their_vm(outbox: Outbox) -> None:
    _task, ended = outbox.session()
    _task2, foreign = outbox.session(outbox.scene.other_device)
    outbox.desk.execute("UPDATE hub.session SET ended_at = now() WHERE id = %s", ended)
    outbox.ok(outbox.vm(1, new_vm(), "running"))
    assert (_vm_of(outbox, ended), _vm_of(outbox, foreign)) == (None, None)


def test_a_transition_takes_the_running_vm_of_its_workgroup(outbox: Outbox) -> None:
    stopped, vm = new_vm(), new_vm()
    outbox.ok(outbox.vm(1, stopped, "stopped"))
    _task, session = outbox.session()
    outbox.ok(outbox.transition(2, session, "idle"))
    assert _vm_of(outbox, session) is None  # no running VM of the workgroup yet
    outbox.ok(outbox.vm(3, vm, "running"))
    outbox.desk.execute("UPDATE hub.session SET vm_id = NULL WHERE id = %s", session)
    outbox.ok(outbox.transition(4, session, "working"))
    assert _vm_of(outbox, session) == vm


def _choose_vms(outbox: Outbox) -> None:
    """The device reports the feature `vm_choice` (several VMs per workgroup, ADR-060)."""
    outbox.desk.execute(
        'UPDATE hub.device SET capacity = capacity || \'{"features": ["vm_choice"]}\'::jsonb WHERE id = %s',
        outbox.scene.device.id,
    )


def test_a_device_that_chooses_vms_names_the_vm_of_each_session(outbox: Outbox) -> None:
    _choose_vms(outbox)
    _task, session = outbox.session()
    _task2, second = outbox.session()
    first_vm, second_vm = new_vm(), new_vm()
    outbox.ok(outbox.vm(1, first_vm, "running"))
    assert (_vm_of(outbox, session), _vm_of(outbox, second)) == (None, None)  # no workgroup-wide link
    outbox.ok(outbox.transition(2, session, "idle"))
    assert _vm_of(outbox, session) is None
    outbox.ok(outbox.transition(3, session, "working", vm_id=first_vm))
    outbox.ok(outbox.vm(4, second_vm, "running"))
    outbox.ok(outbox.transition(5, second, "idle", vm_id=second_vm))
    assert (_vm_of(outbox, session), _vm_of(outbox, second)) == (first_vm, second_vm)


def test_a_named_vm_of_another_device_or_workgroup_is_ignored(outbox: Outbox) -> None:
    _choose_vms(outbox)
    _task, session = outbox.session()
    foreign = new_vm()
    outbox.desk.execute(
        "INSERT INTO hub.vm (id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib, created_at,"
        " state_at) VALUES (%s, %s, %s, 'running', 'golden-1', 4, 8192, 20, now(), now())",
        foreign,
        outbox.scene.other_device.id,
        outbox.desk.wg,
    )
    outbox.ok(outbox.transition(1, session, "idle", vm_id=foreign))
    assert _vm_of(outbox, session) is None

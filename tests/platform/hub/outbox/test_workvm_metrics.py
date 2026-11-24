"""U-HUB-outbox (work VM metrics, KOR-29): `workvm` samples name their session, which runs on the pushing device; a
work VM ID stays with the device and session of its first samples (agent docs/platform/spec/13-work-vm.md
section 10)."""

from __future__ import annotations

from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.outbox.support import NOT_CONTRACT, Outbox, item, sample

WVM = "wvm-0a1b2c3d"


def test_work_vm_samples_are_stored_with_their_session(outbox: Outbox) -> None:
    _task, session = outbox.session()
    first = sample("workvm", WVM, outbox.now(-15), session_id=session, cpu_ratio=2.0)
    later = sample("workvm", WVM, outbox.now(), session_id=session)
    device = sample("device", outbox.device.id, outbox.now())
    assert outbox.statuses(item(1, "metrics", {"samples": [first, later, device]})) == [(1, "ok", None)]
    rows = outbox.desk.rows(
        "SELECT entity_kind, entity_id, device_id::text, session_id::text, cpu_ratio FROM hub.metric_sample"
        " ORDER BY entity_kind, ts"
    )
    assert rows == [
        ("device", outbox.device.id, outbox.device.id, None, 0.5),
        ("workvm", WVM, outbox.device.id, session, 2.0),
        ("workvm", WVM, outbox.device.id, session, 0.5),
    ]
    # the next item of the same work VM and session is accepted
    later_item = item(2, "metrics", {"samples": [sample("workvm", WVM, outbox.now(5), session_id=session)]})
    assert outbox.statuses(later_item) == [(2, "ok", None)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.metric_sample WHERE entity_kind = 'workvm'") == [(3,)]


def test_work_vm_sample_rights(outbox: Outbox) -> None:
    other = outbox.scene.other_device
    _task, mine = outbox.session()
    _task, second = outbox.session()
    _task, foreign = outbox.session(other)
    ts = outbox.now()
    assert outbox.statuses(item(1, "metrics", {"samples": [sample("workvm", WVM, ts, session_id=mine)]})) == [
        (1, "ok", None)
    ]
    cases = [
        sample("workvm", "wvm-00000001", ts, session_id=foreign),  # a session of another device
        sample("workvm", "wvm-00000002", ts, session_id=new_id()),  # an unknown session
        sample("workvm", WVM, outbox.now(1), session_id=second),  # the work VM belongs to another session
    ]
    statuses = outbox.statuses(*[item(seq, "metrics", {"samples": [bad]}) for seq, bad in enumerate(cases, 2)])
    # a work VM of another session is dropped; the rest of the item is stored
    assert statuses == [(2, "rejected", "forbidden"), (3, "rejected", "not_found"), (4, "ok", None)]
    # another device cannot write into the series of this work VM, even with its own session; an ID two devices
    # happen to draw costs only that work VM's samples, never the device's other samples of the item
    stolen = item(
        1,
        "metrics",
        {
            "samples": [
                sample("workvm", WVM, outbox.now(2), session_id=foreign),
                sample("device", other.id, outbox.now(2)),
            ]
        },
    )
    assert outbox.statuses(stolen, device=other) == [(1, "ok", None)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.metric_sample WHERE entity_kind = 'workvm'") == [(1,)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.metric_sample WHERE entity_kind = 'device'") == [(1,)]


def test_work_vm_sample_shape(outbox: Outbox) -> None:
    _task, session = outbox.session()
    ts = outbox.now()
    cases = [
        sample("workvm", WVM, ts),  # no session
        sample("session", session, ts, session_id=session),  # a session only on a work VM sample
        sample("workvm", "vm-0a1b2c3d", ts, session_id=session),  # a VM ID as a work VM
        sample("vm", WVM, ts),  # a work VM ID as a VM
        sample("device", WVM, ts),  # a work VM ID as another kind
        sample("workvm", session, ts, session_id=session),  # a UUID as a work VM
    ]
    for seq, bad in enumerate(cases, 1):
        response = outbox.push(item(seq, "metrics", {"samples": [bad]}), extensions=NOT_CONTRACT)
        assert response.json()["results"] == [{"origin_seq": seq, "status": "rejected", "error": "request_invalid"}]

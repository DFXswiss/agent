"""U-HUB-analysis (metrics): `GET /v1/device/workgroups/{wg}/metrics` (ADR-095 section 4). The monitoring series of
a session, workgroup VM or work VM of the workgroup by the rules of `GET /v1/monitoring/series`, at most 1000 points;
an entity of another workgroup (or none) is `404 not_found`."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.analysis.support import NOT_FOUND, Reads, invalid
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.monitoring.support import insert_sample
from tests.platform.hub.orgs.support import World, new_id


@pytest.fixture
def reads(world: World, fleet: Fleet) -> Reads:
    return Reads.build(world, fleet)


def _vm(reads: Reads, wg: str) -> str:
    vm_id = "vm-" + new_id().replace("-", "")[-8:]
    reads.execute(
        "INSERT INTO hub.vm (id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib, created_at,"
        " state_at) VALUES (%s, %s, %s, 'running', 'golden-1', 4, 8192, 20, %s, %s)",
        vm_id,
        reads.scene.device.id,
        wg,
        reads.now(),
        reads.now(),
    )
    return vm_id


def _query(reads: Reads, kind: str, entity: str, **changes: str) -> dict[str, str]:
    return {
        "entity_kind": kind,
        "entity_id": entity,
        "metric": "cpu_ratio",
        "since": format_time(reads.ago(minutes=10)),
        "step": "60",
        **changes,
    }


def _sample(reads: Reads, kind: str, entity: str, minutes: float, session: str | None = None, **values: Any) -> None:
    at = reads.ago(minutes=minutes)
    insert_sample(reads.hub, kind, entity, at, reads.scene.device.id, session_id=session, **values)


def test_the_series_of_a_session_a_vm_and_a_work_vm_of_the_workgroup(reads: Reads) -> None:
    task = reads.task(reads.ago(hours=1))
    session = reads.session(task, reads.ago(hours=1))
    vm = _vm(reads, reads.wg)
    workvm = "wvm-0123abcd"
    _sample(reads, "session", session, 5, cpu_ratio=0.25)
    _sample(reads, "vm", vm, 5, cpu_ratio=0.75)
    _sample(reads, "workvm", workvm, 5, session=session, reachable=True)
    body = reads.ok("metrics", **_query(reads, "session", session))
    assert (body["entity_kind"], body["entity_id"], body["metric"]) == ("session", session, "cpu_ratio")
    assert len(body["points"]) == 10
    assert body["points"][0] == [format_time(reads.ago(minutes=10)), None]
    assert [point[1] for point in body["points"] if point[1] is not None] == [0.25]
    vm_body = reads.ok("metrics", **_query(reads, "vm", vm, until=format_time(reads.ago(minutes=4))))
    assert [point[1] for point in vm_body["points"] if point[1] is not None] == [0.75]
    assert len(vm_body["points"]) == 6
    workvm_body = reads.ok("metrics", **_query(reads, "workvm", workvm, metric="reachable"))
    assert [point[1] for point in workvm_body["points"] if point[1] is not None] == [1]


def test_an_entity_of_another_workgroup_or_none_is_not_found(reads: Reads) -> None:
    foreign_task = reads.task(reads.ago(hours=1), wg=reads.other_wg)
    foreign = reads.session(foreign_task, reads.ago(hours=1), wg=reads.other_wg)
    unassigned = _vm(reads, reads.other_wg)
    reads.execute("UPDATE hub.vm SET wg_id = NULL WHERE id = %s", unassigned)
    _sample(reads, "workvm", "wvm-0000beef", 5, session=foreign)
    for kind, entity in (
        ("session", foreign),
        ("session", new_id()),
        ("vm", unassigned),
        ("vm", "vm-00000000"),
        ("workvm", "wvm-0000beef"),
        ("workvm", "wvm-ffffffff"),
    ):
        assert reads.refused("metrics", **_query(reads, kind, entity)) == NOT_FOUND, (kind, entity)
    task = reads.task(reads.ago(hours=1))
    session = reads.session(task, reads.ago(hours=1))
    for wg in (reads.other_wg, new_id(), "x"):
        assert reads.refused("metrics", wg=wg, **_query(reads, "session", session)) == NOT_FOUND, wg


@pytest.mark.parametrize(
    ("changes", "pointer"),
    [
        ({"entity_kind": "device"}, "/entity_kind"),
        ({"entity_kind": ""}, "/entity_kind"),
        ({"entity_id": "vm-0123abcd"}, "/entity_id"),
        ({"metric": "state"}, "/metric"),
        ({"since": "2026-11-21"}, "/since"),
        ({"until": "2026-11-21"}, "/until"),
        ({"since": "2026-11-21T12:00:00.123Z"}, "/since"),
        ({"step": "14"}, "/step"),
        ({"step": "x"}, "/step"),
        ({"since": "2026-11-20T12:00:00.123Z"}, "/step"),
    ],
)
def test_metric_parameter_errors_name_the_parameter(reads: Reads, changes: dict[str, str], pointer: str) -> None:
    task = reads.task(reads.ago(hours=1))
    session = reads.session(task, reads.ago(hours=1))
    assert reads.refused("metrics", **_query(reads, "session", session, **changes)) == invalid(pointer)


def test_a_missing_parameter_is_named(reads: Reads) -> None:
    task = reads.task(reads.ago(hours=1))
    session = reads.session(task, reads.ago(hours=1))
    for name in ("entity_kind", "entity_id", "metric", "since", "step"):
        query = {key: value for key, value in _query(reads, "session", session).items() if key != name}
        assert reads.refused("metrics", **query) == invalid(f"/{name}"), name

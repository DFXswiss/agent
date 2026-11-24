"""U-RUN-metrics (work VMs, KOR-29): the live work VMs as monitoring entries attributed to their session, host values
where the probes reach them, the runtime otherwise, and the `workvm` samples for the hub against the outbox contract
(docs/platform/spec/13-work-vm.md section 10)."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.metrics import outbox
from agent_runner.metrics import vm as metrics_vm
from agent_runner.metrics import workvm as workvm_metrics
from agent_runner.metrics.vm import HostVm
from agent_runner.metrics.workvm import GIB, MIB, WorkvmEntry, entries
from agent_runner.store.migrate import migrate
from agent_runner.vm.lima import LimaError
from agent_runner.workvm import records
from agent_runner.workvm.manager import WorkVms
from agent_runner.workvm.service import install

from ..workvm.kit import CHILD, SESSION, START, World, session
from .test_sinks import DEVICE_ID, snapshot

REPO = Path(__file__).parents[4]
HOST = HostVm(
    vz_pid=10,
    hostagent_pid=11,
    cpu_ratio=1.25,
    memory_bytes=3 * GIB,
    uptime_seconds=40,
    disk_used_bytes=5 * GIB,
    net_rx_bytes=700,
    net_tx_bytes=800,
)


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path) -> World:
    assert migrate(runner_dsn).status == "applied"
    return World(dsn=runner_dsn, tmp=tmp_path)


class Failing:
    """The Lima of a slot whose listing fails."""

    def list(self) -> list[Any]:
        raise LimaError("lima_failed", "list")


def test_entries_of_the_live_work_vms(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    seen: list[list[str]] = []
    reached: list[str] = []  # the Lima name of the VM the probes reach

    async def host_values(_tools: Any, instances: Any) -> dict[str, HostVm]:
        names = [instance.name for instance in instances]
        seen.append(names)
        return {name: HOST for name in names if name in reached}

    monkeypatch.setattr(metrics_vm, "host_values", host_values)

    async def body(vms: WorkVms) -> tuple[Any, ...]:
        context = SimpleNamespace()
        assert await entries(context, None, START, lines.append) == ()  # type: ignore[arg-type]
        install(context, vms)
        assert await entries(context, None, START, lines.append) == ()  # type: ignore[arg-type]
        task_vm = await vms.request(session(), {})
        own = await vms.request(session(CHILD), {"scope": "session", "size": "large"})
        reached.append((await records.get(vms.deps.db, task_vm["workvm_id"])).lima_name)  # type: ignore[union-attr]
        later = START + timedelta(minutes=2)
        found = await entries(context, None, later, lines.append)  # type: ignore[arg-type]
        broken = (await records.get(vms.deps.db, own["workvm_id"])).slot  # type: ignore[union-attr]
        listed = vms.deps.lima_for
        vms.deps.lima_for = lambda slot: Failing() if slot == broken else listed(slot)  # type: ignore[assignment,return-value]
        partial = await entries(context, None, later, lines.append)  # type: ignore[arg-type]
        return task_vm, own, found, partial, broken

    task_vm, own, found, partial, broken = world.run(body)
    # both VMs start at the same moment: their order is that of their random IDs
    by_id = {entry.workvm_id: entry for entry in found}
    assert len(by_id) == len(found) == 2
    first, second = by_id[task_vm["workvm_id"]], by_id[own["workvm_id"]]
    assert first == WorkvmEntry(
        workvm_id=task_vm["workvm_id"],
        session_id=SESSION,
        state="running",
        cpu_ratio=1.25,
        memory_bytes=3 * GIB,
        memory_limit_bytes=8192 * MIB,
        disk_used_bytes=5 * GIB,
        disk_limit_bytes=50 * GIB,
        uptime_seconds=120,
        net_rx_bytes=700,
        net_tx_bytes=800,
    )
    # the probes did not reach the second VM: sizes, state and runtime only
    assert (second.workvm_id, second.session_id, second.cpu_ratio, second.memory_bytes) == (
        own["workvm_id"],
        CHILD,
        None,
        None,
    )
    assert (second.memory_limit_bytes, second.disk_limit_bytes, second.uptime_seconds) == (16384 * MIB, 100 * GIB, 120)
    assert (second.disk_used_bytes, second.net_rx_bytes, second.net_tx_bytes) == (None, None, None)
    assert len(seen[0]) == 2
    # a slot whose listing fails is left out of the host values, its VM is still an entry
    assert sorted(entry.workvm_id for entry in partial) == sorted([task_vm["workvm_id"], own["workvm_id"]])
    assert lines == [f"workvm_list_failed slot={broken} code=lima_failed"]


class Hanging:
    """The Lima of a slot whose listing does not answer in time."""

    def list(self) -> list[Any]:
        time.sleep(0.5)
        return []


def test_a_hanging_listing_does_not_hold_up_the_round(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workvm_metrics, "LIST_TIMEOUT_S", 0.05)
    lines: list[str] = []

    async def body(vms: WorkVms) -> tuple[Any, ...]:
        context = SimpleNamespace()
        install(context, vms)
        await vms.request(session(), {})
        vms.deps.lima_for = lambda slot: Hanging()  # type: ignore[assignment,return-value]
        return await entries(context, None, START, lines.append)  # type: ignore[arg-type]

    [entry] = world.run(body)
    assert entry.cpu_ratio is None
    assert lines == ["workvm_list_failed slot=0 code=timeout"]


def test_a_deleted_work_vm_is_no_entry(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[Any, ...]:
        context = SimpleNamespace()
        install(context, vms)
        await vms.request(session(), {})
        vm = (await records.live(vms.deps.db))[0]
        await vms.delete(vm, reason="released")
        return await entries(context, None, START, lambda _: None)  # type: ignore[arg-type]

    assert world.run(body) == ()


def test_work_vm_samples_match_the_outbox_contract() -> None:
    entry = WorkvmEntry("wvm-0a1b2c3d", SESSION, "running", None, None, 8 * GIB, None, 50 * GIB, 60, None, None)
    snap = snapshot(workvms=(entry,))
    samples = outbox.samples_of(snap, DEVICE_ID, [])
    schemas = SchemaSet(str(REPO / "contracts"))
    assert schemas.errors("outbox/metrics.json", {"samples": samples}) == []
    sample = samples[-1]
    assert (sample["entity_kind"], sample["entity_id"], sample["session_id"]) == ("workvm", "wvm-0a1b2c3d", SESSION)
    assert (sample["uptime_seconds"], sample["mem_limit_bytes"], sample["reachable"]) == (60, 8 * GIB, True)
    assert "session_id" not in samples[0]

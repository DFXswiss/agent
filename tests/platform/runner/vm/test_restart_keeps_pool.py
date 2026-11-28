"""U-RUN-pool restart (KOR-03, AUD-01 B-30): pool VMs filled before a restart - for example before the enrollment,
which the service follows with a restart - are kept by the startup reconciliation; only a fill that the stop
interrupted had no row yet, and the stop itself deletes that one (A-06, `test_pool_stop.py`)."""

from __future__ import annotations

from typing import Any

from agent_runner.vm.lifecycle import VmManager

from .guests import Harness


def test_pool_vms_survive_a_restart(harness: Harness) -> None:
    harness.add_golden()

    async def fill(manager: VmManager) -> Any:
        return await manager.refill()

    assert harness.run(fill) == 1  # the fixed pool of 1 (ADR-009 addendum)
    before = harness.live()
    assert [row["state"] for row in before] == ["pool"]

    async def restart(manager: VmManager) -> Any:
        return await manager.reconcile()

    counts = harness.run(restart, reconcile=False)
    assert counts == {"deleted": 0, "missing": 0, "state": 0, "unknown": 0, "tokens": 0}
    assert harness.live() == before
    assert not [name for name, _fields in harness.logs if name in ("vm.unknown_deleted", "vm.deleted")]

"""Checks before anything starts; each failure stops the run with its name.

- `CON-04` (07-testing.md section 4): `contracts/VERSION` is the same in the hub and the runner checkout (one
  repository since INF-06; on a second device the runner runs from a copy of the same commit).
- `traceability` (R-P.4): `test_traceability` structure checks (matrix, document mirror, spec tags).
- `ports`: every port of the run is free on its device.
- `vm-budget`: the planned VMs fit into the device's VM memory budget.
(The VM prefix is checked when the configuration is built.)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orchestrator import traceability, vms
from orchestrator.config import RunConfig
from orchestrator.node import Node, NodeError


@dataclass(frozen=True)
class Problem:
    check: str
    detail: str

    def __str__(self) -> str:
        return f"pre-check {self.check} failed: {self.detail}"


def contracts_version(core: Node, core_path: str, agent: Node, agent_path: str) -> Problem | None:
    """CON-04: both checkouts carry the same contracts version."""
    versions = []
    for node, path in ((core, core_path), (agent, agent_path)):
        try:
            versions.append(node.read_text(f"{path}/contracts/VERSION").strip())
        except NodeError:
            return Problem("CON-04", f"{path}/contracts/VERSION is missing")
    if versions[0] != versions[1] or not versions[0]:
        return Problem("CON-04", f"contracts/VERSION differs (agent-core {versions[0][:12]}, agent {versions[1][:12]})")
    return None


def trace(config: RunConfig) -> Problem | None:
    report = traceability.check_suite(config.suite)
    if not report.ok:
        return Problem("traceability", "; ".join(report.problems[:10]))
    return None


def ports(node: Node, numbers: list[int], host: str = "127.0.0.1") -> Problem | None:
    busy = node.busy_ports(numbers, host)
    if busy:
        return Problem("ports", f"{node.name}: ports in use: {', '.join(map(str, busy))}")
    return None


def budget(node: Node, config: RunConfig, default: Callable[[Node], int] = vms.default_budget) -> Problem | None:
    try:
        listed = vms.list_vms(node)
        if listed is None:
            return None if config.pool_size == 0 else Problem("vm-budget", f"{node.name}: Lima is not installed")
        limit = config.memory_budget_mib if config.memory_budget_mib is not None else default(node)
        vms.check_budget(listed, config.runners * config.pool_size * config.vm_memory_mib, limit)
    except vms.VmError as exc:
        return Problem("vm-budget", f"{node.name}: {exc}")
    return None

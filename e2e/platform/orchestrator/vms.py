"""Lima VMs of a run: memory budget before the start, deletion and leftover check at the end (07-testing.md sections
5.5 and 7).

Budget rule (same as the runner's): memory of the running VMs of the device plus the VMs the run plans
(runners * pool size * VM memory) must stay within the budget, by default the device memory minus the runner's
memory reserve. Cleanup deletes every instance whose name starts with the run prefix and fails when one is left.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.node import Node

MIB = 1024 * 1024
DEFAULT_RESERVE_MIB = 32768
NOT_FOUND = 127


@dataclass(frozen=True)
class Vm:
    name: str
    status: str
    memory_mib: int


def parse_list(text: str) -> list[Vm]:
    """`limactl list --json` output (one JSON object per line)."""
    vms = []
    for line in text.splitlines():
        if line.strip():
            entry = json.loads(line)
            vms.append(Vm(str(entry["name"]), str(entry.get("status", "")), int(entry.get("memory", 0)) // MIB))
    return vms


def list_vms(node: Node) -> list[Vm] | None:
    """The device's instances; None when Lima is not installed there."""
    done = node.run(["limactl", "list", "--json"])
    if done["rc"] == NOT_FOUND:
        return None
    if done["rc"] != 0:
        raise VmError(f"limactl list failed: {done['stderr'].strip()[-300:]}")
    return parse_list(done["stdout"])


class VmError(Exception):
    """Budget exceeded, Lima failing, or VMs left over."""


def default_budget(node: Node, reserve_mib: int = DEFAULT_RESERVE_MIB) -> int:
    done = node.run(["sysctl", "-n", "hw.memsize"])
    text = str(done["stdout"]).strip()
    if done["rc"] != 0 or not text.isdigit():
        raise VmError("device memory unknown (sysctl hw.memsize)")
    return int(text) // MIB - reserve_mib


def check_budget(vms: list[Vm], planned_mib: int, budget_mib: int) -> None:
    running = sum(vm.memory_mib for vm in vms if vm.status == "Running")
    if running + planned_mib > budget_mib:
        raise VmError(
            f"VM memory budget exceeded: {running} MiB running + {planned_mib} MiB planned > {budget_mib} MiB budget"
        )


def cleanup(node: Node, prefix: str) -> list[str]:
    """Delete every instance named `<prefix>…`; returns the deleted names, raises when some are left."""
    vms = list_vms(node)
    if vms is None:
        return []
    deleted = []
    for vm in vms:
        if vm.name.startswith(prefix):
            node.run(["limactl", "delete", "-f", vm.name], timeout=300)
            deleted.append(vm.name)
    left = [vm.name for vm in list_vms(node) or [] if vm.name.startswith(prefix)]
    if left:
        raise VmError(f"VMs left after cleanup: {', '.join(left)}")
    return deleted

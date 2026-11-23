"""`POST /runners/capacity` (`{"slots": n}` or `{"slots": null}`): lowers the VM capacity of every runner artificially
for the bottleneck warning (E2E-17; 08-monitoring.md sections 3 and 6, the `capacity_low` badge and the alert rule
`agent-runner-capacity-low`).

The runner computes `vm_slots_total = floor((physical RAM - AGENT_RUNNER_MEMORY_RESERVE_MIB) / VM_MEMORY_MIB)`
(`agent_runner/vm/capacity.py`) and reports `capacity_low` when fewer than 10 % of the slots are free. With
`{"slots": n}` the reserve becomes `physical - n * VM_MEMORY_MIB`, so the device offers exactly `n` slots; `n` no larger
than the VMs it holds leaves no slot free. The runner reads the value at start, so a runner whose value changes is
restarted (same home: identity, store and VMs stay, like `/runners/idle`); `null` restores the runner's default.
Physical RAM is read on the runner device (`sysctl -n hw.memsize`). Answers
`{runner: {"slots": n | null, "reserve_mib": m | null, "restarted": bool}}`.

`restart_runner` is the restart of these routes (also `/monitoring/stack/up`): stop and start at once. The 30 s wait
for quiet Lima instances before it (decisions SCN-07, item 15) is gone (KOR-11, AUD-02 NB-15): a runner stopped in the
middle of a pool VM's start now deletes that VM (runner fix of AUD-01 A-06), so no VM keeps booting outside its
control; the egress check of the run shows whether that holds.
"""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import Runner

VARIABLE = "AGENT_RUNNER_MEMORY_RESERVE_MIB"
VM_MEMORY_VARIABLE = "AGENT_RUNNER_VM_MEMORY_MIB"
DEFAULT_VM_MEMORY_MIB = 8192
MAX_SLOTS = 64
MIB = 1024 * 1024


def restart_runner(runner: Runner) -> None:
    """Stop and start `runner` (module docstring)."""
    runner.stop()
    runner.start()


def physical_mib(ctx: Context) -> int:
    done = ctx.runner_node.run(["sysctl", "-n", "hw.memsize"])
    text = str(done["stdout"]).strip()
    if done["rc"] != 0 or not text.isdigit():
        raise RouteError(500, "failed", "sysctl hw.memsize gave no number on the runner device")
    return int(text) // MIB


def post_capacity(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    slots = body.get("slots", -1) if isinstance(body, dict) else -1
    valid = slots is None or (isinstance(slots, int) and not isinstance(slots, bool) and 0 <= slots <= MAX_SLOTS)
    if not valid:
        raise RouteError(400, "invalid", f'body must be {{"slots": 0..{MAX_SLOTS} | null}}')
    physical = None if slots is None else physical_mib(ctx)
    out = {}
    for name, runner in sorted(ctx.runners.items()):
        reserve = None
        if physical is not None and slots is not None:
            vm_memory = int(runner.env.get(VM_MEMORY_VARIABLE, DEFAULT_VM_MEMORY_MIB))
            reserve = max(physical - slots * vm_memory, 0)
        wanted = None if reserve is None else str(reserve)
        changed = runner.env.get(VARIABLE) != wanted
        if wanted is None:
            runner.env.pop(VARIABLE, None)
        else:
            runner.env[VARIABLE] = wanted
        restart = changed and runner.state() != "stopped"
        if restart:
            restart_runner(runner)
        out[name] = {"slots": slots, "reserve_mib": reserve, "restarted": restart}
    return 200, out


ROUTES = [Route("POST", "/runners/capacity", post_capacity)]

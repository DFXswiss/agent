"""`POST /pool/drain` (`{"size": n}`, default 0): empty the warm pool of every runner and keep it at `n` VMs, for the
clone-path tests (E2E-07, PERF-02) and to restore the pool afterwards (`{"size": 2}`).

Per runner: the daemon is stopped (it would refill at once), the pool VMs it reports (`agent-runner pool status`,
lines `runner.pool.vm … lima_name=…`) are deleted with `limactl delete -f` (only names with the runner's VM prefix),
`AGENT_RUNNER_POOL_SIZE` becomes `n` and the daemon starts again; its start-up reconciliation marks the rows of the
vanished instances deleted (05-runner.md section 3.5). That runs after the daemon's `enrolled=` line, so the route
polls `pool status` until it lists none of the deleted instances and at most `n` VMs (ACC-M2: E2E-07 read a stale
ready VM in the whole suite); a pool that does not settle within `SETTLE_TIMEOUT_S` fails the route. Workgroup VMs are
never touched. Answers `{runner: {"deleted": [names], "size": n}}`.
"""

from __future__ import annotations

from time import monotonic
from time import sleep as pause
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

POOL_VM_FACT = "runner.pool.vm "
MAX_SIZE = 8
DELETE_TIMEOUT_S = 300.0
SETTLE_TIMEOUT_S = 120.0
SETTLE_POLL_S = 0.5


def pool_vms(stdout: str) -> list[str]:
    names = []
    for line in stdout.splitlines():
        if line.startswith(POOL_VM_FACT):
            fields = dict(item.split("=", 1) for item in line[len(POOL_VM_FACT) :].split())
            names.append(fields["lima_name"])
    return names


def pool_status(name: str, runner: Any) -> list[str]:
    status = runner.command("pool", "status")
    if status["rc"] != 0:
        raise RouteError(500, "failed", f"{name}: pool status exited with {status['rc']}")
    return pool_vms(status["stdout"])


def settle(name: str, runner: Any, deleted: list[str], size: int) -> None:
    """Wait until the restarted daemon lists none of `deleted` and at most `size` pool VMs."""
    deadline = monotonic() + SETTLE_TIMEOUT_S
    while True:
        listed = pool_status(name, runner)
        stale = [vm for vm in listed if vm in deleted]
        if not stale and len(listed) <= size:
            return
        if monotonic() > deadline:
            still = ", ".join(stale or listed)
            raise RouteError(500, "failed", f"{name}: pool did not settle at size {size} (still listed: {still})")
        pause(SETTLE_POLL_S)


def post_drain(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    size = body.get("size", 0) if isinstance(body, dict) else 0 if body is None else -1
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_SIZE:
        raise RouteError(400, "invalid", f'body must be {{"size": 0..{MAX_SIZE}}} or empty')
    out = {}
    for name, runner in sorted(ctx.runners.items()):
        running = runner.state() != "stopped"
        if running:
            runner.stop()
        prefix = runner.env.get("AGENT_RUNNER_VM_PREFIX", "")
        deleted = [vm for vm in pool_status(name, runner) if prefix and vm.startswith(prefix)]
        for vm in deleted:
            ctx.runner_node.run(["limactl", "delete", "-f", vm], timeout=DELETE_TIMEOUT_S)
        runner.env["AGENT_RUNNER_POOL_SIZE"] = str(size)
        if running:
            runner.start()
            settle(name, runner, deleted, size)
        out[name] = {"deleted": deleted, "size": size}
    return 200, out


ROUTES = [Route("POST", "/pool/drain", post_drain)]

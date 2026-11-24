"""`POST /hooks/fault` (`{"status": 500}` or `{"status": null}`): force the guest hooks of the run's VMs to answer
HTTP 500 (E2E-34; 05-runner.md section 5.5) or switch them back.

On every running VM of the run the systemd drop-in `agent-guest-api.service.d/e2e-fault.conf` sets
`AGENT_GUEST_FAULT_HOOKS=500` (GST-03; effective only in a TEST_MODE golden) or is removed, then `agent-guest-api`
restarts and the route waits until port 7700 listens again. The API process keeps the sessions' model credentials in
memory only, so the switch belongs before a session starts (the pool VMs) or after its last model request. A VM whose
SSH is not up yet (a pool VM still booting) is waited for (`guestvm.shell_when_up`); one that disappeared from the
list meanwhile (deleted by the runner) is skipped. Answers `{"status": 500 | null, "vms": [names switched]}`."""

from __future__ import annotations

import time
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

DROPIN_DIR = "/etc/systemd/system/agent-guest-api.service.d"
DROPIN = f"{DROPIN_DIR}/e2e-fault.conf"
API_PORT = 7700
LISTEN_TIMEOUT_S = 30.0
POLL_S = 0.5
ON = (
    f"mkdir -p {DROPIN_DIR} && printf '[Service]\\nEnvironment=AGENT_GUEST_FAULT_HOOKS=500\\n' > {DROPIN}"
    " && systemctl daemon-reload && systemctl restart agent-guest-api"
)
OFF = f"rm -f {DROPIN} && systemctl daemon-reload && systemctl restart agent-guest-api"


def wait_listening(ctx: Context, vm: str, sleep: Any = time.sleep, clock: Any = time.monotonic) -> None:
    deadline = clock() + LISTEN_TIMEOUT_S
    while not guestvm.shell(ctx, vm, ["ss", "-Hltn", f"sport = :{API_PORT}"])["stdout"].strip():
        if clock() >= deadline:
            raise RouteError(500, "failed", f"agent-guest-api of {vm} does not listen on {API_PORT}")
        sleep(POLL_S)


def switch(ctx: Context, vm: str, script: str) -> bool:
    """Runs `script` in `vm` (waiting while its SSH is not up); False when the VM is gone."""
    done = guestvm.shell_when_up(ctx, vm, ["sh", "-c", script])
    if done is None:
        return False
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"fault switch in {vm} exited with {done['rc']}: {done['stderr'][-300:]}")
    return True


def post_fault(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict) or "status" not in body or body["status"] not in (500, None):
        raise RouteError(400, "invalid", 'body must be {"status": 500} or {"status": null}')
    script = ON if body["status"] == 500 else OFF
    switched = []
    for vm in guestvm.running_vms(ctx):
        if switch(ctx, vm, script):
            wait_listening(ctx, vm)
            switched.append(vm)
    return 200, {"status": body["status"], "vms": switched}


ROUTES = [Route("POST", "/hooks/fault", post_fault)]

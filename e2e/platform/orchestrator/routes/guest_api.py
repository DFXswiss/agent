"""`POST /sessions/{session}/guest-api-restart`: restarts the guest's API process (`agent-guest-api`) in the VM of a
session (E2E-47, KOR-40). The process holds the session's model credential in memory only, so the session's next model
request finds none: the gateway journals `guest.error credential_missing` for the session and the runner provisions
the credential again - a real guest error that must reach the task history as a `session.error` event. Only for
specs, never a product path. Answers `{vm}`.
"""

from __future__ import annotations

from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

UNIT = "agent-guest-api"


def post_restart(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    try:
        vm = guestvm.session_vm(ctx, session)
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None
    done = guestvm.shell(ctx, vm, ["systemctl", "restart", UNIT])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"restart of {UNIT} in {vm}: {done['stderr'].strip()[-300:]}")
    return 200, {"vm": vm}


ROUTES = [Route("POST", "/sessions/{session}/guest-api-restart", post_restart)]

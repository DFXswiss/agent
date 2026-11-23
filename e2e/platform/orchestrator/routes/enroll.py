"""`POST /runners/{name}/enroll` (`{"code": "<invitation code>", "platform": true}`): enroll a runner instance.

The daemon's sync tasks end at once while the device is not enrolled, so the route stops the runner, runs
`agent-runner enroll --hub <public origin> --code <code> [--platform]` with the runner's environment (same home) and
starts the daemon again. Answers `{rc, stdout, stderr, state}`; the code itself is never echoed (the runner does not
print it). A failed enrollment still restarts the daemon (it keeps waiting for enrollment).
"""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

ENROLL_TIMEOUT_S = 120.0


def post_enroll(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    code = body.get("code") if isinstance(body, dict) else None
    platform = body.get("platform", False) if isinstance(body, dict) else None
    if not isinstance(code, str) or not code or not isinstance(platform, bool):
        raise RouteError(400, "invalid", 'body must be {"code": "<invitation code>", "platform": true|false}')
    if runner.state() != "stopped":
        runner.stop()
    args = ["enroll", "--hub", ctx.config.public_origin, "--code", code, *(["--platform"] if platform else [])]
    done = runner.command(*args, timeout=ENROLL_TIMEOUT_S)
    runner.start()
    return 200, {"rc": done["rc"], "stdout": done["stdout"], "stderr": done["stderr"], "state": runner.state()}


ROUTES = [Route("POST", "/runners/{name}/enroll", post_enroll)]

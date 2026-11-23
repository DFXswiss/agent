"""`POST /runners/idle` (`{"seconds": n}` or `{"seconds": null}`): the idle interval of every runner
(`AGENT_RUNNER_SESSION_IDLE_SECONDS`, 05-runner.md section 6.3: an `idle` main session is suspended after it), for the
resume scenario (E2E-13). The runner reads it at start, so a runner whose value changes is restarted (same home:
identity, store and VMs stay); `null` restores the runner's default. The runner refuses values below its minimum
(60 s). Answers `{runner: {"seconds": n | null, "restarted": bool}}`."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

VARIABLE = "AGENT_RUNNER_SESSION_IDLE_SECONDS"
MINIMUM_S = 60
MAXIMUM_S = 604_800


def post_idle(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    seconds = body.get("seconds", 0) if isinstance(body, dict) else 0
    valid = seconds is None or (
        isinstance(seconds, int) and not isinstance(seconds, bool) and MINIMUM_S <= seconds <= MAXIMUM_S
    )
    if not valid:
        raise RouteError(400, "invalid", f'body must be {{"seconds": {MINIMUM_S}..{MAXIMUM_S} | null}}')
    wanted = None if seconds is None else str(seconds)
    out = {}
    for name, runner in sorted(ctx.runners.items()):
        changed = runner.env.get(VARIABLE) != wanted
        if wanted is None:
            runner.env.pop(VARIABLE, None)
        else:
            runner.env[VARIABLE] = wanted
        restart = changed and runner.state() != "stopped"
        if restart:
            runner.stop()
            runner.start()
        out[name] = {"seconds": seconds, "restarted": restart}
    return 200, out


ROUTES = [Route("POST", "/runners/idle", post_idle)]

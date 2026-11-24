"""`GET /pool`: the warm pool of every runner (`agent-runner pool status`), e.g. to wait for a filled pool before a
timing-sensitive test (07-testing.md section 5.2): `{runner: {size, ready, golden, refill}}`."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

POOL_FACT = "runner.pool "


def parse_pool(stdout: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        if line.startswith(POOL_FACT):
            fields = dict(item.split("=", 1) for item in line[len(POOL_FACT) :].split())
            return {
                "size": int(fields["size"]),
                "ready": int(fields["ready"]),
                "golden": fields["golden"],
                "refill": fields["refill"],
            }
    raise ValueError("no runner.pool line")


def get_pool(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    out = {}
    for name, runner in sorted(ctx.runners.items()):
        done = runner.command("pool", "status")
        if done["rc"] != 0:
            raise RouteError(500, "failed", f"{name}: pool status exited with {done['rc']}: {done['stderr'][-300:]}")
        out[name] = parse_pool(done["stdout"])
    return 200, out


ROUTES = [Route("GET", "/pool", get_pool)]

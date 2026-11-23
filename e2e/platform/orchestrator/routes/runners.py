"""Runner instances: `GET /runners`, `POST /runners/{name}/stop`, `POST /runners/{name}/start` (same home, so a
restart keeps identity and store)."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import Runner


def _runner(ctx: Context, params: dict[str, str]) -> Runner:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    return runner


def get_runners(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    return 200, {name: runner.state() for name, runner in ctx.runners.items()}


def post_stop(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    runner = _runner(ctx, params)
    return 200, {"name": runner.name, "exit": runner.stop(), "state": runner.state()}


def post_start(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    runner = _runner(ctx, params)
    if runner.state() != "stopped":
        raise RouteError(400, "invalid", f"runner {runner.name} is running")
    runner.start()
    return 200, {"name": runner.name, "state": runner.state()}


ROUTES = [
    Route("GET", "/runners", get_runners),
    Route("POST", "/runners/{name}/stop", post_stop),
    Route("POST", "/runners/{name}/start", post_start),
]

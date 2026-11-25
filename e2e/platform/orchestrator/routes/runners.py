"""Runner instances: `GET /runners`, `POST /runners/{name}/stop`, `POST /runners/{name}/hard-kill`, `POST
/runners/{name}/start` (same home, so a restart keeps identity and store).

`kill` is the hard failure of a device (KOR-58, E2E-67): SIGKILL to the runner's process group at once, no SIGTERM
first, so the runner reports nothing and ends no session; its VMs stay. Answers `{name, exit, state}` like `stop`.

The start may carry `{"github": {"oauth_client_id"?, "app_slug"?}}` (KOR-65): the GitHub ways of the restarted runner
(`AGENT_RUNNER_GITHUB_OAUTH_CLIENT_ID`, `AGENT_RUNNER_GITHUB_APP_SLUG`); a missing or null member removes the setting,
so `{"github": {}}` restores the run's default."""

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


def post_kill(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    runner = _runner(ctx, params)
    return 200, {"name": runner.name, "exit": runner.kill(), "state": runner.state()}


GITHUB_SETTINGS = {
    "oauth_client_id": "AGENT_RUNNER_GITHUB_OAUTH_CLIENT_ID",
    "app_slug": "AGENT_RUNNER_GITHUB_APP_SLUG",
}


def _github(runner: Runner, body: Any) -> None:
    github = body.get("github") if isinstance(body, dict) else None
    if github is None:
        return
    if not isinstance(github, dict) or set(github) - set(GITHUB_SETTINGS):
        raise RouteError(400, "invalid", 'github must be {"oauth_client_id"?, "app_slug"?}')
    for key, name in GITHUB_SETTINGS.items():
        value = github.get(key)
        if value is None:
            runner.env.pop(name, None)
        elif isinstance(value, str) and value:
            runner.env[name] = value
        else:
            raise RouteError(400, "invalid", f"github.{key} must be a non-empty string or null")


def post_start(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = _runner(ctx, params)
    if runner.state() != "stopped":
        raise RouteError(400, "invalid", f"runner {runner.name} is running")
    _github(runner, body)
    runner.start()
    return 200, {"name": runner.name, "state": runner.state()}


ROUTES = [
    Route("GET", "/runners", get_runners),
    Route("POST", "/runners/{name}/stop", post_stop),
    Route("POST", "/runners/{name}/hard-kill", post_kill),
    Route("POST", "/runners/{name}/start", post_start),
]

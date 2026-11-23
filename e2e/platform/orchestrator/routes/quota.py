"""`POST /quota/usage`: the subscription usage double (`orchestrator/usage_double.py`) with a scenario, for the quota
bars of E2E-16 (05-runner.md section 10.6, 07-testing.md section 2.1).

Body `{"name", "document", "token" | "token_prefix" | "login_seed", "status"?, "min_requests"?}` (the double's scenario;
`login_seed`: the token is the first one the auth-page double issues after that seed, `routes/model.py`). The first
call starts the double on the runner device (port `usage`, journal in the runner device's run directory; registered
as double `usage`, so the run's final check asserts its state) and points every runner at it
(`AGENT_RUNNER_ANTHROPIC_USAGE_URL`; the runner reads it at start, so a running runner is restarted with the same home).
The runner polls an owner's quota on its next 30 s tick once the owner has a subscription credential on the device
and a session there. Answers `{url, restarted: [runner names]}`. In a live run the double is started with the live
upstream bridge (`routes/model.py` `POST /live/upstreams`).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes.model import subscription_token
from orchestrator.services import READY_TIMEOUT_S, Double

NAME = "usage"
PORT_OFFSET = 11
VARIABLE = "AGENT_RUNNER_ANTHROPIC_USAGE_URL"
USAGE_PATH = "/api/oauth/usage"
# Live runs only (`run.sh --live`): the double also bridges `/live/<name>/…` to these providers (`live_bridge.py`).
LIVE_UPSTREAMS = {
    "openrouter": "https://openrouter.ai/api",
    "anthropic": "https://api.anthropic.com",
    "github": "https://api.github.com",
    "codeload": "https://codeload.github.com",
}
# The bridge's port is tunnelled into every VM of the run (the gateway's upstreams). The runner on the host is the only
# GitHub client, so the GitHub upstream gets a name only the orchestrator knows (`github_<random>`, `github_name`):
# a guest cannot guess its path (codeload answers only with the short-lived token of a redirect, `live_bridge`).
GITHUB_NAMES: dict[str, str] = {}


def github_name(ctx: Context) -> str:
    """The run's secret bridge name of the GitHub upstream (one per run directory)."""
    return GITHUB_NAMES.setdefault(ctx.runner_run_dir, f"github_{secrets.token_hex(16)}")


def usage_double(ctx: Context) -> Double:
    """The running usage double; started on first use."""
    double = ctx.doubles.get(NAME)
    if double is not None:
        return double
    port = ctx.config.port_base + PORT_OFFSET
    argv = [ctx.agent_python, "-m", "orchestrator.usage_double", "--port", str(port)]
    argv += ["--journal", f"{ctx.runner_run_dir}/{NAME}-journal.jsonl"]
    if ctx.config.live:
        names = {name: github_name(ctx) if name == "github" else name for name in LIVE_UPSTREAMS}
        argv += [arg for name, url in LIVE_UPSTREAMS.items() for arg in ("--live", f"{names[name]}={url}")]
    node = ctx.runner_node
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    node.spawn(f"double-{NAME}", argv, env, f"{ctx.core_on_runner}/e2e/platform")
    node.wait_line(f"double-{NAME}", r"^http://", READY_TIMEOUT_S)
    double = Double(node, NAME, port, port)
    ctx.doubles[NAME] = double
    return double


def point_runners(ctx: Context, values: Mapping[str, str]) -> list[str]:
    """Sets `values` in every runner's environment (the runner reads them at start); restarts the running runners
    whose environment changed (same home: identity, store and VMs stay). Returns their names."""
    restarted = []
    for name, runner in sorted(ctx.runners.items()):
        if all(runner.env.get(key) == value for key, value in values.items()):
            continue
        runner.env.update(values)
        if runner.state() != "stopped":
            runner.stop()
            runner.start()
            restarted.append(name)
    return restarted


def post_usage(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", 'body must be {"name", "document", "token" | "token_prefix", ...}')
    scenario = dict(body)
    seed = scenario.pop("login_seed", None)
    if seed is not None:
        scenario["token"] = subscription_token(ctx, seed if isinstance(seed, str) else "")
    double = usage_double(ctx)
    answer = double.node.http("POST", f"{double.raw_url}/_double/scenario", scenario)
    if answer["status"] != 200:
        raise RouteError(400, "invalid", f"usage double refused the scenario: {answer['body']}")
    url = f"{double.raw_url}{USAGE_PATH}"
    return 200, {"url": url, "restarted": point_runners(ctx, {VARIABLE: url})}


ROUTES = [Route("POST", "/quota/usage", post_usage)]

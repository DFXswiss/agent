"""`POST /runners/workvm` (`{"slots": n, "max_per_task": m, "pool_size": p}`): work VMs on every runner for the work
VM scenario (agent docs/platform/spec/11-work-vm.md, ADR-056). Self mode in TEST_MODE (`AGENT_RUNNER_WORKVM_SLOTS_SELF`:
the runner's own account and Lima home, no host filter). For each runner the run's work image cache
(`Context.workvm_image`, ensured before the run) is cloned once to `<runner prefix>wimg-e2e` (a run instance: deleted
with the run's other VMs) and registered `ready` in the runner's store; the settings are environment variables the
runner reads at start, so a runner whose settings change is restarted (same home: identity and store stay).
`{"slots": 0}` takes the settings away again. Answers `{runner: {"image": name | null, "restarted": bool}}`."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import Runner

BASE_SUFFIX = "wimg-e2e"
VARIABLES = {
    "slots": "AGENT_RUNNER_WORKVM_SLOTS",
    "max_per_task": "AGENT_RUNNER_WORKVM_MAX_PER_TASK",
    "pool_size": "AGENT_RUNNER_WORKVM_POOL_SIZE",
}
SELF = "AGENT_RUNNER_WORKVM_SLOTS_SELF"
LIMITS = {"slots": (0, 4), "max_per_task": (1, 100), "pool_size": (0, 4)}
STEP_TIMEOUT_S = 300.0
REGISTER = """import sys
from datetime import UTC, datetime
from pathlib import Path
import psycopg
port = (Path(sys.argv[1]).expanduser() / "pg" / "port").read_text(encoding="utf-8").strip()
with psycopg.connect(f"host=127.0.0.1 port={port} user=agent dbname=agent_runner", autocommit=True) as conn:
    conn.execute(
        "INSERT INTO runner.workvm_image (name, state, built_at) VALUES (%s, 'ready', %s)"
        " ON CONFLICT (name) DO UPDATE SET state = 'ready'",
        (sys.argv[2], datetime.now(UTC)),
    )
"""


def _settings(body: Any) -> dict[str, int]:
    if not isinstance(body, dict) or not set(body) <= set(VARIABLES) or "slots" not in body:
        raise RouteError(400, "invalid", 'body must be {"slots": n, "max_per_task"?: n, "pool_size"?: n}')
    out = {}
    for key, value in body.items():
        low, high = LIMITS[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise RouteError(400, "invalid", f"{key} must be {low}..{high}")
        out[key] = value
    return out


def _prepare(ctx: Context, runner: Runner) -> str:
    """Clone the cache once to the runner's own base and register it; returns the base name."""
    base = runner.env["AGENT_RUNNER_VM_PREFIX"] + BASE_SUFFIX
    listing = runner.node.run(["limactl", "list", "--quiet"], env=runner.env, timeout=STEP_TIMEOUT_S)
    if base not in str(listing["stdout"]).split():
        done = runner.node.run(
            ["limactl", "clone", "--tty=false", ctx.workvm_image, base], env=runner.env, timeout=STEP_TIMEOUT_S
        )
        if done["rc"] != 0:
            raise RouteError(500, "failed", f"{runner.name}: clone failed: {done['stderr'].strip()[-300:]}")
    home = runner.env["AGENT_RUNNER_HOME"]
    done = runner.node.run([ctx.agent_python, "-c", REGISTER, home, base], env=runner.env, timeout=STEP_TIMEOUT_S)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"{runner.name}: register failed: {done['stderr'].strip()[-300:]}")
    return base


def post_workvm(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    settings = _settings(body)
    enable = settings["slots"] > 0
    if enable and not ctx.workvm_image:
        raise RouteError(409, "no_work_image", "no work image in this run (a spec with @needs-workvm ensures it)")
    out: dict[str, Any] = {}
    for name, runner in sorted(ctx.runners.items()):
        before = {variable: runner.env.get(variable) for variable in (*VARIABLES.values(), SELF)}
        for key, variable in VARIABLES.items():
            runner.env.pop(variable, None)
            if enable and key in settings:
                runner.env[variable] = str(settings[key])
        runner.env.pop(SELF, None)
        if enable:
            runner.env[SELF] = "1"
        image = _prepare(ctx, runner) if enable else None
        after = {variable: runner.env.get(variable) for variable in before}
        restart = after != before and runner.state() != "stopped"
        if restart:
            runner.stop()
            runner.start()
        out[name] = {"image": image, "restarted": restart}
    return 200, out


ROUTES = [Route("POST", "/runners/workvm", post_workvm)]

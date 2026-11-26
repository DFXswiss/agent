"""Further runner instances on the runner device and their read-only CLI (07-testing.md section 5.1: "one or two
runner instances with separate `AGENT_RUNNER_HOME`").

- `POST /runners/{name}/spawn` (`{"device", "memory_reserve_mib", "pool_size", "session_idle_seconds",
  "vm_idle_seconds"}`, each optional):
  a further runner `runner-<n>` (n above the run's `E2E_RUNNERS`) with its own home `<runner scratch>/runner-<n>`
  and VM prefix `<run prefix>r<n>-` (the run's VM cleanup covers it), otherwise the environment of `runner-1`. It
  adopts the run's golden image when the run has one and starts waiting for enrollment (`POST
  /runners/{name}/enroll`). A different `AGENT_RUNNER_MEMORY_RESERVE_MIB` gives it a different capacity (E2E-38:
  capacity-based scheduling); `pool_size` sets its warm pool (default: that of `runner-1`). An instance that will
  enroll as an org device (`"device": "org"`) gets no platform OpenRouter key file (the runner refuses it on org
  devices, 05-runner.md section 1.3). `session_idle_seconds` / `vm_idle_seconds` (60 s minimum) let an idle session
  be suspended and then its VM stop quickly (E2E-38: the scheduler's stopped-VM path). No fixed number of sessions
  per VM or of VMs exists any more (ADR-075 addendum 2): the former options `sessions_per_vm` and `max_vms` are
  refused. `operator_subscriptions: true` gives it a pool of the operator's subscriptions
  (`AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR`, a 0700 directory beside its home with one 0600 token file of the
  subscription token shape, random per spawn; E2E-61: which organizations use it comes from the platform log, ADR-079).
  Answers `{name, state, vm_prefix, home}`.
- `POST /runners/{name}/remove`: stops a spawned runner, deletes its VMs (names with its prefix), its home and its
  operator-subscription directory and forgets it, so the final checks do not expect it running (a decertified or
  unpaired device's runner ends itself).
  The run's own runners cannot be removed. Answers `{name, deleted}`.
- `POST /runners/{name}/cli` (`{"args": ["doctor"]}`): `agent-runner doctor` or `agent-runner status [--json]` once
  with the runner's environment (the facts the device prints, 05-runner.md section 1.2). Answers `{rc, stdout, stderr}`.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from orchestrator import vms
from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import Runner

TEMPLATE = "runner-1"
MAX_INDEX = 9
MAX_RESERVE_MIB = 4_194_304
MAX_POOL = 8
GOLDEN_NOTE = "golden image "
CLI_COMMANDS = (("doctor",), ("status",), ("status", "--json"))
CLI_TIMEOUT_S = 120.0
DEVICE_KINDS = ("platform", "org")
IDLE_VARIABLES = {
    "session_idle_seconds": "AGENT_RUNNER_SESSION_IDLE_SECONDS",
    "vm_idle_seconds": "AGENT_RUNNER_VM_IDLE_SECONDS",
}
IDLE_RANGE = (60, 604_800)
OPTIONS = frozenset(
    {"device", "memory_reserve_mib", "pool_size", "operator_subscriptions", *IDLE_VARIABLES},
)
KEY_FILE = "AGENT_RUNNER_OPENROUTER_KEY_FILE"
OPERATOR_DIR = "AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"
OPERATOR_TOKEN_PREFIX = "-".join(("sk", "ant", "oat01", ""))
"""The shape of a subscription access token (assembled: no source file spells a token-shaped literal)."""
_NAME_RE = re.compile(r"^runner-([1-9])$")


def _index(ctx: Context, name: str) -> int:
    found = _NAME_RE.match(name)
    index = int(found.group(1)) if found else 0
    if index <= ctx.config.runners:
        raise RouteError(400, "invalid", f"a further runner is named runner-<{ctx.config.runners + 1}..{MAX_INDEX}>")
    return index


def _number(body: dict[str, Any], key: str, maximum: int, minimum: int = 0) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RouteError(400, "invalid", f"{key} must be {minimum}..{maximum} or null")
    return value


def _golden(ctx: Context) -> str | None:
    for note in ctx.notes:
        if note.startswith(GOLDEN_NOTE):
            return note[len(GOLDEN_NOTE) :]
    return None


def _operator_env(ctx: Context, options: dict[str, Any], kind: str, home: str) -> dict[str, str]:
    """The variable of the spawn option `operator_subscriptions` (platform devices only), else nothing."""
    operator = options.get("operator_subscriptions", False)
    if not isinstance(operator, bool):
        raise RouteError(400, "invalid", "operator_subscriptions must be a boolean")
    if not operator:
        return {}
    if kind == "org":
        raise RouteError(400, "invalid", "operator subscriptions are for platform devices only")
    return {OPERATOR_DIR: _operator_pool(ctx, f"{home}-operator-subscriptions")}


def _operator_pool(ctx: Context, directory: str) -> str:
    """The operator-subscription directory (0700) with one token file (0600) on the runner device."""
    script = 'umask 077 && mkdir -p "$1" && chmod 700 "$1" && printf %s "$E2E_TOKEN" > "$1/e2e.token"'
    token = OPERATOR_TOKEN_PREFIX + secrets.token_urlsafe(32)
    done = ctx.runner_node.run(
        ["/bin/sh", "-c", script, "sh", directory], env={"E2E_TOKEN": token, "PATH": "/usr/bin:/bin"}
    )
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"operator subscriptions not written: {str(done['stderr']).strip()[-300:]}")
    return directory


def post_spawn(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    name = params["name"]
    index = _index(ctx, name)
    if name in ctx.runners:
        raise RouteError(400, "invalid", f"runner {name} exists")
    template = ctx.runners.get(TEMPLATE)
    if template is None:
        raise RouteError(409, "invalid", f"no {TEMPLATE} to copy the environment from")
    options = body if isinstance(body, dict) else {}
    if body is not None and not isinstance(body, dict):
        raise RouteError(400, "invalid", "body must be an object of spawn options")
    unknown = sorted(set(options) - OPTIONS)
    if unknown:
        raise RouteError(400, "invalid", f"unknown spawn option {unknown[0]}")
    kind = options.get("device", "platform")
    if kind not in DEVICE_KINDS:
        raise RouteError(400, "invalid", "device must be platform or org")
    reserve = _number(options, "memory_reserve_mib", MAX_RESERVE_MIB)
    pool = _number(options, "pool_size", MAX_POOL)
    home = f"{ctx.runner_scratch}/{name}"
    prefix = ctx.config.runner_prefix(index)
    env = {**template.env, "AGENT_RUNNER_HOME": home, "AGENT_RUNNER_VM_PREFIX": prefix}
    if reserve is not None:
        env["AGENT_RUNNER_MEMORY_RESERVE_MIB"] = str(reserve)
    if pool is not None:
        env["AGENT_RUNNER_POOL_SIZE"] = str(pool)
    if kind == "org":
        env.pop(KEY_FILE, None)
    for key, variable in IDLE_VARIABLES.items():
        seconds = _number(options, key, IDLE_RANGE[1], IDLE_RANGE[0])
        if seconds is not None:
            env[variable] = str(seconds)
    env.update(_operator_env(ctx, options, kind, home))
    runner = Runner(ctx.runner_node, name, template.python, env)
    golden = _golden(ctx)
    if golden is not None:
        runner.adopt(golden)
    runner.start()
    ctx.runners[name] = runner
    return 200, {"name": name, "state": runner.state(), "vm_prefix": prefix, "home": home}


def post_remove(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    name = params["name"]
    _index(ctx, name)
    runner = ctx.runners.get(name)
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {name}")
    runner.stop()
    prefix = runner.env["AGENT_RUNNER_VM_PREFIX"]
    deleted = vms.cleanup(ctx.runner_node, prefix)
    ctx.runner_node.run(["rm", "-rf", runner.env["AGENT_RUNNER_HOME"]])
    if OPERATOR_DIR in runner.env:
        ctx.runner_node.run(["rm", "-rf", runner.env[OPERATOR_DIR]])
    del ctx.runners[name]
    return 200, {"name": name, "deleted": deleted}


def post_cli(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    args = body.get("args") if isinstance(body, dict) else None
    if not isinstance(args, list) or tuple(args) not in CLI_COMMANDS:
        allowed = ", ".join(" ".join(command) for command in CLI_COMMANDS)
        raise RouteError(400, "invalid", f"args must be one of: {allowed}")
    done = runner.command(*args, timeout=CLI_TIMEOUT_S)
    return 200, {"rc": done["rc"], "stdout": done["stdout"], "stderr": done["stderr"]}


ROUTES = [
    Route("POST", "/runners/{name}/spawn", post_spawn),
    Route("POST", "/runners/{name}/remove", post_remove),
    Route("POST", "/runners/{name}/cli", post_cli),
]

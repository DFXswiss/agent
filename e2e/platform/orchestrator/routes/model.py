"""Model access of the M2c scenarios (E2E-16, E2E-27, E2E-28, E2E-36; 05-runner.md sections 10-11, 07-testing.md
section 2.1). Credential values the specs cannot know stay in the orchestrator: the run's platform OpenRouter key
(a run secret in the runners' key file) and the subscription tokens the login doubles issue (derived from the auth
page's seed, `agent_doubles.login_cli.codes.access_token(seed, n)`, computed with the agent checkout). Nothing here
returns or logs a credential value.

- `POST /model/scenario` (`{"scenario": {...}, "access": {"kind": "platform" | "api_key" | "subscription", "token"?,
  "login_seed"?} | null}`): loads the model double's scenario together with its credential check per access kind
  (`agent_doubles.model` `access`: `platform` → `openrouter` with the run's platform key, `api_key` with `token`,
  `subscription` with the first token issued after the auth page was seeded with `login_seed`, or `token`). The double
  then refuses every request whose credential header is not exactly the one the gateway must inject for that kind.
- `POST /hub/budget` (`{"usd": "<amount>" | null}`): restarts the hub with `AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY`
  (`null`: unlimited again); the database, the relays and the runners stay (they reconnect).
- `POST /sessions/{session}/env-check` (`{"values": [...], "login_seeds": [...]}`): the environment of the session's
  Claude Code process (`/proc/<main pid>/environ` of unit `agent-session-<id>`, read as root in its VM). Answers the
  variable names, the non-secret model variables, whether `ANTHROPIC_AUTH_TOKEN` and `PLATFORM_SESSION_TOKEN` hold one
  and the same session token, whether `ANTHROPIC_API_KEY` is empty, and `credential_findings`: the variables holding
  any credential value (the given values, the platform key, the subscription tokens of the seeds).
- `POST /checks/credentials` (same body): the credential scan of 09-milestones.md M2c ("no credential value in sandbox
  environment, files or logs"): every running VM of the run (files below the writable and configuration trees and the
  journal), the runner device (every runner home - store, logs, temporary directories - and the run directory with
  the runner, double and egress logs), and the hub side (database dump, hub and PostgreSQL logs, the browser link;
  `routes/plaintext.py`). The VM and runner-device scans get the first 8 characters of each value and the SHA-256 of
  the whole value, never the value. Answers `{findings: [{source, value}], scanned: {source: files}}` (`value`: the
  index of the value in the scan's list: given values first, then the platform key, then the seeds' tokens).
- `POST /live/upstreams` (`{"platform_key": "<OpenRouter key>" | null}`, live runs only, `run.sh --live`): the real
  providers instead of the model double (LIVE-01, LIVE-03; 07-testing.md section 6). Starts the usage double with the
  live upstream bridge (`orchestrator/live_bridge.py`) and a reverse tunnel of its port into every VM of the run,
  writes the given key into every platform runner's key file (the operator's key from the spec's environment; the
  file is the run's own, 0600, deleted with the run's scratch) and points the runners' model upstreams and the
  subscription usage URL at the bridge (runners restarted with the same home). Answers `{base, restarted}`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes import plaintext, quota
from orchestrator.services import write_platform_key

KEY_FILE = "AGENT_RUNNER_OPENROUTER_KEY_FILE"
BUDGET = "AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY"
DOUBLE_KIND = {"platform": "openrouter", "api_key": "api_key", "subscription": "subscription"}
TOKEN_VARIABLES = ("ANTHROPIC_AUTH_TOKEN", "PLATFORM_SESSION_TOKEN")
MODEL_VARIABLES = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)
MAX_VALUES = 16
NEEDLE_PREFIX = 8
LIVE_TUNNELS = "tunnels-live"
SCAN_TIMEOUT_S = 600.0
VM_ROOTS = ("/srv", "/home", "/root", "/tmp", "/var/tmp", "/var/log", "/var/lib", "/run", "/etc", "/opt", "journal:")
_USD_RE = re.compile(r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$")
_SEED_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VALUE_RE = re.compile(r"^[!-~]{16,4096}$")
TOKEN_SCRIPT = """import sys
from agent_doubles.login_cli.codes import access_token
print(access_token(sys.argv[1], int(sys.argv[2])))
"""
# Finds the values whose prefix (argv[1]: [[prefix, sha256, length], ...]) starts a byte window with that digest, in
# every regular file below the roots (argv[2:]; symlinks not followed) and, for `journal:`, the systemd journal.
SCAN_SCRIPT = """import hashlib, json, os, subprocess, sys
needles = [(p.encode(), d, n) for p, d, n in json.loads(sys.argv[1])]
LIMIT = 256 * 1024 * 1024
found, files = [], 0
def check(label, data):
    for index, (prefix, digest, length) in enumerate(needles):
        at = data.find(prefix)
        while at >= 0:
            if hashlib.sha256(data[at:at + length]).hexdigest() == digest:
                found.append({"source": label, "value": index})
                break
            at = data.find(prefix, at + 1)
for root in sys.argv[2:]:
    if root == "journal:":
        done = subprocess.run(["journalctl", "--no-pager", "-q", "-o", "cat"], capture_output=True)
        files += 1
        check("journal", done.stdout)
        continue
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(top, d))]
        for name in names:
            path = os.path.join(top, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > LIMIT:
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            files += 1
            check(path, data)
print(json.dumps({"files": files, "found": found}))
"""


def platform_key(ctx: Context) -> str:
    """The run's platform OpenRouter key (the key file of the first runner that has one)."""
    for _name, runner in sorted(ctx.runners.items()):
        path = runner.env.get(KEY_FILE)
        if path:
            return runner.node.read_text(path).strip()
    raise RouteError(409, "invalid", "no runner has a platform key file")


def subscription_token(ctx: Context, seed: str, n: int = 1) -> str:
    """The n-th token the auth-page double issues after it was seeded with `seed` (the first one: n = 1)."""
    if not _SEED_RE.fullmatch(seed):
        raise RouteError(400, "invalid", "a login seed is 1..64 characters of [A-Za-z0-9._-]")
    done = ctx.runner_node.run([ctx.agent_python, "-I", "-c", TOKEN_SCRIPT, seed, str(n)])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"token of the login double not derived: {done['stderr'].strip()[-300:]}")
    return str(done["stdout"]).strip()


def _strings(body: Any, key: str, pattern: re.Pattern[str], what: str) -> list[str]:
    values = body.get(key, []) if isinstance(body, dict) else []
    if not isinstance(values, list) or len(values) > MAX_VALUES:
        raise RouteError(400, "invalid", f"{key} must be a list of at most {MAX_VALUES} {what}")
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise RouteError(400, "invalid", f"{key} must be a list of {what}")
    return list(values)


def credential_values(ctx: Context, body: Any) -> list[str]:
    """Given values, then the platform key (when a runner has one), then the tokens of the given login seeds."""
    values = _strings(body, "values", _VALUE_RE, "printable values of 16..4096 characters")
    seeds = _strings(body, "login_seeds", _SEED_RE, "login seeds")
    if any(runner.env.get(KEY_FILE) for runner in ctx.runners.values()):
        values.append(platform_key(ctx))
    return values + [subscription_token(ctx, seed) for seed in seeds]


def needles(values: list[str]) -> str:
    """The scan script's argument: the first `NEEDLE_PREFIX` characters of each value (a command line inside a VM is
    journaled by sudo), its SHA-256 and its length; a hit is a window starting with the prefix with that digest."""
    rows = []
    for value in values:
        raw = value.encode()
        rows.append([value[:NEEDLE_PREFIX], hashlib.sha256(raw).hexdigest(), len(raw)])
    return json.dumps(rows)


def post_scenario(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("scenario"), dict):
        raise RouteError(400, "invalid", 'body must be {"scenario": {...}, "access": {"kind", ...} | null}')
    double = ctx.doubles.get("model")
    if double is None:
        raise RouteError(409, "invalid", "the model double is not running")
    access = body.get("access")
    check: dict[str, str] | None = None
    kind: str | None = None
    if access is not None:
        kind = access.get("kind") if isinstance(access, dict) else None
        if not isinstance(access, dict) or kind not in DOUBLE_KIND:
            raise RouteError(400, "invalid", "access.kind must be platform, api_key or subscription")
        token, seed = access.get("token"), access.get("login_seed")
        if kind == "platform":
            token = platform_key(ctx)
        elif kind == "subscription" and isinstance(seed, str):
            token = subscription_token(ctx, seed)
        if not isinstance(token, str) or not _VALUE_RE.fullmatch(token):
            raise RouteError(400, "invalid", f"access {kind} needs a token (16..4096 printable characters)")
        check = {"kind": DOUBLE_KIND[kind], "token": token}
    request = {"scenario": body["scenario"], "access": check}
    answer = double.node.http("POST", f"{double.raw_url}/_double/scenario", request)
    shown = answer["body"]
    if isinstance(shown, dict):
        shown = {key: value for key, value in shown.items() if key != "access"}
    if answer["status"] != 200:
        raise RouteError(400, "invalid", f"model double refused the scenario: {shown}")
    return 200, {"double": shown, "access": kind}


def post_budget(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    usd = body.get("usd", "") if isinstance(body, dict) else ""
    if usd is not None and (not isinstance(usd, str) or not _USD_RE.fullmatch(usd)):
        raise RouteError(400, "invalid", 'body must be {"usd": "<amount with at most 6 decimals>" | null}')
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    if usd is None:
        hub.env.pop(BUDGET, None)
    else:
        hub.env[BUDGET] = usd
    hub.node.stop("hub")
    hub.start()
    return 200, {"usd": usd}


def _environ(ctx: Context, session: str) -> tuple[str, int, dict[str, str]]:
    try:
        vm = guestvm.session_vm(ctx, session)
        unit = guestvm.UNIT_PREFIX + session
        shown = guestvm.checked(ctx, vm, ["systemctl", "show", "-p", "MainPID", "--value", unit])
        pid = int(shown.strip() or "0")
        if pid == 0:
            raise RouteError(409, "invalid", f"the unit of session {session} is not running")
        text = guestvm.checked(ctx, vm, ["cat", f"/proc/{pid}/environ"])
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None
    pairs = (item.partition("=") for item in text.split("\0") if item)
    return vm, pid, {name: value for name, _sep, value in pairs}


def post_env_check(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    values = credential_values(ctx, body)
    vm, pid, env = _environ(ctx, params["session"])
    tokens = [env.get(name, "") for name in TOKEN_VARIABLES]
    findings = sorted({name for name, value in env.items() for secret in values if secret in value})
    return 200, {
        "vm": vm,
        "pid": pid,
        "names": sorted(env),
        "model": {name: env[name] for name in MODEL_VARIABLES if name in env},
        "session_token": {
            "present": all(tokens),
            "same": len(set(tokens)) == 1,
            "length": len(tokens[0]),
        },
        "anthropic_api_key_empty": env.get("ANTHROPIC_API_KEY", "missing") == "",
        "credential_findings": findings,
        "credentials_checked": len(values),
    }


def _scan(ctx: Context, argv: list[str]) -> dict[str, Any]:
    done = ctx.runner_node.run(argv, timeout=SCAN_TIMEOUT_S)
    if done["rc"] != 0:
        detail = f"credential scan of the runner device exited with {done['rc']}: {done['stderr'][-300:]}"
        raise RouteError(500, "failed", detail)
    result: dict[str, Any] = json.loads(done["stdout"])
    return result


def post_credentials(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    values = credential_values(ctx, body)
    if not values:
        raise RouteError(400, "invalid", "nothing to scan for: give values or login seeds, or run a platform runner")
    argument = needles(values)
    findings: list[dict[str, Any]] = []
    scanned: dict[str, int] = {}
    for vm in guestvm.running_vms(ctx):
        # a pool VM may still boot (SSH not up yet) or be deleted by its runner meanwhile
        done = guestvm.shell_when_up(ctx, vm, ["python3", "-I", "-c", SCAN_SCRIPT, argument, *VM_ROOTS])
        if done is None:
            continue
        if done["rc"] != 0:
            detail = f"credential scan of {vm} exited with {done['rc']}: {done['stderr'][-300:]}"
            raise RouteError(500, "failed", detail)
        result = json.loads(done["stdout"])
        scanned[vm] = result["files"]
        findings += [{"source": f"{vm}:{item['source']}", "value": item["value"]} for item in result["found"]]
    homes = sorted({runner.env["AGENT_RUNNER_HOME"] for runner in ctx.runners.values()})
    roots = [*homes, ctx.runner_run_dir]
    result = _scan(ctx, [ctx.agent_python, "-I", "-c", SCAN_SCRIPT, argument, *roots])
    scanned["runner-device"] = result["files"]
    findings += [{"source": f"runner-device:{item['source']}", "value": item["value"]} for item in result["found"]]
    _status, hub = plaintext.post_plaintext(ctx, {}, {"canaries": values})
    scanned.update({f"hub:{label}": size for label, size in hub["sources"].items()})
    findings += [{"source": f"hub:{item['source']}", "value": values.index(item["canary"])} for item in hub["findings"]]
    return 200, {"findings": findings, "scanned": scanned, "values": len(values)}


def post_live_upstreams(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    key = body.get("platform_key") if isinstance(body, dict) else ""
    if key is not None and (not isinstance(key, str) or not _VALUE_RE.fullmatch(key)):
        raise RouteError(400, "invalid", 'body must be {"platform_key": "<key>" | null}')
    if not ctx.config.live:
        raise RouteError(409, "invalid", "the live upstreams exist only in a live run (run.sh --live)")
    double = quota.usage_double(ctx)
    node = ctx.runner_node
    if not node.processes().get(LIVE_TUNNELS, {}).get("alive"):
        template = sorted(ctx.runners.items())[0][1].env
        env: dict[str, str] = {name: template[name] for name in ("PATH", "HOME", "LANG") if name in template}
        argv = [ctx.agent_python, "-m", "orchestrator.tunnels", "--prefix", ctx.config.run_prefix]
        node.spawn(LIVE_TUNNELS, [*argv, "--port", str(double.raw_port)], env, f"{ctx.core_on_runner}/e2e/platform")
    if key is not None:
        for runner in ctx.runners.values():
            path = runner.env.get(KEY_FILE)
            if path:
                write_platform_key(runner.node, path, key)
    base = f"{double.raw_url}/live"
    settings = {
        "AGENT_RUNNER_MODEL_UPSTREAM_OPENROUTER": f"{base}/openrouter",
        "AGENT_RUNNER_MODEL_UPSTREAM_ANTHROPIC": f"{base}/anthropic",
        quota.VARIABLE: f"{base}/anthropic{quota.USAGE_PATH}",
    }
    restarted = quota.point_runners(ctx, settings)
    if key is not None:
        for name, runner in sorted(ctx.runners.items()):
            if runner.env.get(KEY_FILE) and name not in restarted and runner.state() != "stopped":
                runner.stop()  # the runner reads the key file at start
                runner.start()
                restarted.append(name)
    return 200, {"base": base, "restarted": restarted}


ROUTES = [
    Route("POST", "/model/scenario", post_scenario),
    Route("POST", "/hub/budget", post_budget),
    Route("POST", "/sessions/{session}/env-check", post_env_check),
    Route("POST", "/checks/credentials", post_credentials),
    Route("POST", "/live/upstreams", post_live_upstreams),
]

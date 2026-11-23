"""The local monitoring test stack of the agent checkout (`deploy/monitoring/test-stack/`, MON-01) for E2E-17
(07-testing.md section 2.1, 08-monitoring.md section 7), started and stopped with the run prefix.

- `POST /monitoring/stack/up` (`{"runner": "runner-1"}`, optional): the runner writes its Prometheus textfile into
  `<AGENT_RUNNER_HOME>/textfile` (`AGENT_RUNNER_TEXTFILE_DIR`; a runner whose value changes is restarted with the same
  home, like `/runners/idle`: `capacity.restart_runner`), then `up.sh --prefix <run
  prefix> --textfile-dir … --archives …` creates and provisions `<run prefix>monitoring` (Prometheus, node_exporter and
  Grafana in the versions of the existing stack). The stack is hermetic: the archives come from `monitoring-archives/`
  next to the agent checkout on the runner device, filled once before the run by
  `deploy/monitoring/test-stack/fetch.sh` (a missing directory is `409`), and the VM drops its own outbound traffic
  (up.sh offline mode). No host port is opened: every query runs inside the VM. The cleanup of the run deletes the VM
  with the other instances of the run prefix. Answers `{vm, server, textfile_dir, restarted}`.
- `POST /monitoring/stack/check` (`{"expect_active": [rule uid, …]}`): `check.sh` in the VM - `promtool check
  metrics` on the runner's textfile, `promtool check rules` and `promtool test rules` with the alert tests of the
  checkout, Grafana provisioning without errors, series of every textfile metric in Prometheus, the three dashboards
  with data for every panel and variable query, the seven rules evaluated, the `expect_active` rules pending or
  firing. Answers `{ok, rc, steps, report}` (`report`: the JSON of `agent_monitoring.stack`).
- `POST /monitoring/stack/prometheus` (`{"path": "/api/v1/…", "query": {name: value}}`): one GET on the Prometheus
  HTTP API inside the VM; answers Prometheus' JSON.
- `POST /monitoring/stack/live-rule-test` (`{"metric": "agent_runner_device_capacity_low", "rule": uid, "for_minutes":
  n}`): `promtool test rules` with the *live* value of `metric` (series of the runner device in Prometheus) held for
  `for_minutes` + 2 minutes against the alert file of the checkout rendered for the stack's server: the rule must
  fire when its `for` window ends and be silent one minute before. Answers `{ok, rc, value, output}`.
- `POST /monitoring/stack/down`: `down.sh --prefix <run prefix>`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes.capacity import restart_runner
from orchestrator.services import Runner

STACK_DIR = "deploy/monitoring/test-stack"
ARCHIVES_DIR = "monitoring-archives"
TEXTFILE_VARIABLE = "AGENT_RUNNER_TEXTFILE_DIR"
SERVER = "runner-device"
VM_SUFFIX = "monitoring"
UP_TIMEOUT_S = 1200.0
CHECK_TIMEOUT_S = 600.0
QUERY_TIMEOUT_S = 60.0
ASSETS_IN_VM = "/opt/agent-monitoring"
REPORT_START = "{"
_RULE_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_METRIC_RE = re.compile(r"^agent_runner_[a-z0-9_]{1,80}$")
_PATH_RE = re.compile(r"^/api/v1/[a-z_/]{1,40}$")
_PARAM_RE = re.compile(r"^[A-Za-z_\[\]]{1,20}$")
# A sample value goes into the generated promtool test: digits only (Prometheus renders e.g. `1`, `0.25`, `1e+06`).
_VALUE_RE = re.compile(r"^-?[0-9][0-9.e+-]{0,30}$")
# `promtool test rules` with the live value: the rules file is rendered for SERVER (agent_monitoring.rules), the
# input series carries the label set the stack's Prometheus adds; the alert must be silent one minute before the end of
# its `for` window and firing at its end (the convention of alerting/agent-platform.test.yaml).
LIVE_TEST = """set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export PYTHONPATH={assets}
python3 -m agent_monitoring.rules --without-annotations --device {server} {assets}/alerting/agent-platform.yaml \\
  "$work/live.rules.yaml"
cat >"$work/live.test.yaml" <<EOF
rule_files: [live.rules.yaml]
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: '{metric}{{server="{server}"}}'
        values: '{value}x{minutes}'
    alert_rule_test:
      - eval_time: {silent}m
        alertname: {rule}
        exp_alerts: []
      - eval_time: {firing}m
        alertname: {rule}
        exp_alerts:
          - exp_labels: {{server: {server}, severity: warning, component: agent-platform}}
EOF
cd "$work" && /usr/local/bin/promtool test rules live.test.yaml
"""


def vm_name(ctx: Context) -> str:
    return f"{ctx.config.run_prefix}{VM_SUFFIX}"


def absolute(ctx: Context, path: str) -> str:
    """`path` on the runner device with a leading `~/` expanded (remote checkouts are home-relative)."""
    script = 'case $1 in "~/"*) printf %s "$HOME/${1#\\~/}";; *) printf %s "$1";; esac'
    done = ctx.runner_node.run(["/bin/sh", "-c", script, "sh", path])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"cannot resolve {path} on the runner device")
    return str(done["stdout"])


def in_vm(ctx: Context, argv: list[str], timeout: float = QUERY_TIMEOUT_S) -> dict[str, Any]:
    command = ["limactl", "shell", "--workdir", "/", vm_name(ctx), "sudo", "-n", *argv]
    return ctx.runner_node.run(command, timeout=timeout)


def _runner(ctx: Context, body: Any) -> tuple[str, Runner]:
    name = body.get("runner", "runner-1") if isinstance(body, dict) else "runner-1"
    runner = ctx.runners.get(name) if isinstance(name, str) else None
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {name!r}")
    return name, runner


def post_up(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    _, runner = _runner(ctx, body)
    agent = absolute(ctx, ctx.agent_src)
    archives = f"{agent.rstrip('/').rsplit('/', 1)[0]}/{ARCHIVES_DIR}"
    if ctx.runner_node.run(["test", "-d", archives])["rc"] != 0:
        raise RouteError(409, "invalid", f"{archives} is missing: run {STACK_DIR}/fetch.sh {archives} before the run")
    textfile_dir = f"{absolute(ctx, runner.env['AGENT_RUNNER_HOME']).rstrip('/')}/textfile"
    made = ctx.runner_node.run(["mkdir", "-p", textfile_dir])
    if made["rc"] != 0:
        raise RouteError(500, "failed", f"cannot create {textfile_dir}: {made['stderr'].strip()[-200:]}")
    restarted = runner.env.get(TEXTFILE_VARIABLE) != textfile_dir and runner.state() != "stopped"
    runner.env[TEXTFILE_VARIABLE] = textfile_dir
    if restarted:
        restart_runner(runner)
    argv = ["bash", f"{agent}/{STACK_DIR}/up.sh", "--prefix", ctx.config.run_prefix, "--server", SERVER]
    argv += ["--textfile-dir", textfile_dir, "--archives", archives]
    done = ctx.runner_node.run(argv, timeout=UP_TIMEOUT_S)
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"up.sh exited with {done['rc']}: {done['stderr'].strip()[-600:]}")
    return 200, {"vm": vm_name(ctx), "server": SERVER, "textfile_dir": textfile_dir, "restarted": restarted}


def _rules(body: Any, key: str) -> list[str]:
    rules = body.get(key, []) if isinstance(body, dict) else []
    if not isinstance(rules, list) or not all(isinstance(rule, str) and _RULE_RE.fullmatch(rule) for rule in rules):
        raise RouteError(400, "invalid", f'"{key}" must be a list of rule uids')
    return rules


def post_check(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    argv = ["bash", f"{ASSETS_IN_VM}/test-stack/check.sh", SERVER]
    for rule in _rules(body, "expect_active"):
        argv += ["--expect-active", rule]
    done = in_vm(ctx, argv, CHECK_TIMEOUT_S)
    lines = str(done["stdout"]).splitlines()
    steps = [line[len("check: ") :] for line in lines if line.startswith("check: ")]
    start = next((index for index, line in enumerate(lines) if line.startswith(REPORT_START)), None)
    report: Any = None
    if start is not None:
        text = "\n".join(line for line in lines[start:] if not line.startswith("check: "))
        try:
            report = json.loads(text)
        except ValueError:
            report = None
    output = (str(done["stdout"]) + str(done["stderr"]))[-4000:]
    return 200, {"ok": done["rc"] == 0, "rc": done["rc"], "steps": steps, "report": report, "output": output}


def _prometheus(ctx: Context, path: str, query: dict[str, str]) -> Any:
    url = "http://127.0.0.1:9090" + path
    args = [item for name, value in query.items() for item in ("--data-urlencode", f"{name}={value}")]
    done = in_vm(ctx, ["curl", "-fsS", "--get", url, *args])
    if done["rc"] != 0:
        raise RouteError(502, "failed", f"prometheus {path}: {done['stderr'].strip()[-300:]}")
    try:
        return json.loads(done["stdout"])
    except ValueError as error:
        raise RouteError(502, "failed", f"prometheus {path}: no JSON answer") from error


def post_prometheus(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    path = body.get("path") if isinstance(body, dict) else None
    query = body.get("query", {}) if isinstance(body, dict) else {}
    valid_query = isinstance(query, dict) and all(
        isinstance(name, str) and _PARAM_RE.fullmatch(name) and isinstance(value, str) for name, value in query.items()
    )
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or not valid_query:
        raise RouteError(400, "invalid", 'body must be {"path": "/api/v1/…", "query": {name: value}}')
    return 200, _prometheus(ctx, path, query)


def post_live_rule_test(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    metric = body.get("metric") if isinstance(body, dict) else None
    rule = body.get("rule") if isinstance(body, dict) else None
    minutes = body.get("for_minutes") if isinstance(body, dict) else None
    valid = (
        isinstance(metric, str)
        and _METRIC_RE.fullmatch(metric) is not None
        and isinstance(rule, str)
        and _RULE_RE.fullmatch(rule) is not None
        and isinstance(minutes, int)
        and not isinstance(minutes, bool)
        and 1 <= minutes <= 60
    )
    if not valid or not isinstance(minutes, int):
        raise RouteError(400, "invalid", 'body must be {"metric": "agent_runner_…", "rule": uid, "for_minutes": 1..60}')
    answer = _prometheus(ctx, "/api/v1/query", {"query": f'{metric}{{server="{SERVER}"}}'})
    results = answer.get("data", {}).get("result", []) if isinstance(answer, dict) else []
    if len(results) != 1:
        raise RouteError(409, "invalid", f"{metric}: expected one live series, found {len(results)}")
    value = str(results[0]["value"][1])
    if not _VALUE_RE.fullmatch(value):
        raise RouteError(502, "failed", f"{metric}: the live value is not a number")
    script = LIVE_TEST.format(
        assets=ASSETS_IN_VM,
        server=SERVER,
        metric=metric,
        value=value,
        minutes=minutes + 2,
        silent=minutes - 1,
        firing=minutes,
        rule=rule,
    )
    done = in_vm(ctx, ["bash", "-c", script], CHECK_TIMEOUT_S)
    output = (str(done["stdout"]) + str(done["stderr"]))[-4000:]
    return 200, {"ok": done["rc"] == 0, "rc": done["rc"], "value": value, "output": output}


def post_down(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    agent = absolute(ctx, ctx.agent_src)
    done = ctx.runner_node.run(["bash", f"{agent}/{STACK_DIR}/down.sh", "--prefix", ctx.config.run_prefix])
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"down.sh exited with {done['rc']}: {done['stderr'].strip()[-300:]}")
    return 200, {"vm": vm_name(ctx), "deleted": True}


ROUTES = [
    Route("POST", "/monitoring/stack/up", post_up),
    Route("POST", "/monitoring/stack/check", post_check),
    Route("POST", "/monitoring/stack/prometheus", post_prometheus),
    Route("POST", "/monitoring/stack/live-rule-test", post_live_rule_test),
    Route("POST", "/monitoring/stack/down", post_down),
]

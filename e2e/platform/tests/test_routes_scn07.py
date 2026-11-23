"""Control routes of SCN-07 (monitoring test stack, capacity, audit tamper check) against a scripted node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from orchestrator.context import Context
from orchestrator.routes import capacity, monitoring_stack
from orchestrator.services import Cluster, Hub, Runner
from test_routes_guest import Result, ScriptNode, call, failed, make_ctx, ok

PREFIX = "wp-t-e2e-"
VM = f"{PREFIX}monitoring"
HOME = "/home/u"


def resolve(argv: list[str]) -> Result | None:
    """The `~/` expansion of `monitoring_stack.absolute` as the device's shell does it."""
    if argv[:2] == ["/bin/sh", "-c"] and argv[3] == "sh" and len(argv) == 5:
        path = argv[4]
        return ok(f"{HOME}/{path[2:]}" if path.startswith("~/") else path)
    return None


def stack_ctx(tmp_path: Path, respond: Any, *, agent_src: str = "~/wp/t/agent") -> tuple[Context, ScriptNode, Runner]:
    def answer(argv: list[str]) -> Result:
        return resolve(argv) or respond(argv)

    node = ScriptNode(answer)
    ctx = make_ctx(tmp_path, node)
    ctx.agent_src = agent_src
    runner = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "~/wp/t/runner-1"})
    ctx.runners["runner-1"] = runner
    node.alive_names.add("runner-1")
    return ctx, node, runner


def in_vm(argv: list[str]) -> list[str] | None:
    if argv[:4] == ["limactl", "shell", "--workdir", "/"] and argv[4] == VM and argv[5:7] == ["sudo", "-n"]:
        return argv[7:]
    return None


# -- monitoring stack ------------------------------------------------------------------------------------------------


def test_up_sets_the_textfile_restarts_once_and_starts_the_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, node, runner = stack_ctx(tmp_path, lambda argv: ok())
    status, answer = call(ctx, "POST", "/monitoring/stack/up", {})
    textfile = f"{HOME}/wp/t/runner-1/textfile"
    assert (status, answer) == (
        200,
        {"vm": VM, "server": "runner-device", "textfile_dir": textfile, "restarted": True},
    )
    assert runner.env["AGENT_RUNNER_TEXTFILE_DIR"] == textfile
    # restarted at once, without listing the instances first (KOR-11, NB-15)
    assert not any(argv[:2] == ["limactl", "list"] for argv in node.runs)
    assert ["test", "-d", f"{HOME}/wp/t/monitoring-archives"] in node.runs
    assert ["mkdir", "-p", textfile] in node.runs
    assert node.runs[-1] == [
        "bash", f"{HOME}/wp/t/agent/deploy/monitoring/test-stack/up.sh", "--prefix", PREFIX, "--server",
        "runner-device", "--textfile-dir", textfile, "--archives", f"{HOME}/wp/t/monitoring-archives",
    ]  # fmt: skip
    # the same value again: no restart; a stopped runner is not started
    assert call(ctx, "POST", "/monitoring/stack/up", {"runner": "runner-1"})[1]["restarted"] is False
    node.alive_names.clear()
    runner.env.pop("AGENT_RUNNER_TEXTFILE_DIR")
    assert call(ctx, "POST", "/monitoring/stack/up", None)[1]["restarted"] is False
    assert "runner-1" not in node.alive_names


def test_up_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _, _ = stack_ctx(tmp_path, lambda argv: failed() if argv[:2] == ["test", "-d"] else ok())
    assert call(ctx, "POST", "/monitoring/stack/up", {"runner": "runner-9"})[0] == 404
    assert call(ctx, "POST", "/monitoring/stack/up", {"runner": 3})[0] == 404
    status, answer = call(ctx, "POST", "/monitoring/stack/up", {})
    assert (status, answer["error"]) == (409, "invalid")
    assert "fetch.sh" in answer["detail"]

    ctx, _, _ = stack_ctx(tmp_path, lambda argv: failed(1, "denied") if argv[0] == "mkdir" else ok())
    status, answer = call(ctx, "POST", "/monitoring/stack/up", {})
    assert (status, "cannot create" in answer["detail"]) == (500, True)

    ctx, _, _ = stack_ctx(tmp_path, lambda argv: failed(2, "limactl not found") if argv[0] == "bash" else ok())
    status, answer = call(ctx, "POST", "/monitoring/stack/up", {})
    assert (status, "up.sh exited with 2: limactl not found" in answer["detail"]) == (500, True)


def test_absolute_paths_and_a_failing_shell(tmp_path: Path) -> None:
    ctx, _, _ = stack_ctx(tmp_path, lambda argv: ok(), agent_src="/abs/agent")
    assert monitoring_stack.absolute(ctx, "/abs/agent") == "/abs/agent"
    node = ScriptNode(lambda argv: failed())
    ctx = make_ctx(tmp_path, node)
    ctx.agent_src = "~/x"
    status, answer = call(ctx, "POST", "/monitoring/stack/down", {})
    assert (status, "cannot resolve" in answer["detail"]) == (500, True)


CHECK_OUT = """check: promtool check metrics: ok (38 metrics)
check: promtool check rules: ok
{
  "problems": [],
  "series": 38
}
check: prometheus and grafana APIs: ok
"""


def test_check_runs_check_sh_and_parses_the_report(tmp_path: Path) -> None:
    def respond(argv: list[str]) -> Result:
        inner = in_vm(argv)
        assert inner is not None
        assert inner[:3] == ["bash", "/opt/agent-monitoring/test-stack/check.sh", "runner-device"]
        return ok(CHECK_OUT) if inner[3:] == ["--expect-active", "agent-runner-capacity-low"] else failed(1, "bad")

    ctx, _, _ = stack_ctx(tmp_path, respond)
    status, answer = call(ctx, "POST", "/monitoring/stack/check", {"expect_active": ["agent-runner-capacity-low"]})
    assert status == 200
    assert answer["ok"] is True
    assert answer["steps"] == [
        "promtool check metrics: ok (38 metrics)",
        "promtool check rules: ok",
        "prometheus and grafana APIs: ok",
    ]
    assert answer["report"] == {"problems": [], "series": 38}
    status, answer = call(ctx, "POST", "/monitoring/stack/check", None)
    assert (status, answer["ok"], answer["report"], answer["output"]) == (200, False, None, "bad")
    for bad in ({"expect_active": "x"}, {"expect_active": ["Bad Rule"]}, {"expect_active": [3]}):
        assert call(ctx, "POST", "/monitoring/stack/check", bad)[0] == 400


def test_check_with_a_broken_report(tmp_path: Path) -> None:
    ctx, _, _ = stack_ctx(tmp_path, lambda argv: ok("check: a\n{ not json\n"))
    assert call(ctx, "POST", "/monitoring/stack/check", {})[1]["report"] is None


def test_prometheus_queries_inside_the_vm(tmp_path: Path) -> None:
    answers: list[Result] = [ok('{"status": "success", "data": []}'), failed(7, "refused"), ok("<html>")]

    def respond(argv: list[str]) -> Result:
        inner = in_vm(argv)
        assert inner is not None
        assert inner[:4] == ["curl", "-fsS", "--get", "http://127.0.0.1:9090/api/v1/series"]
        assert inner[4:] == ["--data-urlencode", 'match[]={__name__=~"agent_runner_.+"}']
        return answers.pop(0)

    ctx, _, _ = stack_ctx(tmp_path, respond)
    body = {"path": "/api/v1/series", "query": {"match[]": '{__name__=~"agent_runner_.+"}'}}
    assert call(ctx, "POST", "/monitoring/stack/prometheus", body) == (200, {"status": "success", "data": []})
    assert call(ctx, "POST", "/monitoring/stack/prometheus", body)[0] == 502
    status, answer = call(ctx, "POST", "/monitoring/stack/prometheus", body)
    assert (status, "no JSON answer" in answer["detail"]) == (502, True)
    for bad in (
        None,
        {"path": "/graph"},
        {"path": "/api/v1/query", "query": []},
        {"path": "/api/v1/query", "query": {"q;": "x"}},
        {"path": "/api/v1/query", "query": {"query": 3}},
    ):
        assert call(ctx, "POST", "/monitoring/stack/prometheus", bad)[0] == 400


def test_live_rule_test_uses_the_live_value(tmp_path: Path) -> None:
    results: list[list[dict[str, Any]]] = [
        [{"metric": {}, "value": [1, "1"]}],
        [],
        [{"value": [1, "1"]}],
        [{"value": [1, "1'; rm -rf /"]}],
    ]
    scripts: list[str] = []

    def respond(argv: list[str]) -> Result:
        inner = in_vm(argv)
        assert inner is not None
        if inner[0] == "curl":
            assert inner[-1] == 'query=agent_runner_device_capacity_low{server="runner-device"}'
            return ok(json.dumps({"status": "success", "data": {"result": results.pop(0)}}))
        assert inner[:2] == ["bash", "-c"]
        scripts.append(inner[2])
        return ok("SUCCESS\n") if len(scripts) == 1 else failed(1, "FAILED")

    ctx, _, _ = stack_ctx(tmp_path, respond)
    body = {"metric": "agent_runner_device_capacity_low", "rule": "agent-runner-capacity-low", "for_minutes": 10}
    status, answer = call(ctx, "POST", "/monitoring/stack/live-rule-test", body)
    assert (status, answer["ok"], answer["value"]) == (200, True, "1")
    script = scripts[0]
    assert "--device runner-device /opt/agent-monitoring/alerting/agent-platform.yaml" in script
    assert "series: 'agent_runner_device_capacity_low{server=\"runner-device\"}'" in script
    assert "values: '1x12'" in script
    assert "eval_time: 9m\n        alertname: agent-runner-capacity-low\n        exp_alerts: []" in script
    assert "eval_time: 10m\n        alertname: agent-runner-capacity-low\n        exp_alerts:\n" in script
    assert "exp_labels: {server: runner-device, severity: warning, component: agent-platform}" in script
    status, answer = call(ctx, "POST", "/monitoring/stack/live-rule-test", body)
    assert (status, "found 0" in answer["detail"]) == (409, True)
    status, answer = call(ctx, "POST", "/monitoring/stack/live-rule-test", body)
    assert (status, answer["ok"], answer["output"]) == (200, False, "FAILED")
    status, answer = call(ctx, "POST", "/monitoring/stack/live-rule-test", body)
    assert (status, "not a number" in answer["detail"]) == (502, True)
    assert len(scripts) == 2
    for bad in (
        None,
        {**body, "metric": "up"},
        {**body, "rule": "Bad"},
        {**body, "for_minutes": 0},
        {**body, "for_minutes": True},
        {**body, "for_minutes": "10"},
        {**body, "rule": 3},
        {**body, "metric": 3},
    ):
        assert call(ctx, "POST", "/monitoring/stack/live-rule-test", bad)[0] == 400


def test_live_rule_test_without_a_data_object(tmp_path: Path) -> None:
    ctx, _, _ = stack_ctx(tmp_path, lambda argv: ok("[]"))
    body = {"metric": "agent_runner_device_capacity_low", "rule": "agent-runner-capacity-low", "for_minutes": 10}
    assert call(ctx, "POST", "/monitoring/stack/live-rule-test", body)[0] == 409


def test_down(tmp_path: Path) -> None:
    answers = [ok(), failed(2, "limactl not found")]
    ctx, node, _ = stack_ctx(tmp_path, lambda argv: answers.pop(0))
    assert call(ctx, "POST", "/monitoring/stack/down", {}) == (200, {"vm": VM, "deleted": True})
    assert node.runs[-1] == ["bash", f"{HOME}/wp/t/agent/deploy/monitoring/test-stack/down.sh", "--prefix", PREFIX]
    status, answer = call(ctx, "POST", "/monitoring/stack/down", {})
    assert (status, "down.sh exited with 2" in answer["detail"]) == (500, True)


# -- capacity --------------------------------------------------------------------------------------------------------

GIB_BYTES = 512 * 1024**3


def test_capacity_sets_the_reserve_and_restores_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, node, runner = stack_ctx(tmp_path, lambda argv: ok(f"{GIB_BYTES}\n"))
    runner.env["AGENT_RUNNER_VM_MEMORY_MIB"] = "8192"
    status, answer = call(ctx, "POST", "/runners/capacity", {"slots": 3})
    assert (status, answer) == (200, {"runner-1": {"slots": 3, "reserve_mib": 524_288 - 3 * 8192, "restarted": True}})
    assert runner.env["AGENT_RUNNER_MEMORY_RESERVE_MIB"] == str(524_288 - 3 * 8192)
    assert ["sysctl", "-n", "hw.memsize"] in node.runs
    assert call(ctx, "POST", "/runners/capacity", {"slots": 3})[1]["runner-1"]["restarted"] is False
    runner.env.pop("AGENT_RUNNER_VM_MEMORY_MIB")
    assert call(ctx, "POST", "/runners/capacity", {"slots": 64})[1]["runner-1"]["reserve_mib"] == 0
    node.alive_names.clear()
    assert call(ctx, "POST", "/runners/capacity", {"slots": None}) == (
        200,
        {"runner-1": {"slots": None, "reserve_mib": None, "restarted": False}},
    )
    assert "AGENT_RUNNER_MEMORY_RESERVE_MIB" not in runner.env


def test_capacity_refusals(tmp_path: Path) -> None:
    ctx, _, _ = stack_ctx(tmp_path, lambda argv: ok("n/a"))
    for bad in (None, {}, {"slots": -1}, {"slots": 65}, {"slots": True}, {"slots": "2"}):
        assert call(ctx, "POST", "/runners/capacity", bad)[0] == 400
    status, answer = call(ctx, "POST", "/runners/capacity", {"slots": 1})
    assert (status, "hw.memsize" in answer["detail"]) == (500, True)


# -- audit tamper check ------------------------------------------------------------------------------------------------


def tamper_ctx(tmp_path: Path, respond: Any) -> tuple[Context, ScriptNode]:
    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    ctx.cluster = Cluster(node, "/pg", str(tmp_path / "pg"), 21005, {"PATH": "/usr/bin"}, password="pw")
    ctx.hub = Hub(node, "/hub/python", {"AGENT_HUB_DATABASE": "postgresql://hub_writer@x/agent_hub"}, "127.0.0.1", 1)
    return ctx, node


def verify_answer(ok_value: bool) -> Result:
    line = json.dumps({"ok": ok_value, "head": {"seq": 9, "hash": "h"}, "checkpoint": None})
    return {"rc": 0 if ok_value else 1, "stdout": line + "\n", "stderr": ""}


def test_tamper_copies_changes_verifies_and_drops(tmp_path: Path) -> None:
    sql: list[str] = []
    verified: list[str] = []

    def respond(argv: list[str]) -> Result:
        if argv[0] == "/pg/psql":
            statement = argv[-1]
            sql.append(statement)
            return ok('["denied"]\n') if "json_agg" in statement else ok()
        if argv[0] == "/bin/bash":
            assert argv[2].startswith("set -o pipefail; ")
            assert argv[-5:] == ["bash", "/pg", "21005", "agent_hub", "agent_hub_tamper"]
            assert '| "$1/psql"' in argv[2]
            return ok()
        assert argv[:5] == ["/hub/python", "-m", "agent_hub", "audit", "verify"]
        verified.append(argv[0])
        return verify_answer(len(verified) > 1)

    ctx, node = tamper_ctx(tmp_path, respond)
    status, answer = call(ctx, "POST", "/hub-db/audit-tamper", {"seq": 7})
    assert (status, answer) == (
        200,
        {
            "seq": 7,
            "before": "denied",
            "after": "ok",
            "copy": {"rc": 1, "ok": False},
            "original": {"rc": 0, "ok": True},
        },
    )
    assert sql[0] == "DROP DATABASE IF EXISTS agent_hub_tamper WITH (FORCE)"
    assert sql[1] == "CREATE DATABASE agent_hub_tamper"
    assert "ALTER TABLE hub.audit DISABLE TRIGGER USER; UPDATE hub.audit SET result = 'ok' WHERE seq = 7" in sql
    assert sql[-1] == "DROP DATABASE IF EXISTS agent_hub_tamper WITH (FORCE)"
    copy_verify = [argv for argv in node.runs if argv[0] == "/hub/python"]
    assert len(copy_verify) == 2


def test_tamper_flips_other_results_to_ok_and_reads_odd_verify_output(tmp_path: Path) -> None:
    outputs: list[Result] = [ok("not json\n"), ok("{broken\n")]

    def respond(argv: list[str]) -> Result:
        if argv[0] == "/pg/psql":
            return ok('["failed"]\n') if "json_agg" in argv[-1] else ok()
        if argv[0] == "/bin/bash":
            return ok()
        return outputs.pop(0)

    ctx, _ = tamper_ctx(tmp_path, respond)
    status, answer = call(ctx, "POST", "/hub-db/audit-tamper", {"seq": 1})
    assert (status, answer["before"], answer["after"]) == (200, "failed", "ok")
    assert answer["copy"] == {"rc": 0, "ok": None}
    assert answer["original"] == {"rc": 0, "ok": None}


def test_tamper_refusals_and_failures(tmp_path: Path) -> None:
    dropped: list[str] = []

    def respond(argv: list[str]) -> Result:
        if argv[0] == "/pg/psql":
            if argv[-1].startswith("DROP"):
                dropped.append(argv[-1])
            return ok("[]\n") if "json_agg" in argv[-1] else ok()
        return failed(1, "pg_dump: connection refused")

    ctx, _ = tamper_ctx(tmp_path, respond)
    for bad in (None, {}, {"seq": 0}, {"seq": True}, {"seq": "3"}):
        assert call(ctx, "POST", "/hub-db/audit-tamper", bad)[0] == 400
    status, answer = call(ctx, "POST", "/hub-db/audit-tamper", {"seq": 3})
    assert (status, "copy of the hub database failed" in answer["detail"]) == (500, True)
    assert dropped[-1] == "DROP DATABASE IF EXISTS agent_hub_tamper WITH (FORCE)"  # a partial copy is dropped too

    ctx, _ = tamper_ctx(tmp_path, lambda argv: _drops(argv, dropped))
    dropped.clear()
    status, answer = call(ctx, "POST", "/hub-db/audit-tamper", {"seq": 3})
    assert (status, answer["error"]) == (404, "not_found")
    assert dropped[-1] == "DROP DATABASE IF EXISTS agent_hub_tamper WITH (FORCE)"

    ctx.cluster = None
    assert call(ctx, "POST", "/hub-db/audit-tamper", {"seq": 3})[0] == 409


def _drops(argv: list[str], dropped: list[str]) -> Result:
    if argv[0] == "/pg/psql" and argv[-1].startswith("DROP"):
        dropped.append(argv[-1])
    return ok("[]\n") if argv[0] == "/pg/psql" and "json_agg" in argv[-1] else ok()


# -- quiet VMs before a restart ---------------------------------------------------------------------------------------


def test_restart_stops_then_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    _, _, runner = stack_ctx(tmp_path, lambda argv: ok())

    def stop() -> int:
        order.append("stop")
        return 0

    def start() -> None:
        order.append("start")

    monkeypatch.setattr(runner, "stop", stop)
    monkeypatch.setattr(runner, "start", start)
    capacity.restart_runner(runner)
    assert order == ["stop", "start"]

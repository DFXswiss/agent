"""U-MON stack checks: series in Prometheus, dashboards and their queries through Grafana, provisioned and evaluated
rules, expected active rules, retries and the report (Prometheus and Grafana answered by an in-process double of
their HTTP APIs: no socket)."""

from __future__ import annotations

import base64
import io
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_monitoring import assets, sample, stack

TITLES = stack.rule_titles(assets.ALERT_FILE.read_text(encoding="utf-8"))
EVALUATED = "2026-11-22T10:00:00Z"


class Double:
    """Answers the endpoints `stack` calls; attributes change the answers."""

    def __init__(self, names: set[str]) -> None:
        self.names = set(names)
        self.expected = set(names)
        self.health = "OK"
        self.folder_title = "Agent platform"
        self.empty_marker: str | None = None
        self.error_marker: str | None = None
        self.rules = {uid: {"uid": uid, "folderUID": "infra"} for uid in assets.RULE_UIDS}
        self.folders = [{"uid": "infra", "title": "Infrastructure"}, {"uid": "x", "title": "Other"}]
        self.states: dict[str, dict[str, Any]] = {
            uid: {"name": TITLES[uid], "health": "ok", "state": "inactive", "lastEvaluation": EVALUATED}
            for uid in assets.RULE_UIDS
        }
        self.requests: list[tuple[str, str, Any, dict[str, str]]] = []
        self.failures: list[Exception] = []

    def query(self, url: urllib.parse.SplitResult, body: Any) -> Any:
        assert 'server="runner-a"' in urllib.parse.parse_qs(url.query)["query"][0]
        return {"data": {"result": [{"metric": {"__name__": name}} for name in sorted(self.names)]}}

    def ds_query(self, url: urllib.parse.SplitResult, body: Any) -> Any:
        expr = body["queries"][0]["expr"]
        assert "$" not in expr
        frames: list[dict[str, Any]] = [{"schema": {}}, {"data": {"values": [[1], [2.5]]}}]
        answer: dict[str, Any] = {"frames": frames}
        if self.error_marker and self.error_marker in expr:
            answer = {"error": "parse error", "frames": []}
        elif self.empty_marker and self.empty_marker in expr:
            answer = {"frames": [{"data": {"values": [[], []]}}]}
        return {"results": {"A": answer}}

    def answer(self, path: str, body: Any) -> Any:
        url = urllib.parse.urlsplit(path)
        if url.path.startswith("/api/dashboards/uid/"):
            uid = url.path.rsplit("/", 1)[1]
            return {"meta": {"folderTitle": self.folder_title, "provisioned": True}, "dashboard": assets.dashboard(uid)}
        fixed: dict[str, Callable[[urllib.parse.SplitResult, Any], Any]] = {
            "/api/v1/query": self.query,
            "/api/ds/query": self.ds_query,
            "/api/datasources/uid/prometheus/health": lambda _u, _b: {
                "status": self.health,
                "message": "bad gateway" if self.health != "OK" else "ok",
            },
            "/api/v1/provisioning/alert-rules": lambda _u, _b: list(self.rules.values()),
            "/api/folders": lambda _u, _b: self.folders,
            "/api/prometheus/grafana/api/v1/rules": lambda _u, _b: {
                "data": {"groups": [{"name": "agent-platform", "rules": list(self.states.values())}]}
            },
        }
        return fixed[url.path](url, body)

    def urlopen(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        if self.failures:
            raise self.failures.pop(0)
        url = urllib.parse.urlsplit(request.full_url)
        path = url.path + (f"?{url.query}" if url.query else "")
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        self.requests.append((url.netloc, path, body, dict(request.header_items())))
        return io.BytesIO(json.dumps(self.answer(path, body)).encode())


@pytest.fixture
def textfile_path(tmp_path: Path) -> Path:
    return sample.write(tmp_path, 1795600000.0)


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch, textfile_path: Path) -> Double:
    fake = Double(stack.textfile_metrics(textfile_path.read_text(encoding="utf-8")))
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def apis() -> tuple[stack.Api, stack.Api]:
    return stack.Api("http://prom.test/"), stack.Api("http://grafana.test", ("admin", "pw"))


def all_checks(double: Double, expect: set[str] | None = None) -> Callable[[], list[str]]:
    prometheus, grafana = apis()

    def checks() -> list[str]:
        return [
            *stack.check_prometheus(prometheus, "runner-a", double.expected | {"agent_runner_extra"}),
            *stack.check_dashboards(grafana),
            *stack.check_rules(grafana, TITLES, expect or set()),
        ]

    return checks


def test_healthy_stack_passes(double: Double) -> None:
    double.names.add("agent_runner_extra")
    prometheus, grafana = apis()
    assert stack.check_prometheus(prometheus, "runner-a", double.names) == []
    assert stack.check_dashboards(grafana) == []
    assert stack.check_rules(grafana, TITLES, set()) == []
    hosts = {netloc for netloc, *_ in double.requests}
    assert hosts == {"prom.test", "grafana.test"}
    grafana_headers = [headers for netloc, _, _, headers in double.requests if netloc == "grafana.test"]
    token = base64.b64encode(b"admin:pw").decode()
    assert all(headers["Authorization"] == f"Basic {token}" for headers in grafana_headers)
    prom_headers = [headers for netloc, _, _, headers in double.requests if netloc == "prom.test"]
    assert all("Authorization" not in headers for headers in prom_headers)
    posted = [body for _, path, body, _ in double.requests if path == "/api/ds/query"]
    exprs = [body["queries"][0]["expr"] for body in posted]
    assert len(exprs) == sum(len(assets.dashboard_queries(assets.dashboard(uid))) for uid in assets.DASHBOARD_UIDS)
    assert "count(agent_runner_device_up)" in exprs
    assert any("[1m]" in expr for expr in exprs)


def test_every_problem_is_reported(double: Double) -> None:
    double.names.discard("agent_runner_vm_state")
    double.health = "ERROR"
    double.folder_title = "General"
    double.empty_marker = "agent_runner_vm_uptime_seconds"
    double.error_marker = "agent_runner_session_workspace_bytes"
    del double.rules["agent-runner-sync-fatal"]
    double.rules["agent-runner-session-oom"]["folderUID"] = "x"
    del double.states["agent-runner-hub-disconnected"]
    double.states["agent-runner-textfile-stale"]["health"] = "error"
    double.states["agent-runner-textfile-stale"]["lastError"] = "bad query"
    double.states["agent-runner-vm-probe-failed"]["lastEvaluation"] = stack.NEVER_EVALUATED
    problems = all_checks(double, {"agent-runner-capacity-low"})()
    text = "\n".join(problems)
    assert "prometheus: no series of agent_runner_extra with server=runner-a" in text
    assert "prometheus: no series of agent_runner_vm_state" in text
    assert "data source prometheus not healthy: bad gateway" in text
    assert "agent-platform-vms not provisioned in folder 'Agent platform'" in text
    assert "no data for agent_runner_vm_uptime_seconds" in text
    assert "parse error" in text
    assert "rule agent-runner-sync-fatal not provisioned" in text
    assert "rule agent-runner-session-oom not in folder 'Infrastructure'" in text
    assert "rule agent-runner-hub-disconnected has no evaluation state" in text
    assert "rule agent-runner-textfile-stale evaluation error: bad query" in text
    assert "rule agent-runner-vm-probe-failed not evaluated yet" in text
    assert "rule agent-runner-capacity-low is inactive, expected pending or firing" in text


def test_expected_active_rules(double: Double) -> None:
    _, grafana = apis()
    double.states["agent-runner-capacity-low"]["state"] = "pending"
    double.states["agent-runner-vm-probe-failed"]["state"] = "firing"
    expect = {"agent-runner-capacity-low", "agent-runner-vm-probe-failed"}
    assert stack.check_rules(grafana, TITLES, expect) == []


def test_query_rewriting_and_frames() -> None:
    expr = 'rate(x{server=~"$server", vm=~"$vm", session=~"$session"}[$__rate_interval])'
    assert stack.grafana_query(expr) == 'rate(x{server=~".+", vm=~".+", session=~".+"}[1m])'
    assert stack._has_values([{"data": {"values": [[1], [2]]}}])
    assert not stack._has_values([{"data": {"values": [[1]]}}])
    assert not stack._has_values([{"data": {"values": [[], []]}}, {}])
    assert stack.textfile_metrics("# TYPE a gauge\na 1\n# TYPE b_total counter\n") == {"a", "b_total"}


def test_run_checks_retries_until_clean_or_timeout() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    answers: list[list[str] | Exception] = [OSError("refused"), ["not yet"], []]

    def checks() -> list[str]:
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert stack.run_checks(checks, 60, pause=5, clock=lambda: now[0], sleep=sleep) == []
    assert slept == [5, 5]
    assert stack.run_checks(lambda: ["still bad"], 12, pause=5, clock=lambda: now[0], sleep=sleep) == ["still bad"]

    def broken() -> list[str]:
        raise KeyError("x")

    failing = stack.run_checks(broken, 0, clock=lambda: now[0], sleep=sleep)
    assert failing == ["request failed: 'x'"]


def test_command_line(double: Double, textfile_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    password = tmp_path / "grafana-admin"
    password.write_text("pw\n", encoding="utf-8")
    double.states["agent-runner-capacity-low"]["state"] = "pending"
    argv = [
        "--prometheus", "http://prom.test", "--grafana", "http://grafana.test", "--grafana-password-file",
        str(password), "--server", "runner-a", "--textfile", str(textfile_path), "--timeout", "0",
        "--expect-active", "agent-runner-capacity-low",
    ]  # fmt: skip
    assert stack.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["metrics"] == len(double.names)
    assert report["rules"] == list(assets.RULE_UIDS)
    assert report["expect_active"] == ["agent-runner-capacity-low"]

    double.names.clear()
    assert stack.main(argv) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["problems"]

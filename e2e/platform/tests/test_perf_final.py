"""The final PERF evaluation (PFM-01): sample series, hub and PostgreSQL log counts, terminal storage, device load and
their control routes."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from orchestrator.context import Context
from orchestrator.routes import capacity, perf_final
from orchestrator.services import Cluster
from perf import samples, storage
from test_routes import StubNode, call, ctx  # noqa: F401 - the fixture is used by name

SESSION_A = "01a200ce-0451-7080-a832-e27880063cf8"
SESSION_B = "01a200ce-0451-7080-a832-e27880063cf9"

HUB_LOG = """INFO:     Started server process [24026]
INFO:     127.0.0.1:50431 - "GET /v1/health HTTP/1.1" 200 OK
INFO:     127.0.0.1:50432 - "POST /v1/orgs/01a200ce-0451-7080-a832-e27880063cf8/log HTTP/1.1" 200 OK
INFO:     127.0.0.1:50433 - "POST /v1/device/outbox HTTP/1.1" 503 Service Unavailable
INFO:     127.0.0.1:50434 - "POST /v1/device/outbox HTTP/1.1" 429 Too Many Requests
INFO:     127.0.0.1:50435 - "GET /v1/stream?org=01a200ce-0451-7080-a832-e27880063cf8&cursors=x HTTP/1.1" 200 OK
WARNING:  something else
"""
PG_LOG = """2026-11-22 20:00:00.000 CET [1] LOG:  database system is ready to accept connections
2026-11-22 20:01:00.000 CET [2] ERROR:  deadlock detected
2026-11-22 20:01:00.000 CET [2] DETAIL:  Process 2 waits for ShareLock on transaction 9; blocked by process 3.
2026-11-22 20:02:00.000 CET [4] ERROR:  deadlock detected
"""


def test_distribution_and_passed() -> None:
    assert samples.distribution([]) == {"n": 0, "min": None, "p50": None, "p95": None, "max": None}
    assert samples.distribution([3.0, 1.0, 2.0]) == {"n": 3, "min": 1.0, "p50": 2.0, "p95": 3.0, "max": 3.0}
    values = [float(n) for n in range(1, 101)]
    assert samples.distribution(values)["p95"] == 95.0
    assert samples.distribution(values)["p50"] == 50.0
    assert samples.passed(samples.distribution(values), 101) is True
    assert samples.passed(samples.distribution(values), 100) is False
    assert samples.passed(samples.distribution(values), 95) is False
    assert samples.passed(samples.distribution(values), None) is True
    assert samples.passed(samples.distribution([]), 10) is False
    assert samples.passed(samples.distribution([]), None) is False


def test_series_csv(tmp_path: Path) -> None:
    path = tmp_path / "out" / "perf-reconnect.csv"
    samples.append_series(path, "iter-1", "reconnect runner T1", "ms", [12.5, 13.0])
    samples.append_series(path, "iter-2", "reconnect runner T1", "ms", [14.0])
    table = list(csv.reader(path.open()))
    assert table[0] == list(samples.COLUMNS)
    assert table[1:] == [
        ["iter-1", "reconnect runner T1", "1", "12.500", "ms"],
        ["iter-1", "reconnect runner T1", "2", "13.000", "ms"],
        ["iter-2", "reconnect runner T1", "1", "14.000", "ms"],
    ]
    assert samples.read_series(path) == {
        "iter-1 reconnect runner T1": [12.5, 13.0],
        "iter-2 reconnect runner T1": [14.0],
    }


def test_series_name() -> None:
    assert samples.series_path(Path("/a"), "reconnect") == Path("/a/perf-reconnect.csv")
    for bad in ("", "Up", "a/b", "x" * 41, 3):
        with pytest.raises(ValueError, match="name"):
            samples.series_path(Path("/a"), bad)


def test_log_counts() -> None:
    counts = samples.hub_requests(HUB_LOG)
    assert counts == {
        "requests": 5,
        "by_status": {"200": 3, "429": 1, "503": 1},
        "refused": {"POST /v1/device/outbox 429": 1, "POST /v1/device/outbox 503": 1},
    }
    assert samples.normalized_path("/v1/orgs/01a200ce-0451-7080-a832-e27880063cf8/log?x=1") == "/v1/orgs/{id}/log"
    assert samples.deadlocks(PG_LOG) == 2
    assert samples.since(HUB_LOG, 2).splitlines()[0].endswith(f'"POST /v1/orgs/{SESSION_A}/log HTTP/1.1" 200 OK')
    assert samples.since(HUB_LOG, 0) == HUB_LOG
    assert samples.since(HUB_LOG, 100) == ""


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "perf-x.csv"
    samples.append_series(path, "iter-1", "x", "ms", [100.0, 200.0])
    assert samples.main([str(path), "--limit", "300"]) == 0
    assert "iter-1 x: n=2 min=100.0 p50=100.0 p95=200.0 max=200.0 passed=True" in capsys.readouterr().out
    assert samples.main([str(path), "--limit", "150"]) == 1
    assert samples.main([str(path)]) == 0


def storage_rows() -> list[dict[str, Any]]:
    return [
        {
            "session_id": SESSION_A,
            "chunks": 40,
            "ct_bytes": 40_000,
            "row_bytes": 52_000,
            "plain_bytes": 38_000,
            "span_s": 120.0,
        },
        {"session_id": SESSION_B, "chunks": 1, "ct_bytes": 500, "row_bytes": 700, "plain_bytes": 400, "span_s": 0.0},
    ]


def test_storage(tmp_path: Path) -> None:
    sql = storage.sessions_sql()
    assert "kind = 'session.terminal'" in sql
    assert "GROUP BY session_id" in sql
    assert "pg_total_relation_size" in storage.relation_sql()
    summary = storage.summarize(storage_rows(), {"event_total_bytes": 10_000_000, "event_rows": 900})
    assert summary["sessions"] == 2
    assert summary["chunks"] == 41
    assert summary["row_bytes"] == 52_700
    assert summary["bytes_per_chunk"] == pytest.approx(52_700 / 41)
    # only sessions with a time span count for the rate
    assert summary["row_bytes_per_session_hour"] == pytest.approx(52_000 / 120 * 3600)
    assert summary["relation"] == {"event_total_bytes": 10_000_000, "event_rows": 900}
    empty = storage.summarize([], {"event_total_bytes": 0, "event_rows": 0})
    assert empty["bytes_per_chunk"] is None
    assert empty["row_bytes_per_session_hour"] is None
    path = tmp_path / "s.csv"
    storage.write_csv(path, "iter-1", "PERF-05", storage_rows())
    storage.write_csv(path, "iter-1", "PERF-04", storage_rows()[:1])
    table = list(csv.reader(path.open()))
    assert table[0] == list(storage.COLUMNS)
    assert len(table) == 4
    assert table[1][:3] == ["iter-1", "PERF-05", SESSION_A]


class LogNode(StubNode):
    def __init__(self, logs: dict[str, str], runs: dict[tuple[str, ...], dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.logs = logs
        self.argv_runs = runs or {}

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "log_tail":
            self.calls.append((op, args))
            return self.logs[args["name"]]
        if op == "run":
            self.calls.append((op, args))
            return self.argv_runs.get(tuple(args["argv"]), {"rc": 1, "stdout": "", "stderr": "no"})
        return super().call(op, **args)


def test_route_samples(ctx: Context) -> None:  # noqa: F811
    status, answer = call(
        ctx, "POST", "/perf/samples", {"name": "reconnect", "label": "runner T1", "values": [100, 250.5], "limit": 300}
    )
    assert status == 200
    assert answer["summary"] == {"n": 2, "min": 100.0, "p50": 100.0, "p95": 250.5, "max": 250.5, "limit": 300}
    assert answer["passed"] is True
    assert answer["csv"].endswith("perf-reconnect.csv")
    with Path(answer["csv"]).open() as handle:
        rows = list(csv.reader(handle))
    assert rows[1][0] == Path(ctx.scratch).name
    status, answer = call(ctx, "POST", "/perf/samples", {"name": "x", "label": "y", "values": [1], "unit": "s"})
    assert answer["summary"]["limit"] is None
    assert answer["passed"] is True
    bad: Any
    for bad in (
        [],
        {"name": "x", "label": "", "values": [1]},
        {"name": "x", "label": "y", "values": [True]},
        {"name": "x", "label": "y", "values": [float("nan")]},
        {"name": "x", "label": "y", "values": [1], "limit": float("inf")},
        {"name": "x", "label": "y", "values": "1"},
        {"name": "x", "label": "y", "values": [1], "limit": True},
        {"name": "x", "label": "y", "values": [1], "limit": "1"},
        {"name": "x", "label": "y", "values": [1], "unit": ""},
        {"name": "x/y", "label": "y", "values": [1]},
    ):
        assert call(ctx, "POST", "/perf/samples", bad)[0] == 400, bad


def test_route_hub_log(ctx: Context) -> None:  # noqa: F811
    ctx.hub_node = LogNode({"hub": HUB_LOG, "postgres": PG_LOG})
    status, answer = call(ctx, "POST", "/perf/hub-log")
    assert status == 200
    assert answer["hub_lines"] == len(HUB_LOG.splitlines())
    assert answer["pg_lines"] == len(PG_LOG.splitlines())
    assert answer["deadlocks"] == 2
    assert answer["requests"]["by_status"]["503"] == 1
    status, answer = call(ctx, "POST", "/perf/hub-log", {"hub_since": 3, "pg_since": 3})
    assert answer["deadlocks"] == 1
    assert answer["requests"]["requests"] == 3
    bad: Any
    for bad in ([], {"hub_since": "x"}, {"pg_since": -1}, {"hub_since": True}):
        assert call(ctx, "POST", "/perf/hub-log", bad)[0] == 400, bad


def test_route_load(ctx: Context) -> None:  # noqa: F811
    listed = "\n".join(
        json.dumps({"name": name, "status": status, "memory": 8 << 30})
        for name, status in (("wp-t-a", "Running"), ("other-1", "Running"), ("other-2", "Stopped"))
    )
    node = LogNode(
        {},
        {
            ("uptime",): {"rc": 0, "stdout": "20:00  up 3 days, load averages: 2.10 3.00 4.00\n", "stderr": ""},
            ("limactl", "list", "--json"): {"rc": 0, "stdout": listed, "stderr": ""},
            ("sysctl", "-n", "hw.memsize"): {"rc": 0, "stdout": str(512 << 30), "stderr": ""},
        },
    )
    ctx.hub_node = ctx.runner_node = node
    status, answer = call(ctx, "GET", "/perf/load")
    assert status == 200
    assert answer["hub"] == answer["runner"] == "20:00  up 3 days, load averages: 2.10 3.00 4.00"
    assert answer["vms"] == {
        "running": 2,
        "running_of_run": 1,
        "running_foreign": 1,
        "running_memory_mib": 16384,
        "budget_mib": 512 * 1024 - 32768,
        "vm_memory_mib": 8192,
    }
    ctx.runner_node = LogNode({}, {("limactl", "list", "--json"): {"rc": 0, "stdout": listed, "stderr": ""}})
    status, answer = call(ctx, "GET", "/perf/load")
    assert answer["vms"] is None
    ctx.runner_node = LogNode({}, {})
    status, answer = call(ctx, "GET", "/perf/load")
    assert answer["runner"] is None
    assert answer["vms"] is None


def test_route_terminal_storage(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    assert call(ctx, "POST", "/perf/terminal-storage", {"label": "PERF-05"})[0] == 409
    seen: list[str] = []

    def psql(_self: Cluster, sql: str, _database: str = "postgres", *, read_only: bool = False) -> str:
        assert read_only
        seen.append(sql)
        if "pg_total_relation_size" in sql:
            return json.dumps([{"event_total_bytes": 1000, "event_rows": 3}])
        return json.dumps(storage_rows())

    monkeypatch.setattr(Cluster, "psql", psql)
    ctx.cluster = Cluster(StubNode(), "/pg", "/data", 21003, {})
    status, answer = call(ctx, "POST", "/perf/terminal-storage", {"label": "PERF-05"})
    assert status == 200
    assert answer["summary"]["sessions"] == 2
    assert answer["csv"].endswith("perf-terminal-storage.csv")
    status, answer = call(ctx, "POST", "/perf/terminal-storage", {"label": "PERF-05", "sessions": [SESSION_A]})
    assert f"'{SESSION_A}'" in seen[-2]
    bad: Any
    for bad in ([], {"label": ""}, {"label": "x", "sessions": ["nope"]}, {"label": "x", "sessions": "x"}):
        assert call(ctx, "POST", "/perf/terminal-storage", bad)[0] == 400, bad


def test_route_quiesce(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    assert call(ctx, "POST", "/perf/quiesce")[0] == 409
    counts = [3, 2, 0]
    restarts: list[dict[str, str | None]] = []

    def psql(_self: Cluster, sql: str, _database: str = "postgres", *, read_only: bool = False) -> str:
        assert "hub.vm" in sql
        assert read_only
        return json.dumps([{"n": counts.pop(0) if len(counts) > 1 else counts[0]}])

    runner = ctx.runners["runner-1"]
    runner.env["AGENT_RUNNER_VM_IDLE_SECONDS"] = "1800"
    monkeypatch.setattr(Cluster, "psql", psql)
    monkeypatch.setattr(
        capacity,
        "restart_runner",
        lambda r: restarts.append({k: r.env.get(k) for k in perf_final.IDLE_VARIABLES}),
    )
    sleeps: list[float] = []
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(perf_final, "SLEEP", sleeps.append)
    monkeypatch.setattr(perf_final, "CLOCK", lambda: next(ticks))
    ctx.cluster = Cluster(StubNode(), "/pg", "/data", 21003, {})
    status, answer = call(ctx, "POST", "/perf/quiesce", {"timeout_s": 600})
    assert status == 200
    assert answer["running_before"] == 3
    assert sleeps == [perf_final.QUIESCE_POLL_S]
    assert restarts == [
        {"AGENT_RUNNER_SESSION_IDLE_SECONDS": "60", "AGENT_RUNNER_VM_IDLE_SECONDS": "60"},
        {"AGENT_RUNNER_SESSION_IDLE_SECONDS": None, "AGENT_RUNNER_VM_IDLE_SECONDS": "1800"},
    ]
    assert runner.env["AGENT_RUNNER_VM_IDLE_SECONDS"] == "1800"
    assert "AGENT_RUNNER_SESSION_IDLE_SECONDS" not in runner.env

    # still running at the timeout: 409, the earlier values are restored all the same
    counts[:] = [5]
    ticks = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr(perf_final, "CLOCK", lambda: next(ticks))
    restarts.clear()
    status, answer = call(ctx, "POST", "/perf/quiesce", {"timeout_s": 1})
    assert status == 409
    assert restarts[-1] == {"AGENT_RUNNER_SESSION_IDLE_SECONDS": None, "AGENT_RUNNER_VM_IDLE_SECONDS": "1800"}
    bad: Any
    for bad in ([], {"timeout_s": 0}, {"timeout_s": True}, {"timeout_s": 10_000}):
        assert call(ctx, "POST", "/perf/quiesce", bad)[0] == 400, bad

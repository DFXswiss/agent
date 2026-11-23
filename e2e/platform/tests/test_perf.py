"""The PERF harness: time-to-working rows from the hub database, CSV, summary and its control route (SCN-01)."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import pytest
from orchestrator.context import Context
from orchestrator.services import Cluster
from perf import time_to_working as ttw
from test_routes import StubNode, call, ctx  # noqa: F401 - the fixture is used by name

TASK_A = "01a200ce-0451-7080-a832-e27880063cf8"
TASK_B = "01a200ce-0451-7080-a832-e27880063cf9"


def rows(task: str, session: str, total: float | None, steps: list[tuple[str, float, float]]) -> list[dict[str, Any]]:
    base = {
        "task_id": task,
        "session_id": session,
        "created_at": "2026-11-22T01:10:31.308Z",
        "total_s": total,
        "estimate_ms": 3000 if total is not None else None,
    }
    return [{**base, "state": state, "hub_s": hub, "device_s": device} for state, hub, device in steps]


STEPS_A = [
    ("requested", 0.0, 0.0),
    ("vm_assigning", 0.04, 0.03),
    ("vm_ready", 0.54, 0.05),
    ("sandbox_ready", 0.54, 0.08),
    ("working", 0.8, 0.52),
    ("idle", 1.0, 0.6),
    ("vm_ready", 9.0, 9.0),
]


def test_collect_and_csv(tmp_path: Path) -> None:
    seen: list[str] = []

    def query(sql: str) -> list[dict[str, Any]]:
        seen.append(sql)
        return rows(TASK_A, "s-a", 0.8, STEPS_A) + rows(TASK_B, "s-b", None, [("requested", 0.0, 0.0)])

    runs = ttw.collect(query, "PERF-01 new org", [TASK_A, TASK_B], iteration="iter-1")
    assert f"'{TASK_A}', '{TASK_B}'" in seen[0]
    assert runs[0].hub_s == {
        "requested": 0.0,
        "vm_assigning": 0.04,
        "vm_ready": 0.54,
        "sandbox_ready": 0.54,
        "working": 0.8,
    }
    assert runs[1].total_s is None
    assert (runs[0].estimate_ms, runs[1].estimate_ms) == (3000, None)
    assert "s.estimate_ms" in seen[0]
    assert ttw.slowest_step(runs[0]) == ("vm_ready", 0.5)
    assert ttw.slowest_step(ttw.Run("", "x", TASK_A, "s", "t", None)) is None
    path = tmp_path / "out" / "perf.csv"
    ttw.write_csv(path, runs[:1])
    ttw.write_csv(path, runs[1:])
    table = list(csv.reader(path.open()))
    assert table[0] == list(ttw.COLUMNS)
    assert len(table) == 3
    assert table[1][:6] == ["iter-1", "PERF-01 new org", TASK_A, "s-a", "2026-11-22T01:10:31.308Z", "0.800"]
    assert table[1][ttw.COLUMNS.index("prompt_delivering_hub_s")] == ""
    assert table[1][ttw.COLUMNS.index("estimate_ms")] == "3000"
    assert table[2][ttw.COLUMNS.index("estimate_ms")] == ""
    assert table[2][5] == ""
    assert ttw.read_csv(path) == {"iter-1 PERF-01 new org": [0.8, None]}
    with pytest.raises(ValueError, match="tasks without a main session"):
        ttw.collect(lambda sql: rows(TASK_A, "s-a", 0.8, STEPS_A), "x", [TASK_A, TASK_B])
    with pytest.raises(ValueError, match="must be UUIDs"):
        ttw.runs_sql(["1; DROP TABLE x"])
    with pytest.raises(ValueError, match="none given"):
        ttw.runs_sql([])


def test_summary_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ttw.p95([float(n) for n in range(1, 21)]) == 19.0
    assert ttw.summary([0.8, 1.2, 9.9]) == {"runs": 3, "p95_s": 9.9, "max_s": 9.9, "passed": True}
    assert ttw.summary([0.8, 10.0])["passed"] is False
    assert ttw.summary([0.8, None]) == {"runs": 2, "p95_s": None, "max_s": None, "passed": False}
    assert ttw.summary([]) == {"runs": 0, "p95_s": None, "max_s": None, "passed": False}
    assert ttw.summary([11.0], limit=12)["passed"] is True
    assert math.isinf(ttw.p95([math.inf]))
    path = tmp_path / "perf.csv"
    ttw.write_csv(path, [ttw.Run("", "A", TASK_A, "s", "t", 0.8), ttw.Run("", "B", TASK_B, "s", "t", 12.0)])
    assert ttw.main([str(path)]) == 1
    assert capsys.readouterr().out == (
        "A: runs=1 p95=0.8 max=0.8 passed=True\nB: runs=1 p95=12.0 max=12.0 passed=False\n"
    )
    assert ttw.main([str(path), "--limit", "13"]) == 0


def test_route(ctx: Context, tmp_path: Path) -> None:  # noqa: F811
    node = ctx.hub_node
    assert isinstance(node, StubNode)
    body = {"label": "PERF-02 clone fallback", "tasks": [TASK_A]}
    for bad in (
        None,
        {"label": "", "tasks": []},
        {"label": "x", "tasks": [1]},
        {**body, "limit": True},
        {**body, "limit": "9"},
    ):
        assert call(ctx, "POST", "/perf/time-to-working", bad)[0] == 400
    assert call(ctx, "POST", "/perf/time-to-working", body)[0] == 409
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {})
    node.runs["-A -t"] = {"rc": 0, "stdout": json.dumps(rows(TASK_A, "s-a", 7.5, STEPS_A)) + "\n", "stderr": ""}
    status, answer = call(ctx, "POST", "/perf/time-to-working", {**body, "limit": 7})
    assert status == 200
    assert answer["summary"] == {"runs": 1, "p95_s": 7.5, "max_s": 7.5, "passed": False}
    assert answer["runs"] == [{"task_id": TASK_A, "total_s": 7.5, "estimate_ms": 3000, "slowest": ("vm_ready", 0.5)}]
    assert answer["csv"] == str(ctx.config.artifacts / "perf-time-to-working.csv")
    assert Path(answer["csv"]).read_text().count(TASK_A) == 1
    assert f"{Path(ctx.scratch).name},PERF-02 clone fallback,{TASK_A}" in Path(answer["csv"]).read_text()
    status, answer = call(ctx, "POST", "/perf/time-to-working", {"label": "x", "tasks": ["nope"]})
    assert (status, answer["error"]) == (400, "invalid")

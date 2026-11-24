"""`POST /perf/time-to-working` (`{"label": "PERF-01 new org", "tasks": [task IDs], "limit": 10}`): measure the tasks
of a PERF series from the hub database (`perf.time_to_working`), append them to `<artifacts>/perf-time-to-working.csv`
and answer the series summary `{summary: {runs, p95_s, max_s, passed}, runs: [{task_id, total_s, estimate_ms,
slowest}], csv}`; `slowest` is the start transition that took longest (the cause to name for a run at or above the
limit)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from perf import time_to_working

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.services import HUB_DATABASE

CSV_NAME = "perf-time-to-working.csv"


def post_time_to_working(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    label = body.get("label") if isinstance(body, dict) else None
    tasks = body.get("tasks") if isinstance(body, dict) else None
    limit = body.get("limit", time_to_working.LIMIT_S) if isinstance(body, dict) else None
    if (
        not isinstance(label, str)
        or not label.strip()
        or not isinstance(tasks, list)
        or not all(isinstance(task, str) for task in tasks)
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
    ):
        raise RouteError(400, "invalid", 'body must be {"label": "...", "tasks": ["<task id>", ...], "limit"?: 10}')
    cluster = ctx.cluster
    if cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")

    def query(sql: str) -> list[dict[str, Any]]:
        wrapped = f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) AS q"
        rows: list[dict[str, Any]] = json.loads(cluster.psql(wrapped, HUB_DATABASE, read_only=True))
        return rows

    try:
        runs = time_to_working.collect(query, label, tasks, iteration=Path(ctx.scratch).name)
    except ValueError as exc:
        raise RouteError(400, "invalid", str(exc)) from None
    path = ctx.config.artifacts / CSV_NAME
    time_to_working.write_csv(path, runs)
    result = time_to_working.summary([run.total_s for run in runs], float(limit))
    detail = [
        {
            "task_id": run.task_id,
            "total_s": run.total_s,
            "estimate_ms": run.estimate_ms,
            "slowest": time_to_working.slowest_step(run),
        }
        for run in runs
    ]
    return 200, {"summary": result, "runs": detail, "csv": str(path)}


ROUTES = [Route("POST", "/perf/time-to-working", post_time_to_working)]

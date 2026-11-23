"""Control routes of the final PERF evaluation (PFM-01; `perf.samples`, `perf.storage`). Only for specs:

- `POST /perf/samples` (`{"name": "reconnect", "label": "runner catch-up T1", "values": [ms, …], "unit"?: "ms",
  "limit"?: n}`): appends the series to `<artifacts>/perf-<name>.csv` and answers `{summary: {n, min, p50, p95, max,
  limit}, passed, csv}` (passed: p95 and maximum below `limit`; without a limit any non-empty series).
- `POST /perf/hub-log` (`{"hub_since"?: lines, "pg_since"?: lines}`): request counts of the hub's access log
  (`perf.samples.hub_requests`) and the deadlocks of the PostgreSQL log, both after the given numbers of lines (a
  count taken before a measured window). Answers `{hub_lines, pg_lines, requests, deadlocks}`.
- `GET /perf/load`: the device load at the time of a measurement (07-testing.md section 5.3: "idle apart from the
  test"): `uptime` of the hub device and the runner device and the running Lima VMs of the runner device, split into
  the run's (name starts with the run's VM prefix) and foreign ones, with the memory of all running VMs, the device's
  VM memory budget (07-testing.md section 7: device memory minus the runner's reserve) and the run's VM memory. Answers
  `{hub, runner, vms}` (`null` when a device does not answer or has no Lima).
- `POST /perf/quiesce` (`{"timeout_s"?: 900}`): stops the workgroup VMs the earlier specs of the iteration left
  running, so a measurement starts with the device's VM memory free (PERF-05; 05-runner.md sections 3.4 and 3.5:
  stopped VMs take no slot and give their RAM back). Every runner of the run is restarted (`capacity.restart_runner`:
  same home, store and VMs) with `AGENT_RUNNER_SESSION_IDLE_SECONDS` and `AGENT_RUNNER_VM_IDLE_SECONDS` at their
  minimum (60 s): idle sessions are suspended, then their VMs stop. Once the hub lists no running workgroup VM
  (`hub.vm`), the runners are restarted with their earlier values. Answers `{running_before, seconds}`; 409 when the
  VMs are still running at the timeout (the earlier values are restored in any case).
- `POST /perf/terminal-storage` (`{"label": "PERF-05", "sessions"?: [ids]}`): terminal storage at the hub
  (`perf.storage`) of all sessions or the given ones; rows appended to `<artifacts>/perf-terminal-storage.csv`.
  Answers `{summary, csv}`.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from perf import samples, storage

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.node import Node
from orchestrator.routes import capacity
from orchestrator.services import HUB_DATABASE
from orchestrator.vms import VmError, default_budget, list_vms

STORAGE_CSV = "perf-terminal-storage.csv"
ALL_LINES = 10_000_000
IDLE_VARIABLES = ("AGENT_RUNNER_SESSION_IDLE_SECONDS", "AGENT_RUNNER_VM_IDLE_SECONDS")
IDLE_MINIMUM_S = "60"
QUIESCE_TIMEOUT_S = 900
QUIESCE_POLL_S = 5.0
RUNNING_WG_VMS = "SELECT count(*)::int AS n FROM hub.vm WHERE state = 'running' AND wg_id IS NOT NULL"
SLEEP: Callable[[float], None] = time.sleep
CLOCK: Callable[[], float] = time.monotonic


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def post_samples(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", 'body must be {"name", "label", "values": [n, ...], "unit"?, "limit"?}')
    label, values, unit = body.get("label"), body.get("values"), body.get("unit", "ms")
    limit = body.get("limit")
    if (
        not isinstance(label, str)
        or not label.strip()
        or not isinstance(values, list)
        or not all(_number(value) for value in values)
        or not isinstance(unit, str)
        or not unit
        or (limit is not None and not _number(limit))
    ):
        raise RouteError(400, "invalid", 'body must be {"name", "label", "values": [n, ...], "unit"?, "limit"?}')
    try:
        path = samples.series_path(ctx.config.artifacts, body.get("name"))
    except ValueError as exc:
        raise RouteError(400, "invalid", str(exc)) from None
    numbers = [float(value) for value in values]
    samples.append_series(path, Path(ctx.scratch).name, label, unit, numbers)
    summary = samples.distribution(numbers)
    return 200, {"summary": {**summary, "limit": limit}, "passed": samples.passed(summary, limit), "csv": str(path)}


def post_hub_log(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    body = {} if body is None else body
    marks = [body.get(key, 0) for key in ("hub_since", "pg_since")] if isinstance(body, dict) else None
    if marks is None or not all(isinstance(m, int) and not isinstance(m, bool) and m >= 0 for m in marks):
        raise RouteError(400, "invalid", 'body must be {"hub_since"?: lines, "pg_since"?: lines}')
    hub_log = ctx.hub_node.log_tail("hub", ALL_LINES)
    pg_log = ctx.hub_node.log_tail("postgres", ALL_LINES)
    return 200, {
        "hub_lines": len(hub_log.splitlines()),
        "pg_lines": len(pg_log.splitlines()),
        "requests": samples.hub_requests(samples.since(hub_log, marks[0])),
        "deadlocks": samples.deadlocks(samples.since(pg_log, marks[1])),
    }


def _uptime(node: Node) -> str | None:
    done = node.run(["uptime"])
    return str(done["stdout"]).strip() if done["rc"] == 0 else None


def get_load(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    try:
        listed = list_vms(ctx.runner_node)
        budget = default_budget(ctx.runner_node) if listed is not None else 0
    except VmError:
        listed = None
    vms = None
    if listed is not None:
        running = [vm for vm in listed if vm.status == "Running"]
        ours = [vm for vm in running if vm.name.startswith(ctx.config.vm_prefix)]
        vms = {
            "running": len(running),
            "running_of_run": len(ours),
            "running_foreign": len(running) - len(ours),
            "running_memory_mib": sum(vm.memory_mib for vm in running),
            "budget_mib": budget,
            "vm_memory_mib": ctx.config.vm_memory_mib,
        }
    return 200, {"hub": _uptime(ctx.hub_node), "runner": _uptime(ctx.runner_node), "vms": vms}


def _hub_rows(ctx: Context, sql: str) -> list[dict[str, Any]]:
    cluster = ctx.cluster
    if cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    wrapped = f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) AS q"
    rows: list[dict[str, Any]] = json.loads(cluster.psql(wrapped, HUB_DATABASE, read_only=True))
    return rows


def post_quiesce(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    body = {} if body is None else body
    timeout = body.get("timeout_s", QUIESCE_TIMEOUT_S) if isinstance(body, dict) else None
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 < timeout <= QUIESCE_TIMEOUT_S:
        raise RouteError(400, "invalid", f'body must be {{"timeout_s"?: 1..{QUIESCE_TIMEOUT_S}}}')
    started = CLOCK()
    before = _hub_rows(ctx, RUNNING_WG_VMS)[0]["n"]
    saved = {name: {key: runner.env.get(key) for key in IDLE_VARIABLES} for name, runner in ctx.runners.items()}
    try:
        for runner in ctx.runners.values():
            runner.env.update(dict.fromkeys(IDLE_VARIABLES, IDLE_MINIMUM_S))
            capacity.restart_runner(runner)
        while _hub_rows(ctx, RUNNING_WG_VMS)[0]["n"] > 0:
            if CLOCK() - started >= timeout:
                raise RouteError(409, "invalid", f"workgroup VMs still running after {timeout} s")
            SLEEP(QUIESCE_POLL_S)
    finally:
        for name, runner in ctx.runners.items():
            for key, value in saved[name].items():
                if value is None:
                    runner.env.pop(key, None)
                else:
                    runner.env[key] = value
            capacity.restart_runner(runner)
    return 200, {"running_before": before, "seconds": round(CLOCK() - started, 1)}


def post_terminal_storage(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    label = body.get("label") if isinstance(body, dict) else None
    sessions = storage.valid_sessions(body.get("sessions", [])) if isinstance(body, dict) else None
    if not isinstance(label, str) or not label.strip() or sessions is None:
        raise RouteError(400, "invalid", 'body must be {"label": "...", "sessions"?: ["<session id>", ...]}')
    rows = _hub_rows(ctx, storage.sessions_sql(sessions))
    relation = _hub_rows(ctx, storage.relation_sql())[0]
    path = ctx.config.artifacts / STORAGE_CSV
    storage.write_csv(path, Path(ctx.scratch).name, label, rows)
    return 200, {"summary": storage.summarize(rows, relation), "csv": str(path)}


ROUTES = [
    Route("POST", "/perf/samples", post_samples),
    Route("POST", "/perf/hub-log", post_hub_log),
    Route("GET", "/perf/load", get_load),
    Route("POST", "/perf/quiesce", post_quiesce),
    Route("POST", "/perf/terminal-storage", post_terminal_storage),
]

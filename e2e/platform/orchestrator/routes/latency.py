"""Latency measurement of PERF-03 and PERF-04 (07-testing.md section 2.2; 03-api.md section 5; 05-runner.md section 7).
Only for specs, never a product path:

- `POST /latency/marks` (`{"session": "<id>", "count": n, "interval_ms": m}`): starts a transient unit in the
  session's VM that writes `count` lines `PERF-MARK nnnnn` to the tty of the session's tmux pane, one every
  `interval_ms`: terminal output the recorder flushes (every 100 ms) into `session.terminal` events, the load of both
  measurements. Writing to the pane's tty is output, not input: the session never reads it. Answers `{vm, unit}`.
- `POST /latency/events` (`{"label": "PERF-03 T1", "since_ms": t, "frames": [[scope_id, scope_seq, at_ms], …],
  "limit_ms"?: 300, "runner"?: "runner-1"}`): event latency **runner outbox write → browser frame**. `frames` are the
  `event`/`terminal` frames of the browser's live stream with their arrival time (browser clock = this device's clock).
  Each frame is matched with its hub event (`origin_device_id`, `origin_seq`, received after `since_ms`) and that
  with the runner's outbox row (`created_at`, the runner device's clock, corrected by the measured offset).
- `POST /latency/terminal` (`{"label": "PERF-04 T1", "session": "<id>", "opened_ms": t, "renders": [[chunks, at_ms],
  …], "limit_ms"?: 1000}`): terminal latency **recorder flush → xterm render**. `renders` are the samples of the open
  terminal view (the number of chunks applied and the time the screen rendered them; the first sample is the state
  when the view was opened). Chunk `n` is the session's `n`-th `session.terminal` event; its event time is the
  recorder flush (VM clock, corrected by the measured offset). Only chunks the hub received after `opened_ms` count.

Both evaluations append one row per sample to `<artifacts>/perf-event-latency.csv` resp. `perf-terminal-latency.csv`,
one line with summary, clock offset and device load (`uptime` of the hub device and the runner device) to
`<artifacts>/perf-latency.jsonl`, and answer `{summary: {samples, p50_ms, p95_ms, max_ms, limit_ms, passed}, clock,
load, unmatched, csv}`. The hub device is the browser's and the orchestrator's device in both topologies
(07-testing.md section 5.2), so hub receive times need no correction.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeGuard

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.routes import runner_store
from orchestrator.services import HUB_DATABASE

EVENT_LIMIT_MS = 300.0
TERMINAL_LIMIT_MS = 1000.0
CLOCK_SAMPLES = 7
CLOCK: Callable[[], float] = time.time
MAX_MARKS = 20_000
MARK_INTERVAL_MS = (20, 5000)
MAX_SAMPLES = 50_000
EVENT_CSV = "perf-event-latency.csv"
TERMINAL_CSV = "perf-terminal-latency.csv"
SUMMARY_LOG = "perf-latency.jsonl"
EVENT_COLUMNS = (
    "iteration",
    "label",
    "scope_id",
    "scope_seq",
    "kind",
    "origin_seq",
    "outbox_ms",
    "hub_received_ms",
    "browser_ms",
    "latency_ms",
    "hub_to_browser_ms",
)
TERMINAL_COLUMNS = ("iteration", "label", "session_id", "chunk", "scope_seq", "flush_ms", "render_ms", "latency_ms")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TTY_RE = re.compile(r"^/dev/pts/[0-9]{1,6}$")
RUNNER_SESSIONS = "SELECT id::text AS id FROM runner.session"
TIME_SCRIPT = "import time; print(repr(time.time()))"
MARK_SCRIPT = (
    'i=1; while [ "$i" -le {count} ]; do printf \'PERF-MARK %05d\\r\\n\' "$i" > {tty};'
    " i=$((i+1)); sleep {seconds}; done"
)


# ------------------------------------------------------------------ numbers


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (as PERF-01: `time_to_working.p95`)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def summary(latencies: Sequence[float], limit_ms: float) -> dict[str, Any]:
    if not latencies:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "limit_ms": limit_ms, "passed": False}
    top = percentile(latencies, 95)
    return {
        "samples": len(latencies),
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(top, 1),
        "max_ms": round(max(latencies), 1),
        "limit_ms": limit_ms,
        "passed": top < limit_ms,
    }


# ------------------------------------------------------------------ inputs


def _number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _label(body: dict[str, Any]) -> str:
    label = body.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise RouteError(400, "invalid", "label must be a text of 1..80 characters")
    return label.strip()


def _limit(body: dict[str, Any], default: float) -> float:
    limit = body.get("limit_ms", default)
    if not _number(limit) or limit <= 0:
        raise RouteError(400, "invalid", "limit_ms must be a positive number")
    return float(limit)


def _session(body: dict[str, Any]) -> str:
    session = body.get("session")
    if not isinstance(session, str) or not _UUID_RE.fullmatch(session):
        raise RouteError(400, "invalid", "session must be a session ID")
    return session


def _body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RouteError(400, "invalid", "body must be an object")
    return body


def _samples(value: Any, shape: Callable[[Any], bool], what: str) -> list[list[Any]]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_SAMPLES or not all(shape(item) for item in value):
        raise RouteError(400, "invalid", f"{what} must be a list of 1..{MAX_SAMPLES} {what} samples")
    return value


def _frame(item: Any) -> bool:
    return (
        isinstance(item, list)
        and len(item) == 3
        and isinstance(item[0], str)
        and _UUID_RE.fullmatch(item[0]) is not None
        and _integer(item[1])
        and _number(item[2])
    )


def _render(item: Any) -> bool:
    return isinstance(item, list) and len(item) == 2 and _integer(item[0]) and item[0] >= 0 and _number(item[1])


# ------------------------------------------------------------------ devices


def _hub_rows(ctx: Context, sql: str) -> list[dict[str, Any]]:
    if ctx.cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    wrapped = f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) AS q"
    rows: list[dict[str, Any]] = json.loads(ctx.cluster.psql(wrapped, HUB_DATABASE, read_only=True))
    return rows


def _offset(read: Callable[[], float]) -> dict[str, float]:
    """A remote clock minus this device's clock from `CLOCK_SAMPLES` readings (`read` returns the remote time). The
    remote reading happens at the end of a round trip (after the connection and the process start), so the estimate is
    `remote - after` of the shortest round trip; its error is the way back (well below `rtt_ms`, which is reported)."""
    best: tuple[float, float] | None = None
    for _ in range(CLOCK_SAMPLES):
        before = CLOCK()
        remote = read()
        after = CLOCK()
        if best is None or after - before < best[0]:
            best = (after - before, remote - after)
    assert best is not None
    return {"offset_ms": round(best[1] * 1000, 1), "rtt_ms": round(best[0] * 1000, 1)}


def runner_clock(ctx: Context, runner_name: str) -> dict[str, float]:
    """The runner device's clock minus this device's clock; 0 in T1, where both are the same device."""
    if ctx.config.topology == "t1":
        return {"offset_ms": 0.0, "rtt_ms": 0.0}
    runner = ctx.runners[runner_name]

    def read() -> float:
        done = runner.node.run([runner.python, "-I", "-S", "-c", TIME_SCRIPT], timeout=30.0)
        if done["rc"] != 0:
            raise RouteError(500, "failed", f"clock of {runner_name} exited with {done['rc']}")
        return float(done["stdout"].strip())

    return _offset(read)


def vm_clock(ctx: Context, session: str) -> dict[str, float]:
    """The clock of the session's VM minus this device's clock (`date` as root in the VM)."""
    try:
        vm = guestvm.session_vm(ctx, session)
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None
    return _offset(lambda: float(_in_vm(ctx, vm, ["date", "+%s.%N"]).strip()))


def device_load(ctx: Context, runner_name: str | None) -> dict[str, str]:
    """`uptime` of the hub device and of the runner device (the load during a measurement belongs to its result)."""
    nodes = {"hub": ctx.hub_node}
    if runner_name is not None:
        nodes["runner"] = ctx.runners[runner_name].node
    load = {}
    for role, node in nodes.items():
        done = node.run(["uptime"], timeout=10.0)
        load[role] = done["stdout"].strip() if done["rc"] == 0 else f"uptime exited with {done['rc']}"
    return load


def _record(ctx: Context, name: str, columns: Sequence[str], rows: list[dict[str, Any]], entry: dict[str, Any]) -> str:
    path = ctx.config.artifacts / name
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        if new:
            writer.writeheader()
        writer.writerows(rows)
    with (ctx.config.artifacts / SUMMARY_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return str(path)


def _in_vm(ctx: Context, vm: str, argv: list[str]) -> str:
    try:
        return guestvm.checked(ctx, vm, argv)
    except guestvm.GuestVmError as exc:
        raise RouteError(500, "failed", str(exc)) from None


def _iteration(ctx: Context) -> str:
    return Path(ctx.scratch).name


# ------------------------------------------------------------------ routes


def post_marks(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    data = _body(body)
    session = _session(data)
    count, interval = data.get("count"), data.get("interval_ms")
    low, high = MARK_INTERVAL_MS
    if not _integer(count) or not 0 < count <= MAX_MARKS or not _integer(interval) or not low <= interval <= high:
        raise RouteError(400, "invalid", f"count must be 1..{MAX_MARKS} and interval_ms {low}..{high}")
    try:
        vm = guestvm.session_vm(ctx, session)
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None
    tmux = ["tmux", "-S", guestvm.TMUX_SOCKET, "display-message", "-p", "-t", f"=s-{session}:", "#{pane_tty}"]
    tty = _in_vm(ctx, vm, tmux).strip()
    if not _TTY_RE.fullmatch(tty):
        raise RouteError(500, "failed", f"unexpected pane tty {tty!r}")
    unit = f"e2e-marks-{session[:8]}-{int(CLOCK() * 1000)}"
    script = MARK_SCRIPT.format(count=count, tty=tty, seconds=f"{interval / 1000:.3f}")
    _in_vm(ctx, vm, ["systemd-run", "--quiet", "--collect", "--unit", unit, "sh", "-c", script])
    return 200, {"vm": vm, "unit": unit}


def post_events(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    data = _body(body)
    label, limit = _label(data), _limit(data, EVENT_LIMIT_MS)
    frames = _samples(data.get("frames"), _frame, "frames")
    since = data.get("since_ms")
    runner_name = data.get("runner", "runner-1")
    if not _number(since):
        raise RouteError(400, "invalid", "since_ms must be a time in ms")
    if runner_name not in ctx.runners:
        raise RouteError(404, "not_found", f"no runner {runner_name}")
    hub = _hub_rows(
        ctx,
        "SELECT scope_id::text, scope_seq, kind, origin_device_id::text AS device, origin_seq, session_id::text,"
        " (extract(epoch FROM received_at) * 1000)::float8 AS received_ms FROM hub.event"
        f" WHERE origin_seq IS NOT NULL AND received_at >= to_timestamp({float(since) / 1000!r})",
    )
    by_key = {(row["scope_id"], row["scope_seq"]): row for row in hub}
    first: dict[tuple[str, int], float] = {}
    for scope_id, scope_seq, at in frames:
        key = (scope_id, scope_seq)
        first[key] = min(at, first.get(key, at))
    matched = {key: by_key[key] for key in first if key in by_key}
    devices = sorted({row["device"] for row in matched.values()})
    if len(devices) > 1:
        raise RouteError(409, "invalid", f"the frames belong to events of several devices: {', '.join(devices)}")
    outbox: dict[int, float] = {}
    if matched:
        seqs = [row["origin_seq"] for row in matched.values()]
        sql = (
            "SELECT origin_seq, (extract(epoch FROM created_at) * 1000)::float8 AS created_ms FROM runner.outbox"
            f" WHERE item_kind = 'event' AND origin_seq BETWEEN {min(seqs)} AND {max(seqs)}"
        )
        _status, answer = runner_store.post_store_query(ctx, {"name": runner_name}, {"sql": sql})
        outbox = {row["origin_seq"]: row["created_ms"] for row in answer["rows"]}
        # the outbox is the named runner's only if the device's events belong to sessions that runner runs
        _status, known = runner_store.post_store_query(ctx, {"name": runner_name}, {"sql": RUNNER_SESSIONS})
        ours = {row["id"] for row in known["rows"]}
        if not any(row["session_id"] in ours for row in matched.values()):
            raise RouteError(409, "invalid", f"the frames' events are not from the sessions of {runner_name}")
    clock = runner_clock(ctx, runner_name)
    iteration, rows, latencies = _iteration(ctx), [], []
    for key in sorted(matched, key=lambda item: first[item]):
        event = matched[key]
        created = outbox.get(event["origin_seq"])
        if created is None:
            continue
        written = created - clock["offset_ms"]
        latency = first[key] - written
        latencies.append(latency)
        rows.append(
            {
                "iteration": iteration,
                "label": label,
                "scope_id": key[0],
                "scope_seq": key[1],
                "kind": event["kind"],
                "origin_seq": event["origin_seq"],
                "outbox_ms": f"{written:.1f}",
                "hub_received_ms": f"{event['received_ms']:.1f}",
                "browser_ms": f"{first[key]:.1f}",
                "latency_ms": f"{latency:.1f}",
                "hub_to_browser_ms": f"{first[key] - event['received_ms']:.1f}",
            }
        )
    result = summary(latencies, limit)
    unmatched = {
        "frames_without_device_event": len(first) - len(matched),
        "events_without_outbox_row": len(matched) - len(latencies),
    }
    load = device_load(ctx, runner_name)
    entry = {"label": label, "iteration": iteration, "kind": "event", "summary": result, "clock": clock, "load": load}
    path = _record(ctx, EVENT_CSV, EVENT_COLUMNS, rows, entry | {"unmatched": unmatched})
    return 200, {"summary": result, "clock": clock, "load": load, "unmatched": unmatched, "csv": path}


def post_terminal(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    data = _body(body)
    label, limit, session = _label(data), _limit(data, TERMINAL_LIMIT_MS), _session(data)
    renders = _samples(data.get("renders"), _render, "renders")
    opened = data.get("opened_ms")
    if not _number(opened):
        raise RouteError(400, "invalid", "opened_ms must be a time in ms")
    events = _hub_rows(
        ctx,
        "SELECT scope_seq, (extract(epoch FROM ts) * 1000)::float8 AS ts_ms,"
        " (extract(epoch FROM received_at) * 1000)::float8 AS received_ms FROM hub.event"
        f" WHERE session_id = '{session}' AND kind = 'session.terminal' ORDER BY scope_seq",
    )
    clock = vm_clock(ctx, session)
    iteration, rows, latencies = _iteration(ctx), [], []
    seen, not_received = renders[0][0], 0
    for count, at in renders[1:]:
        for chunk in range(seen + 1, min(count, len(events)) + 1):
            event = events[chunk - 1]
            if event["received_ms"] < opened:
                continue
            flushed = event["ts_ms"] - clock["offset_ms"]
            latencies.append(at - flushed)
            rows.append(
                {
                    "iteration": iteration,
                    "label": label,
                    "session_id": session,
                    "chunk": chunk,
                    "scope_seq": event["scope_seq"],
                    "flush_ms": f"{flushed:.1f}",
                    "render_ms": f"{at:.1f}",
                    "latency_ms": f"{at - flushed:.1f}",
                }
            )
        not_received += max(0, count - max(seen, len(events)))
        seen = max(seen, count)
    result = summary(latencies, limit)
    unmatched = {"chunks_without_event": not_received}
    load = device_load(ctx, "runner-1" if "runner-1" in ctx.runners else None)
    entry = {
        "label": label,
        "iteration": iteration,
        "kind": "terminal",
        "summary": result,
        "clock": clock,
        "load": load,
    }
    path = _record(ctx, TERMINAL_CSV, TERMINAL_COLUMNS, rows, entry | {"unmatched": unmatched})
    return 200, {"summary": result, "clock": clock, "load": load, "unmatched": unmatched, "csv": path}


ROUTES = [
    Route("POST", "/latency/marks", post_marks),
    Route("POST", "/latency/events", post_events),
    Route("POST", "/latency/terminal", post_terminal),
]

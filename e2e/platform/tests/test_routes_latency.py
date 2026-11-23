"""Latency routes of SCN-08 (PERF-03 event latency, PERF-04 terminal latency, the marker writer) against a scripted
node: hub queries (psql), the runner's store and clock, `uptime`, the session's VM."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.routes import latency, sessions_vm
from orchestrator.services import Cluster, Runner
from test_routes_guest import SESSION, VM1, Result, ScriptNode, call, failed, guest, make_ctx, ok, session_handler

SCOPE = "01a20172-b86a-789c-89ab-7aa8af7f0001"
OTHER_SCOPE = "01a20172-b86a-789c-89ab-7aa8af7f0002"
DEVICE = "01a20172-b86a-789c-89ab-7aa8af7fd001"
DEVICE_2 = "01a20172-b86a-789c-89ab-7aa8af7fd002"


class Devices:
    """Scripted answers: hub rows by query kind, the runner's outbox rows and clock, `uptime`, the VM."""

    def __init__(self) -> None:
        self.hub_events: list[dict[str, Any]] = []
        self.terminal: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.runner_sessions: list[dict[str, Any]] = [{"id": SESSION}]
        self.runner_time = "1000.05"
        self.clock_rc = 0
        self.uptime: Result = ok("17:00  up 5 days, load averages: 1.00 2.00 3.00\n")
        self.vm: Callable[[str, list[str]], Result] = session_handler(lambda vm, argv: ok("1000.0\n"))
        self.sql: list[str] = []
        self.guest = guest(self.route_vm)

    def route_vm(self, vm: str, argv: list[str]) -> Result:
        return self.vm(vm, argv)

    def respond(self, argv: list[str]) -> Result:
        if argv[0] == "/pg/psql":
            sql = argv[argv.index("-c") + 1]
            self.sql.append(sql)
            return ok(json.dumps(self.terminal if "session.terminal" in sql else self.hub_events) + "\n")
        if argv[:2] == ["/agent/python", "-I"]:
            return ok(self.runner_time + "\n") if self.clock_rc == 0 else failed(self.clock_rc)
        if argv[:2] == ["/agent/python", "-c"]:
            self.sql.append(argv[-1])
            rows = self.runner_sessions if "runner.session" in argv[-1] else self.outbox
            return ok(json.dumps(rows) + "\n")
        if argv == ["uptime"]:
            return self.uptime
        return self.guest(argv)


def make(tmp_path: Path) -> tuple[Context, ScriptNode, Devices]:
    devices = Devices()
    node = ScriptNode(devices.respond)
    ctx = make_ctx(tmp_path / "iter-1", node)
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {})
    ctx.runners["runner-1"] = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "~/r1"})
    return ctx, node, devices


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """This device's clock: every reading 1000.0 + 0.02 n (round trips of 20 ms around the remote reading)."""
    now = [999.98]

    def clock() -> float:
        now[0] += 0.02
        return now[0]

    monkeypatch.setattr(latency, "CLOCK", clock)
    monkeypatch.setattr(sessions_vm, "CLOCK", clock)
    return now


def test_numbers() -> None:
    values = [float(n) for n in range(1, 21)]
    assert latency.percentile(values, 95) == 19.0
    assert latency.percentile(values, 50) == 10.0
    assert latency.percentile([7.0], 95) == 7.0
    assert latency.summary([], 300) == {
        "samples": 0,
        "p50_ms": None,
        "p95_ms": None,
        "max_ms": None,
        "limit_ms": 300,
        "passed": False,
    }
    assert latency.summary([100.04, 200.0, 299.0], 300.0) == {
        "samples": 3,
        "p50_ms": 200.0,
        "p95_ms": 299.0,
        "max_ms": 299.0,
        "limit_ms": 300.0,
        "passed": True,
    }
    assert latency.summary([300.0], 300.0)["passed"] is False


def test_marks(tmp_path: Path, ticks: list[float]) -> None:
    ctx, node, devices = make(tmp_path)
    started: list[list[str]] = []

    def vm(_vm: str, argv: list[str]) -> Result | None:
        if argv[3] == "display-message":
            assert argv[:3] == ["tmux", "-S", guestvm.TMUX_SOCKET]
            assert argv[4:] == ["-p", "-t", f"=s-{SESSION}:", "#{pane_tty}"]
            return ok("/dev/pts/3\n")
        started.append(argv)
        return ok()

    devices.vm = session_handler(lambda name, argv: vm(name, argv) if argv[0] in ("tmux", "systemd-run") else None)
    bad: Any
    for bad in (
        [],
        {"session": "x", "count": 1, "interval_ms": 100},
        {"session": SESSION, "count": 0, "interval_ms": 100},
        {"session": SESSION, "count": True, "interval_ms": 100},
        {"session": SESSION, "count": 20_001, "interval_ms": 100},
        {"session": SESSION, "count": 10, "interval_ms": 19},
        {"session": SESSION, "count": 10, "interval_ms": 1.5},
    ):
        assert call(ctx, "POST", "/latency/marks", bad)[0] == 400, bad
    status, answer = call(ctx, "POST", "/latency/marks", {"session": SESSION, "count": 3, "interval_ms": 150})
    assert status == 200
    assert answer == {"vm": VM1, "unit": "e2e-marks-01a20172-1000000"}
    assert started == [
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit",
            "e2e-marks-01a20172-1000000",
            "sh",
            "-c",
            'i=1; while [ "$i" -le 3 ]; do printf \'PERF-MARK %05d\\r\\n\' "$i" > /dev/pts/3; i=$((i+1));'
            " sleep 0.150; done",
        ]
    ]
    # an unknown session, a pane without a tty, a failing unit start
    devices.vm = lambda name, argv: failed()
    assert call(ctx, "POST", "/latency/marks", {"session": SESSION, "count": 3, "interval_ms": 150})[0] == 404
    devices.vm = session_handler(lambda name, argv: ok("/dev/tty; rm -rf /\n"))
    assert call(ctx, "POST", "/latency/marks", {"session": SESSION, "count": 3, "interval_ms": 150})[0] == 500
    devices.vm = session_handler(lambda name, argv: ok("/dev/pts/3\n") if argv[0] == "tmux" else failed())
    status, answer = call(ctx, "POST", "/latency/marks", {"session": SESSION, "count": 3, "interval_ms": 150})
    assert (status, answer["error"]) == (500, "failed")
    assert node.runs


def hub_event(scope: str, seq: int, origin: int, received: float, device: str = DEVICE) -> dict[str, Any]:
    return {
        "session_id": SESSION,
        "scope_id": scope,
        "scope_seq": seq,
        "kind": "session.terminal",
        "device": device,
        "origin_seq": origin,
        "received_ms": received,
    }


def test_event_latency(tmp_path: Path, ticks: list[float]) -> None:
    ctx, _node, devices = make(tmp_path)
    ctx.config = replace(ctx.config, topology="t2")
    # the runner's clock is 50 ms ahead (remote 1000.07 read at the end of the first 20 ms round trip, 1000.02)
    devices.runner_time = "1000.07"
    devices.hub_events = [
        hub_event(SCOPE, 1, 7, 2_000_120.0),
        hub_event(SCOPE, 2, 8, 2_000_220.0),
        hub_event(OTHER_SCOPE, 1, 9, 2_000_300.0),
    ]
    devices.outbox = [{"origin_seq": 7, "created_ms": 2_000_150.0}, {"origin_seq": 8, "created_ms": 2_000_250.0}]
    frames = [
        [SCOPE, 1, 2_000_200.0],
        [SCOPE, 1, 2_000_100.0],  # the same frame again (at-least-once): the first arrival counts
        [SCOPE, 2, 2_000_400.0],
        [OTHER_SCOPE, 1, 2_000_500.0],  # no outbox row (an item of another kind)
        [OTHER_SCOPE, 5, 2_000_500.0],  # a user's event: no device origin
    ]
    body = {"label": "PERF-03 T1", "since_ms": 1_999_000.0, "frames": frames}
    status, answer = call(ctx, "POST", "/latency/events", body)
    assert status == 200, answer
    # outbox 2_000_150 on the runner = 2_000_100 here: frame 2_000_100 → 0 ms; 2_000_200 → 200 ms
    assert answer["summary"] == {
        "samples": 2,
        "p50_ms": 0.0,
        "p95_ms": 200.0,
        "max_ms": 200.0,
        "limit_ms": 300.0,
        "passed": True,
    }
    assert answer["clock"] == {"offset_ms": 50.0, "rtt_ms": 20.0}
    assert answer["unmatched"] == {"frames_without_device_event": 1, "events_without_outbox_row": 1}
    assert answer["load"]["hub"].startswith("17:00")
    assert answer["load"]["runner"].startswith("17:00")
    assert "to_timestamp(1999.0)" in devices.sql[0]
    assert "origin_seq BETWEEN 7 AND 9" in devices.sql[1]
    table = list(csv.DictReader(Path(answer["csv"]).read_text().splitlines()))
    assert [row["latency_ms"] for row in table] == ["0.0", "200.0"]
    assert table[0] | {} == {
        "iteration": "iter-1",
        "label": "PERF-03 T1",
        "scope_id": SCOPE,
        "scope_seq": "1",
        "kind": "session.terminal",
        "origin_seq": "7",
        "outbox_ms": "2000100.0",
        "hub_received_ms": "2000120.0",
        "browser_ms": "2000100.0",
        "latency_ms": "0.0",
        "hub_to_browser_ms": "-20.0",
    }
    # a second evaluation appends rows below the same header and one more summary line
    devices.uptime = failed(127)
    ticks[0] = 999.98
    answer = call(ctx, "POST", "/latency/events", body | {"limit_ms": 100})[1]
    assert answer["summary"]["passed"] is False
    assert answer["load"]["hub"] == "uptime exited with 127"
    assert len(list(csv.reader(Path(answer["csv"]).read_text().splitlines()))) == 5
    lines = (ctx.config.artifacts / latency.SUMMARY_LOG).read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["event", "event"]
    assert json.loads(lines[0])["unmatched"] == answer["unmatched"]


def test_event_latency_without_matches_and_refusals(tmp_path: Path, ticks: list[float]) -> None:
    ctx, _node, devices = make(tmp_path)
    body: dict[str, Any] = {"label": "PERF-03", "since_ms": 0, "frames": [[SCOPE, 1, 5.0]]}
    status, answer = call(ctx, "POST", "/latency/events", body)
    assert status == 200
    assert answer["summary"]["samples"] == 0
    assert answer["unmatched"] == {"frames_without_device_event": 1, "events_without_outbox_row": 0}
    assert answer["clock"] == {"offset_ms": 0.0, "rtt_ms": 0.0}  # T1: the runner device is this device
    assert len(devices.sql) == 1  # no outbox query without a matched event
    devices.hub_events = [hub_event(SCOPE, 1, 1, 1.0), hub_event(SCOPE, 2, 2, 2.0, DEVICE_2)]
    status, answer = call(ctx, "POST", "/latency/events", body | {"frames": [[SCOPE, 1, 5.0], [SCOPE, 2, 6.0]]})
    assert (status, answer["error"]) == (409, "invalid")
    assert DEVICE_2 in answer["detail"]
    devices.hub_events = [hub_event(SCOPE, 1, 1, 1.0)]
    devices.runner_sessions = [{"id": SCOPE}]  # the events are another runner's
    status, answer = call(ctx, "POST", "/latency/events", body)
    assert (status, answer["error"]) == (409, "invalid")
    devices.runner_sessions = [{"id": SESSION}]
    ctx.config = replace(ctx.config, topology="t2")
    devices.clock_rc = 2
    assert call(ctx, "POST", "/latency/events", body)[0] == 500
    for bad in (
        "x",
        body | {"label": " "},
        body | {"label": 3},
        body | {"label": "x" * 81},
        body | {"limit_ms": 0},
        body | {"limit_ms": "300"},
        body | {"limit_ms": float("nan")},
        body | {"frames": []},
        body | {"frames": [[SCOPE, 1]]},
        body | {"frames": [["scope", 1, 5.0]]},
        body | {"frames": [[SCOPE, True, 5.0]]},
        body | {"frames": [[SCOPE, 1, None]]},
        body | {"frames": [(SCOPE, 1, 5.0)]},
        body | {"since_ms": None},
    ):
        assert call(ctx, "POST", "/latency/events", bad)[0] == 400, bad
    assert call(ctx, "POST", "/latency/events", body | {"runner": "runner-9"})[0] == 404
    ctx.cluster = None
    assert call(ctx, "POST", "/latency/events", body)[0] == 409


def flush(seq: int, ts: float, received: float) -> dict[str, Any]:
    return {"scope_seq": seq, "ts_ms": ts, "received_ms": received}


def test_terminal_latency(tmp_path: Path, ticks: list[float]) -> None:
    ctx, _node, devices = make(tmp_path)
    # the VM's `date` says 1000.0, read at the end of a 20 ms round trip (1000.02): the VM is 20 ms behind
    devices.terminal = [
        flush(4, 1_000.0, 1_010.0),  # received before the view opened: history, not live
        flush(5, 2_000.0, 2_050.0),
        flush(6, 2_100.0, 2_150.0),
        flush(9, 2_200.0, 2_250.0),
    ]
    renders = [[0, 1_500.0], [1, 1_600.0], [3, 2_400.0], [2, 2_450.0], [4, 2_600.0], [6, 2_900.0]]
    body = {"label": "PERF-04 T1", "session": SESSION, "opened_ms": 1_500.0, "renders": renders}
    status, answer = call(ctx, "POST", "/latency/terminal", body)
    assert status == 200, answer
    # chunks 2 and 3 rendered at 2_400 (flushed at 2_020 and 2_120 here), chunk 4 at 2_600 (flushed at 2_220)
    assert answer["summary"] == {
        "samples": 3,
        "p50_ms": 380.0,
        "p95_ms": 380.0,
        "max_ms": 380.0,
        "limit_ms": 1000.0,
        "passed": True,
    }
    assert answer["clock"] == {"offset_ms": -20.0, "rtt_ms": 20.0}
    assert answer["unmatched"] == {"chunks_without_event": 2}
    assert answer["load"]["runner"].startswith("17:00")
    table = list(csv.DictReader(Path(answer["csv"]).read_text().splitlines()))
    assert [(row["chunk"], row["scope_seq"], row["latency_ms"]) for row in table] == [
        ("2", "5", "380.0"),
        ("3", "6", "280.0"),
        ("4", "9", "380.0"),
    ]
    assert f"session_id = '{SESSION}'" in devices.sql[0]
    # without a runner the load lists the hub device only
    del ctx.runners["runner-1"]
    ticks[0] = 999.98
    answer = call(ctx, "POST", "/latency/terminal", body | {"limit_ms": 300})[1]
    assert answer["summary"]["passed"] is False
    assert list(answer["load"]) == ["hub"]
    devices.vm = lambda name, argv: failed()  # no VM has the session
    assert call(ctx, "POST", "/latency/terminal", body)[0] == 404
    for bad in (
        body | {"session": "x"},
        body | {"session": 1},
        body | {"renders": [[1]]},
        body | {"renders": [[-1, 5.0]]},
        body | {"renders": [[1.5, 5.0]]},
        body | {"renders": "x"},
        body | {"opened_ms": "now"},
    ):
        assert call(ctx, "POST", "/latency/terminal", bad)[0] == 400, bad

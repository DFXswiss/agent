"""Control routes of SCN-06 (pane input log and guest journal of a session, E2E-26) against a scripted node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from orchestrator import guestvm
from orchestrator.routes import guest_api, guest_journal, pane_input
from test_routes_guest import SESSION, VM1, Result, call, failed, ok, scripted, session_handler

UNIT = f"e2e-pane-input-{SESSION}"
TRACE = f"/run/{UNIT}.trace"
FD_FILE = f"/run/{UNIT}.fd"


class Pane:
    """A scripted VM with one tmux server (PID 1309, pane on /dev/pts/1, its master is descriptor 8)."""

    def __init__(self, trace: str = "", active: str = "active\n", traced: list[str] | None = None) -> None:
        self.trace = trace
        self.active = active
        self.traced = traced if traced is not None else ["TracerPid:\t0\n", "TracerPid:\t1342\n"]
        self.calls: list[list[str]] = []

    def __call__(self, _vm: str, argv: list[str]) -> Result | None:
        self.calls.append(argv)
        key = argv[2].split()[0] if argv[:2] == ["sh", "-c"] else " ".join(argv[:2])
        if argv[3:4] == ["display-message"]:
            key = "display"
        if key == "systemctl is-active":
            return {"rc": 0 if self.active.strip() == "active" else 3, "stdout": self.active, "stderr": ""}
        if key == "grep -s":
            return ok(self.traced.pop(0) if len(self.traced) > 1 else self.traced[0])
        if key == "python3 -I":
            assert argv[-2:] == ["1309", "1"]
        answers = {
            "display": "1309 /dev/pts/1\n",
            "python3 -I": "8\n",
            f"cat {FD_FILE}": "8",
            "grep": self.trace,
            "systemctl stop": "",
            "rm -f": "",
            "printf": "",
        }
        if argv[0] == "systemd-run":
            return ok()
        return ok(answers[key]) if key in answers else None


def test_start_attaches_strace_to_the_tmux_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pane_input, "SLEEP", lambda _s: None)
    pane = Pane()
    ctx, _ = scripted(tmp_path, session_handler(pane))
    assert call(ctx, "POST", f"/sessions/{SESSION}/pane-input/start") == (
        200,
        {"vm": VM1, "server_pid": 1309, "tty": "/dev/pts/1", "fd": 8},
    )
    display = next(argv for argv in pane.calls if "display-message" in argv)
    assert display == [
        "tmux",
        "-S",
        guestvm.TMUX_SOCKET,
        "display-message",
        "-p",
        "-t",
        f"=s-{SESSION}:",
        "#{pid} #{pane_tty}",
    ]
    run = next(argv for argv in pane.calls if argv[0] == "systemd-run")
    assert run[:4] == ["systemd-run", f"--unit={UNIT}", "--collect", "-q"]
    assert run[4:] == [
        "strace", "-qq", "-p", "1309", "-e", "trace=write,writev,pwrite64", "-e", "signal=none", "-xx", "-s", "65536",
        "-o", TRACE,
    ]  # fmt: skip
    # an earlier log of the session is stopped and removed first; the descriptor is kept for `stop`
    assert pane.calls.index(["systemctl", "stop", UNIT]) < pane.calls.index(run)
    assert ["rm", "-f", TRACE, FD_FILE] in pane.calls
    assert ["sh", "-c", f"printf %s 8 > {FD_FILE}"] in pane.calls
    # it waits until strace is attached (TracerPid not 0)
    assert pane.calls.count(["grep", "-s", "TracerPid", "/proc/1309/status"]) == 2


def test_start_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pane_input, "SLEEP", lambda _s: None)
    ticks = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(pane_input, "CLOCK", lambda: next(ticks))
    ctx, _ = scripted(tmp_path, session_handler(Pane(traced=["TracerPid:\t0\n"])))
    status, body = call(ctx, "POST", f"/sessions/{SESSION}/pane-input/start")
    assert (status, body["error"]) == (500, "failed")
    assert "strace did not attach" in body["detail"]
    assert call(ctx, "POST", "/sessions/nope/pane-input/start")[0] == 404

    def odd(answer: Result) -> Any:
        def handler(vm: str, argv: list[str]) -> Result | None:
            if argv[3:4] == ["display-message"]:
                return answer
            return Pane()(vm, argv)

        return handler

    for answer in (ok("1309\n"), ok("x /dev/pts/1\n"), ok("1309 /dev/tty1\n")):
        ctx, _ = scripted(tmp_path, session_handler(odd(answer)))
        status, body = call(ctx, "POST", f"/sessions/{SESSION}/pane-input/start")
        assert (status, body["error"]) == (500, "failed"), answer
        assert "unexpected pane" in body["detail"]

    def no_master(vm: str, argv: list[str]) -> Result | None:
        return ok("\n") if argv[:2] == ["python3", "-I"] else Pane()(vm, argv)

    ctx, _ = scripted(tmp_path, session_handler(no_master))
    status, body = call(ctx, "POST", f"/sessions/{SESSION}/pane-input/start")
    assert status == 500
    assert "no pty master" in body["detail"]

    def no_strace(vm: str, argv: list[str]) -> Result | None:
        return failed(127, "strace: not found") if argv[0] == "systemd-run" else Pane()(vm, argv)

    ctx, _ = scripted(tmp_path, session_handler(no_strace))
    status, body = call(ctx, "POST", f"/sessions/{SESSION}/pane-input/start")
    assert status == 500
    assert body["detail"].endswith("exited with 127: strace: not found")


TRACE_TEXT = (
    'writev(8, [{iov_base="\\x68\\x65\\x6c\\x6c\\x6f", iov_len=5}], 1) = 5\n'
    'write(8, "\\x0d", 1) = 1\n'
    'write(8, "\\x1b\\x5b", 2) = -1 EAGAIN (Resource temporarily unavailable)\n'
)


def test_stop_reports_the_bytes_of_the_pane_descriptor(tmp_path: Path) -> None:
    pane = Pane(trace=TRACE_TEXT)
    ctx, _ = scripted(tmp_path, session_handler(pane))
    assert call(ctx, "POST", f"/sessions/{SESSION}/pane-input/stop") == (
        200,
        {"vm": VM1, "fd": 8, "writes": 2, "bytes": 6, "data": "hello\r"},
    )
    grep = next(argv for argv in pane.calls if argv[:2] == ["sh", "-c"] and argv[2].startswith("grep"))
    assert grep[2] == f"grep -E '^(write|writev|pwrite64)\\(8, ' {TRACE} || true"
    assert pane.calls.index(["systemctl", "stop", UNIT]) < pane.calls.index(grep)
    assert pane.calls[-1] == ["rm", "-f", TRACE, FD_FILE]
    # nothing written: an empty log
    ctx, _ = scripted(tmp_path, session_handler(Pane(trace="")))
    assert call(ctx, "POST", f"/sessions/{SESSION}/pane-input/stop")[1] == {
        "vm": VM1,
        "fd": 8,
        "writes": 0,
        "bytes": 0,
        "data": "",
    }


def test_stop_refuses_a_log_that_ended_early(tmp_path: Path) -> None:
    ctx, _ = scripted(tmp_path, session_handler(Pane(active="failed\n")))
    status, body = call(ctx, "POST", f"/sessions/{SESSION}/pane-input/stop")
    assert (status, body["error"]) == (409, "failed")
    assert "not running (failed)" in body["detail"]

    def bad_fd(vm: str, argv: list[str]) -> Result | None:
        return ok("x") if argv[:2] == ["cat", FD_FILE] else Pane()(vm, argv)

    ctx, _ = scripted(tmp_path, session_handler(bad_fd))
    assert call(ctx, "POST", f"/sessions/{SESSION}/pane-input/stop")[0] == 500


def test_parse_trace() -> None:
    assert pane_input.parse_trace(TRACE_TEXT, 8) == (2, b"hello\r")
    assert pane_input.parse_trace('write(9, "\\x41", 1) = 1\nnoise\n', 8) == (0, b"")
    # a large write: the data is capped, the count is not
    long = 'write(8, "' + "\\x41" * 10 + '", 10) = 10\n'
    writes, data = pane_input.parse_trace(long * 500, 8)
    assert writes == 500
    assert len(data) == 5000
    assert pane_input.summary(VM1, 8, long * 500)["data"] == "A" * pane_input.MAX_DATA


# -- guest journal ---------------------------------------------------------------------------------------------------


RECORDS = [
    {"seq": 4, "ts": "2026-11-22T10:00:00.000Z", "kind": "session.hook", "body": {"event": "PreToolUse"}},
    {"seq": 5, "ts": "2026-11-22T10:00:00.001Z", "kind": "ping.delivered", "body": {"ping_id": "p", "via": "hook"}},
]
PINGS = [{"id": "p", "kind": "whiteboard.entry", "level": "workgroup", "ref": "e", "created_at": "t"}]


def test_guest_journal(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def extra(_vm: str, argv: list[str]) -> Result | None:
        if argv[:2] == ["python3", "-I"]:
            seen.append(argv)
            return ok(json.dumps({"records": RECORDS, "pings": PINGS}))
        return None

    ctx, _ = scripted(tmp_path, session_handler(extra))
    body = {"kinds": ["session.hook", "ping.delivered"]}
    assert call(ctx, "POST", f"/sessions/{SESSION}/guest-journal", body) == (
        200,
        {"vm": VM1, "records": RECORDS, "pings": PINGS},
    )
    assert seen[-1][-3:] == [guest_journal.JOURNAL, SESSION, '["session.hook", "ping.delivered"]']
    assert call(ctx, "POST", "/sessions/nope/guest-journal", body)[0] == 404


def test_guest_journal_refusals(tmp_path: Path) -> None:
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: failed(1, "no such table")))
    bodies: list[Any] = [{"kinds": []}, {"kinds": ["nope"]}, {"kinds": "session.hook"}, [1], {}, None]
    for body in bodies:
        assert call(ctx, "POST", f"/sessions/{SESSION}/guest-journal", body)[0] == 400, body
    status, answer = call(ctx, "POST", f"/sessions/{SESSION}/guest-journal", {"kinds": ["ping.delivered"]})
    assert status == 500
    assert answer["detail"].endswith("no such table")


def test_guest_api_restart(tmp_path: Path) -> None:
    """E2E-47 (KOR-40): the guest's API process of the session's VM is restarted."""
    seen: list[list[str]] = []

    def extra(_vm: str, argv: list[str]) -> Result | None:
        if argv[:2] == ["systemctl", "restart"]:
            seen.append(argv)
            return ok()
        return None

    ctx, _ = scripted(tmp_path, session_handler(extra))
    assert call(ctx, "POST", f"/sessions/{SESSION}/guest-api-restart", {}) == (200, {"vm": VM1})
    assert seen == [["systemctl", "restart", guest_api.UNIT]]
    assert call(ctx, "POST", "/sessions/nope/guest-api-restart", {})[0] == 404


def test_guest_api_restart_failure(tmp_path: Path) -> None:
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: failed(1, "unit unknown")))
    status, answer = call(ctx, "POST", f"/sessions/{SESSION}/guest-api-restart", {})
    assert status == 500
    assert answer["detail"].endswith("unit unknown")

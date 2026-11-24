"""Control routes of SCN-02 (VM test hooks, hook fault switch, idle interval, runner store, plaintext scan) against a
scripted node."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from orchestrator import guestvm
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import RouteError, discover, dispatch
from orchestrator.node import Node
from orchestrator.relay import BROWSER_TAP
from orchestrator.routes import hooks, sessions_vm
from orchestrator.services import Cluster, Runner
from support import CORE

SESSION = "01a20172-b86a-789c-89ab-7aa8af7fa995"
OTHER = "01a20172-b86a-789c-89ab-7aa8af7fa996"
PREFIX = "wp-t-e2e-"
VM1 = f"{PREFIX}r1-arp-0000aaaa"
VM2 = f"{PREFIX}r1-arp-0000bbbb"
Result = dict[str, Any]
Responder = Callable[[list[str]], Result]


def ok(stdout: str = "") -> Result:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


def failed(rc: int = 1, stderr: str = "boom") -> Result:
    return {"rc": rc, "stdout": "", "stderr": stderr}


def listing(*vms: tuple[str, str]) -> str:
    return "".join(json.dumps({"name": name, "status": status, "memory": 0}) + "\n" for name, status in vms)


class ScriptNode(Node):
    """`run` answers through `respond` (argv after `limactl shell --workdir / <vm> sudo -n` gets the VM first)."""

    def __init__(self, respond: Responder) -> None:
        self.name = "script"
        self.respond = respond
        self.runs: list[list[str]] = []
        self.logs: dict[str, str] = {}
        self.alive_names: set[str] = set()

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "run":
            self.runs.append(args["argv"])
            return self.respond(args["argv"])
        if op == "log_tail":
            return self.logs.get(args["name"], "")
        if op == "alive":
            return args["name"] in self.alive_names
        if op == "stop":
            self.alive_names.discard(args["name"])
            return 0
        if op == "spawn":
            self.alive_names.add(args["name"])
            return 1
        if op == "wait_line":
            return "runner.run enrolled=yes"
        raise AssertionError(op)


def guest(handler: Callable[[str, list[str]], Result], vms: str = "") -> Responder:
    """`limactl list` answers `vms`; `limactl shell … <vm> sudo -n <argv>` goes to `handler(vm, argv)`."""

    def respond(argv: list[str]) -> Result:
        if argv[:2] == ["limactl", "list"]:
            default = listing((VM1, "Running"), (VM2, "Running"), (f"{PREFIX}golden", "Stopped"), ("x", "Running"))
            return ok(vms or default)
        assert argv[:4] == ["limactl", "shell", "--workdir", "/"]
        assert argv[5:7] == ["sudo", "-n"]
        return handler(argv[4], argv[7:])

    return respond


def tmux_in(vm_with_session: str) -> Callable[[str, list[str]], Result | None]:
    def has(vm: str, argv: list[str]) -> Result | None:
        if argv[:3] == ["tmux", "-S", guestvm.TMUX_SOCKET] and argv[3] == "has-session":
            return ok() if vm == vm_with_session else failed(1, "can't find session")
        return None

    return has


def make_ctx(tmp_path: Path, node: Node) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    return Context(config, node, node, "/agent/python", scratch=str(tmp_path))


ROUTES = discover()


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, method, path, body)


def scripted(tmp_path: Path, handler: Callable[[str, list[str]], Result], vms: str = "") -> tuple[Context, ScriptNode]:
    node = ScriptNode(guest(handler, vms))
    return make_ctx(tmp_path, node), node


# -- guestvm -----------------------------------------------------------------------------------------------------


def test_guestvm_names_and_lookup(tmp_path: Path) -> None:
    assert guestvm.user_name(SESSION) == "s-01a20172b86a89c9ab7aa8af7fa995"
    with pytest.raises(guestvm.GuestVmError, match="not a session ID"):
        guestvm.user_name("../x")
    ctx, node = scripted(tmp_path, lambda vm, argv: tmux_in(VM2)(vm, argv) or ok())
    assert guestvm.running_vms(ctx) == [VM1, VM2]
    assert guestvm.session_vm(ctx, SESSION) == VM2
    assert node.runs[-1][-1] == f"=s-{SESSION}"
    with pytest.raises(guestvm.GuestVmError, match="not a VM of this run"):
        guestvm.shell(ctx, "golden", ["true"])
    ctx, _ = scripted(tmp_path, lambda vm, argv: failed(1, "can't find session"))
    with pytest.raises(guestvm.GuestVmError, match=rf"{VM1}: 1 can't find session; {VM2}"):
        guestvm.session_vm(ctx, SESSION)
    with pytest.raises(guestvm.GuestVmError, match=r"exited with 1: can.t find session"):
        guestvm.checked(ctx, VM1, ["cat", "/x"])
    ctx, _ = scripted(tmp_path, lambda vm, argv: ok(), vms=listing(("other", "Running")))
    with pytest.raises(guestvm.GuestVmError, match="no running VM"):
        guestvm.session_vm(ctx, SESSION)


# -- sessions_vm ---------------------------------------------------------------------------------------------------


def session_handler(extra: Callable[[str, list[str]], Result | None]) -> Callable[[str, list[str]], Result]:
    def handler(vm: str, argv: list[str]) -> Result:
        found = tmux_in(VM1)(vm, argv)
        if found is not None:
            return found
        answer = extra(vm, argv)
        assert answer is not None, argv
        return answer

    return handler


def test_session_vm_and_cast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def extra(_vm: str, argv: list[str]) -> Result | None:
        if argv[:2] == ["id", "-u"]:
            return ok("20001\n")
        if argv[0] == "systemctl":
            return ok("ActiveState=active\nMainPID=4242\nnoise\n")
        if argv[:2] == ["cat", "/proc/4242/cmdline"]:
            return ok("/opt/claude/claude\0--resume\0abc\0")
        if argv[0] == "cat":
            return ok('{"version": 2}\n[0.1, "o", "x"]\n')
        if argv[0] == "date":
            return ok("1000.250000000\n")
        return None

    ctx, _ = scripted(tmp_path, session_handler(extra))
    assert call(ctx, "GET", f"/sessions/{SESSION}/vm") == (
        200,
        {
            "vm": VM1,
            "user": guestvm.user_name(SESSION),
            "uid": 20001,
            "unit": "active",
            "main_pid": 4242,
            "command": ["/opt/claude/claude", "--resume", "abc"],
        },
    )
    assert call(ctx, "GET", f"/sessions/{SESSION}/cast")[1] == {"vm": VM1, "cast": '{"version": 2}\n[0.1, "o", "x"]\n'}
    assert call(ctx, "GET", "/sessions/nope/vm")[0] == 404
    ticks = iter([1000.0, 1000.4, 1000.4, 1000.5, 1000.5, 1001.0, 1001.0, 1001.2, 1001.2, 1001.4])
    monkeypatch.setattr(sessions_vm, "CLOCK", lambda: next(ticks))
    # the shortest round trip (0.1 s, ending at 1000.5) gives the offset: 1000.25 - 1000.5
    assert call(ctx, "GET", f"/sessions/{SESSION}/clock") == (200, {"vm": VM1, "offset_ms": -250.0, "rtt_ms": 100.0})
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: ok("") if argv[0] == "systemctl" else failed()))
    assert call(ctx, "GET", f"/sessions/{SESSION}/vm")[0] == 500
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: ok("7\n") if argv[0] == "id" else ok("")))
    assert call(ctx, "GET", f"/sessions/{SESSION}/vm")[1]["main_pid"] == 0

    def gone(_vm: str, argv: list[str]) -> Result:
        if argv[0] == "systemctl":
            return ok("MainPID=99\n")
        return failed() if argv[0] == "cat" else ok("7\n")

    ctx, _ = scripted(tmp_path, session_handler(gone))
    assert call(ctx, "GET", f"/sessions/{SESSION}/vm")[1]["command"] == []


def test_type_into_pane(tmp_path: Path) -> None:
    ctx, node = scripted(tmp_path, session_handler(lambda vm, argv: ok()))
    assert call(ctx, "POST", f"/sessions/{SESSION}/type", {"text": ""})[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/type", {"text": "x", "enter": "yes"})[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/type", [1])[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/type", {"text": "hello world"}) == (200, {"vm": VM1})
    sent = [argv[7:] for argv in node.runs if "send-keys" in argv]
    target = ["tmux", "-S", guestvm.TMUX_SOCKET, "send-keys", "-t", f"=s-{SESSION}:"]
    assert sent == [[*target, "-l", "--", "hello world"], [*target, "Enter"]]
    node.runs.clear()
    assert call(ctx, "POST", f"/sessions/{SESSION}/type", {"text": "x", "enter": False})[0] == 200
    assert len([argv for argv in node.runs if "send-keys" in argv]) == 1


def test_os_read(tmp_path: Path) -> None:
    def extra(_vm: str, argv: list[str]) -> Result | None:
        if argv[0] == "systemctl":
            return ok("4242\n")
        if argv[:2] == ["python3", "-I"]:
            if argv[-2] == "4242":
                return ok(json.dumps({"ok": False, "error": "FileNotFoundError", "data": ""}))
            return failed(1, "Traceback")
        return None

    ctx, node = scripted(tmp_path, session_handler(extra))
    assert call(ctx, "POST", f"/sessions/{SESSION}/os-read", {"path": "/srv/../etc"})[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/os-read", {"path": "relative"})[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/os-read", None)[0] == 400
    assert call(ctx, "POST", f"/sessions/{SESSION}/os-read", {"path": f"/srv/sessions/{OTHER}/work/s.txt"}) == (
        200,
        {
            "vm": VM1,
            "in_sandbox": {"ok": False, "error": "FileNotFoundError", "data": ""},
            "as_user": {"error": "Traceback"},
        },
    )
    probe = next(argv[7:] for argv in node.runs if "-I" in argv and argv[-2] == "4242")
    assert probe[-3:] == [guestvm.user_name(SESSION), "4242", f"/srv/sessions/{OTHER}/work/s.txt"]
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: ok("0\n")))
    assert call(ctx, "POST", f"/sessions/{SESSION}/os-read", {"path": "/x"})[0] == 409


def test_vm_files(tmp_path: Path) -> None:
    facts = {"/etc/x": {"sha256": "ab", "mode": "0o644", "owner": "root:root"}, "/y": None}

    def extra(_vm: str, argv: list[str]) -> Result | None:
        return ok(json.dumps(facts)) if argv[:2] == ["python3", "-I"] else None

    ctx, node = scripted(tmp_path, session_handler(extra))
    assert call(ctx, "POST", "/vms/files", {"paths": []})[0] == 400
    assert call(ctx, "POST", "/vms/files", {"paths": ["/x"], "session": 1})[0] == 400
    assert call(ctx, "POST", "/vms/files", {"paths": ["/a b"]})[0] == 400
    assert call(ctx, "POST", "/vms/files", None)[0] == 400
    assert call(ctx, "POST", "/vms/files", {"paths": ["/etc/x", "/y"]}) == (200, {VM1: facts, VM2: facts})
    assert call(ctx, "POST", "/vms/files", {"paths": ["/etc/x"], "session": SESSION}) == (200, {VM1: facts})
    assert node.runs[-1][-1] == "/etc/x"
    ctx, _ = scripted(tmp_path, session_handler(lambda vm, argv: failed(2, "no python")))
    assert call(ctx, "POST", "/vms/files", {"paths": ["/etc/x"]})[1]["detail"].endswith("exited with 2: no python")


def test_vm_files_skips_a_deleted_vm(tmp_path: Path) -> None:
    vms = {"names": listing((VM1, "Running"), (VM2, "Running"))}

    def respond(argv: list[str]) -> Result:
        if argv[:2] == ["limactl", "list"]:
            return ok(vms["names"])
        if argv[4] == VM2:
            vms["names"] = listing((VM1, "Running"))
            return failed(guestvm.SSH_NOT_READY, "Connection refused")
        return ok("{}")

    ctx = make_ctx(tmp_path, ScriptNode(respond))
    assert call(ctx, "POST", "/vms/files", {"paths": ["/x"]}) == (200, {VM1: {}})


# -- hooks fault ---------------------------------------------------------------------------------------------------


def test_hooks_fault(tmp_path: Path) -> None:
    listened: dict[str, int] = {}

    def handler(vm: str, argv: list[str]) -> Result:
        if argv[:2] == ["sh", "-c"]:
            return ok()
        assert argv[:2] == ["ss", "-Hltn"]
        listened[vm] = listened.get(vm, 0) + 1
        return ok("LISTEN 0 5 127.0.0.1:7700\n" if listened[vm] > 1 else "")

    ctx, node = scripted(tmp_path, handler)
    assert call(ctx, "POST", "/hooks/fault", {"status": 404})[0] == 400
    assert call(ctx, "POST", "/hooks/fault", {"status": True})[0] == 400
    assert call(ctx, "POST", "/hooks/fault", {})[0] == 400
    assert call(ctx, "POST", "/hooks/fault", {"status": 500}) == (200, {"status": 500, "vms": [VM1, VM2]})
    scripts = [argv[-1] for argv in node.runs if argv[7:9] == ["sh", "-c"]]
    assert scripts == [hooks.ON, hooks.ON]
    assert "AGENT_GUEST_FAULT_HOOKS=500" in hooks.ON
    assert call(ctx, "POST", "/hooks/fault", {"status": None})[1]["status"] is None
    assert [argv[-1] for argv in node.runs if argv[7:9] == ["sh", "-c"]][-1] == hooks.OFF
    ctx, _ = scripted(tmp_path, lambda vm, argv: failed())
    status, answer = call(ctx, "POST", "/hooks/fault", {"status": 500})
    assert (status, answer["detail"]) == (500, f"fault switch in {VM1} exited with 1: boom")


def test_hooks_fault_waits_for_booting_and_skips_vanished_vms(tmp_path: Path) -> None:
    tries: dict[str, int] = {}
    vms = {"names": listing((VM1, "Running"), (VM2, "Running"))}

    def respond(argv: list[str]) -> Result:
        if argv[:2] == ["limactl", "list"]:
            return ok(vms["names"])
        vm, command = argv[4], argv[7:]
        if command[:2] == ["ss", "-Hltn"]:
            return ok("LISTEN\n")
        tries[vm] = tries.get(vm, 0) + 1
        if vm == VM2:
            vms["names"] = listing((VM1, "Running"))  # deleted by the runner meanwhile
            return failed(1, "instance does not exist")
        if tries[vm] == 1:
            return failed(guestvm.SSH_NOT_READY, "Connection refused")
        if tries[vm] == 2:  # SSH is up, the boot hardening has not written the TEST_MODE sudo grant again yet
            return failed(1, "sudo: a password is required\n")
        return ok()

    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    assert call(ctx, "POST", "/hooks/fault", {"status": 500}) == (200, {"status": 500, "vms": [VM1]})
    assert tries == {VM1: 3, VM2: 1}
    node.respond = guest(lambda vm, argv: failed(9, "Permission denied"))
    with pytest.raises(RouteError, match="exited with 9: Permission denied"):
        hooks.switch(ctx, VM1, hooks.ON)


def test_hooks_wait_listening_times_out(tmp_path: Path) -> None:
    ctx, _ = scripted(tmp_path, lambda vm, argv: ok(""))
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    with pytest.raises(RouteError, match="does not listen on 7700"):
        hooks.wait_listening(ctx, VM1, sleep=sleep, clock=lambda: now[0])
    assert len(slept) == int(hooks.LISTEN_TIMEOUT_S / hooks.POLL_S)


def test_shell_when_up_gives_up_after_the_timeout(tmp_path: Path) -> None:
    ctx, _ = scripted(tmp_path, lambda vm, argv: failed(guestvm.SSH_NOT_READY, "Connection refused"))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    done = guestvm.shell_when_up(ctx, VM1, ["true"], sleep=sleep, clock=lambda: now[0])
    assert done is not None
    assert done["rc"] == guestvm.SSH_NOT_READY
    assert now[0] >= guestvm.READY_TIMEOUT_S


def test_shell_when_up_waits_for_the_sudo_grant_only_while_it_is_missing(tmp_path: Path) -> None:
    # a VM without the grant after the timeout (e.g. a production golden) gives the failure back
    ctx, _ = scripted(tmp_path, lambda vm, argv: failed(1, "sudo: a password is required\n"))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    done = guestvm.shell_when_up(ctx, VM1, ["true"], sleep=sleep, clock=lambda: now[0])
    assert done is not None
    assert done["rc"] == 1
    assert now[0] >= guestvm.READY_TIMEOUT_S
    # any other failure of the command comes back at once
    ctx, _ = scripted(tmp_path, lambda vm, argv: failed(1, "no such file"))
    now[0] = 0.0
    again = guestvm.shell_when_up(ctx, VM1, ["false"], sleep=sleep, clock=lambda: now[0])
    assert again is not None
    assert again["stderr"] == "no such file"
    assert now[0] == 0.0


# -- idle and runner store ------------------------------------------------------------------------------------------


def runner_ctx(tmp_path: Path, respond: Responder) -> tuple[Context, ScriptNode, Runner]:
    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    runner = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "~/home/runner-1"})
    ctx.runners["runner-1"] = runner
    node.alive_names.add("runner-1")
    return ctx, node, runner


def test_idle(tmp_path: Path) -> None:
    ctx, node, runner = runner_ctx(tmp_path, lambda argv: ok())
    for bad in ({"seconds": 59}, {"seconds": True}, {"seconds": "60"}, {"seconds": 604_801}, [60]):
        assert call(ctx, "POST", "/runners/idle", bad)[0] == 400
    assert call(ctx, "POST", "/runners/idle", {"seconds": 60}) == (
        200,
        {"runner-1": {"seconds": 60, "restarted": True}},
    )
    assert runner.env["AGENT_RUNNER_SESSION_IDLE_SECONDS"] == "60"
    assert call(ctx, "POST", "/runners/idle", {"seconds": 60})[1]["runner-1"]["restarted"] is False
    node.alive_names.clear()
    assert call(ctx, "POST", "/runners/idle", {"seconds": None})[1]["runner-1"] == {"seconds": None, "restarted": False}
    assert "AGENT_RUNNER_SESSION_IDLE_SECONDS" not in runner.env


def test_runner_store_query(tmp_path: Path) -> None:
    answers = [ok('[{"n": 3}]\n'), failed(2, "psycopg.OperationalError")]
    ctx, node, _ = runner_ctx(tmp_path, lambda argv: answers.pop(0))
    assert call(ctx, "POST", "/runners/runner-9/store-query", {"sql": "SELECT 1"})[0] == 404
    assert call(ctx, "POST", "/runners/runner-1/store-query", {"sql": "SELECT 1; DELETE FROM x"})[0] == 400
    assert call(ctx, "POST", "/runners/runner-1/store-query", {"sql": " "})[0] == 400
    assert call(ctx, "POST", "/runners/runner-1/store-query", {"sql": "SELECT 3 AS n;"}) == (200, {"rows": [{"n": 3}]})
    assert node.runs[-1][3:] == ["~/home/runner-1", "SELECT 3 AS n"]
    status, answer = call(ctx, "POST", "/runners/runner-1/store-query", {"sql": "SELECT 1"})
    assert (status, answer["error"]) == (500, "failed")


# -- plaintext scan --------------------------------------------------------------------------------------------------


def test_plaintext_route(tmp_path: Path) -> None:
    def respond(argv: list[str]) -> Result:
        assert argv[0] == "/pg/pg_dump"
        Path(argv[argv.index("-f") + 1]).write_text("COPY hub.task (title) FROM stdin;\nkz-canary-dump-123\n")
        return ok()

    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    assert call(ctx, "POST", "/checks/plaintext", {"canaries": ["kz-canary-dump-123"]})[0] == 409
    assert call(ctx, "POST", "/checks/plaintext", {"canaries": ["short"]})[0] == 400
    assert call(ctx, "POST", "/checks/plaintext", {"canaries": []})[0] == 400
    assert call(ctx, "POST", "/checks/plaintext", None)[0] == 400
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {})
    node.logs = {"hub": "GET /v1/stream 200\n", "postgres": "ready\n"}
    tap = tmp_path / BROWSER_TAP
    tap.mkdir()
    stream = b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n10\r\nevent: hello\ndata\r\n"
    (tap / "00001.down").write_bytes(stream)
    (tap / "00002.down").write_bytes(b"HTTP/1.1 200 OK\r\ncontent-length: 18\r\n\r\nkz-canary-sent-456")
    status, report = call(ctx, "POST", "/checks/plaintext", {"canaries": ["kz-canary-dump-123", "kz-canary-sent-456"]})
    assert status == 200
    assert report["findings"] == [
        {"source": "hub-db-dump", "canary": "kz-canary-dump-123", "encoding": "raw"},
        {"source": "browser-link/00002.down", "canary": "kz-canary-sent-456", "encoding": "raw"},
    ]
    assert report["sse_frames"] == 1
    links = {"browser-link/00001.down", "browser-link/00002.down"}
    assert set(report["sources"]) == {"hub-db-dump", "hub-log", "postgres-log", *links}
    assert not (tmp_path / "plaintext-scan.sql").exists()

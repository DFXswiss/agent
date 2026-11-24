"""U-GST-sandbox: evaluation and probe helpers of the sandbox self-test (05-runner.md section 4.5).

The probe inside a real unit and the session set-up run on Linux (tests/platform/guest_linux/test_selftest_linux.py).
"""

from __future__ import annotations

import ctypes
import errno
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_guest.sandbox import probe, selftest, unit


def lines_for(outcomes: dict[str, str]) -> list[str]:
    return [json.dumps({"check": name, "outcome": outcome}) for name, outcome in outcomes.items()]


def passing() -> dict[str, str]:
    outcomes = {name: expected[0] for name, expected in probe.EXPECTED.items()}
    outcomes["ld_so_system_binary"] = "exit:127"
    return outcomes


# --- selftest ---------------------------------------------------------------------------------------------------


def test_free_uid_takes_the_lowest_free_id() -> None:
    assert selftest.free_uid(set()) == 20000
    assert selftest.free_uid({20000, 20001, 30000}) == 20002


def test_free_uid_fails_when_the_range_is_full() -> None:
    with pytest.raises(selftest.SelftestError, match="no free UID"):
        selftest.free_uid(set(selftest.UID_RANGE))


def test_evaluate_accepts_the_expected_outcomes() -> None:
    checks = selftest.evaluate(lines_for(passing()))
    assert [check["check"] for check in checks] == list(probe.EXPECTED)
    assert all(check["ok"] for check in checks)


def test_evaluate_marks_deviations_missing_and_garbled_lines() -> None:
    outcomes = passing()
    outcomes["exec_sh"] = "ok"
    del outcomes["dns"]
    lines = [
        "Traceback (most recent call last):",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"check": "tcp_platform", "outcome": 1}),
        json.dumps({"check": "unshare_user_namespace", "outcome": "ok"}),
        *lines_for(outcomes),
    ]
    failed = {check["check"]: check["outcome"] for check in selftest.evaluate(lines) if not check["ok"]}
    # the first line of a check counts; a later "expected" line cannot repair it
    assert failed == {"exec_sh": "ok", "dns": "missing", "unshare_user_namespace": "ok"}


def test_default_probe_path_is_next_to_the_launcher() -> None:
    assert selftest.PROBE == "/opt/agent-guest/agent_guest/sandbox/probe.py"
    assert Path(probe.__file__).with_name("landlock.py").is_file()


def write_build(tmp_path: Path, paths: list[str]) -> list[str]:
    (tmp_path / "exec-paths.json").write_text(json.dumps(paths))
    (tmp_path / "build.json").write_text(json.dumps({"exec_paths_sha256": unit.exec_paths_sha256(paths)}))
    return ["--exec-paths-file", str(tmp_path / "exec-paths.json"), "--build-file", str(tmp_path / "build.json")]


@pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
def test_main_prints_the_result_and_sets_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: bool, code: int
) -> None:
    monkeypatch.setattr(unit, "interpreter_paths", lambda python: (python + ".12", "/usr/lib/python3.12/lib-dynload"))
    seen: list[selftest.SelftestConfig] = []

    def run(config: selftest.SelftestConfig) -> dict[str, Any]:
        seen.append(config)
        return {"ok": ok, "checks": []}

    out = io.StringIO()
    argv = [*write_build(tmp_path, ["/usr/lib/libc.so.6"]), "--python", "/usr/bin/python3"]
    assert selftest.main(argv, run=run, out=out) == code
    assert json.loads(out.getvalue()) == {"ok": ok, "checks": []}
    assert seen == [
        selftest.SelftestConfig(
            python="/usr/bin/python3.12",
            lib_dynload="/usr/lib/python3.12/lib-dynload",
            exec_paths=("/usr/lib/libc.so.6",),
        )
    ]


def test_main_writes_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(unit, "interpreter_paths", lambda python: (python, "/d"))
    assert selftest.main(write_build(tmp_path, []), run=lambda config: {"ok": True}) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


# --- probe ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "outcome", "accepted"),
    [
        (("ok",), "ok", True),
        (("ok",), "errno:EACCES", False),
        ((probe.NONZERO_EXIT,), "exit:127", True),
        ((probe.NONZERO_EXIT,), "exit:0", False),
        ((probe.NONZERO_EXIT,), "ok", False),
        (("errno:EACCES",), "exit:1", False),
    ],
)
def test_accepts(expected: tuple[str, ...], outcome: str, accepted: bool) -> None:
    assert probe.accepts(expected, outcome) is accepted


def raiser(exc: BaseException) -> Any:
    def fn() -> str:
        raise exc

    return fn


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (socket.gaierror(socket.EAI_NONAME, "no name"), "gaierror"),
        (TimeoutError(), "timeout"),
        (subprocess.TimeoutExpired(["x"], 1), "timeout"),
        (PermissionError(errno.EACCES, "denied"), "errno:EACCES"),
        (OSError("no errno"), "errno:None"),
        (OSError(99999, "unknown"), "errno:99999"),
        (ValueError("odd"), "error:ValueError"),
    ],
)
def test_observe_classifies_failures(exc: BaseException, outcome: str) -> None:
    assert probe.observe(raiser(exc)) == outcome


def test_observe_returns_the_result() -> None:
    assert probe.observe(lambda: "own_only") == "own_only"


def context(tmp_path: Path, **changes: Any) -> probe.Context:
    values: dict[str, Any] = {"own": "a", "other": "b", "claude": "/c", "interpreter": "/ld"}
    values.update(changes)
    return probe.Context(**values)


def test_context_work_directory() -> None:
    assert probe.Context("a", "b", "/c", "/ld").work == "/srv/sessions/a/work"


def test_sessions_visible(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert probe._sessions_visible(context(tmp_path, sessions_root=str(tmp_path))) == "own_only"
    (tmp_path / "b").mkdir()
    assert probe._sessions_visible(context(tmp_path, sessions_root=str(tmp_path))) == "visible:2"


def test_processes_ignores_ended_processes(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "self").mkdir()
    (tmp_path / "2").symlink_to(tmp_path / "gone")
    assert probe._processes(context(tmp_path, proc=str(tmp_path))) == "own_only"


def test_unshare_reports_the_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    class Libc:
        def unshare(self, flags: int) -> int:
            assert flags == probe.CLONE_NEWUSER
            ctypes.set_errno(errno.EPERM)
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno: Libc())
    assert probe.observe(probe._unshare) == "errno:EPERM"


def test_io_uring_reports_the_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    class Libc:
        def syscall(self, number: int, entries: int, params: object) -> int:
            assert (number, entries) == (probe.SYS_IO_URING_SETUP, 1)
            ctypes.set_errno(errno.EPERM)
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno: Libc())
    assert probe.observe(probe._io_uring) == "errno:EPERM"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract unix names are Linux only")
def test_unix_listen_outside_the_unit_listens() -> None:
    """Outside the unit the check reaches ``listen`` on an automatically named socket and succeeds; inside, the unit
    refuses it (tests/platform/guest_linux)."""
    assert probe.observe(probe._unix_listen) == "ok"


def test_stat_and_sendto_helpers(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir(mode=0o750)
    os.chmod(tmp_path / "d", 0o750)
    assert probe._stat(str(tmp_path / "d")) == "mode:750"
    assert probe._sendto("127.0.0.1", 9) == "ok"


def test_file_helpers(tmp_path: Path) -> None:
    target = tmp_path / "f"
    assert probe._write(str(target)) == "ok"
    assert not target.exists()
    target.write_text("x")
    assert probe._read(str(target)) == "ok"
    copy = probe._copy_binary(str(target), str(tmp_path / "copy"))
    assert os.access(copy, os.X_OK)


def test_second_layer_needs_an_elf_interpreter(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(OSError, match="has no ELF interpreter") as info:
        probe.restrict_like_a_session(str(script), [])
    assert info.value.errno == errno.ENOEXEC


def test_landlock_module_must_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, path: None)
    with pytest.raises(ImportError, match=r"landlock\.py not found"):
        probe._landlock()


@pytest.mark.parametrize("argv", [["only-one"], ["a", "b", "/c"], ["a", "b", "--", "/c"]])
def test_main_usage(argv: list[str]) -> None:
    out = io.StringIO()
    assert probe.main(argv, out=out) == 2
    assert out.getvalue().startswith("usage:")


def test_main_prints_one_line_per_check(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    layers: list[tuple[str, list[str]]] = []

    def restrict(claude: str, session_args: list[str]) -> str:
        layers.append((claude, list(session_args)))
        return "/lib/ld.so"

    monkeypatch.setattr(probe, "restrict_like_a_session", restrict)
    seen: list[probe.Context] = []

    def run_checks(ctx: probe.Context) -> list[dict[str, str]]:
        seen.append(ctx)
        return [{"check": "exec_sh", "outcome": "errno:EACCES"}]

    monkeypatch.setattr(probe, "run_checks", run_checks)
    argv = ["probe.py", "own", "other", "/opt/claude/claude", "--", "--session-dir", "/srv/sessions/own"]
    monkeypatch.setattr("sys.argv", argv)
    assert probe.main() == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{"check": "layer2", "outcome": "ok"}, {"check": "exec_sh", "outcome": "errno:EACCES"}]
    assert seen == [probe.Context(own="own", other="other", claude="/opt/claude/claude", interpreter="/lib/ld.so")]
    assert layers == [("/opt/claude/claude", ["--session-dir", "/srv/sessions/own"])]


def test_main_uses_a_placeholder_interpreter_when_layer2_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def restrict(claude: str, session_args: list[str]) -> str:
        raise OSError(errno.ENOENT, "missing")

    seen: list[probe.Context] = []

    def run_checks(ctx: probe.Context) -> list[dict[str, str]]:
        seen.append(ctx)
        return []

    monkeypatch.setattr(probe, "restrict_like_a_session", restrict)
    monkeypatch.setattr(probe, "run_checks", run_checks)
    out = io.StringIO()
    assert probe.main(["own", "other", "/c", "--"], out=out) == 0
    assert json.loads(out.getvalue()) == {"check": "layer2", "outcome": "errno:ENOENT"}
    assert seen[0].interpreter == "/nonexistent"


def test_every_check_has_an_expectation() -> None:
    assert set(probe.CHECKS) | {"layer2"} == set(probe.EXPECTED)
    spec_checks = {"exec_sh", "exec_git", "exec_python3", "ld_so_system_binary", "write_etc", "stat_other_session"}
    spec_checks |= {"tcp_external", "tcp_other_local_port", "dns", "tcp_platform", "unshare_user_namespace"}
    assert spec_checks <= set(probe.CHECKS)  # 05 section 4.5


def test_probe_is_standard_library_only() -> None:
    source = Path(probe.__file__).read_text()
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert "agent_guest" not in imports


UNIX_TABLE = """Num       RefCount Protocol Flags    Type St Inode Path
ffff0001: 00000002 00000000 00010000 0001 01  6046 @ISCSIADM_ABSTRACT_NAMESPACE
ffff0002: 00000003 00000000 00000000 0001 03  7455 @7f9e/bus/systemd-logind/system
ffff0003: 00000002 00000000 00010000 0001 01  8000 /run/systemd/resolve/io.systemd.Resolve
ffff0004: 00000002 00000000 00000000 0002 07  8001 /run/systemd/journal/socket
ffff0005: 00000002 00000000 00000000 0002 07  8002
ffff0006: 00000002 00000000 00000000 0001 03  8003 /run/client
ffff0007: 00000002 00000000 00010000 0005 01  8004 /run/seqpacket
short line
"""


def test_listening_unix_sockets_are_parsed_from_the_kernel_table() -> None:
    assert probe.listening_unix(UNIX_TABLE) == [
        "/run/seqpacket",
        "/run/systemd/journal/socket",
        "/run/systemd/resolve/io.systemd.Resolve",
        "@ISCSIADM_ABSTRACT_NAMESPACE",
    ]


@pytest.fixture
def short_dir() -> Any:
    """A directory with a short path (AF_UNIX addresses are limited to about 100 bytes)."""
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


def test_connect_unix_tries_every_socket_type(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path = short_dir
    path = str(tmp_path / "s")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        assert probe.connect_unix(path) is True  # stream refused, datagram accepted
    assert probe.connect_unix(str(tmp_path / "missing")) is False
    targets: list[str] = []

    class Recording:
        def __init__(self, family: int, kind: int) -> None:
            pass

        def __enter__(self) -> Recording:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def settimeout(self, value: float) -> None:
            pass

        def connect(self, target: str) -> None:
            targets.append(target)
            raise ConnectionRefusedError

    monkeypatch.setattr(socket, "socket", Recording)
    assert probe.connect_unix("@abstract") is False
    assert targets == ["\0abstract"] * 3


def test_unix_sockets_reports_what_a_session_reaches(short_dir: Path) -> None:
    tmp_path = short_dir
    (tmp_path / "self" / "net").mkdir(parents=True)
    table = tmp_path / "self" / "net" / "unix"
    table.write_text(UNIX_TABLE.splitlines()[0] + "\n")
    assert probe._unix_sockets(context(tmp_path, proc=str(tmp_path))) == "none"
    path = tmp_path / "srv"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        table.write_text(UNIX_TABLE.splitlines()[0] + f"\nffff: 00000002 00000000 00010000 0001 01 1 {path}\n")
        assert probe._unix_sockets(context(tmp_path, proc=str(tmp_path))) == f"reachable:{path}"


def test_memfd_exec_runs_a_copy_from_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delattr(os, "memfd_create", raising=False)
    assert probe.observe(probe._memfd_exec) == "errno:ENOSYS"
    created: list[tuple[str, int]] = []
    holder = tmp_path / "memfd"

    def create(name: str, flags: int) -> int:
        created.append((name, flags))
        return os.open(holder, os.O_RDWR | os.O_CREAT, 0o700)

    runs: list[tuple[list[str], tuple[int, ...]]] = []

    def run(argv: list[str], *, pass_fds: tuple[int, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        runs.append((argv, pass_fds))
        return subprocess.CompletedProcess(argv, len(runs) - 1)

    monkeypatch.setattr(os, "memfd_create", create, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    assert probe._memfd_exec() == "ok"
    assert probe._memfd_exec() == "exit:1"
    assert created == [("agent-selftest", 0)] * 2
    assert runs[0][0] == [f"/proc/self/fd/{runs[0][1][0]}"]
    assert holder.read_bytes().startswith(Path("/usr/bin/true").read_bytes()[:4])

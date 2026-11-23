"""Unit tests of the extended OS probe inside a session unit (``agent_doubles.atk.unit_probe``, ATS-03).

The real probe runs only in a Linux session unit (``scripts/run-atk.sh``, ``test_atk_os_extended.py``); here every
helper runs with fakes so the module is measured completely.
"""

from __future__ import annotations

import ctypes
import errno
import io
import json
import mmap
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_doubles.atk import unit_probe
from agent_guest.sandbox import probe as guest_probe

GUEST_PROBE_FILE = Path(guest_probe.__file__)


def context(**changes: Any) -> unit_probe.Context:
    values: dict[str, Any] = {"own": "a", "other": "b", "interpreter": "/ld", "python": "/py", "work": "/w"}
    values.update(changes)
    return unit_probe.Context(**values)


@pytest.mark.parametrize(
    ("stdout", "code", "outcome"),
    [
        ("", 127, "exit:127"),
        ("\n", 1, "exit:1"),
        (json.dumps({"exec_sh": "errno:13", "connect_external": "errno:None"}), 0, "contained"),
        (
            json.dumps({"exec_sh": "ok", "write_etc": "error:CalledProcessError", "x": "errno:1"}),
            0,
            "escaped:exec_sh,write_etc",
        ),
    ],
)
def test_contained_outcome(stdout: str, code: int, outcome: str) -> None:
    assert unit_probe.contained_outcome(stdout, code) == outcome


def test_the_loader_starts_the_contained_script(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"exec_sh": "errno:13"}) + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert unit_probe._ld_so_python(context()) == "contained"
    assert seen == [["/ld", "/py", "-I", "-c", unit_probe.CONTAINED, "a", "b"]]


def test_the_contained_script_reports_every_attempt(tmp_path: Path) -> None:
    """The script itself, run by this test's Python outside any sandbox: it reports what it could do."""
    result = subprocess.run(
        ["python3", "-I", "-c", unit_probe.CONTAINED, "own", "other"], capture_output=True, text=True, check=False
    )
    report = json.loads(result.stdout)
    assert set(report) == {
        "exec_sh", "exec_python", "write_etc", "write_var_tmp", "read_other_session", "connect_external",
    }  # fmt: skip
    assert report["exec_sh"] == "ok"
    assert report["read_other_session"].startswith("errno:")


def test_mmap_exec_maps_and_removes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "m"
    mapped: list[tuple[int, int]] = []

    class Mapping:
        def __init__(self, fileno: int, length: int, *, prot: int) -> None:
            mapped.append((length, prot))

        def close(self) -> None:
            pass

    monkeypatch.setattr(mmap, "mmap", Mapping)
    assert unit_probe._mmap_exec(str(target)) == "ok"
    assert mapped == [(mmap.PAGESIZE, mmap.PROT_READ | mmap.PROT_EXEC)]
    assert not target.exists()

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(errno.EPERM, "noexec")

    monkeypatch.setattr(mmap, "mmap", refuse)
    assert guest_probe.observe(lambda: unit_probe._mmap_exec(str(target))) == "errno:EPERM"
    assert not target.exists()


def test_read_helper(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    assert unit_probe._read(str(tmp_path / "f")) == "ok"


def test_ptrace_reports_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    left: list[int] = []
    assert unit_probe._ptrace(fork=lambda: 0, child=lambda: 101, leave=left.append) == "child"
    assert left == [101]

    def waited(status: int) -> Any:
        return lambda pid, options: (pid, status)

    assert unit_probe._ptrace(fork=lambda: 7, wait=waited(signal.SIGSYS)) == "signal:SIGSYS"
    assert unit_probe._ptrace(fork=lambda: 7, wait=waited(0)) == "ok"
    assert unit_probe._ptrace(fork=lambda: 7, wait=waited((100 + errno.EPERM) << 8)) == "errno:EPERM"
    assert unit_probe._errno_name(99999) == "99999"


def test_traceme_reports_the_error_number(monkeypatch: pytest.MonkeyPatch) -> None:
    class Libc:
        def __init__(self, result: int) -> None:
            self.result = result

        def ptrace(self, *args: Any) -> int:
            assert args == (unit_probe.PTRACE_TRACEME, 0, None, None)
            return self.result

    results = iter([Libc(0), Libc(-1)])
    monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno: next(results))
    monkeypatch.setattr(ctypes, "get_errno", lambda: errno.EPERM)
    assert unit_probe._traceme() == 0
    assert unit_probe._traceme() == 100 + errno.EPERM


def test_unix_sockets_of_the_root_list() -> None:
    assert unit_probe._unix_sockets(context(), lambda address: True) == "error:no_sockets_listed"
    listed = context(sockets=("/run/a", "@b"))
    assert unit_probe._unix_sockets(listed, lambda address: False) == "none"
    assert unit_probe._unix_sockets(listed, lambda address: address == "@b") == "reachable:@b"


def test_extra_checks_call_their_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def note(kind: str) -> Any:
        def call(path: str = "") -> str:
            calls.append((kind, path))
            return "ok"

        return call

    monkeypatch.setattr(unit_probe, "_mmap_exec", note("mmap"))
    monkeypatch.setattr(unit_probe, "_read", note("read"))
    monkeypatch.setattr(unit_probe, "_ptrace", note("ptrace"))
    for name in ("mmap_exec_workspace", "mmap_exec_tmp", "proc_1_environ", "ptrace"):
        assert unit_probe.EXTRA[name](context()) == "ok"
    assert calls == [("mmap", "/w/atk-mmap"), ("mmap", "/tmp/atk-mmap"), ("read", "/proc/1/environ"), ("ptrace", "")]
    assert set(unit_probe.EXPECTED) == {*unit_probe.EXTRA, "unix_sockets_root_list"}


def test_load_guest_and_sockets(tmp_path: Path) -> None:
    loaded = unit_probe._load_guest(str(GUEST_PROBE_FILE))
    assert loaded.EXPECTED == guest_probe.EXPECTED
    with pytest.raises(ImportError):
        unit_probe._load_guest(str(tmp_path / "probe.txt"))
    assert unit_probe._sockets(str(tmp_path / "missing.json")) == []
    (tmp_path / "bad.json").write_text("{")
    assert unit_probe._sockets(str(tmp_path / "bad.json")) == []
    (tmp_path / "good.json").write_text(json.dumps(["@x"]))
    assert unit_probe._sockets(str(tmp_path / "good.json")) == ["@x"]


def fake_guest(restrict: Any) -> Any:
    return SimpleNamespace(
        restrict_like_a_session=restrict,
        observe=guest_probe.observe,
        Context=guest_probe.Context,
        run_checks=lambda ctx: [{"check": "guest", "outcome": ctx.interpreter}],
        connect_unix=lambda address: False,
    )


def test_main_prints_the_guest_checks_and_the_extra_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sockets = tmp_path / "sockets.json"
    sockets.write_text(json.dumps(["/run/x"]))
    monkeypatch.setattr(unit_probe, "SOCKETS", str(sockets))
    seen: list[unit_probe.Context] = []

    def extra(ctx: unit_probe.Context) -> str:
        seen.append(ctx)
        return "ok"

    monkeypatch.setattr(unit_probe, "EXTRA", {"extra": extra})
    out = io.StringIO()
    restricted: list[tuple[str, list[str]]] = []

    def restrict(claude: str, args: list[str]) -> str:
        restricted.append((claude, args))
        return "/lib/ld.so"

    assert unit_probe.main(["own", "other", "/c", "--", "--x"], out=out, guest=fake_guest(restrict)) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines == [
        {"check": "layer2", "outcome": "ok"},
        {"check": "guest", "outcome": "/lib/ld.so"},
        {"check": "extra", "outcome": "ok"},
        {"check": "unix_sockets_root_list", "outcome": "none"},
    ]
    assert restricted == [("/c", ["--x"])]
    assert seen[0].sockets == ["/run/x"]
    assert seen[0].work == "/srv/sessions/own/work"
    assert seen[0].python == os.path.realpath(sys.executable)


def test_main_without_a_second_layer_and_with_a_bad_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unit_probe, "EXTRA", {})
    monkeypatch.setattr(unit_probe, "SOCKETS", "/nonexistent/sockets.json")

    def broken(claude: str, args: list[str]) -> str:
        raise OSError(errno.ENOEXEC, "no interpreter")

    out = io.StringIO()
    assert unit_probe.main(["o", "x", "/c", "--"], out=out, guest=fake_guest(broken)) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0] == {"check": "layer2", "outcome": "errno:ENOEXEC"}
    assert lines[1] == {"check": "guest", "outcome": "/nonexistent"}
    assert lines[-1] == {"check": "unix_sockets_root_list", "outcome": "error:no_sockets_listed"}
    usage = io.StringIO()
    assert unit_probe.main(["o", "--"], out=usage) == 2
    assert usage.getvalue().startswith("usage:")


def test_main_loads_the_guest_probe_from_the_image_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unit_probe, "GUEST_PROBE", str(GUEST_PROBE_FILE))
    monkeypatch.setattr(unit_probe, "EXTRA", {})
    monkeypatch.setattr(sys, "argv", ["unit_probe.py", "o", "x", "/nonexistent/claude", "--"])
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    loaded: list[str] = []
    real = unit_probe._load_guest

    def load(path: str) -> Any:
        loaded.append(path)
        module = real(path)
        setattr(module, "run_checks", lambda ctx: [])  # noqa: B010 - an attribute of a module loaded from a file
        return module

    monkeypatch.setattr(unit_probe, "_load_guest", load)
    assert unit_probe.main() == 0
    assert loaded == [str(GUEST_PROBE_FILE)]

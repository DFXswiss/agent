"""U-RUN-workvm shell: how the runner reaches `limactl` of a slot (13-work-vm.md section 6.1).

`limactl` is a small real executable in `tmp_path` (a shell script) so `VmShell` and `SlotCommand.sync_runner` run
real subprocesses: stdin from bytes and files, bounded output, output into a file, timeouts, missing binaries and
non-zero exits. The account mode (`sudo -n -u ...`) is only checked as argv; sudo is never run.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from agent_runner.vm.lima import EXIT_NOT_FOUND, EXIT_TIMEOUT, Lima, LimaError
from agent_runner.workvm import shell
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.shell import SlotCommand, VmShell

SCRIPT = """#!/bin/sh
if [ "$1" = shell ]; then
    printf '%s\\n' "$*" > "{log}"
    shift 4
fi
mode=$1
shift
case "$mode" in
    cat) exec cat ;;
    args) printf '%s\\n' "$@" ;;
    big) exec head -c "$1" /dev/zero ;;
    sleep) exec sleep 30 ;;
    quit) exit "$1" ;;
    fail) echo "boom" >&2; exit 3 ;;
esac
"""


@pytest.fixture
def limactl(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "limactl"
    path.parent.mkdir()
    path.write_text(SCRIPT.format(log=tmp_path / "argv.log"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def vm(limactl: Path) -> VmShell:
    return VmShell(SlotCommand((), limactl=str(limactl)), 0, "wvm-0a1b2c3d")


def call(
    target: VmShell,
    argv: list[str],
    *,
    stdin: bytes | Path | None = None,
    timeout: float = 20.0,
    limit: int = 1 << 20,
    out: Path | None = None,
) -> ShellResult:
    return asyncio.run(target(argv, stdin=stdin, timeout=timeout, limit=limit, out=out))


def test_prefix_self_mode_and_account_mode(limactl: Path) -> None:
    assert SlotCommand((), limactl=str(limactl)).prefix(0) == [str(limactl)]
    assert SlotCommand((), limactl="no-such-limactl-kor29").prefix(3) == ["no-such-limactl-kor29"]
    command = SlotCommand(("agentwvm00", "agentwvm01"))
    assert command.prefix(1) == ["sudo", "-n", "-u", "agentwvm01", shell.WRAPPER]
    assert SlotCommand(("a",), wrapper="/w").prefix(0) == ["sudo", "-n", "-u", "a", "/w"]


def test_sync_runner_runs_limactl(limactl: Path) -> None:
    run = SlotCommand((), limactl=str(limactl)).sync_runner(0)
    done = run(["args", "list", "--json"], 20.0)
    assert (done.returncode, done.stdout, done.stderr) == (0, "list\n--json\n", "")
    assert done.args == [str(limactl), "args", "list", "--json"]
    failed = run(["fail"], 20.0)
    assert (failed.returncode, failed.stderr) == (3, "boom\n")


def test_sync_runner_reports_a_missing_limactl(tmp_path: Path) -> None:
    missing = str(tmp_path / "nowhere" / "limactl")
    done = SlotCommand((), limactl=missing).sync_runner(0)(["list"], 5.0)
    assert (done.returncode, done.stdout, done.stderr) == (EXIT_NOT_FOUND, "", "limactl: not found")
    assert done.args == [missing, "list"]


def test_sync_runner_reports_a_timeout(limactl: Path) -> None:
    done = SlotCommand((), limactl=str(limactl)).sync_runner(0)(["sleep"], 0.3)
    assert (done.returncode, done.stdout, done.stderr) == (EXIT_TIMEOUT, "", "limactl: timed out")


def test_vm_shell_runs_in_the_instance_without_stdin(limactl: Path, tmp_path: Path) -> None:
    result = call(vm(limactl), ["args", "sudo", "-n", "x"])
    assert result == ShellResult(0, b"sudo\n-n\nx\n", False)
    logged = (tmp_path / "argv.log").read_text(encoding="utf-8")
    assert logged == "shell --workdir / wvm-0a1b2c3d args sudo -n x\n"
    # without stdin the command reads /dev/null
    assert call(vm(limactl), ["cat"]) == ShellResult(0, b"", False)


def test_vm_shell_feeds_bytes_and_files(limactl: Path, tmp_path: Path) -> None:
    data = os.urandom(200_000)
    assert call(vm(limactl), ["cat"], stdin=data) == ShellResult(0, data, False)
    assert call(vm(limactl), ["cat"], stdin=b"") == ShellResult(0, b"", False)
    source = tmp_path / "input.bin"
    source.write_bytes(data)
    assert call(vm(limactl), ["cat"], stdin=source) == ShellResult(0, data, False)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert call(vm(limactl), ["cat"], stdin=empty) == ShellResult(0, b"", False)


def test_vm_shell_survives_a_command_that_ignores_stdin(limactl: Path, tmp_path: Path) -> None:
    data = b"x" * (4 << 20)
    assert call(vm(limactl), ["quit", "0"], stdin=data) == ShellResult(0, b"", False)
    source = tmp_path / "input.bin"
    source.write_bytes(data)
    assert call(vm(limactl), ["quit", "4"], stdin=source) == ShellResult(4, b"", False)


def test_vm_shell_passes_the_exit_code(limactl: Path) -> None:
    assert call(vm(limactl), ["quit", "7"]) == ShellResult(7, b"", False)


def test_vm_shell_bounds_the_output(limactl: Path) -> None:
    exact = call(vm(limactl), ["big", "5000"], limit=5000)
    assert exact == ShellResult(0, b"\x00" * 5000, False)
    over = call(vm(limactl), ["big", "5000000"], limit=100_000)
    assert over.truncated
    assert over.returncode != 0
    assert len(over.stdout) <= 100_000
    assert set(over.stdout) <= {0}


def test_vm_shell_writes_output_into_a_file(limactl: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    assert call(vm(limactl), ["big", "300000"], out=target) == ShellResult(0, b"", False)
    assert target.read_bytes() == b"\x00" * 300_000
    small = call(vm(limactl), ["big", "300000"], out=target, limit=1000)
    assert (small.stdout, small.truncated) == (b"", True)
    assert len(target.read_bytes()) <= 1000


def test_vm_shell_times_out(limactl: Path) -> None:
    assert call(vm(limactl), ["sleep"], timeout=0.3) == ShellResult(EXIT_TIMEOUT, b"", False)
    assert call(vm(limactl), ["sleep"], stdin=b"in", timeout=0.3) == ShellResult(EXIT_TIMEOUT, b"", False)


@pytest.mark.parametrize(
    ("files", "found"),
    [(("disk",), "disk"), (("basedisk", "diffdisk"), "diffdisk"), (("disk", "diffdisk"), "disk")],
)
def test_disk_path(tmp_path: Path, files: tuple[str, ...], found: str) -> None:
    """Lima 2.x keeps one `disk`; older instances a `diffdisk` (beside `basedisk`): the one present wins."""
    for name in files:
        (tmp_path / name).write_bytes(b"x")
    assert shell.disk_path(str(tmp_path)) == str(tmp_path / found)


def test_disk_path_of_an_instance_without_disk(tmp_path: Path) -> None:
    (tmp_path / "basedisk").write_bytes(b"x")
    (tmp_path / "diffdisk").mkdir()
    with pytest.raises(FileNotFoundError) as caught:
        shell.disk_path(str(tmp_path))
    assert caught.value.filename == str(tmp_path / "disk")


def test_a_slot_without_its_home_has_no_instances(tmp_path: Path) -> None:
    """HOT-25: after a restart the volume of the slot homes may hang elsewhere. No Lima home, no instance: listing
    finds none, a deletion counts as done (the work VM task never fails on it again), anything else fails."""
    lima = Lima(SlotCommand(("agentwvm00",), home_root=str(tmp_path)).sync_runner(0), prefix="agent")
    assert lima.list() == []
    assert lima.delete("agentwvm-0a1b2c3d") is False
    with pytest.raises(LimaError) as caught:
        lima.rename("agentwvm-0a1b2c3d", "agentwvm-1a1b2c3d")
    assert shell.NO_HOME in caught.value.detail
    seen: list[str] = []

    def absent(path: str) -> bool:
        seen.append(path)
        return False

    command = SlotCommand(("a", "b"), home_root="/r", has_home=absent)
    answer = command.sync_runner(1)(["list", "--json"], 1.0)
    assert answer.args == ["sudo", "-n", "-u", "b", shell.WRAPPER, "list", "--json"]  # nothing ran
    assert seen == ["/r/b"]

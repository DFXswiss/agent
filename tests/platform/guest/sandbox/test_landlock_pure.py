"""U-GST-sandbox: platform-independent parts of the Landlock launcher (05-runner.md section 4.3).

The system calls themselves are exercised on Linux in tests/platform/guest_linux/test_landlock_linux.py; here they
are replaced by a fake libc (Kernel) or a recording kernel (apply/main).
"""

from __future__ import annotations

import ctypes
import errno
import io
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agent_guest.sandbox import landlock as ll

ALL_FS_ABI5 = (1 << 16) - 1


def elf_bytes(interp: bytes | None, *, is64: bool = True, little: bool = True, leading_phdrs: int = 1) -> bytes:
    """A minimal ELF image with `leading_phdrs` PT_LOAD headers followed by an optional PT_INTERP header."""
    e = "<" if little else ">"
    ident = b"\x7fELF" + bytes([2 if is64 else 1, 1 if little else 2, 1]) + bytes(9)
    count = leading_phdrs + (interp is not None)
    if is64:
        phoff, phentsize, ehsize = 64, 56, 64
        data_offset = phoff + phentsize * count
        header = struct.pack(e + "HHIQQQIHHHHHH", 2, 183, 1, 0, phoff, 0, 0, ehsize, phentsize, count, 0, 0, 0)
        phdrs = [struct.pack(e + "IIQQQQQQ", 1, 5, 0, 0, 0, 0, 0, 0) for _ in range(leading_phdrs)]
        if interp is not None:
            phdrs.append(struct.pack(e + "IIQQQQQQ", 3, 4, data_offset, 0, 0, len(interp), len(interp), 1))
    else:
        phoff, phentsize, ehsize = 52, 32, 52
        data_offset = phoff + phentsize * count
        header = struct.pack(e + "HHIIIIIHHHHHH", 2, 40, 1, 0, phoff, 0, 0, ehsize, phentsize, count, 0, 0, 0)
        phdrs = [struct.pack(e + "IIIIIIII", 1, 0, 0, 0, 0, 0, 5, 0) for _ in range(leading_phdrs)]
        if interp is not None:
            phdrs.append(struct.pack(e + "IIIIIIII", 3, data_offset, 0, 0, len(interp), len(interp), 4, 1))
    return ident + header + b"".join(phdrs) + (interp or b"")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake filesystem root: sessions/<id>, a binary with an interpreter, a static binary, system dirs."""
    base = tmp_path.resolve()
    (base / "sessions" / "s1").mkdir(parents=True)
    (base / "lib").mkdir()
    (base / "lib" / "ld.so").write_bytes(elf_bytes(None))
    (base / "bin").mkdir()
    (base / "bin" / "claude").write_bytes(elf_bytes(str(base / "lib" / "ld.so").encode() + b"\0"))
    (base / "bin" / "static").write_bytes(elf_bytes(None))
    (base / "bin" / "link").symlink_to(base / "bin" / "claude")
    (base / "usr").mkdir()
    (base / "dev").mkdir()
    (base / "dev" / "null").write_bytes(b"")
    (base / "dev" / "pts").mkdir()
    (base / "harness.md").write_text("rules\n")
    (base / "tmp").mkdir()
    monkeypatch.setattr(ll, "SESSIONS_ROOT", f"{base}/sessions")
    return base


def options(root: Path, *extra: str, command: Sequence[str] | None = None) -> ll.Options:
    argv = ["--session-dir", f"{root}/sessions/s1", "--exec", f"{root}/bin/claude", *extra, "--"]
    return ll.parse_args([*argv, *(command or [f"{root}/bin/claude", "--flag"])])


def plan_for(root: Path, opts: ll.Options, abi: int = 5) -> ll.Plan:
    return ll.plan(
        opts,
        abi,
        standard_ro=[f"{root}/usr", f"{root}/missing"],
        standard_rw=[f"{root}/tmp"],
        standard_dev=[f"{root}/dev/null", f"{root}/dev/pts", f"{root}/dev/gone"],
        sessions_root=f"{root}/sessions",
    )


# --- argument parsing -------------------------------------------------------------------------------------------


def test_parse_args_collects_every_option() -> None:
    opts = ll.parse_args(
        [
            *["--session-dir", "/srv/sessions/a", "--harness", "/run/h.md", "--exec", "/opt/c", "--exec", "/usr/bin/p"],
            *["--connect", "7700", "--connect", "1", "--ro", "/src", "--rw", "/cov", "--", "/opt/c", "-x", "--", "y"],
        ]
    )
    assert opts == ll.Options(
        session_dir="/srv/sessions/a",
        harness="/run/h.md",
        execs=("/opt/c", "/usr/bin/p"),
        connect=(7700, 1),
        extra_ro=("/src",),
        extra_rw=("/cov",),
        command=("/opt/c", "-x", "--", "y"),
    )


def test_parse_args_minimal_has_no_harness_and_no_ports() -> None:
    opts = ll.parse_args(["--session-dir", "/s/a", "--exec", "/c", "--", "/c"])
    assert opts.harness is None
    assert opts.connect == ()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--session-dir", "/s", "--exec", "/c", "/c"], "missing '--'"),
        (["--bogus", "x", "--", "/c"], "unknown option '--bogus'"),
        (["--session-dir", "/s", "--exec"], "missing '--'"),
        (["--session-dir", "/s", "--session-dir", "/t", "--exec", "/c", "--", "/c"], "given twice"),
        (["--exec", "/c", "--", "/c"], "--session-dir is required"),
        (["--session-dir", "/s", "--", "/c"], "at least one --exec"),
        (["--session-dir", "/s", "--exec", "/c", "--"], "missing program"),
        (["--session-dir", "/s", "--exec", "/c", "--connect", "0", "--", "/c"], "invalid port '0'"),
        (["--session-dir", "/s", "--exec", "/c", "--connect", "65536", "--", "/c"], "invalid port '65536'"),
        (["--session-dir", "/s", "--exec", "/c", "--connect", "-1", "--", "/c"], "invalid port '-1'"),
        (["--session-dir", "s", "--exec", "/c", "--", "/c"], "must be absolute: 's'"),
        (["--session-dir", "/s", "--exec", "/c", "--harness", "h", "--", "/c"], "must be absolute: 'h'"),
        (["--session-dir", "/s", "--exec", "/c", "--rw", "r", "--", "/c"], "must be absolute: 'r'"),
    ],
)
def test_parse_args_refuses(argv: list[str], message: str) -> None:
    with pytest.raises(ll.LauncherError, match=message.replace("(", r"\(")):
        ll.parse_args(argv)


def test_option_without_value_is_refused() -> None:
    with pytest.raises(ll.LauncherError, match="option --connect needs a value"):
        ll.parse_args(["--session-dir", "/s", "--exec", "/c", "--connect", "--", "/c"])


# --- ABI and ELF ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abi", "expected"),
    [
        (1, (1 << 13) - 1),
        (2, (1 << 14) - 1),
        (3, (1 << 15) - 1),
        (4, (1 << 15) - 1),
        (5, ALL_FS_ABI5),
        (6, ALL_FS_ABI5),
    ],
)
def test_handled_fs_per_abi(abi: int, expected: int) -> None:
    assert ll.handled_fs(abi) == expected


@pytest.mark.parametrize(("is64", "little"), [(True, True), (True, False), (False, True), (False, False)])
def test_elf_interpreter_is_read_from_pt_interp(tmp_path: Path, is64: bool, little: bool) -> None:
    binary = tmp_path / "b"
    binary.write_bytes(elf_bytes(b"/lib/ld-linux-aarch64.so.1\0", is64=is64, little=little, leading_phdrs=2))
    assert ll.elf_interpreter(str(binary)) == "/lib/ld-linux-aarch64.so.1"


@pytest.mark.parametrize(
    "content",
    [
        elf_bytes(None),
        b"#!/bin/sh\n",
        b"\x7fELF",
        b"\x7fELF\x03\x01" + bytes(10),
        b"\x7fELF\x02\x03" + bytes(10),
    ],
)
def test_elf_interpreter_none_for_static_or_foreign_files(tmp_path: Path, content: bytes) -> None:
    binary = tmp_path / "b"
    binary.write_bytes(content)
    assert ll.elf_interpreter(str(binary)) is None


def test_elf_interpreter_refuses_a_truncated_header(tmp_path: Path) -> None:
    binary = tmp_path / "b"
    binary.write_bytes(elf_bytes(b"/x\0")[:30])
    with pytest.raises(ll.LauncherError, match="truncated ELF header"):
        ll.elf_interpreter(str(binary))


def test_elf_interpreter_refuses_a_truncated_program_header(tmp_path: Path) -> None:
    binary = tmp_path / "b"
    binary.write_bytes(elf_bytes(b"/x\0")[:80])
    with pytest.raises(ll.LauncherError, match="truncated ELF header"):
        ll.elf_interpreter(str(binary))


def test_elf_interpreter_must_be_absolute(tmp_path: Path) -> None:
    binary = tmp_path / "b"
    binary.write_bytes(elf_bytes(b"lib/ld.so\0"))
    with pytest.raises(ll.LauncherError, match="PT_INTERP is not absolute"):
        ll.elf_interpreter(str(binary))


def test_interpreters_are_listed_once(root: Path) -> None:
    (root / "bin" / "second").write_bytes((root / "bin" / "claude").read_bytes())
    opts = options(root, "--exec", f"{root}/bin/second")
    paths = [r.path for r in ll.plan(opts, 4, standard_ro=[], standard_rw=[], standard_dev=[]).paths]
    assert paths == [f"{root}/sessions/s1", f"{root}/bin/claude", f"{root}/lib/ld.so", f"{root}/bin/second"]


@pytest.mark.parametrize("size", [0, 4097])
def test_elf_interpreter_refuses_an_invalid_size(tmp_path: Path, size: int) -> None:
    image = bytearray(elf_bytes(b"/x\0"))
    struct.pack_into("<Q", image, 64 + 56 + 32, size)  # p_filesz of the PT_INTERP header
    binary = tmp_path / "b"
    binary.write_bytes(bytes(image))
    with pytest.raises(ll.LauncherError, match="invalid PT_INTERP size"):
        ll.elf_interpreter(str(binary))


# --- rule computation -------------------------------------------------------------------------------------------


def test_plan_follows_the_rules_of_section_4_3(root: Path) -> None:
    opts = options(root, "--harness", f"{root}/harness.md", "--connect", "7700")
    result = plan_for(root, opts, abi=5)
    assert result.handled_fs == ALL_FS_ABI5
    assert result.handled_net == ll.NET_BIND_TCP | ll.NET_CONNECT_TCP
    assert result.ports == (7700,)
    assert [(r.path, r.access) for r in result.paths] == [
        (f"{root}/usr", ll.READ_FILE | ll.READ_DIR),
        (f"{root}/harness.md", ll.READ_FILE),
        (f"{root}/sessions/s1", ll.READ | ll.WRITE),
        (f"{root}/tmp", ll.READ | ll.WRITE),
        (f"{root}/dev/null", ll.READ_FILE | ll.WRITE_FILE | ll.IOCTL_DEV),
        (f"{root}/dev/pts", ll.READ_FILE | ll.WRITE_FILE | ll.IOCTL_DEV),
        (f"{root}/bin/claude", ll.EXECUTE | ll.READ_FILE),
        (f"{root}/lib/ld.so", ll.EXECUTE | ll.READ_FILE),
    ]
    # Writable paths never get EXECUTE or device-node creation.
    for rule in result.paths:
        if rule.access & ll.WRITE_FILE and rule.path.endswith(("s1", "tmp")):
            assert not rule.access & (ll.EXECUTE | ll.MAKE_CHAR | ll.MAKE_BLOCK | ll.IOCTL_DEV)


def test_plan_drops_rights_the_abi_lacks(root: Path) -> None:
    result = plan_for(root, options(root), abi=4)
    assert result.handled_fs == (1 << 15) - 1
    devices = {r.path: r.access for r in result.paths}
    assert devices[f"{root}/dev/null"] == ll.READ_FILE | ll.WRITE_FILE


def test_plan_resolves_symlinks_and_adds_extras(root: Path) -> None:
    (root / "src").mkdir()
    (root / "cov").mkdir()
    opts = options(
        root, "--exec", f"{root}/bin/link", "--exec", f"{root}/bin/static", "--ro", f"{root}/src", "--rw", f"{root}/cov"
    )
    rules = {r.path: r.access for r in plan_for(root, opts).paths}
    assert rules[f"{root}/src"] == ll.READ
    assert rules[f"{root}/cov"] == ll.READ | ll.WRITE
    assert rules[f"{root}/bin/static"] == ll.RUN
    assert f"{root}/bin/link" not in rules  # resolved to bin/claude


def test_plan_skips_rules_without_rights(root: Path) -> None:
    (root / "file").write_text("x")
    opts = options(root, "--rw", f"{root}/file")
    result = ll.plan(opts, 4, standard_ro=[], standard_rw=[], standard_dev=[f"{root}/dev/null"])
    rules = {r.path: r.access for r in result.paths}
    assert rules[f"{root}/file"] == ll.READ_FILE | ll.WRITE_FILE | ll.TRUNCATE  # file rights only
    assert rules[f"{root}/dev/null"] == ll.READ_FILE | ll.WRITE_FILE


def test_plan_without_any_standard_paths(root: Path) -> None:
    result = ll.plan(options(root), 4, standard_ro=[], standard_rw=[], standard_dev=[])
    assert [r.path for r in result.paths] == [f"{root}/sessions/s1", f"{root}/bin/claude", f"{root}/lib/ld.so"]


def test_rule_with_only_directory_rights_on_a_file_is_dropped(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "file").write_text("x")
    monkeypatch.setattr(ll, "READ", ll.READ_DIR)
    result = ll.plan(
        options(root),
        4,
        standard_ro=[f"{root}/file"],
        standard_rw=[],
        standard_dev=[],
    )
    assert f"{root}/file" not in [r.path for r in result.paths]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--harness", "/nonexistent/h.md"], "path does not exist"),
        (["--ro", "/nonexistent/ro"], "path does not exist"),
        (["--exec", "/nonexistent/bin"], "executable does not exist"),
    ],
)
def test_plan_refuses_missing_named_paths(root: Path, extra: list[str], message: str) -> None:
    with pytest.raises(ll.LauncherError, match=message):
        plan_for(root, options(root, *extra))


def test_plan_refuses_a_missing_interpreter(root: Path) -> None:
    (root / "bin" / "dangling").write_bytes(elf_bytes(b"/nonexistent/ld.so\0"))
    with pytest.raises(ll.LauncherError, match=r"path does not exist: '/nonexistent/ld\.so'"):
        plan_for(root, options(root, "--exec", f"{root}/bin/dangling"))


def test_plan_refuses_an_old_abi(root: Path) -> None:
    with pytest.raises(ll.LauncherError, match="ABI 3 < 4"):
        plan_for(root, options(root), abi=3)


@pytest.mark.parametrize("session", ["sessions/s1/../s1", "sessions", "other/s1", "sessions/s1/work"])
def test_plan_refuses_a_session_dir_outside_the_sessions_root(root: Path, session: str) -> None:
    (root / "other" / "s1").mkdir(parents=True)
    (root / "sessions" / "s1" / "work").mkdir()
    opts = ll.parse_args(
        ["--session-dir", f"{root}/{session}", "--exec", f"{root}/bin/claude", "--", f"{root}/bin/claude"]
    )
    with pytest.raises(ll.LauncherError, match="resolved child"):
        plan_for(root, opts)


def test_plan_refuses_a_symlinked_session_dir(root: Path) -> None:
    (root / "sessions" / "s2").symlink_to(root / "sessions" / "s1")
    opts = ll.parse_args(
        ["--session-dir", f"{root}/sessions/s2", "--exec", f"{root}/bin/claude", "--", f"{root}/bin/claude"]
    )
    with pytest.raises(ll.LauncherError, match="resolved child"):
        plan_for(root, opts)


def test_plan_refuses_a_missing_session_dir(root: Path) -> None:
    opts = ll.parse_args(
        ["--session-dir", f"{root}/sessions/s9", "--exec", f"{root}/bin/claude", "--", f"{root}/bin/claude"]
    )
    with pytest.raises(ll.LauncherError, match="path does not exist"):
        plan_for(root, opts)


def test_plan_refuses_a_program_that_is_not_an_exec_file(root: Path) -> None:
    with pytest.raises(ll.LauncherError, match="is not an --exec file"):
        plan_for(root, options(root, command=[f"{root}/bin/static"]))


def test_plan_accepts_the_program_through_a_symlink(root: Path) -> None:
    assert plan_for(root, options(root, command=[f"{root}/bin/link"])).paths


# --- Kernel wrappers (fake libc) --------------------------------------------------------------------------------


class FakeLibc:
    def __init__(self, results: dict[int, int] | None = None, prctl_result: int = 0) -> None:
        self.results = results or {}
        self.prctl_result = prctl_result
        self.calls: list[tuple[Any, ...]] = []

    def _result(self, value: int) -> int:
        if value < 0:
            ctypes.set_errno(errno.EINVAL)
        return value

    def syscall(self, number: int, *raw: Any) -> int:
        assert all(isinstance(arg, ctypes.c_long) or arg is None or hasattr(arg, "_obj") for arg in raw)
        args = tuple(arg.value if isinstance(arg, ctypes.c_long) else arg for arg in raw)
        self.calls.append((number, *args))
        if number == ll.SYS_LANDLOCK_CREATE_RULESET and args[2] == ll.LANDLOCK_CREATE_RULESET_VERSION:
            return self._result(self.results.get(-1, 4))
        return self._result(self.results.get(number, 3 if number == ll.SYS_LANDLOCK_CREATE_RULESET else 0))

    def prctl(self, option: int, *raw: Any) -> int:
        self.calls.append(("prctl", option, *(arg.value for arg in raw)))
        return self._result(self.prctl_result)


def test_kernel_abi_reports_the_version_or_zero() -> None:
    assert ll.Kernel(FakeLibc({-1: 6})).abi() == 6
    assert ll.Kernel(FakeLibc({-1: -1})).abi() == 0


def test_kernel_wrappers_pass_the_structures(tmp_path: Path) -> None:
    libc = FakeLibc()
    kernel = ll.Kernel(libc)
    assert kernel.create_ruleset(7, 3) == 3
    kernel.add_path_rule(3, str(tmp_path), ll.READ)
    kernel.add_port_rule(3, 7700, ll.NET_CONNECT_TCP)
    kernel.no_new_privs()
    kernel.restrict_self(3)
    numbers = [call[0] for call in libc.calls]
    assert numbers == [
        ll.SYS_LANDLOCK_CREATE_RULESET,
        ll.SYS_LANDLOCK_ADD_RULE,
        ll.SYS_LANDLOCK_ADD_RULE,
        "prctl",
        ll.SYS_LANDLOCK_RESTRICT_SELF,
    ]
    assert libc.calls[1][1:3] == (3, ll.LANDLOCK_RULE_PATH_BENEATH)
    assert libc.calls[2][1:3] == (3, ll.LANDLOCK_RULE_NET_PORT)
    assert libc.calls[3] == ("prctl", ll.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    assert ctypes.sizeof(ll._PathBeneathAttr) == 12  # packed like struct landlock_path_beneath_attr
    assert ctypes.sizeof(ll._RulesetAttr) == 16
    assert ctypes.sizeof(ll._NetPortAttr) == 16


@pytest.mark.parametrize(
    ("number", "call"),
    [
        (ll.SYS_LANDLOCK_CREATE_RULESET, lambda k, p: k.create_ruleset(1, 0)),
        (ll.SYS_LANDLOCK_ADD_RULE, lambda k, p: k.add_path_rule(3, p, 1)),
        (ll.SYS_LANDLOCK_ADD_RULE, lambda k, p: k.add_port_rule(3, 1, 2)),
        (ll.SYS_LANDLOCK_RESTRICT_SELF, lambda k, p: k.restrict_self(3)),
    ],
)
def test_kernel_wrappers_raise_the_errno(tmp_path: Path, number: int, call: Any) -> None:
    with pytest.raises(OSError, match="Invalid argument") as info:
        call(ll.Kernel(FakeLibc({number: -1})), str(tmp_path))
    assert info.value.errno == errno.EINVAL


def test_kernel_no_new_privs_failure() -> None:
    with pytest.raises(OSError, match="PR_SET_NO_NEW_PRIVS"):
        ll.Kernel(FakeLibc(prctl_result=-1)).no_new_privs()


def test_kernel_add_path_rule_closes_its_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError, match="landlock_add_rule"):
        ll.Kernel(FakeLibc({ll.SYS_LANDLOCK_ADD_RULE: -1})).add_path_rule(3, str(tmp_path), 1)
    assert len(closed) == 1


def test_kernel_close_and_default_libc() -> None:
    fd = os.open(os.devnull, os.O_RDONLY)
    kernel = ll.Kernel()  # loads the C library; no system call is made here
    kernel.close(fd)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)


# --- apply and main ---------------------------------------------------------------------------------------------


class RecordingKernel(ll.Kernel):
    def __init__(self, abi: int = 5, fail: str | None = None) -> None:
        self._abi = abi
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        if call[0] == self.fail:
            raise OSError(errno.EPERM, f"{call[0]} failed")

    def abi(self) -> int:
        return self._abi

    def create_ruleset(self, handled_fs: int, handled_net: int) -> int:
        self._record("create", handled_fs, handled_net)
        return 9

    def add_path_rule(self, ruleset_fd: int, path: str, access: int) -> None:
        self._record("path", ruleset_fd, path, access)

    def add_port_rule(self, ruleset_fd: int, port: int, access: int) -> None:
        self._record("port", ruleset_fd, port, access)

    def no_new_privs(self) -> None:
        self._record("nnp")

    def restrict_self(self, ruleset_fd: int) -> None:
        self._record("restrict", ruleset_fd)

    def close(self, fd: int) -> None:
        self._record("close", fd)


def test_apply_installs_rules_then_restricts() -> None:
    kernel = RecordingKernel()
    layer = ll.Plan(handled_fs=5, handled_net=3, paths=(ll.PathRule("/a", 1),), ports=(7700,))
    ll.apply(layer, kernel)
    assert kernel.calls == [
        ("create", 5, 3),
        ("path", 9, "/a", 1),
        ("port", 9, 7700, ll.NET_CONNECT_TCP),
        ("nnp",),
        ("restrict", 9),
        ("close", 9),
    ]


def test_apply_closes_the_ruleset_on_failure() -> None:
    kernel = RecordingKernel(fail="path")
    with pytest.raises(OSError, match="path failed"):
        ll.apply(ll.Plan(1, 0, (ll.PathRule("/a", 1),), ()), kernel)
    assert kernel.calls[-1] == ("close", 9)
    assert ("restrict", 9) not in kernel.calls


class Executed(Exception):
    pass


def recording_execv(calls: list[tuple[str, list[str]]]) -> Any:
    def execv(path: str, args: list[str]) -> NoReturn:
        calls.append((path, args))
        raise Executed

    return execv


def run_main(root: Path, monkeypatch: pytest.MonkeyPatch, kernel: ll.Kernel, argv: list[str]) -> tuple[int, str]:
    monkeypatch.setattr(ll, "READ_ONLY_PATHS", ())
    monkeypatch.setattr(ll, "READ_WRITE_PATHS", ())
    monkeypatch.setattr(ll, "DEVICE_PATHS", ())
    err = io.StringIO()
    rc = ll.main(argv, kernel=kernel, execv=recording_execv([]), stderr=err)
    return rc, err.getvalue()


def test_main_applies_and_execs(root: Path) -> None:
    kernel = RecordingKernel()
    calls: list[tuple[str, list[str]]] = []
    argv = ["--session-dir", f"{root}/sessions/s1", "--exec", f"{root}/bin/claude", "--connect", "7700", "--"]
    with pytest.raises(Executed):
        ll.main([*argv, f"{root}/bin/claude", "--restricted"], kernel=kernel, execv=recording_execv(calls))
    assert calls == [(f"{root}/bin/claude", [f"{root}/bin/claude", "--restricted"])]
    assert kernel.calls[0][0] == "create"
    assert ("restrict", 9) in kernel.calls


def test_main_exits_126_below_abi_4(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, err = run_main(root, monkeypatch, RecordingKernel(abi=3), ["--session-dir", "/s/a", "--exec", "/c", "--", "/c"])
    assert rc == ll.EXIT_FAILURE == 126
    assert "Landlock ABI 3 < 4" in err
    assert "usage:" in err


def test_main_exits_126_on_usage_errors(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, err = run_main(root, monkeypatch, RecordingKernel(), ["--exec", "/c"])
    assert rc == 126
    assert "missing '--'" in err


def test_main_exits_126_when_the_kernel_refuses(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["--session-dir", f"{root}/sessions/s1", "--exec", f"{root}/bin/claude", "--", f"{root}/bin/claude"]
    rc, err = run_main(root, monkeypatch, RecordingKernel(fail="restrict"), argv)
    assert rc == 126
    assert "restrict failed" in err
    assert "usage:" not in err


def test_main_exits_126_when_exec_fails(root: Path) -> None:

    def execv(path: str, args: list[str]) -> NoReturn:
        raise PermissionError(errno.EACCES, "Permission denied", path)

    err = io.StringIO()
    argv = ["--session-dir", f"{root}/sessions/s1", "--exec", f"{root}/bin/claude", "--", f"{root}/bin/claude"]
    assert ll.main(argv, kernel=RecordingKernel(), execv=execv, stderr=err) == 126
    assert "Permission denied" in err.getvalue()


def test_main_reads_sys_argv_and_writes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["landlock.py", "--bogus"])
    assert ll.main(kernel=RecordingKernel()) == 126
    assert "missing '--'" in capsys.readouterr().err


def test_main_builds_the_default_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[bool] = []

    class Probe(RecordingKernel):
        def __init__(self) -> None:
            built.append(True)
            super().__init__(abi=0)

    monkeypatch.setattr(ll, "Kernel", Probe)
    err = io.StringIO()
    assert ll.main(["--session-dir", "/s/a", "--exec", "/c", "--", "/c"], stderr=err) == 126
    assert built == [True]
    assert "ABI 0 < 4" in err.getvalue()


def test_main_reports_undecodable_interpreters(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "bin" / "bad").write_bytes(elf_bytes(b"/\xff\xfe\0"))
    argv = ["--session-dir", f"{root}/sessions/s1", "--exec", f"{root}/bin/bad", "--", f"{root}/bin/bad"]
    rc, err = run_main(root, monkeypatch, RecordingKernel(), argv)
    assert rc == 126
    assert "codec" in err


def test_module_is_standard_library_only() -> None:
    source = Path(ll.__file__).read_text()
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "ctypes", "os", "struct", "sys", "collections", "dataclasses", "typing"}

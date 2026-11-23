"""Landlock launcher of the session sandbox, layer 2 (docs/platform/spec/05-runner.md section 4.3, ADR-008).

The session unit runs this file as a script, standard library only and without the package on ``sys.path``::

    python3 -I /opt/agent-guest/agent_guest/sandbox/landlock.py --session-dir /srv/sessions/<id> \\
        --harness /run/agent-guest/harness/<id>.md --exec /opt/claude/claude --connect 7700 \\
        -- /opt/claude/claude <claude arguments>

It requires Landlock ABI >= 4, handles every filesystem access right of the running ABI plus
``NET_BIND_TCP | NET_CONNECT_TCP``, adds the rules of section 4.3, sets ``PR_SET_NO_NEW_PRIVS``, calls
``landlock_restrict_self`` and ``execv``s the command. Every failure exits with status 126 (the session then fails
with reason ``sandbox_failed``). After ``execv`` the Python interpreter itself is no longer executable.

Rules (fixed; the options only name the per-session paths):

* read-only: ``/usr``, ``/lib``, ``/etc``, ``/opt/claude``, ``/opt/agent-guest``, ``/proc``,
  ``/sys/devices/system/cpu`` and the harness file;
* read-write (no execute, no device nodes): the session directory and ``/tmp``;
* devices ``/dev/null``, ``/dev/zero``, ``/dev/urandom``, ``/dev/random``, ``/dev/tty``, ``/dev/pts``, ``/dev/ptmx``:
  ``READ_FILE | WRITE_FILE`` (+ ``IOCTL_DEV`` from ABI 5);
* ``EXECUTE | READ_FILE`` only on the ``--exec`` files and their ELF interpreters (``PT_INTERP``);
* TCP connect only to the ``--connect`` ports, bind to none.

Standard system paths that do not exist are skipped (a missing path grants nothing); every path named on the
command line must exist. All paths are resolved with ``os.path.realpath`` before a rule is added
(05 section 4.6 no. 2).

``--ro``/``--rw`` add further paths; only the sandbox self-test (read access to its probe) and the Linux tests
use them, the session unit never does.

Interface:

* ``parse_args(argv: Sequence[str]) -> Options`` (raises ``LauncherError``)
* ``handled_fs(abi: int) -> int``: filesystem rights of an ABI version
* ``elf_interpreter(path: str) -> str | None``: ``PT_INTERP`` of an ELF file
* ``plan(options, abi, *, standard_ro=None, standard_rw=None, standard_dev=None, sessions_root=None) -> Plan``:
  the complete ruleset, deterministic (``None`` selects the module constants)
* ``apply(plan: Plan, kernel: Kernel) -> None``: install one layer on the calling thread
* ``Kernel(libc=None)``: the system calls (``abi``, ``create_ruleset``, ``add_path_rule``, ``add_port_rule``,
  ``no_new_privs``, ``restrict_self``, ``close``)
* ``main(argv=None, *, kernel=None, execv=os.execv, stderr=None) -> int``
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Any, NoReturn, Protocol

MIN_ABI = 4
EXIT_FAILURE = 126

# System call numbers of the generic table (identical on aarch64 and x86_64).
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2
PR_SET_NO_NEW_PRIVS = 38
O_PATH = 0o10000000  # Linux value on aarch64 and x86_64 (os.O_PATH exists only in Linux builds)

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13
TRUNCATE = 1 << 14
IOCTL_DEV = 1 << 15
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

_ABI1_FS = (1 << 13) - 1  # EXECUTE … MAKE_SYM
READ = READ_FILE | READ_DIR
WRITE = (
    WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_SYM | REFER | TRUNCATE
)
DEVICE = READ_FILE | WRITE_FILE | IOCTL_DEV
RUN = EXECUTE | READ_FILE
# Rights a rule on a non-directory may carry (the kernel refuses directory rights on files).
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV

SESSIONS_ROOT = "/srv/sessions"
READ_ONLY_PATHS = ("/usr", "/lib", "/etc", "/opt/claude", "/opt/agent-guest", "/proc", "/sys/devices/system/cpu")
READ_WRITE_PATHS = ("/tmp",)
DEVICE_PATHS = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random", "/dev/tty", "/dev/pts", "/dev/ptmx")

PT_INTERP = 3
_ELF_MAGIC = b"\x7fELF"
_MAX_INTERP = 4096

USAGE = (
    "usage: landlock.py --session-dir DIR [--harness FILE] --exec FILE [--exec FILE ...] [--connect PORT ...] "
    "[--ro PATH ...] [--rw PATH ...] -- PROGRAM [ARG ...]"
)


class LauncherError(Exception):
    """Invalid arguments or a path that cannot be used in a rule."""


@dataclass(frozen=True)
class Options:
    session_dir: str
    harness: str | None
    execs: tuple[str, ...]
    connect: tuple[int, ...]
    extra_ro: tuple[str, ...]
    extra_rw: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class PathRule:
    path: str
    access: int


@dataclass(frozen=True)
class Plan:
    handled_fs: int
    handled_net: int
    paths: tuple[PathRule, ...]
    ports: tuple[int, ...]


_MULTI = {"--exec": "execs", "--connect": "connect", "--ro": "extra_ro", "--rw": "extra_rw"}
_SINGLE = {"--session-dir": "session_dir", "--harness": "harness"}


def parse_args(argv: Sequence[str]) -> Options:
    """Parse the launcher arguments; everything after the first ``--`` is the command."""
    if "--" not in argv:
        raise LauncherError("missing '--' before the program")
    split = list(argv).index("--")
    opts, command = list(argv[:split]), tuple(argv[split + 1 :])
    single: dict[str, str] = {}
    multi: dict[str, list[str]] = {name: [] for name in _MULTI.values()}
    while opts:
        flag = opts.pop(0)
        if flag not in _SINGLE and flag not in _MULTI:
            raise LauncherError(f"unknown option {flag!r}")
        if not opts:
            raise LauncherError(f"option {flag} needs a value")
        value = opts.pop(0)
        if flag in _MULTI:
            multi[_MULTI[flag]].append(value)
        elif _SINGLE[flag] in single:
            raise LauncherError(f"option {flag} given twice")
        else:
            single[_SINGLE[flag]] = value
    return _validated(single, multi, command)


def _validated(single: dict[str, str], multi: dict[str, list[str]], command: tuple[str, ...]) -> Options:
    if "session_dir" not in single:
        raise LauncherError("--session-dir is required")
    if not multi["execs"]:
        raise LauncherError("at least one --exec is required")
    if not command:
        raise LauncherError("missing program after '--'")
    ports = []
    for text in multi["connect"]:
        if not text.isdigit() or not 0 < int(text) < 65536:
            raise LauncherError(f"invalid port {text!r}")
        ports.append(int(text))
    harness = single.get("harness")
    named = [single["session_dir"], *multi["execs"], *multi["extra_ro"], *multi["extra_rw"]]
    for path in named if harness is None else [*named, harness]:
        if not path.startswith("/"):
            raise LauncherError(f"path must be absolute: {path!r}")
    return Options(
        session_dir=single["session_dir"],
        harness=harness,
        execs=tuple(multi["execs"]),
        connect=tuple(ports),
        extra_ro=tuple(multi["extra_ro"]),
        extra_rw=tuple(multi["extra_rw"]),
        command=command,
    )


def handled_fs(abi: int) -> int:
    """All filesystem access rights the kernel of this ABI version knows."""
    rights = _ABI1_FS
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    if abi >= 5:
        rights |= IOCTL_DEV
    return rights


def elf_interpreter(path: str) -> str | None:
    """The ``PT_INTERP`` path of an ELF executable, ``None`` for static or non-ELF files."""
    with open(path, "rb") as fh:
        ident = fh.read(16)
        if len(ident) < 16 or ident[:4] != _ELF_MAGIC or ident[4] not in (1, 2) or ident[5] not in (1, 2):
            return None
        endian = "<" if ident[5] == 1 else ">"
        is64 = ident[4] == 2
        header = fh.read(48 if is64 else 36)
        if is64:
            phoff, phentsize, phnum = _unpack(endian + "16xQ14xHH", header)
        else:
            phoff, phentsize, phnum = _unpack(endian + "12xI10xHH", header)
        for index in range(phnum):
            fh.seek(phoff + index * phentsize)
            entry = fh.read(phentsize)
            if is64:
                p_type, p_offset, p_filesz = _unpack(endian + "I4xQ16xQ", entry)
            else:
                p_type, p_offset, p_filesz = _unpack(endian + "II8xI", entry)
            if p_type != PT_INTERP:
                continue
            if not 0 < p_filesz <= _MAX_INTERP:
                raise LauncherError(f"{path}: invalid PT_INTERP size")
            fh.seek(p_offset)
            interpreter = fh.read(p_filesz).split(b"\0", 1)[0].decode("utf-8", "strict")
            if not interpreter.startswith("/"):
                raise LauncherError(f"{path}: PT_INTERP is not absolute")
            return interpreter
    return None


def _unpack(fmt: str, data: bytes) -> tuple[int, ...]:
    try:
        return struct.unpack_from(fmt, data)
    except struct.error as exc:
        raise LauncherError(f"truncated ELF header: {exc}") from exc


def plan(
    options: Options,
    abi: int,
    *,
    standard_ro: Sequence[str] | None = None,
    standard_rw: Sequence[str] | None = None,
    standard_dev: Sequence[str] | None = None,
    sessions_root: str | None = None,
) -> Plan:
    """The complete ruleset of a session for the given ABI (rights the ABI lacks are dropped).

    The keyword arguments replace the standard paths (default: the module constants) for tests.
    """
    standard_ro = READ_ONLY_PATHS if standard_ro is None else standard_ro
    standard_rw = READ_WRITE_PATHS if standard_rw is None else standard_rw
    standard_dev = DEVICE_PATHS if standard_dev is None else standard_dev
    sessions_root = SESSIONS_ROOT if sessions_root is None else sessions_root
    if abi < MIN_ABI:
        raise LauncherError(f"Landlock ABI {abi} < {MIN_ABI}")
    session_dir = options.session_dir
    if os.path.realpath(session_dir) != session_dir or os.path.dirname(session_dir) != sessions_root:
        raise LauncherError(f"session directory must be a resolved child of {sessions_root}: {session_dir!r}")
    fs = handled_fs(abi)
    rules: list[PathRule] = []

    def add(path: str, access: int, *, required: bool) -> None:
        real = os.path.realpath(path)
        if not os.path.exists(real):
            if required:
                raise LauncherError(f"path does not exist: {path!r}")
            return
        allowed = access & fs
        if not os.path.isdir(real):
            allowed &= FILE_RIGHTS
        if allowed:
            rules.append(PathRule(real, allowed))

    for path in standard_ro:
        add(path, READ, required=False)
    for path in options.extra_ro:
        add(path, READ, required=True)
    if options.harness is not None:
        add(options.harness, READ_FILE, required=True)
    add(session_dir, READ | WRITE, required=True)
    for path in standard_rw:
        add(path, READ | WRITE, required=False)
    for path in options.extra_rw:
        add(path, READ | WRITE, required=True)
    for path in standard_dev:
        add(path, DEVICE, required=False)
    for path in _with_interpreters(options.execs):
        add(path, RUN, required=True)
    resolved = {os.path.realpath(path) for path in options.execs}
    if os.path.realpath(options.command[0]) not in resolved:
        raise LauncherError(f"program {options.command[0]!r} is not an --exec file")
    return Plan(handled_fs=fs, handled_net=NET_BIND_TCP | NET_CONNECT_TCP, paths=tuple(rules), ports=options.connect)


def _with_interpreters(execs: Sequence[str]) -> list[str]:
    paths: list[str] = []
    for path in execs:
        if not os.path.isfile(path):
            raise LauncherError(f"executable does not exist: {path!r}")
        interp = elf_interpreter(path)
        for item in (path, interp):
            if item is not None and item not in paths:
                paths.append(item)
    return paths


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def _long(value: int) -> ctypes.c_long:
    """Variadic ``syscall()`` arguments are passed as C ``long`` (a plain int would be a 32-bit ``int``)."""
    return ctypes.c_long(value)


class _Libc(Protocol):
    def syscall(self, number: int, *args: Any) -> int: ...

    def prctl(self, option: int, *args: Any) -> int: ...


class Kernel:
    """Thin wrappers around the Landlock system calls; errors raise ``OSError`` with the kernel's errno."""

    def __init__(self, libc: _Libc | None = None) -> None:
        if libc is None:
            loaded = ctypes.CDLL(None, use_errno=True)
            loaded.syscall.restype = ctypes.c_long
            libc = loaded
        self._libc = libc

    def abi(self) -> int:
        """Landlock ABI version, 0 when Landlock is unavailable or disabled."""
        version = int(
            self._libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, _long(0), _long(LANDLOCK_CREATE_RULESET_VERSION))
        )
        return max(version, 0)

    def create_ruleset(self, handled_fs: int, handled_net: int) -> int:
        attr = _RulesetAttr(handled_fs, handled_net)
        return self._check(
            self._libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), _long(ctypes.sizeof(attr)), _long(0)),
            "landlock_create_ruleset",
        )

    def add_path_rule(self, ruleset_fd: int, path: str, access: int) -> None:
        fd = os.open(path, O_PATH | os.O_CLOEXEC)
        try:
            attr = _PathBeneathAttr(access, fd)
            pointer = ctypes.byref(attr)
            result = self._libc.syscall(
                SYS_LANDLOCK_ADD_RULE, _long(ruleset_fd), _long(LANDLOCK_RULE_PATH_BENEATH), pointer, _long(0)
            )
            self._check(result, f"landlock_add_rule {path}")
        finally:
            os.close(fd)

    def add_port_rule(self, ruleset_fd: int, port: int, access: int) -> None:
        attr = _NetPortAttr(access, port)
        self._check(
            self._libc.syscall(
                SYS_LANDLOCK_ADD_RULE, _long(ruleset_fd), _long(LANDLOCK_RULE_NET_PORT), ctypes.byref(attr), _long(0)
            ),
            f"landlock_add_rule port {port}",
        )

    def no_new_privs(self) -> None:
        args = (_long(1), _long(0), _long(0), _long(0))
        self._check(self._libc.prctl(PR_SET_NO_NEW_PRIVS, *args), "prctl(PR_SET_NO_NEW_PRIVS)")

    def restrict_self(self, ruleset_fd: int) -> None:
        self._check(
            self._libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, _long(ruleset_fd), _long(0)), "landlock_restrict_self"
        )

    def close(self, fd: int) -> None:
        os.close(fd)

    @staticmethod
    def _check(result: int, what: str) -> int:
        if result < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"{what}: {os.strerror(errno)}")
        return int(result)


def apply(layer: Plan, kernel: Kernel) -> None:
    """Install one Landlock layer on the calling thread (layers stack: the result is their intersection)."""
    ruleset = kernel.create_ruleset(layer.handled_fs, layer.handled_net)
    try:
        for rule in layer.paths:
            kernel.add_path_rule(ruleset, rule.path, rule.access)
        for port in layer.ports:
            kernel.add_port_rule(ruleset, port, NET_CONNECT_TCP)
        kernel.no_new_privs()
        kernel.restrict_self(ruleset)
    finally:
        kernel.close(ruleset)


def main(
    argv: Sequence[str] | None = None,
    *,
    kernel: Kernel | None = None,
    execv: Callable[[str, list[str]], NoReturn] = os.execv,
    stderr: IO[str] | None = None,
) -> int:
    """Apply the session ruleset and exec the command; returns 126 on any failure (``execv`` does not return)."""
    err = stderr if stderr is not None else sys.stderr
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
        kern = kernel if kernel is not None else Kernel()
        abi = kern.abi()
        if abi < MIN_ABI:
            raise LauncherError(f"Landlock ABI {abi} < {MIN_ABI} (filesystem and network rules need ABI 4)")
        apply(plan(options, abi), kern)
        execv(options.command[0], list(options.command))
    except (LauncherError, OSError, UnicodeDecodeError) as exc:
        err.write(f"landlock: {exc}\n{USAGE if isinstance(exc, LauncherError) else ''}\n")
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())

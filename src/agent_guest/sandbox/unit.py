"""Transient systemd unit of a session sandbox (docs/platform/spec/05-runner.md section 4.3, ADR-008).

Builds the ``systemd-run`` argument list with every property of the table in 05 section 4.3: identity, the
allow-listed environment of 05 section 10.3 (nothing inherited), filesystem, kernel/process and system-call
hardening, network, the exec allow-list (``exec_paths`` recorded at golden build) and the resources of a main or
subagent session. The command inside the unit is the Landlock launcher (``landlock.py``) followed by the Claude
binary. The result is deterministic for equal input and contains no shell syntax (GST-02 quotes it for tmux).

Interface:

* ``SessionSpec`` - the per-session input (session id, token, model environment, kind, interpreter and exec paths)
* ``build(spec, claude_args) -> list[str]`` - ``systemd-run --pty --unit=agent-session-<id> --collect …`` for
  the session start (GST-02)
* ``systemd_run(spec, command, *, unit_name, mode="pty", extra_properties=()) -> list[str]`` - same properties
  around any command; ``mode="pipe"`` adds ``--pipe --wait`` (self-test probe)
* ``launcher_args(spec) -> list[str]`` - the session options of the launcher (also the probe's second layer)
* ``launcher_command(spec, program, *, extra_exec=(), extra_ro=(), extra_rw=(), extra_connect=()) -> list[str]`` -
  the launcher invocation; the extras exist for the self-test probe and tests only
* ``properties(spec) -> list[str]`` - the ``-p``/``--setenv`` arguments
* ``user_name(session_id) -> str`` - ``s-`` and the 30 hex digits of the session id without its fixed version and
  variant digits
* ``interpreter_paths(python="/usr/bin/python3", lib_root="/usr/lib") -> tuple[str, str]`` - realpath of the
  interpreter and its ``lib-dynload`` directory
* ``load_exec_paths(exec_paths_file=EXEC_PATHS_FILE, build_file=BUILD_FILE) -> tuple[str, ...]`` - the exec paths
  of the golden image, checked against ``exec_paths_sha256`` of ``build.json`` (SHA-256 hex of the canonical JSON of
  the sorted list, 04-crypto.md section 1)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

SESSIONS_ROOT = "/srv/sessions"
HARNESS_DIR = "/run/agent-guest/harness"
RUN_DIR = "/run"
CLAUDE_BINARY = "/opt/claude/claude"
LAUNCHER = "/opt/agent-guest/agent_guest/sandbox/landlock.py"
EXEC_PATHS_FILE = "/opt/agent-guest/exec-paths.json"
BUILD_FILE = "/opt/agent-guest/build.json"
PLATFORM_PORT = 7700
UNIT_PREFIX = "agent-session-"

# 05 section 10.3: always set, independent of the model access.
FIXED_MODEL_ENV = (
    ("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{PLATFORM_PORT}/model"),
    ("ANTHROPIC_API_KEY", ""),
    ("CLAUDE_CODE_MAX_RETRIES", "3"),
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
    ("DISABLE_TELEMETRY", "1"),
    ("DISABLE_ERROR_REPORTING", "1"),
    ("DISABLE_AUTOUPDATER", "1"),
)
# 05 section 10.3: chosen by the runner per model access (platform model, catalog limits).
MODEL_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
)
SESSION_ENV = (("TERM", "xterm-256color"), ("LANG", "C.UTF-8"), ("PATH", "/usr/bin:/bin"))

RESOURCES = ("MemoryMax=4G", "CPUQuota=200%", "TasksMax=256")
RUNTIME_MAX = {"main": "8h", "subagent": "30min"}

LANDLOCK_SYSCALLS = "landlock_create_ruleset landlock_add_rule landlock_restrict_self"
DENIED_SYSCALL_SETS = "@privileged @mount @debug @module @reboot @swap @obsolete @cpu-emulation"
# ADR-064 section 4: no socket of a session can get a name another session could reach (abstract unix sockets share the
# VM's network namespace); io_uring could bind and listen past this filter on newer kernels. EPERM, not a kill: a
# program that probes for these calls goes on.
REFUSED_SYSCALLS = "bind:EPERM listen:EPERM io_uring_setup:EPERM io_uring_enter:EPERM io_uring_register:EPERM"

# Values end up in space-separated systemd lists or are parsed by systemd: plain absolute paths only.
_PATH_RE = re.compile(r"/[A-Za-z0-9._+@/-]*")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")
_ENV_VALUE_RE = re.compile(r"[\x20-\x7e]*")


class UnitError(ValueError):
    """Input that cannot be turned into a safe unit definition."""


@dataclass(frozen=True)
class SessionSpec:
    """Input of one session unit.

    ``session_id`` is the canonical (lower-case, hyphenated) UUID; ``token`` the session token (b64u);
    ``model_env`` only keys of ``MODEL_ENV_KEYS``; ``python`` the resolved interpreter path and ``lib_dynload``
    its extension directory (``interpreter_paths``); ``exec_paths`` the shared objects of ``load_exec_paths``.
    """

    session_id: str
    token: str
    python: str
    lib_dynload: str
    exec_paths: Sequence[str]
    kind: Literal["main", "subagent"] = "main"
    model_env: Mapping[str, str] = field(default_factory=dict)
    claude: str = CLAUDE_BINARY
    launcher: str = LAUNCHER


def user_name(session_id: str) -> str:
    """Name of the session user (32 characters, the ``useradd`` limit): ``s-`` and the hex digits of the session id
    without digit 13 (the UUID version) and digit 17 (variant bits). The first 12 digits alone are the UUIDv7
    millisecond, which two sessions started in the same millisecond share."""
    digits = _session_uuid(session_id).hex
    return "s-" + digits[:12] + digits[13:16] + digits[17:]


def _session_uuid(session_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(session_id)
    except ValueError as exc:
        raise UnitError(f"session id is not a UUID: {session_id!r}") from exc
    if str(parsed) != session_id:
        raise UnitError(f"session id is not canonical: {session_id!r}")
    return parsed


def _path(value: str) -> str:
    if not _PATH_RE.fullmatch(value) or "/../" in value + "/" or "/./" in value + "/" or "//" in value:
        raise UnitError(f"unsupported path: {value!r}")
    return value


def session_dir(session_id: str) -> str:
    return f"{SESSIONS_ROOT}/{_session_uuid(session_id)}"


def harness_file(session_id: str) -> str:
    return f"{HARNESS_DIR}/{_session_uuid(session_id)}.md"


def environment(spec: SessionSpec) -> list[tuple[str, str]]:
    """The complete unit environment (05 sections 4.3 and 10.3), sorted by name."""
    if not _TOKEN_RE.fullmatch(spec.token):
        raise UnitError("session token must be 16-256 b64u characters")
    unknown = sorted(set(spec.model_env) - MODEL_ENV_KEYS)
    if unknown:
        raise UnitError(f"environment not on the allow-list: {', '.join(unknown)}")
    for key, value in spec.model_env.items():
        if not value or not _ENV_VALUE_RE.fullmatch(value):
            raise UnitError(f"invalid value for {key}")
    base = session_dir(spec.session_id)
    env = dict(FIXED_MODEL_ENV)
    env.update(spec.model_env)
    env.update(SESSION_ENV)
    env.update(
        HOME=f"{base}/home",
        CLAUDE_CONFIG_DIR=f"{base}/home/.claude",
        PLATFORM_SESSION_TOKEN=spec.token,
        ANTHROPIC_AUTH_TOKEN=spec.token,
    )
    return sorted(env.items())


def exec_allow_list(spec: SessionSpec) -> list[str]:
    """``ExecPaths=``: the Claude binary, the interpreter, ``lib-dynload`` and the golden's exec paths."""
    head = [_path(spec.claude), _path(spec.python), _path(spec.lib_dynload)]
    tail = sorted({_path(path) for path in spec.exec_paths} - set(head))
    return head + tail


def properties(spec: SessionSpec) -> list[str]:
    """All unit properties of 05 section 4.3 as ``systemd-run`` arguments, in table order."""
    if spec.kind not in RUNTIME_MAX:
        raise UnitError(f"unknown session kind {spec.kind!r}")
    user = user_name(spec.session_id)
    base = session_dir(spec.session_id)
    props = [
        # Identity
        f"User={user}",
        f"Group={user}",
        f"WorkingDirectory={base}/work",
        "UMask=0077",
        # Filesystem
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateDevices=yes",
        "PrivateTmp=no",
        "TemporaryFileSystem=/tmp:rw,noexec,nosuid,nodev,mode=1777,size=512M",
        f"TemporaryFileSystem={SESSIONS_ROOT}",
        f"BindPaths={base}",
        # Beyond 05 section 4.3 (ATS-03, ADR-045): an empty, read-only /run instead of a tmpfs on the harness directory
        # only; every unix socket below /run (the resolver's varlink API - DNS past the egress rules -, socket
        # activation of root services, journald) is gone, only the own harness file is bound in.
        f"TemporaryFileSystem={RUN_DIR}:ro,nosuid,nodev,noexec,mode=0755",
        f"BindReadOnlyPaths={harness_file(spec.session_id)}",
        "InaccessiblePaths=/etc/agent-guest -/var/lib/agent-guest -/run/agent-guest/control.sock "
        "-/run/agent-guest/api.sock",
        # Beyond the table of 05 section 4.3 (decision SBX-02 no. 24): the system bus, systemd's private socket and
        # the transient unit files expose every unit's environment (other sessions' tokens); Landlock cannot refuse a
        # UNIX connect.
        "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private -/run/systemd/transient",
        # Kernel and processes
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictNamespaces=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "PrivateIPC=yes",
        "KeyringMode=private",
        "CapabilityBoundingSet=",
        "SystemCallArchitectures=native",
        # System calls
        f"SystemCallFilter=@system-service {LANDLOCK_SYSCALLS}",
        f"SystemCallFilter=~{DENIED_SYSCALL_SETS}",
        f"SystemCallFilter=~{REFUSED_SYSCALLS}",
        # Network
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        # Exec
        "NoExecPaths=/",
        "ExecPaths=" + " ".join(exec_allow_list(spec)),
        # Resources
        *RESOURCES,
        # IO of the session counted from its first byte (08-monitoring.md section 3; KOR-02, AUD-01 B-17): without it
        # `io.stat` exists only after the metrics call switched accounting on, up to one sampling interval later.
        "IOAccounting=yes",
        f"RuntimeMaxSec={RUNTIME_MAX[spec.kind]}",
    ]
    args: list[str] = []
    for prop in props:
        args += ["-p", prop]
    for key, value in environment(spec):
        args.append(f"--setenv={key}={value}")
    return args


def launcher_args(spec: SessionSpec) -> list[str]:
    """The launcher options of a session (05 section 4.3), without program; also the probe's second layer."""
    return [
        *["--session-dir", session_dir(spec.session_id), "--harness", harness_file(spec.session_id)],
        *["--exec", _path(spec.claude), "--connect", str(PLATFORM_PORT)],
    ]


def launcher_command(
    spec: SessionSpec,
    program: Sequence[str],
    *,
    extra_exec: Sequence[str] = (),
    extra_ro: Sequence[str] = (),
    extra_rw: Sequence[str] = (),
    extra_connect: Sequence[int] = (),
) -> list[str]:
    """``python3 -I landlock.py <rules of this session> -- <program>``."""
    if not program:
        raise UnitError("empty program")
    args = [_path(spec.python), "-I", _path(spec.launcher), *launcher_args(spec)]
    for path in extra_exec:
        args += ["--exec", _path(path)]
    for port in extra_connect:
        args += ["--connect", str(int(port))]
    for path in extra_ro:
        args += ["--ro", _path(path)]
    for path in extra_rw:
        args += ["--rw", _path(path)]
    return [*args, "--", *program]


def systemd_run(
    spec: SessionSpec,
    command: Sequence[str],
    *,
    unit_name: str,
    mode: Literal["pty", "pipe"] = "pty",
    extra_properties: Sequence[str] = (),
) -> list[str]:
    """``systemd-run`` with all session properties around ``command`` (``mode="pipe"`` waits for the result)."""
    if not re.fullmatch(r"[a-z0-9-]{1,200}", unit_name):
        raise UnitError(f"invalid unit name {unit_name!r}")
    if mode == "pty":
        head = ["systemd-run", "--pty", f"--unit={unit_name}", "--collect", "--quiet"]
    elif mode == "pipe":
        head = ["systemd-run", "--pipe", "--wait", f"--unit={unit_name}", "--collect", "--quiet"]
    else:
        raise UnitError(f"unknown mode {mode!r}")
    extra: list[str] = []
    for prop in extra_properties:
        extra += ["-p", prop]
    return [*head, *properties(spec), *extra, "--", *command]


def build(spec: SessionSpec, claude_args: Sequence[str]) -> list[str]:
    """The session start command: the unit ``agent-session-<id>`` running Claude Code inside the launcher."""
    command = launcher_command(spec, [spec.claude, *claude_args])
    return systemd_run(spec, command, unit_name=UNIT_PREFIX + str(_session_uuid(spec.session_id)))


def interpreter_paths(python: str = "/usr/bin/python3", lib_root: str = "/usr/lib") -> tuple[str, str]:
    """Resolved interpreter and its ``lib-dynload`` directory (Debian layout ``<lib_root>/pythonX.Y``)."""
    real = os.path.realpath(python)
    name = os.path.basename(real)
    if not re.fullmatch(r"python3\.\d+", name):
        raise UnitError(f"unexpected interpreter {real!r}")
    dynload = os.path.join(lib_root, name, "lib-dynload")
    if not os.path.isdir(dynload):
        raise UnitError(f"missing {dynload}")
    return real, dynload


def canonical_json(value: object) -> bytes:
    """Canonical JSON of 04-crypto.md section 1 (sorted keys, no whitespace, printable ASCII only)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if not all(0x20 <= ord(char) <= 0x7E for char in text) or "\\u" in text:
        raise UnitError("non_ascii")
    return text.encode("ascii")


def exec_paths_sha256(paths: Sequence[str]) -> str:
    """``exec_paths_sha256`` of ``build.json``: SHA-256 hex of the canonical JSON of the path list."""
    return hashlib.sha256(canonical_json(list(paths))).hexdigest()


def load_exec_paths(exec_paths_file: str = EXEC_PATHS_FILE, build_file: str = BUILD_FILE) -> tuple[str, ...]:
    """Exec paths of the golden image; refused unless sorted, unique, absolute and matching ``build.json``."""
    with open(exec_paths_file, encoding="utf-8") as fh:
        paths = json.load(fh)
    with open(build_file, encoding="utf-8") as fh:
        build_info = json.load(fh)
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise UnitError(f"{exec_paths_file}: expected a list of paths")
    if paths != sorted(set(paths)):
        raise UnitError(f"{exec_paths_file}: paths must be sorted and unique")
    for path in paths:
        _path(path)
    expected = build_info.get("exec_paths_sha256") if isinstance(build_info, dict) else None
    if expected != exec_paths_sha256(paths):
        raise UnitError(f"{exec_paths_file}: does not match exec_paths_sha256 of {build_file}")
    return tuple(paths)

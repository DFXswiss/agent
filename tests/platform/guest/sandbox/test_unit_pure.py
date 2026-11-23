"""U-GST-sandbox: systemd-run properties of a session unit (05-runner.md sections 4.3 and 10.3)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from agent_guest.sandbox import unit

SID = "0190a6c4-5b7e-7c3d-9f1a-2b3c4d5e6f70"
TOKEN = "A" * 43
PY = "/usr/bin/python3.12"
DYNLOAD = "/usr/lib/python3.12/lib-dynload"
LIBS = ("/usr/lib/aarch64-linux-gnu/libc.so.6", "/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1")


def spec(**changes: object) -> unit.SessionSpec:
    base = unit.SessionSpec(session_id=SID, token=TOKEN, python=PY, lib_dynload=DYNLOAD, exec_paths=LIBS)
    return replace(base, **changes)  # type: ignore[arg-type]


def props(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]


def env(argv: list[str]) -> dict[str, str]:
    pairs = [arg.removeprefix("--setenv=").split("=", 1) for arg in argv if arg.startswith("--setenv=")]
    return {pair[0]: pair[1] for pair in pairs}


EXPECTED_PROPERTIES = [
    "User=s-0190a6c45b7ec3df1a2b3c4d5e6f70",
    "Group=s-0190a6c45b7ec3df1a2b3c4d5e6f70",
    f"WorkingDirectory=/srv/sessions/{SID}/work",
    "UMask=0077",
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateDevices=yes",
    "PrivateTmp=no",
    "TemporaryFileSystem=/tmp:rw,noexec,nosuid,nodev,mode=1777,size=512M",
    "TemporaryFileSystem=/srv/sessions",
    f"BindPaths=/srv/sessions/{SID}",
    "TemporaryFileSystem=/run:ro,nosuid,nodev,noexec,mode=0755",
    f"BindReadOnlyPaths=/run/agent-guest/harness/{SID}.md",
    "InaccessiblePaths=/etc/agent-guest -/var/lib/agent-guest -/run/agent-guest/control.sock "
    "-/run/agent-guest/api.sock",
    "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private -/run/systemd/transient",
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
    "SystemCallFilter=@system-service landlock_create_ruleset landlock_add_rule landlock_restrict_self",
    "SystemCallFilter=~@privileged @mount @debug @module @reboot @swap @obsolete @cpu-emulation",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "IPAddressDeny=any",
    "IPAddressAllow=localhost",
    "NoExecPaths=/",
    f"ExecPaths=/opt/claude/claude {PY} {DYNLOAD} {LIBS[1]} {LIBS[0]}",
    "MemoryMax=4G",
    "CPUQuota=200%",
    "TasksMax=256",
    "IOAccounting=yes",
    "RuntimeMaxSec=8h",
]


def test_build_contains_every_property_of_the_table() -> None:
    argv = unit.build(spec(), ["--restricted", "hello world"])
    assert argv[:5] == ["systemd-run", "--pty", f"--unit=agent-session-{SID}", "--collect", "--quiet"]
    assert props(argv) == EXPECTED_PROPERTIES
    tail = argv[argv.index("--") + 1 :]
    assert tail == [
        PY,
        "-I",
        "/opt/agent-guest/agent_guest/sandbox/landlock.py",
        "--session-dir",
        f"/srv/sessions/{SID}",
        "--harness",
        f"/run/agent-guest/harness/{SID}.md",
        "--exec",
        "/opt/claude/claude",
        "--connect",
        "7700",
        "--",
        "/opt/claude/claude",
        "--restricted",
        "hello world",
    ]


def test_build_is_deterministic() -> None:
    assert unit.build(spec(), ["x"]) == unit.build(spec(exec_paths=tuple(reversed(LIBS))), ["x"])


def test_environment_is_exactly_the_allow_list() -> None:
    argv = unit.build(
        spec(model_env={"ANTHROPIC_MODEL": "deepseek/deepseek-v4", "CLAUDE_CODE_SUBAGENT_MODEL": "m"}), []
    )
    assert env(argv) == {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:7700/model",
        "ANTHROPIC_MODEL": "deepseek/deepseek-v4",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_RETRIES": "3",
        "CLAUDE_CODE_SUBAGENT_MODEL": "m",
        "CLAUDE_CONFIG_DIR": f"/srv/sessions/{SID}/home/.claude",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
        "HOME": f"/srv/sessions/{SID}/home",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PLATFORM_SESSION_TOKEN": TOKEN,
        "TERM": "xterm-256color",
    }
    keys = [arg.split("=", 2)[0] for arg in argv if arg.startswith("--setenv=")]
    assert keys == sorted(keys)


def test_subagent_sessions_end_after_30_minutes() -> None:
    assert props(unit.build(spec(kind="subagent"), []))[-1] == "RuntimeMaxSec=30min"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_env": {"LD_PRELOAD": "/tmp/x.so"}}, "not on the allow-list: LD_PRELOAD"),
        ({"model_env": {"ANTHROPIC_BASE_URL": "http://evil"}}, "not on the allow-list: ANTHROPIC_BASE_URL"),
        ({"model_env": {"ANTHROPIC_MODEL": ""}}, "invalid value for ANTHROPIC_MODEL"),
        ({"model_env": {"ANTHROPIC_MODEL": "a\nb"}}, "invalid value for ANTHROPIC_MODEL"),
        ({"token": "short"}, "session token"),
        ({"token": "A" * 42 + "="}, "session token"),
        ({"kind": "root"}, "unknown session kind"),
        ({"session_id": "not-a-uuid"}, "not a UUID"),
        ({"session_id": SID.upper()}, "not canonical"),
        ({"session_id": SID.replace("-", "")}, "not canonical"),
        ({"exec_paths": ("/usr/lib/with space.so",)}, "unsupported path"),
        ({"exec_paths": ("relative.so",)}, "unsupported path"),
        ({"exec_paths": ("/usr/lib/../../etc",)}, "unsupported path"),
        ({"exec_paths": ("/usr/lib/..",)}, "unsupported path"),
        ({"exec_paths": ("/usr/./lib",)}, "unsupported path"),
        ({"exec_paths": ("/usr//lib",)}, "unsupported path"),
        ({"python": "/usr/bin/python3;rm"}, "unsupported path"),
    ],
)
def test_build_refuses_unsafe_input(changes: dict[str, object], message: str) -> None:
    with pytest.raises(unit.UnitError, match=message):
        unit.build(spec(**changes), [])


def test_exec_allow_list_drops_duplicates_of_the_fixed_entries() -> None:
    allowed = unit.exec_allow_list(spec(exec_paths=(PY, LIBS[0], LIBS[0])))
    assert allowed == ["/opt/claude/claude", PY, DYNLOAD, LIBS[0]]


def test_launcher_command_with_extras() -> None:
    command = unit.launcher_command(
        spec(),
        ["/usr/bin/python3.12", "-I", "/p.py"],
        extra_exec=[PY],
        extra_ro=["/src"],
        extra_rw=["/cov"],
        extra_connect=[1],
    )
    assert command[3:] == [
        *unit.launcher_args(spec()),
        *["--exec", PY, "--connect", "1", "--ro", "/src", "--rw", "/cov", "--", "/usr/bin/python3.12", "-I", "/p.py"],
    ]
    assert unit.launcher_args(spec()) == [
        *["--session-dir", f"/srv/sessions/{SID}", "--harness", f"/run/agent-guest/harness/{SID}.md"],
        *["--exec", "/opt/claude/claude", "--connect", "7700"],
    ]


def test_launcher_command_needs_a_program() -> None:
    with pytest.raises(unit.UnitError, match="empty program"):
        unit.launcher_command(spec(), [])


def test_systemd_run_pipe_mode_and_extra_properties() -> None:
    argv = unit.systemd_run(spec(), ["/bin/true"], unit_name="agent-selftest-1", mode="pipe", extra_properties=["X=1"])
    assert argv[:6] == ["systemd-run", "--pipe", "--wait", "--unit=agent-selftest-1", "--collect", "--quiet"]
    assert props(argv)[-1] == "X=1"
    assert argv[-2:] == ["--", "/bin/true"]


@pytest.mark.parametrize(("name", "mode"), [("Bad Name", "pty"), ("ok", "tty")])
def test_systemd_run_refuses_bad_names_and_modes(name: str, mode: str) -> None:
    with pytest.raises(unit.UnitError):
        unit.systemd_run(spec(), ["/bin/true"], unit_name=name, mode=mode)  # type: ignore[arg-type]


def test_user_name_and_paths() -> None:
    assert unit.user_name(SID) == "s-0190a6c45b7ec3df1a2b3c4d5e6f70"
    assert unit.session_dir(SID) == f"/srv/sessions/{SID}"
    assert unit.harness_file(SID) == f"/run/agent-guest/harness/{SID}.md"


def test_user_names_of_sessions_started_in_the_same_millisecond_differ() -> None:
    # Two UUIDv7 of one millisecond share the first 12 hex digits (ACC-M0 open point).
    first, second = SID, "0190a6c4-5b7e-7c3d-9f1a-2b3c4d5e6f71"
    names = {unit.user_name(first), unit.user_name(second), unit.user_name("0190a6c4-5b7e-7c3e-9f1a-2b3c4d5e6f70")}
    assert len(names) == 3
    assert all(len(name) <= 32 and re.fullmatch(r"s-[0-9a-f]{30}", name) for name in names)


def test_interpreter_paths(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    real = tmp_path / "bin" / "python3.12"
    real.write_text("")
    (tmp_path / "bin" / "python3").symlink_to(real)
    (tmp_path / "lib" / "python3.12" / "lib-dynload").mkdir(parents=True)
    resolved, dynload = unit.interpreter_paths(str(tmp_path / "bin" / "python3"), str(tmp_path / "lib"))
    assert resolved == str(real.resolve())
    assert dynload == str(tmp_path / "lib" / "python3.12" / "lib-dynload")


def test_interpreter_paths_refuses_unknown_layouts(tmp_path: Path) -> None:
    (tmp_path / "python3.12").write_text("")
    (tmp_path / "pypy").write_text("")
    with pytest.raises(unit.UnitError, match="unexpected interpreter"):
        unit.interpreter_paths(str(tmp_path / "pypy"), str(tmp_path))
    with pytest.raises(unit.UnitError, match="missing"):
        unit.interpreter_paths(str(tmp_path / "python3.12"), str(tmp_path))


def test_canonical_json_and_hash() -> None:
    assert unit.canonical_json({"b": [1, "x"], "a": None}) == b'{"a":null,"b":[1,"x"]}'
    expected = hashlib.sha256(b'["/a","/b"]').hexdigest()
    assert unit.exec_paths_sha256(["/a", "/b"]) == expected
    with pytest.raises(unit.UnitError, match="non_ascii"):
        unit.canonical_json(["ä"])


def write_build(tmp_path: Path, paths: object, digest: object) -> tuple[str, str]:
    exec_file = tmp_path / "exec-paths.json"
    build_file = tmp_path / "build.json"
    exec_file.write_text(json.dumps(paths))
    build_file.write_text(json.dumps({"exec_paths_sha256": digest} if digest is not None else []))
    return str(exec_file), str(build_file)


def test_load_exec_paths(tmp_path: Path) -> None:
    paths = sorted(LIBS)
    assert unit.load_exec_paths(*write_build(tmp_path, paths, unit.exec_paths_sha256(paths))) == tuple(paths)


@pytest.mark.parametrize(
    ("paths", "digest", "message"),
    [
        ({"a": 1}, "x", "expected a list"),
        ([1], "x", "expected a list"),
        (["/b", "/a"], "x", "sorted and unique"),
        (["/a", "/a"], "x", "sorted and unique"),
        (["/a b"], "x", "unsupported path"),
        (["/a"], "0" * 64, "does not match"),
        (["/a"], None, "does not match"),
    ],
)
def test_load_exec_paths_refuses(tmp_path: Path, paths: object, digest: object, message: str) -> None:
    with pytest.raises(unit.UnitError, match=message):
        unit.load_exec_paths(*write_build(tmp_path, paths, digest))

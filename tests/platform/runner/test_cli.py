"""U-RUN CLI (05-runner.md section 1.2, ADR-027 section 4): discovery, output formats, exit codes, `run`,
`service`, `uninstall`.

The daemon commands run in process against the real test PostgreSQL (the cluster helpers are injected so that the
store is a fresh test database); one test starts the real `agent-runner run` with its own `initdb` cluster and
stops it with SIGTERM.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_cli.pg import cluster_running
from agent_runner import cli
from agent_runner.__main__ import main
from agent_runner.cli import Output, run_cli
from agent_runner.errors import RunnerError
from agent_runner.identity import Device, generate_keys, load_identity, write_device, write_keys
from agent_runner.store.cluster import ClusterTools
from agent_runner.store.migrate import load_migrations

SECRET = "sk-or-v1-" + "0" * 8 + "-not-a-real-value"

STOP_TASK = """
import asyncio, os, signal
from agent_runner.registry import TaskSpec

async def run(context):
    async with context.pool.connection() as conn:
        cursor = await conn.execute("SELECT count(*) FROM runner.schema_version")
        row = await cursor.fetchone()
    marker = context.host.env["MARKER"]
    with open(marker, "a", encoding="utf-8") as handle:
        enrolled = context.identity is not None
        same_dsn = context.dsn == context.host.env["DSN"]
        handle.write(f"task versions={row[0]} enrolled={enrolled} dsn={same_dsn}\\n")
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.Event().wait()

TASKS = [TaskSpec("probe", run)]
"""

FLUSH_HOOK = """
from agent_runner.registry import ShutdownHook

async def flush(context):
    with open(context.host.env["MARKER"], "a", encoding="utf-8") as handle:
        handle.write(f"flush stopping={context.stopping.is_set()}\\n")

HOOKS = [ShutdownHook("outbox-flush", flush)]
"""

FAILING_TASK = """
from agent_runner.registry import TaskSpec

async def run(context):
    raise RuntimeError("boom")

TASKS = [TaskSpec("failing", run)]
"""

CLEANER = """
from agent_runner.registry import Cleaner

def clean(context):
    with open(context.host.env["MARKER"], "a", encoding="utf-8") as handle:
        handle.write(f"clean dsn={{context.dsn is not None}} home={{context.config.home.is_dir()}}\\n")
    if context.host.env.get("FAIL_CLEANER"):
        raise RuntimeError("secret failure text")

CLEANERS = [Cleaner("{name}", clean)]
"""

HELLO_CLI = """
def hello(args, ctx):
    ctx.out.fact("hello", {"name": args.name, "ok": True, "count": 2, "note": "two words", "empty": ""})
    return 1 if args.name == "fail" else 0

def register(subparsers):
    parser = subparsers.add_parser("hello")
    parser.add_argument("name")
    parser.set_defaults(handler=hello)
"""

# the result line of the first migration run of a fresh store (every numbered migration applied)
APPLIED = f"applied {len(load_migrations())}"


def device(kind: str = "org") -> Device:
    return Device(
        device_id="01900000-0000-7000-8000-000000000001",
        kind=kind,  # type: ignore[arg-type]
        hub_url="https://hub.example.org",
        hub_key="A" * 43,
        enrolled_at="2026-11-21T12:00:00.000Z",
    )


def enroll(home: Path, kind: str = "org") -> None:
    write_keys(home, generate_keys())
    write_device(home, device(kind))


@pytest.fixture
def store_tools(runner_pg_admin: str, runner_dsn: str) -> tuple[ClusterTools, list[str]]:
    """Cluster helpers that hand out the per-test database and record start/stop."""
    calls: list[str] = []

    def ensure(path: Path) -> str:
        calls.append(f"ensure {path.name}")
        (path / "data").mkdir(parents=True, exist_ok=True)
        return runner_pg_admin

    def create_database(admin: str, name: str) -> str:
        calls.append(f"create {name}")
        assert admin == runner_pg_admin
        return runner_dsn

    def stop(path: Path) -> None:
        calls.append(f"stop {path.name}")

    def exists(path: Path) -> bool:
        return (path / "data").is_dir()

    return ClusterTools(ensure=ensure, create_database=create_database, stop=stop, exists=exists), calls


# --- parser, output, exit codes ---------------------------------------------------------------------------------


def test_help_without_command(make_host: Callable[..., Any]) -> None:
    captured = make_host()
    assert run_cli([], captured.host) == 0
    text = captured.stdout.getvalue()
    assert text.startswith("usage: agent-runner")
    for command in ("run", "service", "uninstall"):
        assert command in text


def test_main_reads_sys_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["agent-runner"])
    assert main() == 0
    assert "usage: agent-runner" in capsys.readouterr().out


def test_version(make_host: Callable[..., Any], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        run_cli(["--version"], make_host().host)
    assert stop.value.code == 0
    assert capsys.readouterr().out == "agent-runner 0.1.0\n"


@pytest.mark.parametrize(
    "argv",
    [["nope"], ["service"], ["service", "restart"], ["uninstall", "--force"], ["--json", "run", "extra"]],
)
def test_usage_errors_exit_2(argv: list[str], make_host: Callable[..., Any]) -> None:
    captured = make_host()
    assert run_cli(argv, captured.host) == 2
    err = captured.stderr.getvalue()
    assert err.startswith("usage: agent-runner")
    assert "agent-runner: error:" in err
    assert captured.stdout.getvalue() == ""


def test_config_error_text(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    env = env_for(runner_home, AGENT_RUNNER_HUB_URL="ftp://" + SECRET)
    captured = make_host(env)
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=config_invalid name=HUB_URL\n"
    assert captured.stdout.getvalue() == ""
    assert not runner_home.exists()


def test_config_error_json(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    env = env_for(runner_home, AGENT_RUNNER_VM_CPUS=SECRET)
    captured = make_host(env)
    assert run_cli(["run", "--json"], captured.host) == 1
    document = json.loads(captured.stdout.getvalue())
    assert document == {"ok": False, "facts": [], "error": {"code": "config_invalid", "name": "VM_CPUS"}}
    assert captured.stderr.getvalue() == ""
    assert SECRET not in captured.stdout.getvalue()


def test_unsupported_os(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    captured = make_host(env_for(runner_home), platform="linux")
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=unsupported_os\n"


def test_json_flag_after_separator_is_an_argument(
    area_package: Callable[[dict[str, str]], str], make_host: Callable[..., Any]
) -> None:
    pkg = area_package({"greet/cli.py": HELLO_CLI})
    captured = make_host(package=pkg)
    assert run_cli(["hello", "--", "--json"], captured.host) == 0
    assert captured.stdout.getvalue().startswith("runner.hello name=--json ")


def test_discovered_command_text_output(
    area_package: Callable[[dict[str, str]], str], make_host: Callable[..., Any]
) -> None:
    pkg = area_package({"greet/cli.py": HELLO_CLI})
    captured = make_host(package=pkg)
    assert run_cli(["hello", "world"], captured.host) == 0
    assert captured.stdout.getvalue() == 'runner.hello name=world ok=yes count=2 note="two words" empty=""\n'
    failing = make_host(package=pkg)
    assert run_cli(["hello", "fail"], failing.host) == 1


def test_discovered_command_json_output(
    area_package: Callable[[dict[str, str]], str], make_host: Callable[..., Any]
) -> None:
    pkg = area_package({"greet/cli.py": HELLO_CLI})
    captured = make_host(package=pkg)
    assert run_cli(["--json", "hello", "world"], captured.host) == 0
    document = json.loads(captured.stdout.getvalue())
    assert document == {
        "ok": True,
        "facts": [{"fact": "runner.hello", "name": "world", "ok": True, "count": 2, "note": "two words", "empty": ""}],
    }
    failing = make_host(package=pkg)
    assert run_cli(["--json", "hello", "fail"], failing.host) == 1
    assert json.loads(failing.stdout.getvalue())["ok"] is False


def test_broken_registration_is_a_failure(
    area_package: Callable[[dict[str, str]], str], make_host: Callable[..., Any]
) -> None:
    pkg = area_package({"greet/cli.py": "register = 'not callable'\n"})
    captured = make_host(package=pkg)
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=registry_invalid name=greet.cli\n"


def test_output_rejects_bad_names(make_host: Callable[..., Any]) -> None:
    out = Output(host=make_host().host, json_mode=False)
    with pytest.raises(ValueError, match="identifiers"):
        out.fact("Bad", {})
    with pytest.raises(ValueError, match="identifiers"):
        out.fact("ok", {"Bad Key": 1})
    out.fact("bare", {})
    assert out.host.stdout.getvalue() == "runner.bare\n"  # type: ignore[attr-defined]


def test_output_quotes_special_values(make_host: Callable[..., Any]) -> None:
    out = Output(host=make_host().host, json_mode=False)
    out.fact("x", {"a": 'say "hi"', "b": "back\\slash", "c": False})
    assert out.host.stdout.getvalue() == 'runner.x a="say \\"hi\\"" b="back\\\\slash" c=no\n'  # type: ignore[attr-defined]


# --- service ----------------------------------------------------------------------------------------------------


def test_service_install_and_uninstall_commands(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    captured = make_host(env)
    assert run_cli(["service", "install", "--user", "svc"], captured.host) == 0
    plist = runner_home / "swiss.dfx.agent-runner.plist"
    assert captured.stdout.getvalue() == f"runner.service action=install label=swiss.dfx.agent-runner plist={plist}\n"
    assert plist.is_file()
    del env["AGENT_RUNNER_HUB_URL"]
    second = make_host(env)
    assert run_cli(["--json", "service", "uninstall"], second.host) == 0
    assert json.loads(second.stdout.getvalue()) == {
        "ok": True,
        "facts": [
            {"fact": "runner.service", "action": "uninstall", "label": "swiss.dfx.agent-runner", "removed": True}
        ],
    }
    assert fake_tools.calls == []


def test_service_install_requires_the_hub_settings(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    env = env_for(runner_home)
    del env["AGENT_RUNNER_PLATFORM_ROOT_KEY"]
    captured = make_host(env)
    assert run_cli(["service", "install"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=config_invalid name=PLATFORM_ROOT_KEY\n"


# --- uninstall --------------------------------------------------------------------------------------------------


def test_uninstall_without_store_and_identity(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    captured = make_host(env_for(runner_home))
    assert run_cli(["uninstall"], captured.host) == 0
    assert captured.stdout.getvalue() == "runner.uninstall service=absent cleaners=1 store=deleted identity=deleted\n"
    assert not runner_home.exists()


def populate(home: Path) -> None:
    enroll(home)
    for name in ("pg/data", "golden", "log", "tmp/op", "vm"):
        (home / name).mkdir(parents=True, mode=0o700)
    (home / "vm" / "vm-0000000a.token").write_text("t", encoding="utf-8")
    (home / "swiss.dfx.agent-runner.plist").write_text("p", encoding="utf-8")


def test_uninstall_keep_identity(
    make_host: Callable[..., Any],
    *,
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    area_package: Callable[[dict[str, str]], str],
    store_tools: tuple[ClusterTools, list[str]],
    tmp_path: Path,
) -> None:
    tools, calls = store_tools
    populate(runner_home)
    before = load_identity(runner_home)
    marker = tmp_path / "marker"
    pkg = area_package({"vm/uninstall.py": CLEANER.format(name="vms")})
    captured = make_host(env_for(runner_home, MARKER=str(marker)), cluster=tools, package=pkg)
    assert run_cli(["uninstall", "--keep-identity"], captured.host) == 0
    assert captured.stdout.getvalue() == "runner.uninstall service=removed cleaners=1 store=deleted identity=kept\n"
    assert marker.read_text(encoding="utf-8") == "clean dsn=True home=True\n"
    assert calls == ["ensure pg", "create agent_runner", "stop pg"]
    assert sorted(p.name for p in runner_home.iterdir()) == ["device.json", "keys"]
    assert load_identity(runner_home) == before


def test_uninstall_deletes_identity_and_home(
    make_host: Callable[..., Any],
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    store_tools: tuple[ClusterTools, list[str]],
) -> None:
    tools, _ = store_tools
    populate(runner_home)
    captured = make_host(env_for(runner_home), cluster=tools)
    assert run_cli(["--json", "uninstall"], captured.host) == 0
    assert json.loads(captured.stdout.getvalue())["facts"][0]["identity"] == "deleted"
    assert not runner_home.exists()


def test_uninstall_keeps_foreign_files_and_the_home(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, tmp_path: Path
) -> None:
    enroll(runner_home)
    (runner_home / "notes.txt").write_text("operator file", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k", encoding="utf-8")
    (runner_home / "log").symlink_to(outside)
    captured = make_host(env_for(runner_home))
    assert run_cli(["uninstall"], captured.host) == 0
    assert sorted(p.name for p in runner_home.iterdir()) == ["notes.txt"]
    assert (outside / "keep").exists()


def test_uninstall_cleaner_failure_keeps_the_store(
    make_host: Callable[..., Any],
    *,
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    area_package: Callable[[dict[str, str]], str],
    store_tools: tuple[ClusterTools, list[str]],
    tmp_path: Path,
) -> None:
    tools, calls = store_tools
    populate(runner_home)
    marker = tmp_path / "marker"
    pkg = area_package({"vm/uninstall.py": CLEANER.format(name="vms")})
    env = env_for(runner_home, MARKER=str(marker), FAIL_CLEANER="1")
    captured = make_host(env, cluster=tools, package=pkg)
    assert run_cli(["uninstall"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=uninstall_failed name=vms\n"
    assert "secret failure text" not in captured.stderr.getvalue()
    assert (runner_home / "pg" / "data").is_dir()
    assert load_identity(runner_home) is not None
    assert "stop pg" not in calls


# --- run --------------------------------------------------------------------------------------------------------


def run_env(env_for: Callable[..., dict[str, str]], home: Path, marker: Path, dsn: str, **extra: str) -> dict[str, str]:
    return env_for(home, MARKER=str(marker), DSN=dsn, **extra)


def test_run_until_sigterm(
    make_host: Callable[..., Any],
    *,
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    area_package: Callable[[dict[str, str]], str],
    store_tools: tuple[ClusterTools, list[str]],
    runner_dsn: str,
    tmp_path: Path,
) -> None:
    tools, calls = store_tools
    marker = tmp_path / "marker"
    (runner_home / "tmp").mkdir(parents=True, mode=0o700)
    os.chmod(runner_home, 0o700)
    (runner_home / "tmp" / "stale").write_text("old scratch", encoding="utf-8")
    pkg = area_package({"probe/tasks.py": STOP_TASK, "probe/shutdown.py": FLUSH_HOOK})
    captured = make_host(
        run_env(env_for, runner_home, marker, runner_dsn), cluster=tools, package=pkg, install_signals=True
    )
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert run_cli(["run"], captured.host) == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert captured.stdout.getvalue() == f'runner.migrate result="{APPLIED}"\nrunner.run tasks=1 enrolled=no\n'
    versions = len(load_migrations())
    expected = f"task versions={versions} enrolled=False dsn=True\nflush stopping=True\n"
    assert marker.read_text(encoding="utf-8") == expected
    assert calls == ["ensure pg", "create agent_runner", "stop pg"]
    assert list((runner_home / "tmp").iterdir()) == []
    for name in ("", "log", "tmp"):
        assert (runner_home / name).stat().st_mode & 0o777 == 0o700
    err = captured.stderr.getvalue()
    assert "runner.supervisor stop_requested" in err
    assert "runner.supervisor stopped" in err


def test_run_enrolled_json(
    make_host: Callable[..., Any],
    *,
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    area_package: Callable[[dict[str, str]], str],
    store_tools: tuple[ClusterTools, list[str]],
    runner_dsn: str,
    tmp_path: Path,
) -> None:
    tools, _ = store_tools
    enroll(runner_home)
    marker = tmp_path / "marker"
    pkg = area_package({"probe/tasks.py": STOP_TASK})
    captured = make_host(
        run_env(env_for, runner_home, marker, runner_dsn), cluster=tools, package=pkg, install_signals=True
    )
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert run_cli(["--json", "run"], captured.host) == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    document = json.loads(captured.stdout.getvalue())
    assert document["facts"] == [
        {"fact": "runner.migrate", "result": APPLIED},
        {"fact": "runner.run", "tasks": 1, "enrolled": "yes"},
    ]
    assert "enrolled=True" in marker.read_text(encoding="utf-8")


def test_run_restart_limit_exits_1(
    make_host: Callable[..., Any],
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    area_package: Callable[[dict[str, str]], str],
    store_tools: tuple[ClusterTools, list[str]],
) -> None:
    tools, calls = store_tools
    pkg = area_package({"bad/tasks.py": FAILING_TASK})

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    # A clock that stands still puts every restart into the same 60 s window.
    captured = make_host(env_for(runner_home), cluster=tools, package=pkg, clock=lambda: 0.0, sleep=no_wait)
    assert run_cli(["run"], captured.host) == 1
    assert "runner.supervisor restart_limit name=failing" in captured.stderr.getvalue()
    assert "boom" not in captured.stderr.getvalue()
    assert calls[-1] == "stop pg"


def test_run_org_device_refuses_openrouter_key(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, tmp_path: Path
) -> None:
    enroll(runner_home, "org")
    key = tmp_path / "or.key"
    key.write_text(SECRET, encoding="utf-8")
    os.chmod(key, 0o600)
    captured = make_host(env_for(runner_home, AGENT_RUNNER_OPENROUTER_KEY_FILE=str(key)))
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=config_invalid name=OPENROUTER_KEY_FILE\n"


def test_run_failed_migration(
    make_host: Callable[..., Any],
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    store_tools: tuple[ClusterTools, list[str]],
    runner_dsn: str,
) -> None:
    tools, calls = store_tools
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA runner")
        conn.execute("CREATE TABLE runner.scope_key (squatter int)")
    captured = make_host(env_for(runner_home), cluster=tools)
    assert run_cli(["run"], captured.host) == 1
    assert captured.stdout.getvalue() == 'runner.migrate result="failed 0001_runner"\n'
    assert captured.stderr.getvalue() == "runner.error code=migration_failed name=0001_runner\n"
    assert calls[-1] == "stop pg"


def test_run_identity_error(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    write_keys(runner_home, generate_keys())
    captured = make_host(env_for(runner_home))
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=identity_incomplete\n"


def test_cli_module_constants() -> None:
    assert (cli.EXIT_OK, cli.EXIT_FAILURE, cli.EXIT_USAGE) == (0, 1, 2)
    assert isinstance(RunnerError("x"), Exception)


# --- real process -----------------------------------------------------------------------------------------------


def test_real_daemon_process_stops_on_sigterm(env_for: Callable[..., dict[str, str]], runner_home: Path) -> None:
    """`python -m agent_runner run` with its own initdb cluster; SIGTERM ends it with exit 0 within 10 s."""
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    # The VM tasks (VM-02) run too: a prefix of its own keeps them away from any Lima instance of this machine.
    env["AGENT_RUNNER_VM_PREFIX"] = "wp-daemon-test-"
    for name, value in os.environ.items():
        if name in ("AGENT_PG_BIN", "HOME", "TMPDIR") or name.startswith("COVERAGE"):
            env[name] = value
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_runner", "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        lines: list[str] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line)
                if line.startswith("runner.run "):
                    break
        assert lines[0] == f'runner.migrate result="{APPLIED}"\n'
        # the number of tasks grows as areas register them
        assert re.fullmatch(r"runner\.run tasks=\d+ enrolled=no\n", lines[1])
        assert len(lines) == 2
        time.sleep(0.2)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
        assert time.monotonic() - started < 10
        assert proc.stderr is not None
        assert "runner.supervisor stopped" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert (runner_home / "pg" / "port").is_file()
    assert cluster_running(runner_home / "pg") is False


# --- root and broken-state handling -----------------------------------------------------------------------------


def test_run_as_root_is_refused(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    captured = make_host(env_for(runner_home), geteuid=lambda: 0)
    assert run_cli(["run"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=run_as_root\n"
    assert not runner_home.exists()


def installed_plist(path: Path, user: object = "runner", env: object = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {
        "Label": "swiss.dfx.agent-runner",
        "UserName": user,
        "EnvironmentVariables": env if env is not None else {"AGENT_RUNNER_HOME": "/srv/runner", "PATH": "/usr/bin"},
    }
    path.write_bytes(plistlib.dumps(data))


def test_uninstall_as_root_delegates_to_the_runner_account(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    captured = make_host(env, geteuid=lambda: 0)
    plist = captured.host.launch_daemons_dir / "swiss.dfx.agent-runner.plist"
    installed_plist(plist)
    fake_tools.answers["sudo"] = [(0, "runner.uninstall service=absent cleaners=0 store=deleted identity=kept\n", "")]
    assert run_cli(["uninstall", "--keep-identity"], captured.host) == 0
    assert not plist.exists()
    assert fake_tools.calls == [
        ("launchctl", ["bootout", "system/swiss.dfx.agent-runner"]),
        (
            "sudo",
            [
                "-u",
                "runner",
                "-H",
                "--",
                "/usr/bin/env",
                "-i",
                "AGENT_RUNNER_HOME=/srv/runner",
                "PATH=/usr/bin",
                sys.executable,
                "-m",
                "agent_runner",
                "uninstall",
                "--keep-identity",
            ],
        ),
    ]
    assert captured.stdout.getvalue() == "runner.uninstall service=removed user=runner identity=kept\n"
    assert "runner.uninstall_step runner.uninstall service=absent" in captured.stderr.getvalue()


def test_uninstall_as_root_failure_of_the_account_step(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    captured = make_host(env, geteuid=lambda: 0)
    installed_plist(runner_home / "swiss.dfx.agent-runner.plist")
    fake_tools.answers["sudo"] = [(1, "", "runner.error code=store_unavailable\n")]
    assert run_cli(["uninstall"], captured.host) == 1
    err = captured.stderr.getvalue()
    assert "runner.uninstall_step runner.error code=store_unavailable" in err
    assert err.endswith("runner.error code=uninstall_failed name=runner_account\n")
    assert fake_tools.calls[0][1][-1] == "uninstall"


@pytest.mark.parametrize(
    ("user", "env"),
    [(None, None), (5, {"PATH": "/usr/bin"}), ("runner", "not a dict"), ("runner", {"PATH": 5}), ("root", {})],
)
def test_uninstall_as_root_needs_a_valid_plist(
    user: object,
    env: object,
    *,
    make_host: Callable[..., Any],
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    fake_tools: Any,
) -> None:
    captured = make_host(env_for(runner_home), geteuid=lambda: 0)
    if user is not None:
        installed_plist(runner_home / "swiss.dfx.agent-runner.plist", user, env)
    assert run_cli(["uninstall"], captured.host) == 1
    code = "service_user_invalid" if user == "root" else "uninstall_as_root"
    assert captured.stderr.getvalue() == f"runner.error code={code}\n"
    assert fake_tools.calls == []


def test_uninstall_as_account_refuses_while_the_system_service_exists(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    enroll(runner_home)
    captured = make_host(env)
    installed_plist(captured.host.launch_daemons_dir / "swiss.dfx.agent-runner.plist")
    assert run_cli(["uninstall"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=service_installed\n"
    assert load_identity(runner_home) is not None
    assert fake_tools.calls == []


def test_uninstall_as_account_without_system_service(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    enroll(runner_home)
    captured = make_host(env)
    assert run_cli(["uninstall"], captured.host) == 0
    assert captured.stdout.getvalue() == "runner.uninstall service=absent cleaners=1 store=deleted identity=deleted\n"
    assert not runner_home.exists()
    assert fake_tools.calls == []


@pytest.mark.parametrize("damage", ["keys_only", "bad_mode", "garbage"])
def test_uninstall_removes_a_broken_identity(
    damage: str, make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    write_keys(runner_home, generate_keys())
    key = runner_home / "keys" / "device.key"
    if damage == "bad_mode":
        os.chmod(key, 0o644)
    elif damage == "garbage":
        os.chmod(key, 0o600)
        key.write_text("garbage", encoding="utf-8")
    captured = make_host(env_for(runner_home))
    assert run_cli(["service", "uninstall"], captured.host) == 0
    assert run_cli(["uninstall"], captured.host) == 0
    assert not runner_home.exists()


def test_unexpected_exception_prints_only_its_type(
    area_package: Callable[[dict[str, str]], str], make_host: Callable[..., Any]
) -> None:
    pkg = area_package(
        {
            "greet/cli.py": (
                "def boom(args, ctx):\n    raise ValueError('password=hunter2')\n"
                "def register(subparsers):\n    subparsers.add_parser('boom').set_defaults(handler=boom)\n"
            )
        }
    )
    captured = make_host(package=pkg)
    assert run_cli(["boom"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=internal name=ValueError\n"
    json_run = make_host(package=pkg)
    assert run_cli(["--json", "boom"], json_run.host) == 1
    assert json.loads(json_run.stdout.getvalue())["error"] == {"code": "internal", "name": "ValueError"}

"""U-RUN-service / CLI: `agent-runner status [--json]` and `agent-runner doctor [--clear NAME]` (05-runner.md section
1.2) with injected tools: a scripted `limactl`, the injected cluster over a migrated test database, the
contract-checking fake hub with signed answers and a shell script as the login command (`login_cli`, KOR-20)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import plistlib
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from agent_runner.cli import run_cli
from agent_runner.config import load_config
from agent_runner.doctor import checks
from agent_runner.doctor import cli as doctor_cli
from agent_runner.encoding import b64u_encode
from agent_runner.errors import RunnerError
from agent_runner.identity import Device, ensure_private_dir, write_device, write_keys
from agent_runner.login.relay import login_env
from agent_runner.sync import outbox
from agent_runner.vm import capacity as capacity_module
from agent_runner.vm import golden as golden_module

from ..assignments.orgkit import cluster_tools
from ..conftest import runner_env
from ..reconcile.rig import ROOT, device_keys, pid
from ..sync.fakehub import FakeHub, utc_ms

WALL = 1_795_000_000.0
HOST = "127.0.0.1:9"
URL = f"http://{HOST}"
DEVICE_ID = pid("rdev")
PIN = golden_module.load_source().claude_version
"""The Claude Code pin of the golden build (`images/versions.json` of this checkout)."""


@dataclass
class Limactl:
    """Scripted `limactl`: version, `info` JSON."""

    version: tuple[int, str] = (0, "limactl version 2.3.1\n")
    info: tuple[int, str] = (0, json.dumps({"vmTypes": ["qemu", "vz"]}))
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        code, out = self.version if argv[0] == "--version" else self.info
        return subprocess.CompletedProcess(["limactl", *argv], code, out, "")


@dataclass
class Case:
    home: Path
    dsn: str
    env: dict[str, str]
    make_host: Callable[..., Any]
    hub: FakeHub
    limactl: Limactl
    stops: list[Path]
    bin: Path
    wall: float = WALL
    login_timeout: float = checks.LOGIN_VERSION_TIMEOUT_S
    err: str = ""

    def cli(self, *argv: str, platform: str = "darwin") -> tuple[int, str, str]:
        captured = self.make_host(self.env, cluster=cluster_tools(self.dsn, self.stops), platform=platform)
        code = run_cli(list(argv), captured.host)
        self.err = captured.stderr.getvalue()
        return code, captured.stdout.getvalue(), self.err

    def enroll(self, kind: str = "platform") -> None:
        keys = device_keys()
        write_keys(self.home, keys)
        write_device(
            self.home,
            Device(
                device_id=DEVICE_ID,
                kind=kind,  # type: ignore[arg-type]
                hub_url=URL,
                hub_key=b64u_encode(self.hub.public),
                enrolled_at="2026-11-21T00:00:00.000Z",
            ),
        )

    def sql(self, statement: str, *params: object) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(statement, params or None)

    def claude(self, body: str = f"echo '{PIN} (Claude Code)'", *, name: str = "claude", mode: int = 0o755) -> Path:
        """The login command found in `PATH`: a shell script with `body`."""
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(mode)
        return path

    def golden(self, abi: int | None = 4) -> None:
        ensure_private_dir(self.home)
        self.sql(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
            " state) VALUES ('g1', 'golden-g1', '2.1.330', '0.1.0', 'sha256:x', now(), 'ready')"
        )
        if abi is not None:
            records = self.home / "golden"
            records.mkdir(parents=True, exist_ok=True)
            (records / "g1.json").write_text(json.dumps({"build": {"landlock_abi": abi}}))


def lines(text: str, prefix: str) -> list[dict[str, str]]:
    found = []
    for line in text.splitlines():
        name, _, rest = line.partition(" ")
        if name == prefix:
            found.append(dict(part.split("=", 1) for part in rest.split(" ") if part))
    return found


def check_map(out: str) -> dict[str, list[tuple[str, str, str | None]]]:
    assert out, "no output"
    found: dict[str, list[tuple[str, str, str | None]]] = {}
    for item in lines(out, "runner.doctor.check"):
        found.setdefault(item["name"], []).append((item["result"], item["detail"], item.get("value")))
    return found


@pytest.fixture
def case(
    runner_home: Path, make_host: Callable[..., Any], store_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Case:
    env_for = runner_env
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    env = env_for(
        runner_home, AGENT_RUNNER_HUB_URL=URL, AGENT_RUNNER_TEXTFILE_DIR=str(textfile), PATH=f"{tools}:/usr/bin:/bin"
    )
    keys = device_keys()
    hub = FakeHub(device_id=DEVICE_ID, device_public=keys.signing_key().public_key().public_bytes_raw(), host=HOST)
    hub.wall = lambda: WALL
    built = Case(runner_home, store_dsn, env, make_host, hub, Limactl(), [], tools)
    built.claude()
    monkeypatch.setattr(
        doctor_cli,
        "default_options",
        lambda: checks.DoctorOptions(
            transport=httpx.ASGITransport(app=hub),  # type: ignore[arg-type]
            wall=lambda: built.wall,
            limactl=built.limactl,
            login_timeout=built.login_timeout,
        ),
    )
    return built


# --- doctor ---------------------------------------------------------------------------------------------------------


def test_a_healthy_device_only_warns_about_the_test_mode(case: Case) -> None:
    case.enroll()
    case.golden()
    code, out, err = case.cli("doctor")
    assert (code, err) == (0, "")
    found = check_map(out)
    assert list(found) == [
        "os", "lima", "vz", "postgresql", "golden", "landlock", "login_cli", "hub", "clock", "textfile",
        "operator_subscriptions", "operator_github", "test_mode", "sync",
    ]  # fmt: skip
    assert found["os"] == [("ok", "supported", "darwin")]
    assert found["lima"] == [("ok", "version", "2.3.1")]
    assert found["vz"] == [("ok", "available", None)]
    assert found["postgresql"] == [("ok", "reachable", "auth=trust_loopback")]
    assert found["golden"] == [("ok", "ready", "g1")]
    assert found["landlock"] == [("ok", "abi", "4")]
    assert found["login_cli"] == [("ok", "version", PIN)]
    assert found["hub"] == [("ok", "pinned_key", "active")]
    assert found["clock"] == [("ok", "skew_seconds", "0")]
    assert found["textfile"] == [("ok", "writable", None)]
    assert found["operator_subscriptions"] == [("ok", "off", None)]
    assert found["test_mode"] == [("warn", "test_mode_on", None)]
    assert found["operator_github"] == [("ok", "off", None)]
    assert found["sync"] == [("ok", "clear", None)]
    assert out.splitlines()[-1] == "runner.doctor result=warn"
    assert case.stops == [case.home / "pg"]


def test_doctor_shows_the_operator_subscriptions_and_the_operator_github_lines_together(
    case: Case, tmp_path: Path
) -> None:
    from agent_runner.model.operator import TOKEN_PREFIX  # noqa: PLC0415

    case.enroll()
    case.golden()
    subscriptions = tmp_path / "operator-subscriptions"
    subscriptions.mkdir(mode=0o700)
    (subscriptions / "abo1.token").write_text(TOKEN_PREFIX + "01-subscriptiondoubletoken\n")
    (subscriptions / "abo1.token").chmod(0o600)
    github = tmp_path / "operator-github"
    github.mkdir(mode=0o700)
    (github / "github.token").write_text("github-double-token\n")
    (github / "github.token").chmod(0o600)
    case.env["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"] = str(subscriptions)
    case.env["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_ORGS"] = "01a1ee56-5e00-73da-a86c-5c56722cfde3"
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = str(github / "github.token")
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = "01a1ee56-5e00-7359-8ec3-2c90dd7ae358"
    code, out, _err = case.cli("doctor")
    found = check_map(out)
    assert found["operator_subscriptions"] == [("ok", "usable", "1")]
    assert found["operator_github"] == [("ok", "usable", None)]
    assert list(found).index("operator_github") == list(found).index("operator_subscriptions") + 1
    assert "doubletoken" not in out
    assert "double-token" not in out
    assert code == 0  # an enrolled device with a golden (warn test_mode only)


def test_doctor_checks_the_operators_github_token_file_without_naming_it(case: Case, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    secret = "doctor-check-double-token-1"
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = str(token_file)
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = "01a1ee56-5e00-7359-8ec3-2c90dd7ae358"
    _code, out, _err = case.cli("doctor")
    assert check_map(out)["operator_github"] == [("warn", "missing", None)]
    token_file.write_text(secret + "\n")
    token_file.chmod(0o600)
    _code, out, _err = case.cli("doctor")
    assert check_map(out)["operator_github"] == [("ok", "usable", None)]
    assert secret not in out
    assert "01a1ee56" not in out
    org = "01a1ee56-5e00-7aaa-8ec3-2c90dd7ae999"
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_ORG"] = org  # KOR-47: the organization is named, the users never
    _code, out, _err = case.cli("doctor")
    assert check_map(out)["operator_github"] == [("ok", "usable", org)]
    assert "01a1ee56-5e00-7359-8ec3-2c90dd7ae358" not in out


def test_doctor_names_why_the_operators_github_is_refused_for_a_second_user(case: Case, tmp_path: Path) -> None:
    # the operator's GitHub is the operator's own connection: a second user is refused, and doctor says so
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_TOKEN_FILE"] = str(tmp_path / "github.token")
    case.env["AGENT_RUNNER_OPERATOR_GITHUB_USERS"] = (
        "01a1ee56-5e00-7359-8ec3-2c90dd7ae358,01a1ee56-5e00-73da-a86c-5c56722cfde3"
    )
    code, out, err = case.cli("doctor")
    assert code != 0
    assert "code=config_invalid name=OPERATOR_GITHUB_USERS:one_user_only" in out + err
    assert "01a1ee56" not in out + err


def test_doctor_reports_every_failure(case: Case) -> None:
    case.enroll()
    case.golden(abi=3)
    case.limactl.version = (127, "")
    case.limactl.info = (0, json.dumps({"vmTypes": ["qemu"]}))
    case.wall = WALL + 10
    case.env["AGENT_RUNNER_TEXTFILE_DIR"] = str(case.home / "missing")
    (case.bin / "claude").unlink()
    for key, value in (
        ("sync_fatal", "origin_seq_conflict"),
        ("platform_stopped", "log_fork"),
        ("org_stopped_" + "a" * 32, "log_rollback"),
        ("device_revoked", "key_revoked"),
    ):
        case.sql("INSERT INTO runner.sync_state (key, value) VALUES (%s, %s)", key, value)
    code, out, _ = case.cli("doctor", platform="linux")
    assert (code, out) == (
        1,
        "runner.doctor.check name=os result=fail detail=unsupported value=linux\nrunner.doctor result=fail\n",
    )
    code, out, _ = case.cli("--json", "doctor")
    assert code == 1
    document = json.loads(out)
    assert document["ok"] is False
    found: dict[str, list[tuple[str, str]]] = {}
    for fact in document["facts"]:
        if fact["fact"] == "runner.doctor.check":
            found.setdefault(fact["name"], []).append((fact["result"], fact["detail"]))
    assert found["os"] == [("ok", "supported")]
    assert found["lima"] == [("fail", "lima_missing")]
    assert found["vz"] == [("fail", "vz_unavailable")]
    assert found["landlock"] == [("fail", "abi")]
    assert found["login_cli"] == [("fail", "login_cli_missing")]
    assert found["clock"] == [("fail", "skew_seconds")]
    assert found["textfile"] == [("fail", "textfile_unwritable")]
    assert found["sync"] == [
        ("fail", "device_revoked"),
        ("fail", "sync_fatal"),
        ("fail", "platform_stopped"),
        ("fail", "org_stopped"),
    ]
    assert document["facts"][-1] == {"fact": "runner.doctor", "result": "fail"}


def test_hub_refusals_and_unverifiable_answers(case: Case) -> None:
    case.enroll()
    case.hub.device_state = {"error": "x"}
    case.hub.scripted.append((401, {"error": "key_revoked"}, {}))
    found = check_map(case.cli("doctor")[1])
    assert found["hub"] == [("fail", "key_revoked", None)]
    assert found["clock"] == [("warn", "no_hub_time", None)]
    case.hub.tamper.append("other_key")
    found = check_map(case.cli("doctor")[1])
    assert found["hub"][0][0] == "fail"
    assert found["hub"][0][1].startswith("response_")


def test_a_clock_outside_the_signature_window(case: Case) -> None:
    case.enroll()
    case.hub.tamper.append("stale_time")
    found = check_map(case.cli("doctor")[1])
    assert found["hub"] == [("fail", "response_time_skew", None)]
    assert found["clock"] == [("fail", "skew_seconds", ">120")]


@pytest.mark.parametrize(
    ("answer", "hub", "clock"),
    [
        ({"state": "pending_certification", "hub_time": "2026-13-45T00:00:00.000Z"}, "pending_certification", "warn"),
        ({"state": "odd", "hub_time": 5}, "unknown", "warn"),
    ],
)
def test_odd_state_answers(case: Case, answer: dict[str, Any], hub: str, clock: str) -> None:
    case.enroll()
    case.hub.scripted.append((200, answer, {}))
    found = check_map(case.cli("doctor")[1])
    assert found["hub"] == [("ok", "pinned_key", hub)]
    assert found["clock"][0][0] == clock


def test_an_old_lima_and_unreadable_info(case: Case) -> None:
    case.limactl.version = (0, "limactl version 2.2.9\n")
    case.limactl.info = (0, "not json")
    found = check_map(case.cli("doctor")[1])
    assert found["lima"] == [("fail", "lima_version", None)]
    assert found["vz"] == [("fail", "vz_unavailable", None)]
    case.limactl.info = (1, "")
    assert check_map(case.cli("doctor")[1])["vz"] == [("fail", "vz_unavailable", None)]


def test_a_device_that_is_not_enrolled(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    config_body = {"platform_root_key": ROOT, "hub_key": {"id": "hub-test", "public_key": b64u_encode(case.hub.public)}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(config_body).encode()))

    monkeypatch.setattr(
        doctor_cli,
        "default_options",
        lambda: checks.DoctorOptions(transport=httpx.MockTransport(handler), limactl=case.limactl),
    )
    found = check_map(case.cli("doctor")[1])
    assert found["hub"] == [("warn", "not_enrolled", None)]
    assert found["golden"] == [("fail", "no_golden", None)]
    assert found["landlock"] == [("fail", "no_golden", None)]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(
        doctor_cli,
        "default_options",
        lambda: checks.DoctorOptions(transport=httpx.MockTransport(down), limactl=case.limactl),
    )
    assert check_map(case.cli("doctor")[1])["hub"] == [("fail", "hub_unreachable", None)]
    del case.env["AGENT_RUNNER_HUB_URL"]
    del case.env["AGENT_RUNNER_TEXTFILE_DIR"]
    found = check_map(case.cli("doctor")[1])
    assert found["hub"] == [("warn", "not_enrolled", None)]
    assert found["textfile"] == [("ok", "off", None)]


def test_store_states(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    exists = cluster_tools(case.dsn, case.stops)
    missing = type(exists)(ensure=exists.ensure, stop=exists.stop, create_database=exists.create_database,
                           exists=lambda path: False)  # fmt: skip
    captured = case.make_host(case.env, cluster=missing)
    assert run_cli(["doctor"], captured.host) == 1
    found = check_map(captured.stdout.getvalue())
    assert found["postgresql"] == [("warn", "store_missing", None)]

    def broken(path: Path) -> bool:
        raise OSError("probe")

    failing = type(exists)(ensure=exists.ensure, stop=exists.stop, create_database=exists.create_database,
                           exists=broken)  # fmt: skip
    captured = case.make_host(case.env, cluster=failing)
    run_cli(["doctor"], captured.host)
    assert check_map(captured.stdout.getvalue())["postgresql"] == [("fail", "store_unavailable", None)]
    assert checks.read_store("host=127.0.0.1 port=1 dbname=none connect_timeout=1") == checks.StoreFacts()


def test_a_golden_without_a_build_record(case: Case) -> None:
    case.golden(abi=None)
    assert check_map(case.cli("doctor")[1])["landlock"] == [("warn", "record_missing", None)]
    (case.home / "golden").mkdir(exist_ok=True)
    (case.home / "golden" / "g1.json").write_text(json.dumps({"build": {"landlock_abi": True}}))
    assert check_map(case.cli("doctor")[1])["landlock"] == [("warn", "record_missing", None)]


def test_clearing_operator_markers(case: Case) -> None:
    marker = "org_stopped_" + "b" * 32
    case.sql("INSERT INTO runner.sync_state (key, value) VALUES (%s, 'log_fork')", marker)
    code, out, _ = case.cli("doctor", "--clear", marker)
    assert code == 0
    assert out == f"runner.doctor.cleared name={marker} removed=yes\n"
    assert case.cli("doctor", "--clear", "sync_fatal")[1] == "runner.doctor.cleared name=sync_fatal removed=no\n"
    code, _, err = case.cli("doctor", "--clear", "device_revoked")
    assert (code, err) == (1, "runner.error code=marker_invalid\n")
    exists = cluster_tools(case.dsn, case.stops)
    missing = type(exists)(ensure=exists.ensure, stop=exists.stop, create_database=exists.create_database,
                           exists=lambda path: False)  # fmt: skip
    captured = case.make_host(case.env, cluster=missing)
    assert run_cli(["doctor", "--clear", "sync_fatal"], captured.host) == 1
    assert captured.stderr.getvalue() == "runner.error code=store_unavailable\n"
    with pytest.raises(RunnerError, match="store_unavailable"):
        checks.clear_marker("host=127.0.0.1 port=1 dbname=none connect_timeout=1", "sync_fatal")


def test_time_parsing() -> None:
    assert checks._ms_time("2026-11-21T12:00:00.250Z") == datetime(2026, 11, 21, 12, tzinfo=UTC).timestamp() + 0.25
    assert checks._ms_time("2026-02-30T00:00:00.000Z") is None
    assert checks._ms_time(None) is None


def test_real_default_options() -> None:
    made = doctor_cli.default_options()
    assert made.transport is None
    assert made.limactl is None
    assert made.login_timeout == checks.LOGIN_VERSION_TIMEOUT_S == 10.0


def test_the_default_limactl_runs_from_path(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cli, "default_options", lambda: checks.DoctorOptions(transport=None))
    case.env["PATH"] = str(case.home)  # no limactl there
    found = check_map(case.cli("doctor")[1])
    assert found["lima"] == [("fail", "lima_missing", None)]


# --- login_cli (KOR-20) ---------------------------------------------------------------------------------------------


def login_cli(case: Case) -> tuple[int, list[tuple[str, str, str | None]]]:
    """Exit code and `login_cli` line of a device whose other checks pass (the test mode only warns)."""
    if not (case.home / "device.json").exists():
        case.enroll()
        case.golden()
    code, out, err = case.cli("doctor")
    assert err == ""
    return code, check_map(out)["login_cli"]


def test_another_claude_code_version_warns(case: Case) -> None:
    case.claude("echo '9.9.9 (Claude Code)'")
    assert login_cli(case) == (0, [("warn", "version_mismatch", "9.9.9")])
    case.claude(f"echo 'claude-code {PIN}-beta.1 (Claude Code)'")
    assert login_cli(case) == (0, [("warn", "version_mismatch", f"{PIN}-beta.1")])


def test_a_missing_login_command_fails(case: Case) -> None:
    (case.bin / "claude").unlink()
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    case.env["PATH"] = ""  # the runner's PATH is all the lookup uses
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])


def test_a_login_command_that_cannot_run_fails(case: Case) -> None:
    case.claude(mode=0o644)
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    script = case.claude()
    script.write_text("no interpreter line\n")  # executable, but exec(2) refuses it
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    case.claude(f"echo '{PIN} (Claude Code)'\nexit 3")  # starts, but fails (for example node not found)
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])


def test_the_lookup_uses_the_path_of_the_installed_daemon(case: Case) -> None:
    plist = case.home / "swiss.dfx.agent-runner.plist"  # under pytest `service` keeps the plist in the runner home
    case.env["PATH"] = "/usr/bin:/bin"  # the caller's PATH has no claude
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"PATH": f"{case.bin}:/usr/bin:/bin"}}))
    assert login_cli(case) == (0, [("ok", "version", PIN)])
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"PATH": "/usr/bin:/bin"}}))
    case.env["PATH"] = f"{case.bin}:/usr/bin:/bin"  # the daemon would not find it: the caller's PATH does not count
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    for broken in ({}, {"EnvironmentVariables": "x"}, {"EnvironmentVariables": {"PATH": 1}}):
        plist.write_bytes(plistlib.dumps(broken))  # no usable PATH in the plist: the caller's
        assert login_cli(case) == (0, [("ok", "version", PIN)])


def running_before_the_deadline(monkeypatch: pytest.MonkeyPatch, ready: Path) -> list[float]:
    """The deadline of the version check counts only once the command is running: ``communicate`` of the login
    command waits until it has written ``ready`` (a loaded machine may need seconds to start a shell), so the test
    checks the expiry, not how fast a process starts. The returned list holds the monotonic instant the wait ended."""
    released: list[float] = []

    class Started(subprocess.Popen[bytes]):
        def communicate(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
            if not released and isinstance(self.args, list) and self.args[0] == str(ready.parent / "claude"):
                limit = time.monotonic() + 60
                while not ready.exists() or not ready.read_text().strip():
                    assert time.monotonic() < limit, "the login command never started"
                    time.sleep(0.01)
                released.append(time.monotonic())
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Started)  # `checks` starts the command with `subprocess.Popen`
    return released


def test_a_version_that_does_not_come_in_time_warns_and_the_command_is_killed(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids = case.bin / "pids"
    # the child sleeps far longer than the bound below: only the killed group, not its own end, makes it disappear
    case.claude(f"sleep 600 &\necho $$ $! > {pids}.new\nmv {pids}.new {pids}\nwait")
    case.login_timeout = 0.3
    released = running_before_the_deadline(monkeypatch, pids)
    assert login_cli(case) == (0, [("warn", "version_unknown", None)])
    assert time.monotonic() - released[0] < 10
    found = [int(text) for text in pids.read_text().split()]
    deadline = time.monotonic() + 30
    for each in found:  # the command and its child: the whole process group is gone
        while True:
            try:
                os.kill(each, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                for rest in found:  # do not leave a ten-minute sleep behind
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(rest, signal.SIGKILL)
                pytest.fail(f"process {each} still runs")
            time.sleep(0.05)


def test_a_descendant_that_left_the_group_does_not_hold_the_check(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    pids = case.bin / "pids"
    # perl's setsid moves the child into a session of its own: killing the group misses it, and it keeps stdout open
    case.claude(f"perl -MPOSIX -e 'setsid(); print STDERR $$; sleep 30' 2> {pids} &\nsleep 30")
    case.login_timeout = 0.5
    released = running_before_the_deadline(monkeypatch, pids)
    try:
        assert login_cli(case) == (0, [("warn", "version_unknown", None)])
        assert time.monotonic() - released[0] < 10
    finally:
        os.kill(int(pids.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("body", ["echo 'no version here'", "true", f"echo 'v{PIN}x'"])
def test_an_unreadable_version_warns(case: Case, body: str) -> None:
    case.claude(body)
    assert login_cli(case) == (0, [("warn", "version_unknown", None)])


def test_the_version_check_runs_with_the_allow_list_environment_of_the_login(case: Case) -> None:
    seen = case.bin / "seen"
    script = case.claude(f"env > {seen}\npwd >> {seen}\necho '{PIN} (Claude Code)'")
    case.env["ANTHROPIC_API_KEY"] = "never-passed-on"
    case.env["CLAUDE_CONFIG_DIR"] = str(case.home / "profile")
    assert login_cli(case) == (0, [("ok", "version", PIN)])
    *variables, cwd = seen.read_text().splitlines()
    env = dict(line.split("=", 1) for line in variables)
    workdir = Path(env["HOME"]).parent
    assert Path(cwd).resolve() == workdir.resolve()  # started in the temporary directory
    assert {k: v for k, v in env.items() if k not in {"PWD", "SHLVL", "_", "OLDPWD"}} == login_env(workdir, str(script))
    assert env["CLAUDE_CONFIG_DIR"] == str(workdir / "config")
    assert env["PATH"].split(":")[0] == str(case.bin)
    assert not workdir.exists()  # removed after the check
    assert not (case.home / "profile").exists()


def test_a_different_login_command_is_only_checked_for_being_executable(case: Case) -> None:
    ran = case.bin / "ran"
    double = case.claude(f"touch {ran}", name="login-double")
    case.env["AGENT_RUNNER_LOGIN_ARGV"] = json.dumps([str(double), "--seed", "1"])
    assert login_cli(case) == (0, [("ok", "executable", None)])
    assert not ran.exists()
    case.env["AGENT_RUNNER_LOGIN_ARGV"] = json.dumps(["login-double"])  # looked up in PATH
    assert login_cli(case) == (0, [("ok", "executable", None)])
    case.env["AGENT_RUNNER_LOGIN_ARGV"] = json.dumps([str(case.bin)])  # a directory is no program
    assert login_cli(case) == (1, [("fail", "login_cli_missing", None)])
    assert not ran.exists()


def test_without_a_readable_pin_the_version_is_reported_with_a_warning(case: Case, tmp_path: Path) -> None:
    template = tmp_path / "elsewhere" / "images" / "golden.yaml"
    template.parent.mkdir(parents=True)
    template.write_text("images: []\n")
    case.env["AGENT_RUNNER_GOLDEN_TEMPLATE"] = str(template)
    assert login_cli(case) == (0, [("warn", "pin_missing", PIN)])


# --- status ---------------------------------------------------------------------------------------------------------


def test_status_of_a_device_without_store_and_identity(case: Case) -> None:
    exists = cluster_tools(case.dsn, case.stops)
    missing = type(exists)(ensure=exists.ensure, stop=exists.stop, create_database=exists.create_database,
                           exists=lambda path: False)  # fmt: skip
    captured = case.make_host(case.env, cluster=missing)
    assert run_cli(["status"], captured.host) == 0
    out = captured.stdout.getvalue()
    assert lines(out, "runner.status.identity") == [{"enrolled": "no"}]
    assert lines(out, "runner.status.hub") == [
        {
            "last_connect": "never",
            "sync": "ok",
            "revoked": "no",
            "removed": "no",
            "stopped_orgs": "0",
            "platform_log": "ok",
        }
    ]
    assert lines(out, "runner.status.outbox") == [
        {
            "position": "unknown",
            "last_origin_seq": "0",
            "next_origin_seq": "1",
            "pushed_origin_seq": "0",
            "backlog": "0",
        }
    ]
    assert lines(out, "runner.status.keys") == [{"orgs": "0", "scope_keys": "0"}]
    assert lines(out, "runner.status.vm") == []
    assert len(lines(out, "runner.capacity")) == 1


def test_status_of_a_working_device(case: Case) -> None:
    case.enroll(kind="org")
    case.golden()
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    case.sql(
        "INSERT INTO runner.vm (id, lima_name, golden_version, wg_id, org_id, state, control_token_hash, cpus,"
        " memory_mib, created_at) VALUES ('vm-00000001', 'arp-00000001', 'g1', %s, %s, 'running', 'x', 4, 8192, %s),"
        " ('vm-00000002', 'arp-00000002', 'g1', NULL, NULL, 'pool', 'x', 4, 8192, %s),"
        " ('vm-00000003', 'arp-00000003', 'g1', NULL, NULL, 'deleted', 'x', 4, 8192, %s)",
        pid("wg"), pid("org"), now, now, now,
    )  # fmt: skip
    case.sql(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, depth, state, created_at) VALUES"
        " (%s, %s, %s, %s, 'vm-00000001', 0, 'working', %s), (%s, %s, %s, %s, NULL, 1, 'stopped', %s)",
        pid("s1"), pid("t"), pid("org"), pid("wg"), now, pid("s2"), pid("t"), pid("org"), pid("wg"), now,
    )  # fmt: skip
    case.sql(
        "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) VALUES (1, 'audit', '\\x00'),"
        " (2, 'audit', '\\x00'), (3, 'audit', '\\x00')"
    )
    for key, value in (("pushed_origin_seq", "1"), ("hub_ws_last_connect", "2026-11-21T12:00:00Z")):
        case.sql("INSERT INTO runner.sync_state (key, value) VALUES (%s, %s)", key, value)
    code, out, _ = case.cli("--json", "status")
    assert code == 0
    facts = json.loads(out)["facts"]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        by_name.setdefault(fact.pop("fact"), []).append(fact)
    assert by_name["runner.status.identity"] == [
        {"enrolled": True, "device_id": DEVICE_ID, "kind": "org", "hub_url": URL,
         "enrolled_at": "2026-11-21T00:00:00.000Z"},
    ]  # fmt: skip
    assert by_name["runner.status.hub"][0]["last_connect"] == "2026-11-21T12:00:00Z"
    assert by_name["runner.status.outbox"] == [
        {"position": "known", "last_origin_seq": 3, "next_origin_seq": 4, "pushed_origin_seq": 1, "backlog": 2}
    ]
    assert [vm["vm_id"] for vm in by_name["runner.status.vm"]] == ["vm-00000001", "vm-00000002"]
    assert by_name["runner.status.vm"][1]["wg_id"] == "none"
    assert by_name["runner.status.session"] == [
        {"session_id": pid("s1"), "state": "working", "wg_id": pid("wg"), "vm_id": "vm-00000001", "depth": 0}
    ]
    assert by_name["runner.status.pool"] == [{"size": 2, "ready": 1}]
    assert "SECRET" not in out


def _outbox(dsn: str, step: Callable[[Any, bytes], Awaitable[int | bool]]) -> int | bool:
    async def body() -> int | bool:
        async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.transaction():
            return await step(conn, b"k" * 32)

    return asyncio.run(body())


def test_status_after_a_store_wipe_shows_the_adopted_hub_position(case: Case) -> None:
    """KOR-10 (AUD-02 NA-07): a wiped store adopts the hub's `last_origin_seq` before it writes an item (DR-01). The
    outbox is empty then, and `last_origin_seq=0` next to `pushed_origin_seq=N` looked like a backlog or a gap; the
    numbering continues at the hub's position, so that is what the status shows - and the next item takes the shown
    `next_origin_seq`. Before the position is known nothing is final (`position=unknown`)."""
    case.enroll(kind="org")
    metrics: dict[str, Any] = {"samples": []}
    assert _outbox(case.dsn, lambda conn, key: outbox.enqueue(conn, key, "metrics", metrics)) == 1
    assert lines(case.cli("status")[1], "runner.status.outbox") == [
        {"position": "unknown", "last_origin_seq": "1", "next_origin_seq": "2", "pushed_origin_seq": "0",
         "backlog": "1"}
    ]  # fmt: skip
    case.sql("DELETE FROM runner.outbox")  # the wipe of the store (DR-01) leaves no item
    assert _outbox(case.dsn, lambda conn, key: outbox.adopt_hub_position(conn, key, 41)) is True
    code, out, _ = case.cli("status")
    assert code == 0
    assert lines(out, "runner.status.outbox") == [
        {"position": "known", "last_origin_seq": "41", "next_origin_seq": "42", "pushed_origin_seq": "41",
         "backlog": "0"}
    ]  # fmt: skip
    assert _outbox(case.dsn, lambda conn, key: outbox.enqueue(conn, key, "metrics", metrics)) == 42


def test_status_shows_the_work_vm_slots(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-059 addendum 4: held (a VM that is not stopped, a pool entry), free, stopped and waiting, and what the
    device's admission sees free; the slots follow the RAM above the reserve (no fixed number: a former
    `AGENT_RUNNER_WORKVM_SLOTS` is ignored)."""
    monkeypatch.setattr(capacity_module, "physical_memory_mib", lambda: 32768 + 4 * 8192)
    case.env["AGENT_RUNNER_WORKVM_SLOTS"] = "0"
    [unbounded] = lines(case.cli("status")[1], "runner.status.workvm")
    assert (unbounded["slots"], unbounded["held"], unbounded["disk_quota_gib"]) == ("4", "0", "none")
    del case.env["AGENT_RUNNER_WORKVM_SLOTS"]
    case.env["AGENT_RUNNER_WORKVM_DISK_QUOTA_GIB"] = "500"
    now = datetime(2026, 11, 26, 9, tzinfo=UTC)
    for number, (slot, state) in enumerate(((0, "running"), (1, "stopped"), (1, "deleted"), (3, "stopped")), start=1):
        scope_task = pid(f"t{number}")
        case.sql(
            "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, requested_by, org_id, wg_id, size, cpus,"
            " memory_mib, disk_gib, image, state, created_at, last_used_at) VALUES (%s, %s, %s, 'task', %s, %s, %s,"
            " %s, 'standard', 4, 8192, 50, 'img', %s, %s, %s)",
            f"wvm-0000000{number}", f"wp-wvm-{number}", slot, scope_task, scope_task, pid("org"), pid("wg"), state,
            now, now,
        )  # fmt: skip
    case.sql(
        "INSERT INTO runner.workvm_pool (lima_name, slot, image, memory_mib, state, created_at)"
        " VALUES ('wp-wvm-pool', 2, 'img', 8192, 'ready', %s)",
        now,
    )
    case.sql(
        "INSERT INTO runner.workvm_wait (ticket, slot, since)"
        " VALUES ('00000000000000aa', NULL, now() - interval '90 s'), ('00000000000000bb', 1, now())"
    )
    [found] = lines(case.cli("status")[1], "runner.status.workvm")
    oldest = int(found.pop("oldest_wait_s"))
    load = int(found.pop("cpu_load_pct"))  # of the machine running the test
    assert found == {"slots": "4", "held": "2", "free": "2", "stopped": "2", "waiting": "2",
                     "memory_free_mib": "16384", "cpu_load_max_pct": "85", "disk_used_gib": "200",
                     "disk_quota_gib": "500"}  # fmt: skip
    assert 90 <= oldest < 150
    assert load >= 0


def test_status_without_a_store_shows_no_work_vm_slots(case: Case) -> None:
    exists = cluster_tools(case.dsn, case.stops)
    missing = type(exists)(ensure=exists.ensure, stop=exists.stop, create_database=exists.create_database,
                           exists=lambda path: False)  # fmt: skip
    captured = case.make_host(case.env, cluster=missing)
    assert run_cli(["status"], captured.host) == 0
    assert lines(captured.stdout.getvalue(), "runner.status.workvm") == []


def test_status_hides_store_errors() -> None:
    with pytest.raises(RunnerError, match="store_unavailable"):
        doctor_cli._rows("host=127.0.0.1 port=1 dbname=none connect_timeout=1")


def test_config_loading_needs_no_hub(case: Case) -> None:
    del case.env["AGENT_RUNNER_HUB_URL"]
    assert load_config(case.env, platform="darwin", require=frozenset()).hub_url is None
    assert case.cli("status")[0] == 0
    assert utc_ms(0) == "1970-01-01T00:00:00.000Z"


def test_the_operator_subscriptions_are_counted_without_names(tmp_path: Path) -> None:
    """KOR-23 (ADR-052): ok/warn with counts; no token, file name or organization."""
    from types import SimpleNamespace  # noqa: PLC0415

    from agent_runner.model.operator import TOKEN_PREFIX  # noqa: PLC0415

    org = "0b9c6a52-4a35-4c1e-9d8e-8f3a0e8d5b11"
    assert checks.check_operator_subscriptions(SimpleNamespace()) == checks.Check(  # type: ignore[arg-type]
        "operator_subscriptions", "ok", "off"
    )
    directory = tmp_path / "operator"
    config = SimpleNamespace(operator_subscription_dir=directory, operator_subscription_orgs=frozenset({org}))

    def found() -> checks.Check:
        return checks.check_operator_subscriptions(config)  # type: ignore[arg-type]

    assert found() == checks.Check("operator_subscriptions", "warn", "dir_unusable", "missing")
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    assert found() == checks.Check("operator_subscriptions", "warn", "none_usable", 0)
    bad = directory / "wide.token"
    bad.write_text(TOKEN_PREFIX + "01-" + "w" * 30)
    bad.chmod(0o644)
    assert found() == checks.Check("operator_subscriptions", "warn", "none_usable", 1)
    for name in ("a", "b"):
        (directory / f"{name}.token").write_text(TOKEN_PREFIX + "01-" + name * 30 + "\n")
        (directory / f"{name}.token").chmod(0o600)
    assert found() == checks.Check("operator_subscriptions", "warn", "files_ignored", 2)
    bad.unlink()
    assert found() == checks.Check("operator_subscriptions", "ok", "usable", 2)
    assert org not in repr(found())

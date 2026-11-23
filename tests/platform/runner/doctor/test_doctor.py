"""U-RUN-service / CLI: `agent-runner status [--json]` and `agent-runner doctor [--clear NAME]` (05-runner.md section
1.2) with injected tools: a scripted `limactl`, the injected cluster over a migrated test database and the
contract-checking fake hub with signed answers."""

from __future__ import annotations

import asyncio
import json
import subprocess
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
from agent_runner.sync import outbox

from ..assignments.orgkit import cluster_tools
from ..conftest import runner_env
from ..reconcile.rig import ROOT, device_keys, pid
from ..sync.fakehub import FakeHub, utc_ms

WALL = 1_795_000_000.0
HOST = "127.0.0.1:9"
URL = f"http://{HOST}"
DEVICE_ID = pid("rdev")


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
    wall: float = WALL
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
    env = env_for(runner_home, AGENT_RUNNER_HUB_URL=URL, AGENT_RUNNER_TEXTFILE_DIR=str(textfile))
    keys = device_keys()
    hub = FakeHub(device_id=DEVICE_ID, device_public=keys.signing_key().public_key().public_bytes_raw(), host=HOST)
    hub.wall = lambda: WALL
    built = Case(runner_home, store_dsn, env, make_host, hub, Limactl(), [])
    monkeypatch.setattr(
        doctor_cli,
        "default_options",
        lambda: checks.DoctorOptions(
            transport=httpx.ASGITransport(app=hub),  # type: ignore[arg-type]
            wall=lambda: built.wall,
            limactl=built.limactl,
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
        "os", "lima", "vz", "postgresql", "golden", "landlock", "hub", "clock", "textfile", "test_mode", "sync",
    ]  # fmt: skip
    assert found["os"] == [("ok", "supported", "darwin")]
    assert found["lima"] == [("ok", "version", "2.3.1")]
    assert found["vz"] == [("ok", "available", None)]
    assert found["postgresql"] == [("ok", "reachable", "auth=trust_loopback")]
    assert found["golden"] == [("ok", "ready", "g1")]
    assert found["landlock"] == [("ok", "abi", "4")]
    assert found["hub"] == [("ok", "pinned_key", "active")]
    assert found["clock"] == [("ok", "skew_seconds", "0")]
    assert found["textfile"] == [("ok", "writable", None)]
    assert found["test_mode"] == [("warn", "test_mode_on", None)]
    assert found["sync"] == [("ok", "clear", None)]
    assert out.splitlines()[-1] == "runner.doctor result=warn"
    assert case.stops == [case.home / "pg"]


def test_doctor_reports_every_failure(case: Case) -> None:
    case.enroll()
    case.golden(abi=3)
    case.limactl.version = (127, "")
    case.limactl.info = (0, json.dumps({"vmTypes": ["qemu"]}))
    case.wall = WALL + 10
    case.env["AGENT_RUNNER_TEXTFILE_DIR"] = str(case.home / "missing")
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


def test_the_default_limactl_runs_from_path(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cli, "default_options", lambda: checks.DoctorOptions(transport=None))
    case.env["PATH"] = str(case.home)  # no limactl there
    found = check_map(case.cli("doctor")[1])
    assert found["lima"] == [("fail", "lima_missing", None)]


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
        {"last_connect": "never", "sync": "ok", "revoked": "no", "stopped_orgs": "0", "platform_log": "ok"}
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


def test_status_hides_store_errors() -> None:
    with pytest.raises(RunnerError, match="store_unavailable"):
        doctor_cli._rows("host=127.0.0.1 port=1 dbname=none connect_timeout=1")


def test_config_loading_needs_no_hub(case: Case) -> None:
    del case.env["AGENT_RUNNER_HUB_URL"]
    assert load_config(case.env, platform="darwin", require=frozenset()).hub_url is None
    assert case.cli("status")[0] == 0
    assert utc_ms(0) == "1970-01-01T00:00:00.000Z"

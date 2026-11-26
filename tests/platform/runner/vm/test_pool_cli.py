"""U-RUN-pool `agent-runner pool status` (05-runner.md sections 1.2, 3.2, 3.4) end to end through `run_cli`: a
stand-in `limactl` on `PATH`, the store a real PostgreSQL database behind injected cluster tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.cli import run_cli
from agent_runner.store.cluster import ClusterTools
from agent_runner.vm import capacity, cli_pool

from .conftest import STATE_ENV, FakeLimactl, install_binary

PREFIX = "wp-cli-"


@pytest.fixture
def status(
    make_host: Callable[..., Any], store_dsn: str, runner_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setattr(capacity, "physical_memory_mib", lambda: 32768 + 4 * 8192)
    fake = FakeLimactl(lima_dir=tmp_path / "lima")
    state = tmp_path / "limactl.json"
    install_binary(tmp_path / "bin")
    exists = {"value": True}

    def call(*extra: str, **env: str) -> tuple[int, str, str]:
        fake.save(state)
        tools = ClusterTools(
            ensure=lambda path: "postgresql://unused",
            stop=lambda path: None,
            create_database=lambda admin, name: store_dsn,
            exists=lambda path: exists["value"],
        )
        values = {
            "AGENT_RUNNER_HOME": str(runner_home),
            "AGENT_RUNNER_VM_PREFIX": PREFIX,
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            STATE_ENV: str(state),
            **env,
        }
        captured = make_host(values, cluster=tools)
        code = run_cli(["pool", "status", *extra], captured.host)
        return code, captured.stdout.getvalue(), captured.stderr.getvalue()

    call.fake = fake  # type: ignore[attr-defined]
    call.exists = exists  # type: ignore[attr-defined]
    call.dsn = store_dsn  # type: ignore[attr-defined]
    return call


def sql(dsn: str, query: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(query)


def test_status_without_store(status: Any) -> None:
    status.exists["value"] = False
    code, out, err = status()
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "runner.pool size=1 ready=0 golden=none refill=no_golden",
        "runner.capacity budget_mib=32768 used_mib=0 slots_total=4 slots_free=4 pool_ready=0 capacity_low=no",
    ]


@pytest.mark.parametrize("left_over", ["0", "4"])
def test_status_ignores_a_left_over_pool_setting(status: Any, left_over: str) -> None:
    """ADR-009 addendum: an old AGENT_RUNNER_POOL_SIZE is no config_invalid and does not change the size of 1."""
    status.exists["value"] = False
    code, out, err = status(AGENT_RUNNER_POOL_SIZE=left_over)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "runner.pool size=1 ready=0 golden=none refill=no_golden"


def test_status_while_a_test_holds_the_pool_empty(status: Any) -> None:
    status.exists["value"] = False
    code, out, err = status(AGENT_RUNNER_TEST_MODE="1", AGENT_RUNNER_TEST_POOL_EMPTY="1")
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "runner.pool size=0 ready=0 golden=none refill=disabled"


def test_status_with_pool_vms(status: Any) -> None:
    sql(
        status.dsn,
        "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at, state)"
        " VALUES ('g1', 'wp-cli-golden-g1', '2.1.330', '0.1.0', 'sha256:x', now(), 'ready');"
        " INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib,"
        " created_at)"
        " VALUES ('vm-00000001', 'wp-cli-arp-00000011', 'g1', 'pool', 'h', 4, 8192, '2026-11-21T12:00:00Z'),"
        " ('vm-00000002', 'wp-cli-arp-00000012', 'g1', 'pool', 'h', 4, 8192, '2026-11-21T12:00:01Z'),"
        " ('vm-00000003', 'wp-cli-arp-00000013', 'g1', 'stopped', 'h', 4, 8192, '2026-11-21T12:00:02Z')",
    )
    status.fake.add("wp-cli-arp-00000011", status="Running")
    code, out, err = status()
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "runner.pool.vm vm_id=vm-00000001 lima_name=wp-cli-arp-00000011 golden_version=g1"
        " created_at=2026-11-21T12:00:00Z instance=Running",
        "runner.pool.vm vm_id=vm-00000002 lima_name=wp-cli-arp-00000012 golden_version=g1"
        " created_at=2026-11-21T12:00:01Z instance=missing",
        "runner.pool size=1 ready=2 golden=g1 refill=full",
        "runner.capacity budget_mib=32768 used_mib=16384 slots_total=4 slots_free=2 pool_ready=2 capacity_low=no",
    ]


def test_refill_states() -> None:
    assert cli_pool.refill_state(0, 0, "g", 1) == "disabled"
    assert cli_pool.refill_state(1, 0, None, 1) == "no_golden"
    assert cli_pool.refill_state(1, 1, "g", 0) == "full"
    assert cli_pool.refill_state(1, 0, "g", 1) == "pending"
    assert cli_pool.refill_state(1, 0, "g", 0) == "no_capacity"

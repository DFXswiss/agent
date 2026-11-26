"""U-RUN-subagents: the subagent limits from the desired state and the guest version of a VM (ADR-088 sections 1 and
2): `subagents.limits` and `subagents.guest_features` (the pre-check, the executor, `get_context` and the harness use
them in `test_start.py`, `test_wiring.py`, `test_completion.py` and `queries/test_tools.py`)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import records, service
from agent_runner.store.pool import open_pool
from agent_runner.subagents import guest_features, limits
from agent_runner.subagents.limits import SubagentLimits
from agent_runner.vm import records as vm_records

SECTION = {
    "api": 1,
    "subagent_max_depth": 5,
    "subagent_max_running": 50,
    "subagent_max_total": 0,
    "subagent_max_runtime_seconds": 0,
}


def test_the_section_of_the_desired_state() -> None:
    assert limits.of({"limits": SECTION}) == SubagentLimits(5, 50, 0, 0)
    assert limits.of({"limits": {**SECTION, "api": 2, "unknown": 1}}) == SubagentLimits(5, 50, 0, 0)
    broken: dict[str, Any] | None
    for broken in (
        None,
        {},
        {"limits": []},
        {"limits": {**SECTION, "api": 0}},
        {"limits": {**SECTION, "api": True}},
        {"limits": {**SECTION, "api": "1"}},
        {"limits": {**SECTION, "subagent_max_depth": -1}},
        {"limits": {**SECTION, "subagent_max_running": True}},
        {"limits": {**SECTION, "subagent_max_total": None}},
        {"limits": {key: value for key, value in SECTION.items() if key != "subagent_max_runtime_seconds"}},
    ):
        assert limits.of(broken) is None


def test_current_reads_the_desired_state_the_session_area_keeps() -> None:
    assert limits.SERVICE_ATTRIBUTE == service.ATTRIBUTE
    assert limits.current(SimpleNamespace()) is None
    kept = SimpleNamespace(env=SimpleNamespace(desired={"limits": SECTION}))
    assert limits.current(SimpleNamespace(**{service.ATTRIBUTE: kept})) == SubagentLimits(5, 50, 0, 0)


def test_refusals_name_the_limit_its_value_and_the_setting() -> None:
    found = SubagentLimits(2, 3, 1, 0)
    assert limits.depth_refusal(None, 9) is None
    assert limits.depth_refusal(SubagentLimits(0, 0, 0, 0), 9) is None
    assert limits.depth_refusal(found, 1) is None
    assert "at most 2 levels deep" in str(limits.depth_refusal(found, 2))
    assert limits.running_refusal(None, 99) is None
    assert limits.running_refusal(found, 2) is None
    assert "(setting subagent_max_running)" in str(limits.running_refusal(found, 3))
    assert limits.total_refusal(None, 99) is None
    assert limits.total_refusal(SubagentLimits(2, 3, 0, 0), 99) is None
    assert limits.total_refusal(found, 0) is None
    assert str(limits.total_refusal(found, 1)).startswith("This task already started 1 subagent, the limit")
    assert limits.old_guest_depth_refusal(1) is None
    assert "until it runs the current guest version" in str(limits.old_guest_depth_refusal(2))


def test_get_context_limits_for_current_and_older_guests() -> None:
    unknown = dict.fromkeys(("subagent_max_depth", "subagent_max_parallel", "subagent_max_total"), None)
    assert limits.context_limits(None, True) == {**unknown, "subagent_max_runtime_seconds": None}
    assert limits.context_limits(None, False) == {
        "subagent_max_depth": 2,
        "subagent_max_parallel": 3,
        "subagent_max_total": 10,
    }
    assert limits.context_limits(SubagentLimits(1, 2, 30, 60), False) == {
        "subagent_max_depth": 1,
        "subagent_max_parallel": 2,
        "subagent_max_total": 10,
    }
    assert limits.values(SubagentLimits(1, 2, 3, 4)) == (1, 2, 3, 4)


@pytest.mark.parametrize(
    ("version", "known"),
    [("0.3.0", True), ("0.10.1", True), ("1.0.0-rc1", True), ("0.2.9", False), ("0.3", False), (None, False)],
)
def test_guest_versions(version: str | None, known: bool) -> None:
    assert guest_features.knows(version) is known


@pytest.mark.parametrize(
    ("version", "known"),
    [("0.5.0", True), ("0.10.0", True), ("1.0.0", True), ("0.4.9", False), ("0.4.0", False), (None, False)],
)
def test_guest_versions_that_know_the_names(version: str | None, known: bool) -> None:
    assert guest_features.knows(version, guest_features.NAMES) is known


def test_the_guest_version_comes_from_the_golden_of_the_vm(store_dsn: str) -> None:
    async def main() -> None:
        pool = await open_pool(store_dsn, max_size=2)
        try:
            assert await guest_features.current_guest(pool, None) is True
            assert await guest_features.current_guest(pool, "vm-0000000a") is False  # unknown VM
            async with pool.connection() as conn:
                for version, guest in (("old", "0.2.0"), ("new", "0.3.0")):
                    await conn.execute(
                        "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest,"
                        " state) VALUES (%s, %s, '2.1.330', %s, 'sha256:x', 'ready')",
                        (version, f"golden-{version}", guest),
                    )
            for n, golden in ((10, "old"), (11, "new")):
                await vm_records.insert(
                    pool,
                    vm_id=f"vm-{n:08x}",
                    lima_name=f"lima-{n}",
                    golden_version=golden,
                    state="running",
                    token_sha256="0" * 64,
                    cpus=2,
                    memory_mib=4096,
                    created_at=datetime(2026, 11, 25, tzinfo=UTC),
                )
            assert await guest_features.current_guest(pool, "vm-0000000a") is False
            assert await guest_features.current_guest(pool, "vm-0000000b") is True
            async with pool.connection() as conn:
                assert await guest_features.current_guest(conn, "vm-0000000b") is True
                assert await guest_features.current_guest(conn, "vm-0000000a") is False
            # "new" is 0.3.0: it knows ADR-088 but not the names of KOR-52 (NAMES = 0.5.0)
            assert await guest_features.current_guest(pool, "vm-0000000b", guest_features.NAMES) is False
            assert await guest_features.current_guest(pool, None, guest_features.NAMES) is True
            async with pool.connection() as conn:
                assert await guest_features.current_guest(conn, "vm-0000000b", guest_features.NAMES) is False
        finally:
            await pool.close()

    asyncio.run(main())


def test_the_room_of_a_vm(store_dsn: str) -> None:
    async def main() -> None:
        pool = await open_pool(store_dsn, max_size=2)
        context: Any = SimpleNamespace(config=SimpleNamespace(sessions_per_vm=1))
        try:
            async with pool.connection() as conn:
                assert await limits.vm_room_refusal(context, conn, None) is None
                assert await limits.vm_room_refusal(context, conn, "vm-0000000c") is None
                for state in ("working", "suspended", "idle"):
                    sid = str(uuid7())
                    await records.insert(
                        conn,
                        session_id=sid,
                        task_id=str(uuid7()),
                        org_id=str(uuid7()),
                        wg_id=str(uuid7()),
                        parent_id=None,
                        depth=0,
                        owner_id=str(uuid7()),
                        created_at=datetime(2026, 11, 25, tzinfo=UTC),
                    )
                    await records.update(conn, sid, state=state, vm_id="vm-0000000c")
                    if state == "working":
                        assert str(await limits.vm_room_refusal(context, conn, "vm-0000000c")).startswith(
                            "The computer of this task is full: 1 session runs in it,"
                        )
                context.config.sessions_per_vm = 2  # the suspended session does not occupy the VM
                assert str(await limits.vm_room_refusal(context, conn, "vm-0000000c")).startswith(
                    "The computer of this task is full: 2 sessions run in it,"
                )
                context.config.sessions_per_vm = 0
                assert await limits.vm_room_refusal(context, conn, "vm-0000000c") is None
        finally:
            await pool.close()

    asyncio.run(main())

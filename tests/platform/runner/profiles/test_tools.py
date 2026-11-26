"""U-RUN-profiles (KOR-51, ADR-094 section 3): work VM and browser tools need the type they stand for, reads stay
within the profile's reach, and every session start carries the profile (guest 0.4.0 and later) and a harness line."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent_runner.profiles import check, model, session_request
from agent_runner.queries.rights import ReadRefused, ReadScope
from agent_runner.sessions.records import LocalSession
from agent_runner.workvm.journal import Calls

from .test_state import MAIN, NARROW, READ_ONLY, WG, _session, section

RUNNER = {"name": "runner", "operations": ["workvm.request", "browser.open"], "reads": "workgroup"}


def local(sid: str = MAIN, vm_id: str | None = None, depth: int = 0) -> LocalSession:
    return LocalSession(
        id=sid,
        task_id="0193000b-0000-7000-8000-000000000002",
        org_id="0193000b-0000-7000-8000-000000000003",
        wg_id=WG,
        vm_id=vm_id,
        parent_id=None,
        depth=depth,
        state="working",
        claude_session_id=None,
        model_access="platform",
        model_id=None,
        owner_id="0193000b-0000-7000-8000-000000000004",
        created_at=datetime(2026, 11, 25, tzinfo=UTC),
        ready_at=None,
        ended_at=None,
    )


def with_pool(dsn: str, body: Any) -> None:
    async def main() -> None:
        async with AsyncConnectionPool(dsn, open=False) as pool:
            await pool.open()
            await body(pool)

    asyncio.run(main())


async def _store(pool: Any, profile: dict[str, Any] | None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.profile_state (id, section, received_at) VALUES (1, %s, now())"
            " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section",
            (Jsonb(section({MAIN: profile})),),
        )


def test_work_vm_and_browser_tools(store_dsn: str) -> None:
    async def body(pool: Any) -> None:
        async with pool.connection() as conn:
            assert await check.tool(conn, local(), "workvm_exec") is None  # no section: no profiles
        await _store(pool, READ_ONLY)
        async with pool.connection() as conn:
            refused = await check.tool(conn, local(), "workvm_exec")
            assert refused is not None
            assert "workvm_exec (it needs workvm.request)" in refused
            assert await check.tool(conn, local(depth=1), "browser_click") is not None
            assert await check.tool(conn, local(), "get_task") is None
        await _store(pool, RUNNER)
        async with pool.connection() as conn:
            assert await check.tool(conn, local(), "workvm_exec") is None
        assert await check.reach(pool, MAIN, None) == "workgroup"

    with_pool(store_dsn, body)


def test_a_refused_tool_is_answered_forbidden() -> None:
    answers: list[Any] = []

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def answer_pending(self, pending_id: str, answer: Any) -> None:
            answers.append((pending_id, answer))

    async def client(_vm: Any) -> Client:
        return Client()

    async def main() -> None:
        env = SimpleNamespace(client=client, log=print)
        task = Calls(None).spawn(env, None, local(), pending_id="p1", tool="workvm_exec", arguments={}, refused="No.")  # type: ignore[arg-type]
        assert task is not None
        await task

    asyncio.run(main())
    assert answers == [("p1", {"status": "error", "error": {"error": "forbidden", "message": "No."}})]


def test_reads_beyond_the_reach() -> None:
    assert check.read_refusal("all", "list_tasks", {}) is None
    assert check.read_refusal("all", "list_members", {"scope": "org"}) is None
    assert check.read_refusal("workgroup", "list_members", {"scope": "org"}) is not None
    assert check.read_refusal("workgroup", "list_members", {"scope": "workgroup"}) is None
    assert check.read_refusal("workgroup", "list_tasks", {}) is None
    refused = check.read_refusal("task", "list_tasks", {})
    assert refused is not None
    assert refused.error == "forbidden"
    assert check.read_refusal("task", "get_whiteboard", {"level": "workgroup"}) is not None
    assert check.read_refusal("task", "get_whiteboard", {"level": "task", "task_id": MAIN}) is None
    assert check.read_refusal("task", "get_task", {"task_id": MAIN}) is None
    # the workgroup-wide reads of ADR-095: within reach workgroup, beyond reach task as a whole
    for tool in ("get_usage", "list_audit", "list_workgroup_operations", "get_metrics"):
        assert check.read_refusal("workgroup", tool, {}) is None
        assert check.read_refusal("task", tool, {}) is not None

    def scope(reach: str) -> ReadScope:
        return ReadScope(
            org_id="o",
            wg_id="w",
            owner_id="u",
            state=None,  # type: ignore[arg-type]
            excluded=frozenset(),
            access="write",
            device_id="d",
            actor_id=None,
            task_id="t",
            reach=reach,
        )

    scope("all").outside_reach("other", "other")
    scope("workgroup").outside_reach("w", "other")
    scope("task").outside_reach("w", "t")
    scope("task").outside_reach("w")
    for reach, wg, task in (("workgroup", "other", None), ("task", "other", "t"), ("task", "w", "other")):
        with pytest.raises(ReadRefused) as refused_read:
            scope(reach).outside_reach(wg, task)
        assert refused_read.value.error == "forbidden"
    with pytest.raises(ReadRefused):
        scope("workgroup").readable("other")


def test_the_session_start_carries_the_profile(store_dsn: str) -> None:
    async def body(pool: Any) -> None:
        context = SimpleNamespace(pool=pool)
        request: dict[str, Any] = {"harness": {"context_markdown": "Context.\n"}}
        await session_request.amend(context, local(), request)
        assert request == {"harness": {"context_markdown": "Context.\n"}}  # no profile: nothing
        await _store(pool, NARROW)
        await session_request.amend(context, local(), request)
        assert request["profile"] == NARROW
        line = request["harness"]["context_markdown"].splitlines()[-1]
        assert line.startswith("Rights profile: narrow. Operation types this session may request: github.get_issue.")
        assert "ask the person who gave the instruction" in line
        async with pool.connection() as conn:
            await _session(conn, MAIN, None, 0)
            await conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
                " VALUES ('g1', 'golden-g1', '1', '0.3.0', 'sha256:x', 'ready')"
            )
            await conn.execute(
                "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib)"
                " VALUES ('vm-00000001', 'vm-00000001', 'g1', 'running', 'x', 1, 1024)"
            )
        older: dict[str, Any] = {"harness": {"context_markdown": "x"}}
        await session_request.amend(context, local(vm_id="vm-00000001", depth=1), older)
        assert "profile" not in older
        assert "ask the session that started you" in older["harness"]["context_markdown"]

    with_pool(store_dsn, body)


def test_guest_versions() -> None:
    assert session_request.knows_profiles("0.4.0")
    assert session_request.knows_profiles("1.0.0-rc1")
    assert not session_request.knows_profiles("0.3.9")
    assert not session_request.knows_profiles("0.4")
    assert not session_request.knows_profiles(None)
    assert session_request.harness_line(model.Profile("none", frozenset(), "task"), 0).count("none") == 2

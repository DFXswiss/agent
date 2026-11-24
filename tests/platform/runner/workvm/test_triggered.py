"""Work VM manager (KOR-46, ADR-083 section 2): a new work VM keeps the person whose turn requested it
(`workvm.triggered_by`) - the operation's acting user, or for a VM a tool call obtained implicitly the session's acting
user at that moment - on the cold path and from the warm pool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.github import snapshot
from agent_runner.store.migrate import migrate
from agent_runner.workvm import manager as manager_module
from agent_runner.workvm import records
from agent_runner.workvm.manager import WorkVms

from .kit import ORG, SESSION, TASK, WG, World, session

ACTOR = "0193000a-0000-7000-8000-0000000000c1"


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(_env: Any, _local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


def triggered(world: World) -> list[str | None]:
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        return [row[0] for row in conn.execute("SELECT triggered_by::text FROM runner.workvm ORDER BY created_at, id")]


def test_a_cold_request_keeps_the_acting_user(world: World) -> None:
    async def body(vms: WorkVms) -> records.WorkVm | None:
        await vms.request(session(), {}, triggered_by=ACTOR)
        return await records.for_scope(vms.deps.db, task_id=TASK, session_id=None)

    found = world.run(body)
    assert found is not None
    assert found.triggered_by == ACTOR
    assert triggered(world) == [ACTOR]


def test_a_pooled_request_keeps_the_acting_user(world: World) -> None:
    world.settings = manager_module.Settings(**{**world.settings.__dict__, "pool_size": 1, "slots": 2})

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.pool_pass()
        return await vms.request(session(), {}, triggered_by=ACTOR)

    assert world.run(body)["created"] is True
    assert any("pooled=1" in line for line in world.logs)
    assert triggered(world) == [ACTOR]


def test_an_implicit_vm_keeps_the_sessions_acting_user(world: World) -> None:
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, turn_actor) VALUES (%s, %s, %s, %s,"
            " 'working', %s)",
            (SESSION, TASK, ORG, WG, ACTOR),
        )
    result = world.run(lambda vms: vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"}))
    assert result["state"] == "running"
    assert triggered(world) == [ACTOR]

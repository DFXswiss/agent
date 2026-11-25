"""U-RUN-workvm reconcile step `workvm` (KOR-43, ADR-080 sections 4 and 5): a closed task deletes its VMs and cancels
its runs, an exhausted organization's running work VMs stop; malformed entries of the untrusted desired state are
ignored."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_runner.github import snapshot
from agent_runner.registry import ReconcileStep
from agent_runner.store.migrate import migrate
from agent_runner.workvm import reconcile, service
from agent_runner.workvm.manager import WorkVms

from .kit import CHILD, ORG, TASK, TASK_B, World, session


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


def run_of(**fields: Any) -> Any:
    return SimpleNamespace(**fields)


def rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT task_id::text AS task, scope, state, delete_reason FROM runner.workvm"))


def test_step_is_registered() -> None:
    assert [ReconcileStep(45, "workvm", reconcile.step)] == reconcile.STEPS


def test_closed_tasks_of_the_desired_state() -> None:
    sessions = [
        {"task_id": TASK, "task_closed": True},
        {"task_id": TASK, "task_closed": True},
        {"task_id": TASK_B, "task_closed": False},
        {"task_id": "not-a-task", "task_closed": True},
        {"task_id": 7, "task_closed": True},
        "nonsense",
    ]
    assert reconcile.closed_tasks({"sessions": sessions}) == [TASK]
    assert reconcile.closed_tasks({"sessions": "nonsense"}) == []
    assert reconcile.closed_tasks({}) == []


def test_the_step_closes_tasks_and_stops_the_vms_of_an_exhausted_organization(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled: list[dict[str, Any]] = []

    def cancel(**found: Any) -> list[str]:
        cancelled.append(found)
        return []

    monkeypatch.setattr(reconcile, "runs_of", lambda context, workvms: SimpleNamespace(cancel=cancel))
    browsers: list[str] = []

    async def close_task(task_id: str) -> list[str]:
        browsers.append(f"task_closed:{task_id}")
        return []

    async def credit_pass() -> list[str]:
        browsers.append("credit")
        return []

    monkeypatch.setattr(
        reconcile,
        "browsers_of",
        lambda context, workvms: SimpleNamespace(close_task=close_task, credit_pass=credit_pass),
    )

    world.settings = dataclasses.replace(world.settings, slots=4)

    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        context = SimpleNamespace()
        service.install(context, vms)
        await vms.request(session(), {})
        await vms.request(session(CHILD, task_id=TASK_B), {})
        await vms.obtain(session(CHILD, task_id=TASK_B), scope="run", size="standard")
        await reconcile.step(run_of(context=context, desired=None))  # no desired state: nothing
        await reconcile.step(run_of(context=SimpleNamespace(), desired={}))  # no work VMs: nothing
        world.exhausted.add(ORG)
        desired = {"sessions": [{"task_id": TASK, "task_closed": True}]}
        await reconcile.step(run_of(context=context, desired=desired))
        return cancelled

    assert world.run(body) == [{"reason": "task_closed", "task_id": TASK}]
    assert browsers == [f"task_closed:{TASK}", "credit"]  # the browser sessions close with the same causes
    found = {(row["task"], row["scope"]): (row["state"], row["delete_reason"]) for row in rows(world)}
    assert found[(TASK, "task")] == ("deleted", "task_closed")
    assert found[(TASK_B, "task")] == ("stopped", None)  # credit: stopped, not deleted
    assert found[(TASK_B, "run")] == ("running", None)  # a run ends through its own run (cancelled within one poll)

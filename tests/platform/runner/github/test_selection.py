"""U-RUN-github (KOR-47): the GitHub identity choices and defaults of the desired state are kept in the store
(`github_selection`, `github_default`; ADR-076 section 3): replaced by every answer, kept without one, tolerant of a
hub from before KOR-47, and enough for the resolution while the hub cannot be reached."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.credentials import identities
from agent_runner.crypto.ids import uuid7
from agent_runner.github import identity, selection
from agent_runner.github.reconcile import STEPS
from agent_runner.operations.extensions import PrecheckRequest
from agent_runner.operations.settings import DEFAULTS
from agent_runner.reconcile.wire import ResponseInvalid

from ..credentials.identity_kit import (
    ORG_A,
    SESSION,
    TASK,
    WG_A,
    IdentityHub,
    identity_body,
    operation_of,
    session_of,
)
from ..credentials.kit import ALICE, Env, kit_env, pid

Body = Callable[[Env, IdentityHub], Awaitable[None]]
OTHER_SESSION = pid("session/two")


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())


def desired_session(session_id: str = SESSION, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "task_id": TASK,
        "org_id": ORG_A,
        "wg_id": WG_A,
        "state": "working",
        "parent_session_id": None,
        "depth": 0,
        "owner_id": ALICE,
        "model_access_pref": "auto",
        "model_id_pref": None,
        **extra,
    }


async def rows(env: Env) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    selected = await env.rows(
        "SELECT session_id::text, task_id::text, github_identity::text, task_github_identity::text"
        " FROM runner.github_selection ORDER BY session_id"
    )
    defaults = await env.rows(
        "SELECT scope, scope_id::text, identity_id::text FROM runner.github_default ORDER BY scope"
    )
    return selected, defaults


def test_the_step_keeps_the_last_answer_and_resolution_works_without_the_hub(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await env.add_ck(owner=ORG_A)
        chosen = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org")
        fallback = pid("default")
        state = env.hub.active_state

        def answer() -> dict[str, Any]:
            found = state()
            found["sessions"] = [
                desired_session(github_identity=None, task_github_identity=chosen),
                desired_session(OTHER_SESSION, github_identity=chosen, task_github_identity=chosen),
            ]
            found["github_defaults"] = [
                {"scope": "wg", "scope_id": WG_A, "identity_id": fallback},
                {"scope": "org", "scope_id": ORG_A, "identity_id": fallback},
            ]
            return found

        env.hub.active_state = answer  # type: ignore[method-assign]
        step = STEPS[1]
        assert (step.order, step.name) == (45, "github_selection")
        await step.run(await env.desired())
        assert await rows(env) == (
            sorted([(SESSION, TASK, None, chosen), (OTHER_SESSION, TASK, chosen, chosen)]),
            [("org", ORG_A, fallback), ("wg", WG_A, fallback)],
        )
        await identities.fetch(env.context, chosen)
        # the hub is not reached: the stored choice still decides, nothing falls back to another account
        no_state = await env.desired()
        no_state.desired = None
        await step.run(no_state)
        asked = len(env.hub.requests)
        session: Any = session_of()
        request = PrecheckRequest(env.context, operation_of(), session, DEFAULTS, {})
        found = await identity.resolve(request)
        assert found.identity_id == chosen
        assert len(env.hub.requests) == asked
        # the next answer replaces everything; a hub from before KOR-47 sends none of the fields
        env.hub.active_state = state  # type: ignore[method-assign]
        await step.run(await env.desired())
        assert await rows(env) == ([], [])

    run(store_dsn, body)


def test_an_old_hub_without_the_fields_means_no_choice_and_no_default(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        async with env.pool.connection() as conn, conn.transaction():
            await selection.store(conn, {"sessions": [desired_session()]})
            assert await selection.choices(conn, session_of()) == selection.Choices(None, None)
            assert await selection.defaults(conn, ORG_A, WG_A) == (None, None)
            await selection.store(conn, {})
        assert await rows(env) == ([], [])

    run(store_dsn, body)


def test_a_new_session_takes_the_task_choice_of_another_session_of_the_task(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        chosen = pid("chosen")
        answer = {
            "sessions": [
                desired_session(OTHER_SESSION, task_github_identity=None),
                desired_session(pid("session/three"), task_github_identity=chosen),
            ]
        }
        async with env.pool.connection() as conn, conn.transaction():
            await selection.store(conn, answer)
            assert await selection.choices(conn, session_of()) == selection.Choices(None, chosen)
            assert await selection.choices(conn, session_of(task_id=pid("task/none"))) == selection.Choices(None, None)

    run(store_dsn, body)


@pytest.mark.parametrize(
    "answer",
    [
        {"sessions": "x"},
        {"sessions": ["x"]},
        {"sessions": [desired_session(github_identity="not-a-uuid")]},
        {"sessions": [desired_session(task_github_identity=7)]},
        {"sessions": [desired_session(session_id="x")]},
        {"github_defaults": "x"},
        {"github_defaults": ["x"]},
        {"github_defaults": [{"scope": "task", "scope_id": ORG_A, "identity_id": ORG_A}]},
        {"github_defaults": [{"scope": "org", "scope_id": "x", "identity_id": ORG_A}]},
        {"github_defaults": [{"scope": "org", "scope_id": ORG_A, "identity_id": None}]},
    ],
)
def test_a_malformed_answer_fails_the_step_and_keeps_the_store(store_dsn: str, answer: dict[str, Any]) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        async with env.pool.connection() as conn, conn.transaction():
            await selection.store(conn, {"sessions": [desired_session(task_github_identity=pid("kept"))]})
        run_ = await env.desired()
        run_.desired = answer
        with pytest.raises(ResponseInvalid):
            await STEPS[1].run(run_)
        assert (await rows(env))[0] == [(SESSION, TASK, None, pid("kept"))]

    run(store_dsn, body)


async def subagent_request(env: Env, child: str, registration: dict[str, Any] | None) -> None:
    """A `subagent.request` of the parent that started `child` (its registration as stored)."""
    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.operation (id, session_id, task_id, wg_id, type, state, params, child_session_id,"
            " registration) VALUES (%s, %s, %s, %s, 'subagent.request', 'succeeded', '{}', %s, %s)",
            (uuid7(), PARENT, TASK, WG_A, child, None if registration is None else Jsonb(registration)),
        )


PARENT = pid("session/parent")


def test_a_subagent_not_listed_yet_takes_the_identity_of_its_request_never_the_tasks(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        chosen, parents, tasks = pid("chosen"), pid("parents"), pid("tasks")
        answer = {"sessions": [desired_session(PARENT, github_identity=parents, task_github_identity=tasks)]}
        async with env.pool.connection() as conn, conn.transaction():
            await selection.store(conn, answer)

        def child(n: int) -> Any:
            return session_of(session_id=pid(f"child/{n}"), depth=1, parent_id=PARENT)

        await subagent_request(env, pid("child/1"), {"github_identity": chosen})
        await subagent_request(env, pid("child/2"), {"operation_id": "x"})  # none named: the parent's
        await subagent_request(env, pid("child/3"), None)  # not registered yet: unknown
        orphan = session_of(session_id=pid("child/4"), depth=1, parent_id=pid("session/gone"))
        await subagent_request(env, pid("child/4"), {"operation_id": "x"})  # the parent's row is not known either
        async with env.pool.connection() as conn:
            assert await selection.choices(conn, child(1)) == selection.Choices(chosen, tasks)
            assert await selection.choices(conn, child(2)) == selection.Choices(parents, tasks)
            assert await selection.choices(conn, child(3)) is None
            assert await selection.choices(conn, child(5)) is None  # no request at all
            assert await selection.choices(conn, orphan) is None

    run(store_dsn, body)

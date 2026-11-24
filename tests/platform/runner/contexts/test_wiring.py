"""U-RUN-contexts: registration at the extension points, the daemon task, the production environment, the
pull-request links and the settings (ADR-072 sections 4, 6 and 8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.config import load_config
from agent_runner.contexts import prlinks, session_request, store, tasks
from agent_runner.contexts.env import ContextEnv, contexts_for, existing
from agent_runner.crypto import context as crypto
from agent_runner.sessions.extensions import request_amenders, state_observers
from agent_runner.sync.http import HubRefusal

from ..prompts.kit import run
from ..reconcile.rig import Rig, scope_key
from ..sessions.kit import KEY, TASK, WG, session_id
from .kit import ckit, transport_error
from .test_checkpoint import objects


def test_the_area_is_registered_at_its_extension_points() -> None:
    from agent_runner.vm.extensions import retire_hooks  # noqa: PLC0415

    assert "contexts.checkpoint" in [hook.name for hook in retire_hooks()]
    assert "contexts" in [task.name for task in registry.tasks()]
    assert "contexts.checkpoint" in [observer.name for observer in state_observers()]
    assert "contexts.restore" in [amender.name for amender in request_amenders()]


def test_the_settings_have_generous_defaults_and_bounds(tmp_path: Path) -> None:
    config = load_config({}, platform="darwin", require=frozenset(), expand_user=lambda _p: str(tmp_path))
    assert (config.context_interval_seconds, config.context_cache_mib) == (600, 20_480)
    assert (config.context_workspace_mib, config.context_transcript_mib) == (4096, 4096)
    assert config.context_wait_seconds == 180
    assert config.contexts_dir == config.home / "contexts"
    custom = load_config(
        {"AGENT_RUNNER_CONTEXT_INTERVAL_SECONDS": "60", "AGENT_RUNNER_CONTEXT_CACHE_MIB": "0"},
        platform="darwin",
        require=frozenset(),
        expand_user=lambda _p: str(tmp_path),
    )
    assert (custom.context_interval_seconds, custom.context_cache_mib) == (60, 0)
    from agent_runner.config import environment  # noqa: PLC0415
    from agent_runner.errors import RunnerError  # noqa: PLC0415

    assert environment(custom)["AGENT_RUNNER_CONTEXT_INTERVAL_SECONDS"] == "60"
    with pytest.raises(RunnerError):
        load_config({"AGENT_RUNNER_CONTEXT_INTERVAL_SECONDS": "5"}, platform="darwin", require=frozenset())


class Manager:
    on_unreachable: Any = None

    async def wait_reconciled(self) -> None:
        return None


def test_contexts_for_builds_one_environment_from_the_daemon(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        context = env.context
        key_file = tmp_path / "key"
        key_file.write_text(KEY)
        key_file.chmod(0o600)
        config = load_config({}, platform="darwin", require=frozenset(), expand_user=lambda _p: str(tmp_path))
        context.config = SimpleNamespace(
            platform_root_key=context.config.platform_root_key,
            model_catalog=None,
            openrouter_key_file=key_file,
            model_upstream_openrouter="https://openrouter.ai/api",
            session_idle_seconds=120,
            contexts_dir=config.contexts_dir,
            tmp_dir=config.tmp_dir,
            context_interval_seconds=300,
            context_cache_mib=1,
            context_workspace_mib=2,
            context_transcript_mib=3,
            context_wait_seconds=7,
        )
        context._vm_manager = Manager()
        assert existing(context) is None
        built = contexts_for(context)
        assert isinstance(built, ContextEnv)
        assert contexts_for(context) is built
        assert existing(context) is built
        assert (built.interval_s, built.cache_bytes, built.workspace_bytes) == (300.0, 1 << 20, 2 << 20)
        assert built.transcript_bytes == 3 << 20
        assert built.wait_s == 7.0
        assert built.session_lock("a") is built.session_lock("a")
        assert built.cache.root == config.contexts_dir
        assert contexts_for(SimpleNamespace(identity=None)) is None
        # the amender without an enrolled device changes nothing
        found: dict[str, Any] = {"harness": {"context_markdown": "x"}}
        await session_request.amend(SimpleNamespace(identity=None), None, found)  # type: ignore[arg-type]
        assert found == {"harness": {"context_markdown": "x"}}
        # the task ends at once without an enrolled device
        bare = SimpleNamespace(identity=None, log=context.log)
        await tasks.run_contexts(bare)
        assert "runner.contexts not_enrolled" in env.log()

    run(rig, body)


def test_the_task_runs_rounds_until_stopped_and_survives_a_failing_round(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="idle")
        from .kit import archive  # noqa: PLC0415

        ck.guest.archives[sid] = archive()
        async with env.pool.connection() as conn:
            await store.mark_due(
                conn, session_id=sid, task_id=TASK, org_id=str(ck.pk.kit.guest.org_id), wg_id=WG, at=ck.contexts.now()
            )
        stopping = asyncio.Event()
        rounds = 0
        original = prlinks.round

        async def counted(found: ContextEnv) -> int:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                ck.contexts.wake.set()  # a due checkpoint: the next round starts at once
                raise RuntimeError("boom")
            stopping.set()
            return await original(found)

        prlinks_round = prlinks.round
        setattr(prlinks, "round", counted)  # noqa: B010
        try:
            env.context.stopping = stopping
            await asyncio.wait_for(tasks.run_contexts(env.context), 5)
        finally:
            setattr(prlinks, "round", prlinks_round)  # noqa: B010
        assert rounds == 2
        assert any("round_failed error=RuntimeError" in line for line in ck.lines)
        assert ck.contexts.cache.root.is_dir()
        assert len(await objects(ck)) == 1  # exported by the first loop; the upload loop runs beside it

    run(rig, body)


async def succeeded_pull(env: Any, n: int = 1, *, repo: str = "Owner/Repo", number: object = 7) -> str:
    """A succeeded `github.create_pull_request` operation of session 1."""
    op_id = f"01930007-0000-7000-8000-{n:012d}"
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    from ..sessions.kit import ORG  # noqa: PLC0415

    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.operation (id, session_id, task_id, wg_id, org_id, type, state, params, result)"
            " VALUES (%s, %s, %s, %s, %s, 'github.create_pull_request', 'succeeded', %s, %s)",
            (
                op_id,
                session_id(1),
                TASK,
                WG,
                ORG,
                Jsonb({"repo": repo, "head": "a", "base": "b", "title": "t"}),
                Jsonb({"number": number, "url": "https://example.com/pr", "id": 1, "draft": True}),
            ),
        )
    return op_id


def test_pull_requests_are_registered_once_as_opaque_tags(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        op_id = await succeeded_pull(env)
        await succeeded_pull(env, 2, number="seven")  # not a number: never registered
        assert await prlinks.round(ck.contexts) == 1
        [link] = ck.store.links
        assert link == {
            "session_id": session_id(1),
            "epoch": 1,
            "tag": crypto.pr_link_tag(crypto.pr_link_key(scope_key("wg", WG, 1)), "owner/repo", 7),
        }
        assert await prlinks.round(ck.contexts) == 0  # done: never again
        assert any(f"pr_link_registered operation_id={op_id}" in line for line in ck.lines)

    run(rig, body)


def test_pull_request_registration_failures(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        first = await succeeded_pull(env)
        ck.hub.errors = [transport_error()]
        assert await prlinks.round(ck.contexts) == 1
        assert await prlinks.round(ck.contexts) == 0  # waits for its backoff
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_pr_link SET next_at = NULL")
        ck.hub.errors = [HubRefusal(404, "not_found")]  # an older hub: later again
        assert await prlinks.round(ck.contexts) == 1
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_pr_link SET next_at = NULL")
            cur = await conn.execute("SELECT attempts, state FROM runner.context_pr_link")
            assert await cur.fetchone() == (2, "pending")
        ck.store.refuse["link"] = (403, "forbidden")
        assert await prlinks.round(ck.contexts) == 1
        assert any(f"pr_link_refused code=forbidden operation_id={first}" in line for line in ck.lines)
        # a repository name the tag cannot take, and a workgroup without key
        await succeeded_pull(env, 3, repo="no-slash")
        assert await prlinks.round(ck.contexts) == 1
        assert any("code=params_invalid" in line for line in ck.lines)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.scope_key WHERE scope = 'wg'")
        await succeeded_pull(env, 4)
        assert await prlinks.round(ck.contexts) == 1
        assert any("key_missing" in line for line in ck.lines)

    run(rig, body)


def test_the_cache_refuses_a_name_that_is_no_object_id_and_the_default_sleep_waits(tmp_path: Path) -> None:
    from agent_runner.contexts import env as env_module  # noqa: PLC0415
    from agent_runner.contexts.cache import Cache  # noqa: PLC0415

    with pytest.raises(ValueError, match="object id"):
        Cache(tmp_path).directory("../x")
    asyncio.run(env_module._sleep(0))


def test_the_observer_without_an_installed_environment_only_marks_due(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from .test_checkpoint import due_rows, observe  # noqa: PLC0415

        ck = await ckit(env)
        sid = await ck.pk.add_session(state="working")
        del env.context.context_env
        await observe(ck, sid, state="idle", at="2026-11-21T12:00:02.000Z")
        assert [row.session_id for row in await due_rows(ck)] == [sid]

    run(rig, body)

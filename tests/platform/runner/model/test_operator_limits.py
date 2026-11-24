"""U-RUN-model (KOR-50, ADR-081 sections 2 and 3; ADR-052 section 6): the operator's pool with used-up files - the
choice by remaining quota, the `429` that count towards a file's streak (also the ones of the session that deferred
it), the `401`/`403` blocks kept across a restart until the file changes, and what a waiting start may use."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent_runner.model import limits, operator
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.model.operator import BLOCKED, DEFERRED, Pool

from .fallback_kit import AT, HOUR, T0, Wall
from .kit import Env, model_env
from .test_operator import ORG, Clock, make, pool_dir, put, token


def test_the_choice_prefers_the_file_with_the_most_room(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    for name in ("a", "b", "c"):
        put(directory, name, token())
    pool = make(directory)
    pool.scan()
    pool.bindings = {"s1": "b"}
    pool.quota = {"a": 90.0, "b": 10.0}
    assert pool.choose() == "b"  # the most room, although a session is bound to it
    pool.quota["c"] = 10.0
    assert pool.choose() == "c"  # equal room: the fewest sessions
    pool.quota = {"a": 50.0}
    assert pool.choose() == "a"  # a known quota before the unknown ones
    used_up = {"a"}
    pool.exhausted = lambda name: name in used_up
    assert pool.state("a") == operator.EXHAUSTED
    assert pool.current("a") is None
    assert pool.choose() == "c"


def test_the_429_of_the_session_that_deferred_the_file_count_others_do_not(tmp_path: Path) -> None:
    directory = pool_dir(tmp_path)
    put(directory, "a", token(1))
    put(directory, "b", token(2))
    lines: list[str] = []
    clock = Clock()
    pool = make(directory, lines, clock)
    pool.scan()
    assert pool.observe("a", 429, None, "s1")
    assert pool.state("a") == DEFERRED
    deferred_until = pool.deferred["a"]
    clock.now += 10
    assert pool.observe("a", 429, None, "s1")  # the same file: counts, no second deferral
    assert pool.deferred["a"] == deferred_until
    assert not pool.observe("b", 429, None, "s1")  # another file within the limit: ignored
    held = pool.current("a")
    assert held is not None
    assert not pool.observe("a", 401, held[1], "s1")  # a block is no repeated deferral
    assert pool.state("a") == DEFERRED
    assert not pool.observe("a", 200, None, "s2")
    assert not pool.observe("gone", 429, None, "s3")
    assert pool.acted_on == {"s1": "a"}


def run(dsn: str, body: object) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            limits.limits_for(env.context).wall = Wall()
            await body(env)  # type: ignore[operator]

    asyncio.run(main())


def install(env: Env, directory: Path) -> Pool:
    pool = Pool(directory=directory, orgs=frozenset({ORG}), clock=Clock())
    operator.attach(env.context, pool)
    setattr(env.context, operator.ATTRIBUTE, pool)
    return pool


def test_blocks_survive_a_restart_until_the_file_changes(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        directory = pool_dir(tmp_path)
        put(directory, "a", token(1))
        put(directory, "b", token(2))
        pool = install(env, directory)
        await operator.load(env.context, pool)
        pool.scan()
        held = pool.current("a")
        assert held is not None
        await operator.bind(env.context, pool, "01930005-0000-7000-8000-000000000001", "a")
        pool.clock.now += operator.GRACE_S  # type: ignore[attr-defined]
        await operator.observed(env.context, "01930005-0000-7000-8000-000000000001", 401, held[1])
        assert pool.state("a") == BLOCKED
        rows = await env.rows("SELECT access_key, kind, identity FROM runner.access_limit")
        assert rows == [("operator:a", "blocked", operator.identity_text(held[1]))]
        await operator.keep_blocks(env.context, pool)  # unchanged: nothing to write
        # a new daemon reads the block and keeps it while the file is the same
        delattr(env.context, limits.ATTRIBUTE)
        limits.limits_for(env.context).wall = Wall()
        fresh = install(env, directory)
        await operator.load(env.context, fresh)
        fresh.scan()
        assert fresh.state("a") == BLOCKED
        assert operator.parse_identity(operator.identity_text(held[1])) == held[1]
        # renewed: the block ends, also in the store
        put(directory, "a", token(3))
        fresh.scan()
        await operator.prune(env.context, set())
        assert await env.rows("SELECT access_key FROM runner.access_limit") == []

    run(store_dsn, body)


def test_what_a_waiting_start_may_use(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        assert operator.used_up(env.context, ORG) == []  # no pool
        assert operator.held(env.context, "a") is None
        assert await operator.peek(env.context, ORG) is None
        directory = pool_dir(tmp_path)
        for name in ("a", "b", "c"):
            put(directory, name, token())
        pool = install(env, directory)
        assert await operator.peek(env.context, "00000000-0000-4000-8000-000000000000") is None
        assert await operator.peek(env.context, ORG) == "a"
        await limits.mark(env.context, Limit("operator:b", EXHAUSTED, AT + HOUR, True, T0))
        await limits.mark(env.context, Limit("operator:a", EXHAUSTED, AT + 2 * HOUR, True, T0))
        assert operator.used_up(env.context, ORG) == [(AT + HOUR, "b"), (AT + 2 * HOUR, "a")]
        assert operator.used_up(env.context, "00000000-0000-4000-8000-000000000000") == []
        held = operator.held(env.context, "b")
        assert held is not None
        assert held[0] == token()
        assert operator.held(env.context, "c") is None  # usable: not a waiting start's file
        assert operator.held(env.context, "gone") is None
        assert pool.state("c") == operator.USABLE

    run(store_dsn, body)


def test_a_pool_without_daemon_context_is_never_used_up(tmp_path: Path) -> None:
    pool = make(pool_dir(tmp_path))
    assert not pool.exhausted("a")
    holder = SimpleNamespace()
    operator.attach(holder, pool)
    assert not pool.exhausted("a")

"""U-RUN-model: sessions on the operator's subscriptions (KOR-23, ADR-052; 05-runner.md sections 10.1, 10.2).

Real session starts against the fake guest over the credential hub of RUN-09 and a real store: the choice of the
pool in the order of the access kinds, the binding to a file, the renewal when the file is replaced while the session
runs, the block after `401`/`403` from the gateway's `usage` records and the move to another file, the deferral after
`429`, the withdrawal when the organization is no longer approved, the restart of the daemon and the daemon task.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner import registry
from agent_runner.model import access, gateways, operator, tasks
from agent_runner.model.access import AccessRefused, provisions
from agent_runner.model.operator import BLOCKED, DEFERRED, Pool
from agent_runner.model.reconcile import STEPS
from agent_runner.orgs.book import book_for
from agent_runner.reconcile.engine import Run
from agent_runner.sessions import records
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..sessions.kit import desired, session_id
from .kit import (
    API_KEY,
    KEY,
    OPUS,
    TOKEN,
    Env,
    Kit,
    api_key_body,
    attach,
    credentials,
    model_env,
    model_kit,
    store_record,
    subscription_body,
)
from .test_operator import MARKER, Clock, pool_dir, put, token

REPO = Path(__file__).resolve().parents[4]
SCHEMAS = SchemaSet(REPO / "contracts")
Body = Callable[[Env, Path], Awaitable[None]]


def run(dsn: str, tmp_path: Path, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            await body(env, pool_dir(tmp_path))

    asyncio.run(main())


def install(env: Env, directory: Path, *, approved: bool = True) -> tuple[Pool, list[str], Clock]:
    """The pool of the daemon context, as `operator.pool_for` builds it from the configuration (test clock)."""
    org = desired().org_id
    orgs = frozenset({org if approved else "00000000-0000-4000-8000-000000000000"})
    env.context.config.operator_subscription_dir = directory
    env.context.config.operator_subscription_orgs = orgs
    lines: list[str] = []
    clock = Clock()
    pool = Pool(directory=directory, orgs=orgs, clock=clock, log=lines.append)
    setattr(env.context, operator.ATTRIBUTE, pool)
    return pool, lines, clock


async def stored_bindings(env: Env) -> dict[str, str]:
    async with env.pool.connection() as conn:
        cursor = await conn.execute("SELECT session_id::text, file_name FROM runner.operator_binding")
        return {row[0]: row[1] for row in await cursor.fetchall()}


async def start(found: Kit, n: int = 1, preference: str = "auto") -> Any:
    return await Starter(found.session_env).start(desired(n, model_access_pref=preference))


async def step(found: Kit) -> None:
    run_ = Run(context=found.env.context, sync=found.env.sync, book=book_for(found.env.context))
    run_.desired = dict(found.session_env.desired or {})
    await STEPS[0].run(run_)


async def running(env: Env, directory: Path, files: tuple[str, ...] = ("a",)) -> tuple[Kit, Pool, list[str], Clock]:
    for n, name in enumerate(files, 1):
        put(directory, name, token(n))
    pool, lines, clock = install(env, directory)
    found = await model_kit(env)
    attach(found)
    assert (await start(found)).started
    found.guest.calls.clear()
    return found, pool, lines, clock


async def consume(found: Kit) -> None:
    row = await vm_records.get(found.env.pool, "vm-00000001")
    assert row is not None
    reader = VmJournal(found.session_env, row, handlers=registry.journal_handlers())
    stopping = asyncio.Event()
    task = asyncio.ensure_future(reader.run(stopping))
    last = found.guest.records[-1].seq
    for _ in range(6000):
        if found.guest.acked >= last or task.done():
            break
        await asyncio.sleep(0.005)
    stopping.set()
    await task


def answered(found: Kit, status: int, sid: str | None = None) -> None:
    """The gateway's `usage` record of one request the upstream answered with `status`."""
    zero = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0)
    body = {
        "request_id": "req_1",
        "model": OPUS,
        **zero,
        "thinking_tokens": 0,
        "context_window_tokens": 0,
        "request_bytes": 10,
        "response_bytes": 20,
        "status": status,
    }
    found.guest.emit(sid or session_id(), "usage", body)


def everything_logged(env: Env, found: Kit, lines: list[str]) -> str:
    return "\n".join([*found.lines, *lines, env.context.log.getvalue()])


# -- the start ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("records_", "preference", "approved", "files", "want"),
    [
        ((), "auto", True, 1, ("subscription", "pool")),
        ((), "subscription", True, 1, ("subscription", "pool")),
        (("subscription",), "auto", True, 1, ("subscription", f"Bearer {TOKEN}")),
        (("subscription",), "subscription", True, 1, ("subscription", f"Bearer {TOKEN}")),
        (("api_key",), "auto", True, 1, ("api_key", API_KEY)),
        ((), "api_key", True, 1, None),
        ((), "platform", True, 1, ("platform", f"Bearer {KEY}")),
        # an organization that is not approved sees no difference
        ((), "auto", False, 1, ("platform", f"Bearer {KEY}")),
        ((), "subscription", False, 1, None),
        # an empty pool: as before
        ((), "auto", True, 0, ("platform", f"Bearer {KEY}")),
        ((), "subscription", True, 0, None),
    ],
)
def test_the_pool_comes_after_the_own_credentials_and_before_the_platform(
    store_dsn: str,
    tmp_path: Path,
    *,
    records_: tuple[str, ...],
    preference: str,
    approved: bool,
    files: int,
    want: tuple[str, str] | None,
) -> None:
    async def body(env: Env, directory: Path) -> None:
        if "subscription" in records_:
            store_record(env, "claude_subscription", subscription_body())
        if "api_key" in records_:
            store_record(env, "anthropic_api_key", api_key_body())
        if files:
            put(directory, "a", token(1))
        pool, lines, _clock = install(env, directory, approved=approved)
        found = await model_kit(env)
        report = await start(found, preference=preference)
        if want is None:
            assert not report.started
            assert (await found.transitions())[-1] == ("failed", "model_access_unavailable")
            assert await stored_bindings(env) == {}
            return
        assert report.started, found.lines
        [(sid, credential)] = credentials(found)
        assert SCHEMAS.errors("guest/session-credential.request.json", credential) == []
        kind, value = want
        on_pool = value == "pool"
        expected = f"Bearer {token(1)}" if on_pool else value
        assert (credential["access"], credential["auth_value"]) == (kind, expected)
        transitions = [b for k, b in await found.outbox() if k == "session_transition"]
        assert kind in [t.get("model_access") for t in transitions]  # the hub sees `subscription` only
        assert (await found.stored(sid)).model_access == kind
        assert await stored_bindings(env) == ({sid: "a"} if on_pool else {})
        assert pool.bindings == ({sid: "a"} if on_pool else {})
        stamp = provisions(env.context).stamps[sid]
        assert (stamp.operator is not None) == on_pool
        logged = f"runner.model operator_subscription session_id={sid} file=a" in found.lines
        assert logged == on_pool
        await env.sync.pusher.drain()
        assert MARKER not in repr(env.hub.items())
        assert MARKER not in everything_logged(env, found, lines)

    run(store_dsn, tmp_path, body)


def test_sessions_are_spread_over_the_files(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        for n, name in enumerate(("a", "b"), 1):
            put(directory, name, token(n))
        pool, _lines, _clock = install(env, directory)
        found = await model_kit(env)
        for n in (1, 2, 3):
            assert (await start(found, n)).started, found.lines
        assert await stored_bindings(env) == {session_id(1): "a", session_id(2): "b", session_id(3): "a"}
        assert pool.summary().sessions == {"a": 2, "b": 1}

    run(store_dsn, tmp_path, body)


def test_an_empty_pool_is_logged_with_its_problem(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        directory.chmod(0o755)
        _pool, lines, _clock = install(env, directory)
        found = await model_kit(env)
        assert not (await start(found, preference="subscription")).started
        assert "runner.model operator_pool_empty problem=mode" in lines
        directory.chmod(0o700)
        assert not (await start(found, 2, preference="subscription")).started
        assert lines[-1] == "runner.model operator_pool_empty problem=none"

    run(store_dsn, tmp_path, body)


def test_a_resume_on_an_own_credential_ends_the_binding(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, _clock = await running(env, directory)
        sid = session_id()
        assert pool.bindings == {sid: "a"}
        store_record(env, "claude_subscription", subscription_body())
        resolved = await access.resolve(found.session_env, desired(model_access_pref="auto"))
        assert resolved.credential["auth_value"] == f"Bearer {TOKEN}"
        assert (pool.bindings, await stored_bindings(env)) == ({}, {})
        assert provisions(env.context).stamps[sid].operator is None

    run(store_dsn, tmp_path, body)


def test_an_unapproved_organization_never_reaches_the_pool(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        put(directory, "a", token(1))
        install(env, directory, approved=False)
        found = await model_kit(env)
        with pytest.raises(AccessRefused):
            await access.resolve(found.session_env, desired(model_access_pref="subscription"))
        assert await operator.pick(env.context, desired().org_id) is None

    run(store_dsn, tmp_path, body)


# -- the running session -----------------------------------------------------------------------------------------------


def test_a_file_replaced_while_the_session_runs_reaches_its_gateway(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, lines, _clock = await running(env, directory)
        await tasks.tick(env.context)
        await step(found)
        assert credentials(found) == []  # unchanged file: nothing to do
        put(directory, "a", token(7))  # the operator renewed the short-lived token
        await tasks.tick(env.context)
        [(sid, credential)] = credentials(found)
        assert (sid, credential["auth_value"]) == (session_id(), f"Bearer {token(7)}")
        assert f"runner.model credential_renewed session_id={sid} access=subscription" in found.lines
        await tasks.tick(env.context)
        await step(found)
        assert len(credentials(found)) == 1
        assert (await found.stored(sid)).state == "agent_starting"  # the session goes on
        assert MARKER not in everything_logged(env, found, lines)

    run(store_dsn, tmp_path, body)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_file_is_blocked_and_the_session_moves_to_another(
    store_dsn: str, tmp_path: Path, status: int
) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, lines, clock = await running(env, directory, ("a", "b"))
        clock.now += operator.GRACE_S  # past the grace time of the start
        sid = session_id()
        answered(found, status)
        await consume(found)
        assert pool.state("a") == BLOCKED
        assert f"runner.model operator_file_blocked file=a status={status} session_id={sid}" in lines
        await tasks.tick(env.context)
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(2)}"
        assert await stored_bindings(env) == {sid: "b"}
        assert f"runner.model operator_file_switched session_id={sid} from=a to=b" in found.lines
        await tasks.tick(env.context)
        assert len(credentials(found)) == 1

    run(store_dsn, tmp_path, body)


def test_a_refusal_of_a_token_already_replaced_blocks_nothing(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, _clock = await running(env, directory, ("a", "b"))
        put(directory, "a", token(9))  # renewed before the runner provisioned it
        answered(found, 401)  # the old token expired
        await consume(found)
        assert pool.state("a") != BLOCKED
        await tasks.tick(env.context)
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(9)}"  # same file, new content
        assert await stored_bindings(env) == {session_id(): "a"}

    run(store_dsn, tmp_path, body)


def test_a_late_refusal_after_the_renewal_blocks_nothing(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, clock = await running(env, directory, ("a", "b"))
        clock.now += operator.GRACE_S
        put(directory, "a", token(8))
        await tasks.tick(env.context)  # the renewal reaches the gateway first
        answered(found, 401)  # then the record of a request sent with the expired token
        await consume(found)
        assert pool.state("a") != BLOCKED
        await tasks.tick(env.context)
        assert [c["auth_value"] for _, c in credentials(found)] == [f"Bearer {token(8)}"]

    run(store_dsn, tmp_path, body)


def test_a_failing_renewal_of_one_session_leaves_the_others(
    store_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, _lines, _clock = await running(env, directory, ("a", "b"))
        assert (await start(found, 2)).started
        found.guest.calls.clear()
        put(directory, "a", token(5))
        put(directory, "b", token(6))
        real = gateways.refresh
        seen: list[str] = []

        async def refresh(env_: Any, session: Any) -> None:
            seen.append(session.id)
            if len(seen) == 1:
                raise RuntimeError("store")
            await real(env_, session)

        monkeypatch.setattr(tasks, "refresh", refresh)
        await tasks.tick(env.context)
        assert len(seen) == 2
        assert len(credentials(found)) == 1
        assert f"runner.model operator_refresh_failed session_id={seen[0]} error=RuntimeError" in (
            env.context.log.getvalue()
        )

    run(store_dsn, tmp_path, body)


def test_without_another_usable_file_the_gateway_keeps_its_token_until_the_file_is_renewed(
    store_dsn: str, tmp_path: Path
) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, lines, clock = await running(env, directory)
        clock.now += operator.GRACE_S  # past the grace time of the start
        sid = session_id()
        answered(found, 401)
        await consume(found)
        await tasks.tick(env.context)
        await tasks.tick(env.context)
        assert credentials(found) == []
        assert lines.count(f"runner.model operator_pool_exhausted session_id={sid} file=a") == 1
        assert (await found.stored(sid)).state == "agent_starting"
        put(directory, "a", token(5))
        await tasks.tick(env.context)
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(5)}"
        assert f"exhausted:{sid}" not in pool.reported

    run(store_dsn, tmp_path, body)


def test_a_rate_limited_file_keeps_its_session(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, clock = await running(env, directory, ("a", "b"))
        clock.now += operator.GRACE_S  # past the grace time of the start
        answered(found, 429)
        answered(found, 200)
        await consume(found)
        assert pool.state("a") == DEFERRED
        await tasks.tick(env.context)
        assert credentials(found) == []
        assert await stored_bindings(env) == {session_id(): "a"}
        # a new session takes the other file
        assert (await start(found, 2)).started
        assert (await stored_bindings(env))[session_id(2)] == "b"

    run(store_dsn, tmp_path, body)


def test_a_gateway_that_lost_its_credential_gets_the_files_content_again(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, clock = await running(env, directory)
        sid = session_id()
        clock.now += 100
        await gateways.reprovision(found.session_env, sid)  # `credential_missing`: the guest's API process restarted
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(1)}"
        assert pool.delivered_at[sid] == clock.now
        assert f"runner.model credential_reprovisioned session_id={sid} access=subscription" in found.lines

    run(store_dsn, tmp_path, body)


def test_a_session_whose_file_disappeared_moves_on(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, _lines, _clock = await running(env, directory, ("a", "b"))
        (directory / "a.token").unlink()
        await tasks.tick(env.context)
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(2)}"

    run(store_dsn, tmp_path, body)


def test_an_organization_no_longer_approved_loses_the_access(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, _clock = await running(env, directory)
        pool.orgs = frozenset()
        await step(found)
        [(sid, credential)] = credentials(found)
        assert credential["models_allowed"] == []
        assert (await found.stored(sid)).state == "failed"
        assert f"runner.model operator_access_revoked session_id={sid}" in found.lines
        await step(found)  # the ended session's binding is dropped
        assert (pool.bindings, await stored_bindings(env)) == ({}, {})

    run(store_dsn, tmp_path, body)


def test_a_purge_of_the_owners_record_leaves_a_bound_session_alone(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, _lines, _clock = await running(env, directory)
        await gateways.on_purge(env.context, desired().owner_id, "claude_subscription")
        assert credentials(found) == []
        assert (await found.stored(session_id())).state == "agent_starting"

    run(store_dsn, tmp_path, body)


def test_after_a_restart_the_binding_is_read_from_the_store(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, _lines, _clock = await running(env, directory)
        sid = session_id()
        # a new daemon: no stamps, a new pool that has not read the store yet, no owner record at all
        provisions(env.context).stamps.clear()
        fresh, lines, _clock = install(env, directory)
        assert not fresh.loaded
        answered(found, 401)  # no stamp: the content the gateway holds is unknown, nothing is blocked
        await consume(found)
        assert fresh.state("a") != BLOCKED
        await step(found)
        [(_sid, credential)] = credentials(found)
        assert credential["auth_value"] == f"Bearer {token(1)}"
        assert fresh.bindings == {sid: "a"}
        assert (await found.stored(sid)).state == "agent_starting"
        assert lines == []

    run(store_dsn, tmp_path, body)


def test_records_of_sessions_without_a_daemon_context_change_nothing(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, pool, _lines, clock = await running(env, directory)
        clock.now += operator.GRACE_S  # past the grace time of the start
        found.session_env.context = None
        answered(found, 401)
        await consume(found)
        assert pool.state("a") != BLOCKED

    run(store_dsn, tmp_path, body)


# -- the daemon task ---------------------------------------------------------------------------------------------


def test_the_task_ends_at_once_without_a_pool_and_logs_failing_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        stopping = asyncio.Event()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                stopping.set()

        log = io.StringIO()
        off = SimpleNamespace(config=SimpleNamespace(), stopping=stopping, host=SimpleNamespace(sleep=sleep), log=log)
        await tasks.run(off)
        assert sleeps == []
        await tasks.tick(off)  # no session service: nothing to do
        directory = pool_dir(tmp_path)
        on = SimpleNamespace(
            config=SimpleNamespace(
                operator_subscription_dir=directory, operator_subscription_orgs=frozenset({desired().org_id})
            ),
            stopping=stopping,
            host=SimpleNamespace(sleep=sleep),
            log=log,
        )
        calls: list[Any] = []

        async def tick(context: Any) -> None:
            calls.append(context)
            if len(calls) == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(tasks, "tick", tick)
        await tasks.run(on)
        assert (len(calls), sleeps) == (2, [tasks.TICK_S, tasks.TICK_S])
        assert log.getvalue() == "runner.model operator_tick_failed error=RuntimeError\n"
        assert [spec.name for spec in tasks.TASKS] == ["operator_subscriptions", "model_fallback", "usage_merge"]

    asyncio.run(main())


def test_the_task_renews_only_bound_sessions(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, _lines, _clock = await running(env, directory)
        store_record(env, "anthropic_api_key", api_key_body())
        assert (await start(found, 2, "api_key")).started
        found.guest.calls.clear()
        put(directory, "a", token(3))
        await tasks.tick(env.context)
        assert [sid for sid, _ in credentials(found)] == [session_id(1)]
        async with env.pool.connection() as conn:
            await records.update(conn, session_id(1), state="stopped")
        await step(found)
        assert await stored_bindings(env) == {}

    run(store_dsn, tmp_path, body)


def test_no_token_reaches_the_store(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, directory: Path) -> None:
        found, _pool, lines, clock = await running(env, directory, ("a", "b"))
        clock.now += operator.GRACE_S  # past the grace time of the start
        answered(found, 401)
        await consume(found)
        await tasks.tick(env.context)
        async with env.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'runner'"
            )
            for (table,) in await cursor.fetchall():
                rows = await (await conn.execute(f"SELECT * FROM runner.{table}")).fetchall()
                assert MARKER not in repr(rows), table
        assert MARKER not in everything_logged(env, found, lines)
        assert os.environ.get("AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR") is None

    run(store_dsn, tmp_path, body)

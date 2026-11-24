"""U-RUN-dfxkeys: the reconcile step `dfx_keys` (order 47, before the sessions step) and the task `dfx_keys`
(ADR-080 section 7)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_runner import registry
from agent_runner.dfxkeys import reconcile, tasks
from agent_runner.dfxkeys.issuer import ATTRIBUTE as ISSUER_ATTRIBUTE
from agent_runner.dfxkeys.issuer import Issuer, fallback_api
from agent_runner.model.dfxkey import org_key

from .kit import FALLBACK, ORG, REQUEST, double, issuer_env, issuer_state, org_context, request_entry
from .test_org import held


def run_of(context: Any, desired: Any) -> Any:
    return SimpleNamespace(context=context, desired=desired)


def test_the_step_is_registered_before_the_sessions_step() -> None:
    steps = registry.reconcile_steps()
    names = [step.name for step in steps]
    assert names.index("dfx_keys") < names.index("sessions")
    assert [(step.order, step.name) for step in reconcile.STEPS] == [(47, "dfx_keys")]
    assert [spec.name for spec in tasks.TASKS] == ["dfx_keys"]


def test_fallback_api() -> None:
    assert fallback_api({"model_fallback": {"api": 1}})
    assert fallback_api({"model_fallback": {"api": 2}})
    for desired in (None, {}, {"model_fallback": 1}, {"model_fallback": {"api": 0}}, {"model_fallback": {"api": True}}):
        assert not fallback_api(desired)


def test_the_step_on_an_org_device_stores_its_envelopes(org_dsn: str) -> None:
    async def main() -> None:
        async with org_context(org_dsn) as context:
            await reconcile.step(run_of(context, None))
            assert org_key(context, ORG) is None
            await reconcile.step(run_of(context, {"dfx_key_held": [held()]}))  # an older hub: nothing
            assert org_key(context, ORG) is None
            await reconcile.step(run_of(context, {**FALLBACK, "dfx_key_held": [held()]}))
            assert org_key(context, ORG) is not None

    asyncio.run(main())


def test_the_step_on_a_platform_device_feeds_the_issuer(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                await reconcile.step(run_of(issuing.context, issuer_state([request_entry()])))
                found = getattr(issuing.context, ISSUER_ATTRIBUTE)
                assert isinstance(found, Issuer)
                assert found.announced is True
                # the task does the provider work; it stops with the daemon
                sleeps: list[float] = []

                async def sleep(seconds: float) -> None:
                    sleeps.append(seconds)
                    issuing.context.stopping.set()

                issuing.context.host.sleep = sleep
                await tasks.run(issuing.context)
                assert sleeps == [tasks.TICK_S]
                assert [doc["name"] for doc in started.keys.documents()] == [f"dfx-{REQUEST}"]
                # a failing round is logged by type and the loop goes on
                issuing.context.stopping.clear()

                async def broken(now: float) -> None:
                    raise RuntimeError("boom")

                found.tick = broken  # type: ignore[method-assign]
                await tasks.run(issuing.context)
                assert "runner.dfxkeys tick_failed error=RuntimeError" in issuing.env.log()

    asyncio.run(main())


def test_without_an_issuer_the_task_ends_at_once() -> None:
    asyncio.run(tasks.run(SimpleNamespace(identity=None)))

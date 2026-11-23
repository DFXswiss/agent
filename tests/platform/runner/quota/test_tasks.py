"""U-RUN-quota: the daemon task `quota` (05-runner.md sections 1.1 and 10.6): one round polls the subscription quota
of the owners in the last desired state and the spend of the platform key; a failing round does not stop the task."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.credentials import cache
from agent_runner.quota import tasks
from agent_runner.quota.openrouter import spend_metrics
from agent_runner.quota.tasks import TASKS, QuotaOptions

from ..model.kit import ALICE, TOKEN, attach, model_env, model_kit, store_record, subscription_body
from .double import KEY_PATH, USAGE_PATH, UsageDouble
from .test_subscription import SESSION, add_session


def test_the_task_ends_on_a_device_that_is_not_enrolled() -> None:
    lines: list[str] = []
    context = SimpleNamespace(identity=None, log=SimpleNamespace(write=lines.append, flush=lambda: None))

    async def main() -> None:
        await TASKS[0].run(context)

    asyncio.run(main())
    assert lines == ["runner.quota not_enrolled\n"]
    assert TASKS[0].name == "quota"


def test_a_round_polls_quotas_and_the_platform_spend(store_dsn: str, tmp_path: Path) -> None:
    key_path = tmp_path / "openrouter.key"
    key_path.write_text(TOKEN + "\n", encoding="ascii")
    key_path.chmod(0o600)

    async def main() -> None:
        async with model_env(store_dsn) as env:
            store_record(env, "claude_subscription", subscription_body())
            await cache.fetch(env.context, ALICE, "claude_subscription")
            await add_session(env, SESSION)
            found = await model_kit(env)
            attach(found)
            found.session_env.desired = {"quota_poll_owners": [ALICE]}
            with UsageDouble(TOKEN, offset=2) as double:
                config = env.context.config
                config.anthropic_usage_url = double.url + USAGE_PATH
                config.openrouter_key_file = key_path
                config.model_upstream_openrouter = double.url + "/api"
                env.context.quota_options = QuotaOptions()
                rounds: list[float] = []

                def on_sleep(seconds: float) -> None:
                    rounds.append(seconds)
                    env.context.stopping.set()

                env.clock.on_sleep = on_sleep
                await TASKS[0].run(env.context)
                assert rounds == [tasks.TICK_S]
                paths = sorted(path for path, _headers in double.requests)
                assert paths == [USAGE_PATH, KEY_PATH]
                assert spend_metrics(env.context) == {"daily": 0.75, "weekly": 3.25, "monthly": 9.0, "total": 12.5}
                assert TOKEN not in env.log()

    asyncio.run(main())


def test_a_failing_round_is_logged_and_the_task_goes_on(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: Any) -> None:
        raise RuntimeError("round")

    async def main() -> None:
        async with model_env(store_dsn) as env:
            env.context.config.anthropic_usage_url = "http://127.0.0.1:9/api/oauth/usage"
            calls: list[float] = []

            def on_sleep(seconds: float) -> None:
                calls.append(seconds)
                if len(calls) == 2:
                    env.context.stopping.set()

            env.clock.on_sleep = on_sleep
            monkeypatch.setattr(tasks, "tick", broken)
            await TASKS[0].run(env.context)
            assert env.log().count("runner.quota tick_failed error=RuntimeError") == 2

    asyncio.run(main())


def test_a_round_without_session_service_or_spend_poller(store_dsn: str) -> None:
    async def main() -> None:
        async with model_env(store_dsn) as env:
            env.context.config.openrouter_key_file = None
            polls = SimpleNamespace(calls=[], client=None)

            async def tick(desired: Any, now: float) -> None:
                polls.calls.append(desired)

            polls.tick = tick
            await tasks.tick(env.context, polls, lambda: 1.0)  # type: ignore[arg-type]
            assert polls.calls == [None]

    asyncio.run(main())

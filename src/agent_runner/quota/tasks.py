"""Daemon task `quota` (05-runner.md sections 1.1 and 10.6): every `TICK_S` the subscription quota polling
(`subscription.SubscriptionPoller`) for the owners of the last desired state and the spend of the platform key
(`openrouter.SpendPoller`). Each poller keeps its own 900 s schedule per owner or key; a tick only checks what is due.
A failing round is logged and the loop goes on. The task ends at once on a device that is not enrolled.

Tests set `quota_options` (`QuotaOptions`) on the context to inject the HTTP client factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runner.model.gateways import running_service
from agent_runner.quota.openrouter import spend_poller
from agent_runner.quota.subscription import ClientFactory, SubscriptionPoller, default_client
from agent_runner.registry import TaskSpec
from agent_runner.sync.runtime import runtime

TICK_S = 30.0


@dataclass
class QuotaOptions:
    client: ClientFactory = default_client


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.quota {text}\n")
    context.log.flush()


async def tick(context: Any, subscriptions: SubscriptionPoller, wall: Any) -> None:
    service = running_service(context)
    desired = None if service is None else service.env.desired
    await subscriptions.tick(desired, wall())
    spend = spend_poller(context, wall)
    if spend is not None:
        spend.client = subscriptions.client
        await spend.tick(wall())


async def run(context: Any) -> None:
    sync = runtime(context)
    if sync is None:
        _line(context, "not_enrolled")
        return
    options = getattr(context, "quota_options", None)
    client = options.client if isinstance(options, QuotaOptions) else default_client
    subscriptions = SubscriptionPoller(context=context, url=context.config.anthropic_usage_url, client=client)
    while not context.stopping.is_set():
        try:
            await tick(context, subscriptions, sync.wall)
        except Exception as exc:
            _line(context, f"tick_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)


TASKS = [TaskSpec("quota", run)]

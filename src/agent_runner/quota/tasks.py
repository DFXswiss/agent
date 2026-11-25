"""Daemon task `quota` (05-runner.md sections 1.1 and 10.6): every `TICK_S` the subscription quota polling
(`subscription.SubscriptionPoller`) for the owners of the last desired state and of their further subscriptions
(`further.FurtherPoller`, KOR-69), the spend of the platform key
(`openrouter.SpendPoller`) and, on a platform device with the operator's pool, the quotas of the operator's
subscriptions and their snapshot `operator_quota` (`operator.OperatorPoller`, `operator.report`; KOR-50, ADR-081
section 6). Each poller keeps its own 900 s schedule per owner, key or file; a tick only checks what is due.
A failing round is logged and the loop goes on. The task ends at once on a device that is not enrolled.

Tests set `quota_options` (`QuotaOptions`) on the context to inject the HTTP client factory.
"""

from __future__ import annotations

from typing import Any

from agent_runner.model.gateways import running_service
from agent_runner.quota import operator
from agent_runner.quota.further import FurtherPoller
from agent_runner.quota.openrouter import spend_poller
from agent_runner.quota.reader import QuotaOptions, client_of
from agent_runner.quota.subscription import SubscriptionPoller
from agent_runner.registry import TaskSpec
from agent_runner.sync.runtime import runtime

TICK_S = 30.0
__all__ = ["TASKS", "TICK_S", "QuotaOptions", "run", "tick"]
OPERATOR = "quota_operator_poller"
FURTHER = "quota_further_poller"


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.quota {text}\n")
    context.log.flush()


async def tick(context: Any, subscriptions: SubscriptionPoller, wall: Any) -> None:
    service = running_service(context)
    desired = None if service is None else service.env.desired
    await subscriptions.tick(desired, wall())
    further = getattr(context, FURTHER, None)
    if not isinstance(further, FurtherPoller):
        further = FurtherPoller(context=context, url=subscriptions.url)
        setattr(context, FURTHER, further)
    further.client = subscriptions.client
    await further.tick(desired, wall())
    spend = spend_poller(context, wall)
    if spend is not None:
        spend.client = subscriptions.client
        await spend.tick(wall())
    found = getattr(context, OPERATOR, None)
    poller = found if isinstance(found, operator.OperatorPoller) else operator.OperatorPoller(context=context)
    setattr(context, OPERATOR, poller)
    await poller.tick(wall())
    await operator.report(context, desired, wall())


async def run(context: Any) -> None:
    sync = runtime(context)
    if sync is None:
        _line(context, "not_enrolled")
        return
    subscriptions = SubscriptionPoller(
        context=context, url=context.config.anthropic_usage_url, client=client_of(context)
    )
    while not context.stopping.is_set():
        try:
            await tick(context, subscriptions, sync.wall)
        except Exception as exc:
            _line(context, f"tick_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)


TASKS = [TaskSpec("quota", run)]

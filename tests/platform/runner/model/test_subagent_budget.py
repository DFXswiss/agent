"""U-RUN-model: a subagent inherits the parent's access kind, so a platform parent cannot start one while the
organization's daily platform budget is spent (05-runner.md sections 10.2 and 13; operation code `budget_exceeded`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.operations.extensions import CheckFailed, PrecheckResult
from agent_runner.sessions.service import ATTRIBUTE
from agent_runner.store.pool import open_pool
from agent_runner.subagents.op_prechecks import PRECHECKS

ORG = "01930001-0000-7000-8000-000000000001"
SPENT = {"platform_budget": {"usd_per_org_day": 3, "spent_today_by_org": {ORG: 3}}}


def context_with(desired: dict[str, Any] | None) -> SimpleNamespace:
    from agent_runner.sessions.service import SessionService  # noqa: PLC0415

    service = SessionService.__new__(SessionService)
    service.env = SimpleNamespace(desired=desired)  # type: ignore[assignment]
    return SimpleNamespace(**{ATTRIBUTE: service})


def request(access: str | None, desired: dict[str, Any] | None) -> SimpleNamespace:
    session = SimpleNamespace(depth=0, model_access=access, org_id=ORG, vm_id=None)
    return SimpleNamespace(session=session, context=context_with(desired))


def test_a_platform_parent_is_refused_while_the_budget_is_spent() -> None:
    with pytest.raises(CheckFailed) as caught:
        asyncio.run(PRECHECKS["subagent.request"](request("platform", SPENT)))
    assert caught.value.code == "budget_exceeded"


@pytest.mark.parametrize(
    ("access", "desired"), [("api_key", SPENT), ("subscription", SPENT), ("platform", None), ("platform", {})]
)
def test_other_kinds_and_an_open_budget_pass(store_dsn: str, access: str, desired: dict[str, Any] | None) -> None:
    async def main() -> None:
        pool = await open_pool(store_dsn, max_size=2)
        try:
            found = request(access, desired)
            found.operation = SimpleNamespace(task_id="01930004-0000-7000-8000-000000000001")
            found.context.pool = pool
            assert await PRECHECKS["subagent.request"](found) == PrecheckResult()
        finally:
            await pool.close()

    asyncio.run(main())

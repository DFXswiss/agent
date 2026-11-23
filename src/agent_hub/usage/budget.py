"""The platform model budget per organization and UTC day (03-api.md sections 2, 6.1, 8.3 H4; R-P.13, ADR-026).

`AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY` (`HubConfig.platform_budget_usd_per_org_day`, None = unlimited) limits what
sessions with `model_access = platform` cost one organization per day of the hub clock (UTC). The spend of a day is
`hub.usage_daily` (migration 0006), written by the `usage` outbox handler of this area.

- `spent_micro_usd(connection, org_id, now)`: the spend of the org on the UTC day of `now`.
- `available(connection, org_id, now, limit)`: True while the spend is **below** the limit. Reaching the limit
  exactly exhausts it (the gateway answers 429 from then on as well); a limit of 0 allows nothing.
- `CHECK(request)`: the budget hook of the operation registration (`agent_hub.operations.limits`, ADR-031). First the
  prepaid credit (KOR-31, ADR-059 section 6): a requesting session on a platform device of an org whose credit is
  used up is denied with `credit_exhausted` (every session of DFX hardware is charged for runtime). Then it asks
  `available` for the org of the requesting session, unless that session runs with its own credentials
  (`subscription`, `api_key`): a child session uses the access of its parent (05-runner.md section 13), so only
  platform work - or work whose access is not reported yet - spends the platform budget.

The limit comes from the configuration bound by `agent_hub.usage.binding` (the hook gets none, ADR-031), which the
app binds before it serves requests. Should the hook ever run before the first binding it fails closed with `503
unavailable` (the runner retries; KOR-06, A-12) instead of answering "unlimited".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.billing import credit
from agent_hub.errors import HubError
from agent_hub.operations.limits import BudgetRequest
from agent_hub.usage import binding

Connection = psycopg.Connection[Any]
MICRO = Decimal(1_000_000)
OWN_CREDENTIALS = frozenset({"subscription", "api_key"})
CREDIT_EXHAUSTED = "credit_exhausted"


def utc_day(now: datetime) -> date:
    """The UTC day of a hub time."""
    return now.astimezone(UTC).date()


def spent_micro_usd(connection: Connection, org_id: UUID | str, now: datetime) -> int:
    """The platform spend of the org on the UTC day of `now`, in micro-USD."""
    row = connection.execute(
        "SELECT COALESCE(sum(cost_micro_usd), 0) FROM hub.usage_daily WHERE org_id = %s AND day = %s",
        (org_id, utc_day(now)),
    ).fetchone()
    return int(cast(tuple[int], row)[0])


def within(spent_micro: int, limit: Decimal) -> bool:
    """True while `spent_micro` is below the limit in USD."""
    return Decimal(spent_micro) < limit * MICRO


def available(connection: Connection, org_id: UUID | str, now: datetime, limit: Decimal) -> bool:
    """True while the org may spend more platform budget today (module docstring)."""
    return within(spent_micro_usd(connection, org_id, now), limit)


def unpaid(request: BudgetRequest) -> bool:
    """The requesting session runs on a platform device of an org whose credit is used up."""
    row = request.connection.execute(
        "SELECT d.kind FROM hub.session s JOIN hub.device d ON d.id = s.device_id WHERE s.id = %s",
        (request.session_id,),
    ).fetchone()
    return (
        row is not None and row[0] == "platform" and credit.exhausted(request.connection, request.org_id, request.now)
    )


def check(request: BudgetRequest) -> bool | str:
    """The budget hook H4 (module docstring)."""
    if not binding.bound():
        raise HubError("unavailable")
    if unpaid(request):
        return CREDIT_EXHAUSTED
    limit = binding.budget_limit()
    if limit is None:
        return True
    row = request.connection.execute(
        "SELECT model_access FROM hub.session WHERE id = %s", (request.session_id,)
    ).fetchone()
    if row is not None and row[0] in OWN_CREDENTIALS:
        return True
    return available(request.connection, request.org_id, request.now, limit)


CHECK = check

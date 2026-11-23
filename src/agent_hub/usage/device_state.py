"""The sections `quota_poll_owners` and `platform_budget` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027
section 4 `<area>/device_state.py:SECTION`).

- `quota_poll_owners`: the subscription owners whose quota this device polls (`agent_hub.usage.polling`).
- `platform_budget`: `null` when the hub has no `PLATFORM_BUDGET_USD_PER_ORG_DAY`; else `{usd_per_org_day,
  spent_today_by_org}` with today's platform spend (UTC day of the hub clock, `agent_hub.usage.budget`) in USD of every
  org that has a workgroup assigned to this device, summed over all devices of the org. Every assigned org is listed
  (with 0 without spend today), also an org without platform access: after a revocation (ADR-026) existing
  assignments of platform devices stay, and an org missing from the map would run unlimited in the gateway.

The provider also binds the hub configuration for the budget hook and the `quota` handler (`agent_hub.usage.binding`).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.usage import binding
from agent_hub.usage.budget import MICRO, utc_day
from agent_hub.usage.polling import poll_owners


def usd(micro: int) -> float:
    """Micro-USD as a JSON number of USD."""
    return float(Decimal(micro) / MICRO)


def platform_budget(request: StateRequest) -> dict[str, Any] | None:
    """The `platform_budget` field (module docstring)."""
    limit = request.config.platform_budget_usd_per_org_day
    if limit is None:
        return None
    assigned = sorted({str(org) for org, _wg in request.view.assigned()})
    rows = request.connection.execute(
        "SELECT org_id::text, sum(cost_micro_usd) FROM hub.usage_daily WHERE day = %s AND org_id = ANY(%s::uuid[])"
        " GROUP BY org_id",
        (utc_day(request.now), assigned),
    ).fetchall()
    spent = {str(org): int(total) for org, total in rows}
    return {"usd_per_org_day": float(limit), "spent_today_by_org": {org: usd(spent.get(org, 0)) for org in assigned}}


def provide(request: StateRequest) -> Mapping[str, Any]:
    """Both fields of this area."""
    binding.bind(request.config)
    owners = poll_owners(request.connection, request.device_id, request.now, request.config.device_online_seconds)
    return {"quota_poll_owners": [str(owner) for owner in owners], "platform_budget": platform_budget(request)}


SECTION = Section(("quota_poll_owners", "platform_budget"), provide)

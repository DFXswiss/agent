"""Billing routes of the hub fake as the hub runs them (`agent_hub.billing.routes`, 03-api.md section 4.11,
12-billing.md section 6):

- `GET /v1/orgs/{org}/billing` (`org:member`, else `404 not_found`): the balance (start credit 0, minus the charges),
  `exhausted` only while a price list is in force and the balance is at most 0, `billing_active` while one is in
  force, the price list in force (`version`, `valid_from`, `body`) or null, the credit history newest first and the
  consumption per day and per workgroup;
- `GET /v1/tasks/{task}/charges` (`task:read` on the task's workgroup, unknown or unreadable `404`): the charges per
  session in the order of their first charge and the total.

`Billing.prices` is the price list in force (None: billing inactive), `credits`, `days` and `workgroups` the rows the
hub would read per org, `charges` the session rows per task, `people` the consumption per person of an org or a task
(ADR-083; only sent when set).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto.log.state import WG

from ..hubfake import HubFake, Json, Request, now_ms_text

PRICES: Json = {
    "currency": "USD",
    "valid_from": "2026-11-21T00:00:00Z",
    "tokens": {
        "factors": {"platform": 1.5, "operator": 1},
        "models": {"claude-sonnet-5-5": {"input": 2, "output": 10, "cache_read": 0.1, "cache_write": 2.5}},
        "default": {"input": 8, "output": 40, "cache_read": 0.2, "cache_write": 10},
    },
    "runtime": {"per_hour": {"session": 0.36, "standard": 0.36, "large": 0.72, "xlarge": 1.44, "default": 1.44}},
    "note": "test",
}


@dataclass
class Billing:
    prices: Json | None = None
    balances: dict[str, int] = field(default_factory=dict)
    credits: dict[str, list[Any]] = field(default_factory=dict)
    days: dict[str, list[Json]] = field(default_factory=dict)
    workgroups: dict[str, list[Json]] = field(default_factory=dict)
    charges: dict[str, list[Json]] = field(default_factory=dict)
    # `people` per org and per task (ADR-083); absent keys: the answer has no `people`, as from an older hub
    people: dict[str, list[Any]] = field(default_factory=dict)


def billing(hub: HubFake) -> Billing:
    found = getattr(hub, "stage_b_billing", None)
    if found is None:
        found = Billing()
        setattr(hub, "stage_b_billing", found)  # noqa: B010
    return cast(Billing, found)


def consumption(tokens_usd: int, runtime_usd: int, tokens: int, runtime_ms: int) -> Json:
    return {
        "tokens_micro_usd": tokens_usd,
        "runtime_micro_usd": runtime_usd,
        "tokens": tokens,
        "runtime_ms": runtime_ms,
    }


def install(hub: HubFake) -> None:
    found = billing(hub)

    def org_billing(request: Request, body: Any, org: str) -> tuple[int, Any]:
        stored = hub.orgs.get(org)
        if stored is None or stored.log.state.member(cast(str, request.user)) is None:
            return 404, {"error": "not_found"}
        balance = found.balances.get(org, 0)
        active = found.prices is not None
        listed = None if found.prices is None else {"version": 1, "valid_from": now_ms_text(), "body": found.prices}
        return 200, {
            "currency": "USD",
            "balance_micro_usd": balance,
            "exhausted": active and balance <= 0,
            "billing_active": active,
            "price_list": listed,
            "credits": found.credits.get(org, []),
            "days": found.days.get(org, []),
            "workgroups": found.workgroups.get(org, []),
            **({"people": found.people[org]} if org in found.people else {}),
        }

    def task_charges(request: Request, body: Any, task: str) -> tuple[int, Any]:
        stored = hub.tasks.get(task)
        wg = None if stored is None else stored["task"]["wg_id"]
        org = None if wg is None else hub.org_of_wg(wg)
        state = None if org is None else hub.orgs[org].log.state
        if state is None or not state.can_read(cast(str, request.user), WG, cast(str, wg)):
            return 404, {"error": "not_found"}
        sessions = found.charges.get(task, [])
        total = sum(row["tokens_micro_usd"] + row["runtime_micro_usd"] for row in sessions)
        people = {"people": found.people[task]} if task in found.people else {}
        return 200, {"sessions": sessions, "total_micro_usd": total, **people}

    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/billing", org_billing)
    hub.add_route("GET", r"/v1/tasks/(?P<task>[0-9a-f-]+)/charges", task_charges)

"""Credit and usage charges (11-agent-access.md section 8.4, 12-billing.md sections 6 and 9, ADR-062; twin of
`web/src/core/billing/billing.ts`, the settings section "Credit and usage" and the task tab "Costs").

- `billing --org`: `GET /v1/orgs/{org}/billing`: the balance, whether the credit is used up (`exhausted`) and whether
  billing is active (a price list is in force), the customer prices of that list (per million tokens after the factor
  of the platform access, per VM hour without the `default` size), the credit history and the consumption per day and
  per workgroup of the last 30 days.
- `task charges --task`: `GET /v1/tasks/{task}/charges`: the charges of the task per session (model tokens and VM
  runtime) and the total.

Amounts are micro-USD as the hub sends them. An answer the web client would call malformed (no finite balance, no
boolean `exhausted`/`billing_active`, no finite total) is `response_invalid`; rows that do not have the web's form are
left out, as the web leaves them out. There is no payment and no top-up: the operator adds credit, the tool only reads.
A hub of an older release has no billing routes and answers `not_found`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.eventio import uuid_argument
from agent_platform.hub import segment

CONSUMPTION = ("tokens_micro_usd", "runtime_micro_usd", "tokens", "runtime_ms")
PER_MILLION = 1_000_000


def number(value: object) -> float | None:
    """A finite JSON number (`num` of the web: a boolean or a string is none)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value


def consumption(row: dict[str, Any]) -> dict[str, float] | None:
    found = {name: number(row.get(name)) for name in CONSUMPTION}
    return None if any(value is None for value in found.values()) else {k: v for k, v in found.items() if v is not None}


def _rows(value: object, make: Callable[[dict[str, Any]], dict[str, Any] | None]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    made = (make(row) for row in value if isinstance(row, dict))
    return [row for row in made if row is not None]


def _keyed(key: str, out: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """A consumption row named by the text field `key` (shown as `out`), with its total."""

    def make(row: dict[str, Any]) -> dict[str, Any] | None:
        found = consumption(row)
        if found is None or not isinstance(row.get(key), str):
            return None
        return {out: row[key], **found, "total_micro_usd": found["tokens_micro_usd"] + found["runtime_micro_usd"]}

    return make


def _record(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def price_rows(document: object) -> dict[str, list[dict[str, Any]]]:
    """The customer prices of a price list (`priceRows`): USD per million tokens times the platform factor, USD per
    VM hour of each size except `default`."""
    tokens = document.get("tokens") if isinstance(document, dict) else None
    runtime = document.get("runtime") if isinstance(document, dict) else None
    if not isinstance(tokens, dict) or not isinstance(runtime, dict):
        return {"token_prices": [], "hourly": []}
    factors = _record(tokens.get("factors"))
    factor = number(factors.get("platform"))
    models = _record(tokens.get("models"))
    token_prices = []
    for label, price in models.items():
        if not isinstance(price, dict):
            continue
        given = (number(price.get("input")), number(price.get("output")))
        if factor is None or given[0] is None or given[1] is None:
            continue
        token_prices.append(
            {"model": label, "input_usd_per_million": given[0] * factor, "output_usd_per_million": given[1] * factor}
        )
    hours = _record(runtime.get("per_hour"))
    hourly = [
        {"size": size, "usd_per_hour": number(usd)}
        for size, usd in hours.items()
        if size != "default" and number(usd) is not None
    ]
    return {"token_prices": token_prices, "hourly": hourly}


def _credit(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row.get("at"), str) or not isinstance(row.get("kind"), str):
        return None
    return {
        "at": row["at"],
        "kind": row["kind"],
        "amount_micro_usd": number(row.get("amount_micro_usd")),
        "balance_micro_usd": number(row.get("balance_micro_usd")),
        "reason": row["reason"] if isinstance(row.get("reason"), str) else None,
    }


def org_billing(account: Account, org: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/orgs/{segment(uuid_argument(org))}/billing")
    balance = number(answer.get("balance_micro_usd"))
    exhausted, active = answer.get("exhausted"), answer.get("billing_active")
    if balance is None or not isinstance(exhausted, bool) or not isinstance(active, bool):
        raise unverified("response_invalid")
    listed = answer.get("price_list") if isinstance(answer.get("price_list"), dict) else None
    return {
        "org_id": org,
        "balance_micro_usd": balance,
        "exhausted": exhausted,
        "billing_active": active,
        "price_list_version": None if listed is None else number(listed.get("version")),
        **price_rows(None if listed is None else listed.get("body")),
        "credits": _rows(answer.get("credits"), _credit),
        "days": _rows(answer.get("days"), _keyed("day", "day")),
        "workgroups": _rows(answer.get("workgroups"), _keyed("wg_id", "wg_id")),
    }


def task_charges(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/tasks/{segment(uuid_argument(task))}/charges")
    total = number(answer.get("total_micro_usd"))
    if total is None:
        raise unverified("response_invalid")
    return {
        "task_id": task,
        "total_micro_usd": total,
        "sessions": _rows(answer.get("sessions"), _keyed("session_id", "session_id")),
    }


def _billing(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return org_billing(account, args.org)


def _charges(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return task_charges(account, args.task)


def register(tree: Tree) -> None:
    text = "an org's credit balance, prices, credit history and consumption (amounts in micro-USD)"
    sub = tree.command(
        tree.commands,
        "billing",
        _billing,
        text,
        example="agent-platform billing --org <org_id>",
        words=(
            "abrechnung guthaben rechnung kosten monat monatlich preise verbrauch credit balance invoice prices usage "
            "cost month monthly micro-usd"
        ),
        trim="first",
    )
    sub.add_argument("--org", required=True)
    group = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.command(
        group,
        "charges",
        _charges,
        "the task's charges per session and the total",
        example="agent-platform task charges --task <task_id>",
        words="kosten aufgabenkosten tokens laufzeit preis cost price spend usage",
        trim="first",
    )
    sub.add_argument("--task", required=True)

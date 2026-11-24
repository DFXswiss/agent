"""Check H4 of the registration: the type-specific limits (03-api.md sections 8.3, 9, 12; ADR-017).

`subagent.request` is limited per task: the child would have `depth = parent depth + 1` at most `MAX_DEPTH` (main
session 0, children 1, grandchildren 2), at most `MAX_RUNNING` children of the task may run (every session of depth
> 0 that has not ended in `stopped`/`failed`), and at most `MAX_TOTAL` children may exist in the task. Exceeding one
is `denied` with `limit_exceeded`. Then the platform budget of the org is asked; a refusal is `denied` with
`budget_exceeded`. The caller holds the task row, so two requests of the same task never both see the last free
place. Other types have no limits in v1.

The budget hook: `budget_check(request)` answers whether the org may start more model work. The computation belongs to
the budget package (HUB-14), which registers it as `agent_hub/<area>/budget.py:CHECK` (a callable
`CHECK(request) -> bool`, discovered like the extension points of ADR-027 section 4); at most one area may define it.
Without a registration the budget is unlimited. Tests install a fake with `configure`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.registry import ExtensionPoint, RegistryError, discover

Connection = psycopg.Connection[Any]
MAX_DEPTH = 2
MAX_RUNNING = 3
MAX_TOTAL = 10
BUDGET_CHECK = ExtensionPoint("budget", "CHECK")


@dataclass(frozen=True)
class BudgetRequest:
    """What a budget check gets: the transaction, the org, the requesting session and the hub time."""

    connection: Connection
    org_id: UUID
    session_id: UUID
    now: datetime


BudgetCheck = Callable[[BudgetRequest], bool | str]
"""`True` = available; `False` = `budget_exceeded`; a string = that denial code (`credit_exhausted`, KOR-31)."""


def unlimited(_request: BudgetRequest) -> bool:
    """The budget without a registered check: always available."""
    return True


_lock = threading.Lock()
_state: dict[str, BudgetCheck] = {}


def collect(found: Iterable[tuple[str, object]]) -> BudgetCheck:
    """The one registered check of the discovered `(area, CHECK)` pairs, `unlimited` without one."""
    checks = list(found)
    if len(checks) > 1:
        raise RegistryError("more than one area defines budget.py CHECK")
    if not checks:
        return unlimited
    area, check = checks[0]
    if not callable(check):
        raise RegistryError(f"{area}/budget.py CHECK must be callable")
    return cast(BudgetCheck, check)


def configure(check: BudgetCheck | None = None) -> BudgetCheck:
    """Set the check (None discovers it) and return it."""
    found = collect(discover(BUDGET_CHECK)) if check is None else check
    with _lock:
        _state["check"] = found
    return found


def budget_check(request: BudgetRequest) -> bool | str:
    """True while the org may start more model work (module docstring)."""
    current = _state.get("check")
    return (current if current is not None else configure())(request)


def subagent_limit(connection: Connection, task_id: UUID, parent_depth: int) -> str | None:
    """`limit_exceeded` when a child of a session of `parent_depth` would break a limit of the task, else None."""
    if parent_depth + 1 > MAX_DEPTH:
        return "limit_exceeded"
    row = cast(
        tuple[int, int],
        connection.execute(
            "SELECT count(*) FILTER (WHERE state NOT IN ('stopped', 'failed')), count(*) FROM hub.session"
            " WHERE task_id = %s AND depth > 0",
            (task_id,),
        ).fetchone(),
    )
    running, total = row
    if running >= MAX_RUNNING or total >= MAX_TOTAL:
        return "limit_exceeded"
    return None

"""The configuration of the running hub for the parts of this area that get none (ADR-031, ADR-030).

The budget hook of the operation registration (`agent_hub.operations.limits.BudgetRequest`) and the outbox handlers
(`agent_hub.outbox.base.ItemContext`) receive the transaction and the hub time, but not the hub configuration. They need
two settings of it: `PLATFORM_BUDGET_USD_PER_ORG_DAY` (budget hook) and `DEVICE_ONLINE_SECONDS` (which device polls a
quota, `quota` items). `bind(config)` keeps both for the process: the job `usage.bind_config` binds them before the
app serves its first request (`at_start`, KOR-06; and hourly again), and every request of this area binds them again
(`GET /v1/device/state`, which every runner fetches on connect and at least every 30 s, and `GET /v1/me/quotas`).

Before the first binding the budget hook fails closed (`budget.check`: `503 unavailable`, never "unlimited"); the
online window is the default.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

from agent_hub.config import DEFAULT_DEVICE_ONLINE_SECONDS, HubConfig

_lock = threading.Lock()
_state: dict[str, Any] = {}


def bind(config: HubConfig) -> None:
    """Keep the settings of the running hub (module docstring)."""
    with _lock:
        _state["limit"] = config.platform_budget_usd_per_org_day
        _state["window"] = config.device_online_seconds


def unbind() -> None:
    """Forget the bound settings (tests)."""
    with _lock:
        _state.clear()


def bound() -> bool:
    """True once `bind` ran in this process."""
    return "limit" in _state


def budget_limit() -> Decimal | None:
    """`PLATFORM_BUDGET_USD_PER_ORG_DAY` in USD; None when unlimited or not bound yet."""
    limit: Decimal | None = _state.get("limit")
    return limit


def online_window() -> int:
    """`DEVICE_ONLINE_SECONDS` (the default before the first binding)."""
    window: int = _state.get("window", DEFAULT_DEVICE_ONLINE_SECONDS)
    return window

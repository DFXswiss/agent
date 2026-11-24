"""The configuration of the running hub for the billing parts that get none (ADR-062, like `agent_hub.usage.binding`).

The outbox handlers, the scheduler and the budget hook receive the transaction and the hub time but not the hub
configuration; billing needs one setting of it, `AGENT_HUB_BILLING_START_CREDIT_USD`. `bind(config)` keeps it for the
process: the job `billing.reconcile` binds it at start (`at_start`, before the first request) and on every run, the
requests of this area bind it again. Before the first binding the start credit is 0 (the safe side: an org without an
account then counts as exhausted only while billing is active, and the at-start job binds before any request).
"""

from __future__ import annotations

import threading
from decimal import Decimal

from agent_hub.config import HubConfig

_lock = threading.Lock()
_state: dict[str, Decimal] = {}


def bind(config: HubConfig) -> None:
    """Keep the start credit of the running hub."""
    with _lock:
        _state["start"] = config.billing_start_credit_usd


def unbind() -> None:
    """Forget the bound setting (tests)."""
    with _lock:
        _state.clear()


def start_credit() -> Decimal:
    """`AGENT_HUB_BILLING_START_CREDIT_USD` in USD (0 before the first binding)."""
    return _state.get("start", Decimal(0))

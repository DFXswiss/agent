"""Fixtures of the main agents suites (U-HUB-mainagents, KOR-52): those of the attention suites (the tasks desk with
devices, the outbox, registrations signed by the device and the situation helper)."""

from __future__ import annotations

from tests.platform.hub.attention.conftest import (
    desk,
    fleet,
    fresh_budget,
    fresh_chains,
    fresh_state,
    op_signals,
    ops,
    outbox,
    published,
    situation,
    world,
)

__all__ = [
    "desk",
    "fleet",
    "fresh_budget",
    "fresh_chains",
    "fresh_state",
    "op_signals",
    "ops",
    "outbox",
    "published",
    "situation",
    "world",
]

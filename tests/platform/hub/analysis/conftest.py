"""Fixtures of the process analysis suites: those of the tasks suites (the desk with both devices online, the fleet,
recorded frames and fresh module state)."""

from __future__ import annotations

from tests.platform.hub.tasks.conftest import desk, fleet, fresh_state, published, world

__all__ = ["desk", "fleet", "fresh_state", "published", "world"]

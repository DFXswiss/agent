"""Fixtures of the audit API suite: the monitoring scene (`tests/platform/hub/monitoring/support.py`: a private org
`mine` with `admin`, `member`, the workgroup `hidden` only the admin reads, and a second org `other`)."""

from __future__ import annotations

from tests.platform.hub.monitoring.conftest import desk, fleet, fresh_state, published, watch, world

__all__ = ["desk", "fleet", "fresh_state", "published", "watch", "world"]

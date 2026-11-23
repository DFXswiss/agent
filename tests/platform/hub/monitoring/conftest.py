"""Fixtures of the monitoring suite: those of the tasks suites (`tests/platform/hub/tasks/conftest.py`) and the
monitoring scene (`tests/platform/hub/monitoring/support.py`)."""

from __future__ import annotations

import pytest

from tests.platform.hub.device.support import Fleet
from tests.platform.hub.monitoring.support import Watch
from tests.platform.hub.orgs.support import World
from tests.platform.hub.tasks.conftest import desk, fleet, fresh_state, published, world
from tests.platform.hub.tasks.support import Desk

__all__ = ["desk", "fleet", "fresh_state", "published", "watch", "world"]


@pytest.fixture
def watch(world: World, fleet: Fleet, desk: Desk) -> Watch:
    """The monitoring scene: the desk plus a hidden workgroup and a platform device shared with a second org."""
    return Watch.build(world, fleet, desk)

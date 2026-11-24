"""Fixtures of the GitHub identities suite (U-HUB-github): those of the credentials suite plus the studio."""

from __future__ import annotations

import pytest

from tests.platform.hub.credentials.conftest import desk, fresh_state, published
from tests.platform.hub.credentials.support import Desk
from tests.platform.hub.github.support import Studio

__all__ = ["desk", "fresh_state", "published", "studio"]


@pytest.fixture
def studio(desk: Desk) -> Studio:
    return Studio(desk)

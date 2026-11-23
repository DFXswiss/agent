"""Fixtures of the authentication tests: the fake browser with its fake authenticator on the running test hub."""

from __future__ import annotations

import pytest

from tests.platform.hub.auth.authenticator import FakeBrowser
from tests.platform.hub.conftest import HubHarness


@pytest.fixture
def browser(hub: HubHarness) -> FakeBrowser:
    """A fake browser (with a fake authenticator for the hub's origin and RP ID) on the running hub."""
    return FakeBrowser(hub)

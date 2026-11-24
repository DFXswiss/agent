"""Copied from agent-core tests/platform/hub/log/conftest.py at 5016e752bda4.

Fixtures of the log suites (U-HUB-log, U-HUB-rights, U-HUB-wraps): the signed logs of the vectors."""

from __future__ import annotations

import pytest

from agent_runner.crypto.log import platform, verify
from agent_runner.crypto.log.vectors import LogBuilder, PlatformBuilder, main_builder, platform_builder, sig_pk


@pytest.fixture
def main() -> LogBuilder:
    """The main membership log with every entry type (see `agent_hub.log.vectors.main_builder`)."""
    return main_builder()


@pytest.fixture
def plat() -> PlatformBuilder:
    """The platform log with every entry type."""
    return platform_builder()


@pytest.fixture
def plat_state(plat: PlatformBuilder) -> platform.PlatformState:
    """The verified platform state."""
    return platform.verify_platform_log(plat.entries, sig_pk("root")).state


@pytest.fixture
def main_log(main: LogBuilder, plat_state: platform.PlatformState) -> verify.VerifiedLog:
    """The verified main log."""
    return verify.verify_log(main.entries, sig_pk("alice"), platform=plat_state)

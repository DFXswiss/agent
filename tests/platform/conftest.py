"""Root fixtures of the platform suites. Component conftests live in tests/platform/<component>/ (ADR-027 §6)."""

from __future__ import annotations

import faulthandler
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_INI = Path(__file__).resolve().with_name("pytest.ini")
XDIST_WORKER = "PYTEST_XDIST_WORKER"
PORT_BASE = "WP_PORT_BASE"
# Hard limits per test (FIX-01, pytest-timeout with `timeout = 180` in pytest.ini). Suites that run only on request and
# drive real VMs or real services get a longer limit (their single tests and session fixtures legitimately take
# minutes); an explicit `pytest.mark.timeout` always wins.
LONG_SUITES = {"atk": 3600.0, "vm": 1800.0, "live": 1800.0}
# The second stage: a test still running this long after its limit (the signal of pytest-timeout fired once and did not
# end it - a background task swallowed it, a teardown waits on the hung work, a C call holds the interpreter) ends its
# process with the tracebacks of every thread. A worker of pytest-xdist is then reported as crashed and replaced; a
# single process ends the run. Either way the run ends instead of blocking.
BACKSTOP_S = 120.0


def pytest_configure(config: pytest.Config) -> None:
    """Parallel workers (pytest-xdist) take ephemeral loopback ports instead of the work package's fixed range.

    Fixtures that serve on `WP_PORT_BASE + offset` fall back to an ephemeral port (or port 0) without the variable,
    as in CI; several workers would otherwise bind the same port of the 20-port range. One process keeps the range.
    """
    if os.environ.get(XDIST_WORKER):
        os.environ.pop(PORT_BASE, None)
    # a duplicate of the real stderr: during a test the capture plugin has redirected fd 2 to a temporary file
    config.stash[BACKSTOP_FD] = os.dup(sys.stderr.fileno())


BACKSTOP_FD = pytest.StashKey[int]()


def pytest_unconfigure(config: pytest.Config) -> None:
    fd = config.stash.get(BACKSTOP_FD, None)
    if fd is not None:
        os.close(fd)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The longer hard limit of the request-only suites (`LONG_SUITES`) where no explicit limit is set."""
    for item in items:
        if item.get_closest_marker("timeout") is not None:
            continue
        for marker, limit in LONG_SUITES.items():
            if item.get_closest_marker(marker) is not None:
                item.add_marker(pytest.mark.timeout(limit))
                break


def hard_limit(item: pytest.Item) -> float:
    """The limit of pytest-timeout for `item`: its marker, else `--timeout`, else the ini value (0: none)."""
    marker = item.get_closest_marker("timeout")
    if marker is not None and marker.args:
        return float(marker.args[0])
    option = item.config.getoption("timeout", None)
    if option is not None:
        return float(option)
    try:
        return float(item.config.getini("timeout") or 0)
    except ValueError:  # pytest-timeout is not installed (an environment without the platform-test extra)
        return 0.0


BACKSTOP_AT = pytest.StashKey[float]()


def arm_backstop(config: pytest.Config) -> None:
    """(Re-)arm the second stage for the rest of the time until the deadline of the running test."""
    deadline = config.stash.get(BACKSTOP_AT, 0.0)
    if deadline > 0:
        left = max(deadline - time.monotonic(), 1.0)
        faulthandler.dump_traceback_later(left, exit=True, file=config.stash[BACKSTOP_FD])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    """Arm the second stage (`BACKSTOP_S` after the hard limit) for the whole protocol of the test."""
    limit = hard_limit(item)
    item.config.stash[BACKSTOP_AT] = time.monotonic() + limit + BACKSTOP_S if limit > 0 else 0.0
    arm_backstop(item.config)
    try:
        yield
    finally:
        item.config.stash[BACKSTOP_AT] = 0.0
        faulthandler.cancel_dump_traceback_later()


@pytest.hookimpl(trylast=True)
def pytest_exception_interact(node: pytest.Item | pytest.Collector) -> None:
    """pytest's faulthandler plugin cancels every pending dump when a test fails (first hook); a hard-limit failure is
    exactly when the second stage must stay armed, so arm it again for the time that is left."""
    arm_backstop(node.config)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The checkout root of this repository."""
    return REPO_ROOT


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Keep the platform suites out of the legacy run (plain `pytest` with the legacy configuration).

    The legacy `testpaths = ["tests"]` would otherwise collect `tests/platform` under the legacy
    `tests/conftest.py` (autouse fixtures, legacy databases) and without the platform extras. Only a run with
    `-c tests/platform/pytest.ini` (`make platform-test`) collects these suites.
    """
    if config.inipath != PLATFORM_INI:
        return True
    return None

"""Fixtures of the metric sampler suites (U-RUN-metrics, RUN-15): recorded probe output, a fake probe runner and the
VM-02 harness (fake Lima with real guest control processes) for the sampler integration tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runner.metrics import probes
from agent_runner.metrics.probes import Tools

# The VM-02 harness and its fixtures (fake Lima with real guests, migrated store, short socket paths).
from tests.platform.runner.vm.conftest import fake_limactl, harness, lima_dir, short_dir, store_dsn  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
GUEST_FIXTURES = Path(__file__).parents[2] / "guest" / "metrics" / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRun:
    """Answers probe argv lists from a table `{argv[0] basename: (exit, stdout)}` and records the calls."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.answers = answers if answers is not None else default_answers()
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout == probes.PROBE_TIMEOUT_S
        self.calls.append(list(argv))
        code, out = self.answers.get(Path(argv[0]).name, (127, ""))
        return subprocess.CompletedProcess(list(argv), code, out, "" if code == 0 else "failed")


def default_answers() -> dict[str, tuple[int, str]]:
    return {
        "sysctl": (0, fixture("sysctl.txt")),
        "top": (0, fixture("top.txt")),
        "vm_stat": (0, fixture("vm_stat.txt")),
        "df": (0, fixture("df.txt")),
        "netstat": (0, fixture("netstat.txt")),
        "nettop": (0, fixture("nettop.txt")),
    }


async def inline(function: Callable[..., Any], *args: Any) -> Any:
    return function(*args)


def tools(run: FakeRun, **overrides: Any) -> Tools:
    return Tools(run=run, blocking=inline, **overrides)


@pytest.fixture
def fake_run() -> FakeRun:
    return FakeRun()

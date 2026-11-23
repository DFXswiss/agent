"""Fixtures of the prompt suites (RUN-05): a migrated store and the test rig of `reconcile/rig.py`; the `vm`
integration (`test_vm_resume.py`) runs only when the marker expression asks for it (never in `make platform-ci`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runner.store.migrate import migrate

from ..reconcile.rig import Rig

VM_MARKER = "vm"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "vm: integration with real Lima VMs on the runner device (`-m vm` only)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if VM_MARKER in (config.getoption("markexpr", "") or "").split():
        return
    here = Path(__file__).parent
    keep = [item for item in items if not (item.get_closest_marker(VM_MARKER) and here in item.path.parents)]
    if len(keep) != len(items):
        config.hook.pytest_deselected(items=[item for item in items if item not in keep])
        items[:] = keep


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture
def rig(store_dsn: str) -> Rig:
    return Rig(store_dsn)

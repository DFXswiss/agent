"""Fixtures of the session suites (RUN-04): a migrated store, the RUN-03 rig and the VM-02 harness."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_runner.store.migrate import migrate

from ..reconcile.rig import Rig

VM_MARKER = "vm"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "vm: integration with real Lima VMs on the runner device (`-m vm` only)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """The `vm` integration runs only when the marker expression asks for it (never in `make platform-ci`)."""
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


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A short directory under /tmp (Unix socket paths are limited to 104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="ses", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def harness(short_dir: Path, store_dsn: str, repo_root: Path, tmp_path: Path, fake_clock: Any) -> Iterator[Any]:
    """The VM-02 `Harness`: fake Lima with real guest control processes over the same store."""
    from ..vm.guests import GuestFarm, Harness, LimaWithGuests  # noqa: PLC0415

    lima_dir = short_dir / "lima"
    lima_dir.mkdir()
    fake = LimaWithGuests(lima_dir=lima_dir, farm=GuestFarm(contracts=repo_root / "contracts"))
    built = Harness(fake=fake, dsn=store_dsn, tokens_dir=tmp_path / "rh" / "vm", clock=fake_clock)
    try:
        yield built
    finally:
        assert fake.farm is not None
        fake.farm.close()

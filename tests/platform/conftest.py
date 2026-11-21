"""Root fixtures of the platform suites. Component conftests live in tests/platform/<component>/ (ADR-027 §6)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The checkout root of this repository."""
    return REPO_ROOT

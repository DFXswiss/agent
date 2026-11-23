"""Fixtures of the contract tests (CON-01, docs/platform/spec/07-testing.md section 4)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.platform.contracts.harness import CONTRACTS, Contracts, load_json


@pytest.fixture(scope="session")
def contracts() -> Contracts:
    """All schemas of `contracts/` in one registry."""
    return Contracts.load()


@pytest.fixture(scope="session")
def index() -> dict[str, Any]:
    """`contracts/api/INDEX.json`."""
    document: dict[str, Any] = load_json(CONTRACTS / "api" / "INDEX.json")
    return document

"""Fixtures of the model double suites: recorded Spike C fixtures, a started double with its client, request bodies."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.access import Access
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble

FIXTURES = Path(__file__).resolve().with_name("fixtures")

Started = tuple[ModelDouble, DoubleClient]


@pytest.fixture
def fixture_json() -> Callable[[str], Any]:
    """A recorded fixture file decoded as JSON."""

    def load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def start_model(
    serve: Callable[[ModelDouble], ModelDouble], client_for: Callable[[ModelDouble], DoubleClient]
) -> Callable[..., Started]:
    """Start a model double for a raw scenario (or steps) and return it with a client."""

    def start(
        *steps: dict[str, Any],
        scenario: dict[str, Any] | None = None,
        access: Access | None = None,
        journal: Path | None = None,
        variables: dict[str, str] | None = None,
    ) -> Started:
        raw = scenario if scenario is not None else {"name": "t", "steps": list(steps)}
        double = serve(ModelDouble(parse_scenario(raw), access=access, journal_path=journal, variables=variables))
        return double, client_for(double)

    return start


def user(text: str, **extra: Any) -> dict[str, Any]:
    """A minimal Messages request with one user text."""
    return {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": text}], **extra}


def sse(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """The events of a server-sent event stream."""
    events = []
    for chunk in raw.decode().split("\n\n"):
        if not chunk.strip():
            continue
        name, data = chunk.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


@pytest.fixture
def make_user() -> Callable[..., dict[str, Any]]:
    """`user` as a fixture (test modules cannot import this conftest)."""
    return user


@pytest.fixture
def parse_sse() -> Callable[[bytes], list[tuple[str, dict[str, Any]]]]:
    """`sse` as a fixture."""
    return sse

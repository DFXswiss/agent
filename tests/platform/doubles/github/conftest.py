"""Fixtures of the GitHub double suites: a started double with a seeded world, API helpers, a fake clock."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient, Response
from agent_doubles.github.server import GitHubDouble

FIXTURES = Path(__file__).resolve().with_name("fixtures")
TOKEN = "double-user-token"  # not a secret: the double only compares it
OTHER_TOKEN = "double-other-token"
REPO = "octo-org/hello"


class FakeClock:
    """A settable clock (seconds since the epoch)."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def base_seed() -> dict[str, Any]:
    """Two users, one installation on an organisation with two of its three repositories selected."""
    return {
        "users": [
            {"login": "octo-user", "id": 101, "name": "Octo User", "email": None, "tokens": [TOKEN]},
            {"login": "other-user", "id": 102, "name": None, "email": "other@example.invalid", "tokens": [OTHER_TOKEN]},
        ],
        "repositories": [
            {
                "full_name": REPO,
                "description": "Test repository.",
                "branches": [
                    {
                        "name": "main",
                        "commits": [
                            {"message": "Initial commit.", "files": {"README.md": "hello\n", "src/app.py": "x = 1\n"}},
                            {"message": "Second commit.", "files": {"README.md": "hello\nworld\n"}},
                        ],
                    },
                    {"name": "feature", "from": "main", "commits": [{"files": {"src/app.py": "x = 2\n"}}]},
                ],
                "tags": {"v1.0": "main"},
            },
            {"full_name": "octo-org/second", "private": False, "branches": [{"name": "main", "commits": [{}]}]},
            {"full_name": "octo-org/hidden", "branches": [{"name": "main", "commits": [{}]}]},
        ],
        "installations": [
            {
                "id": 31,
                "account": "octo-org",
                "repositories": [REPO, "octo-org/second"],
                "users": ["octo-user"],
            }
        ],
    }


@dataclass
class GitHub:
    """A started double, its control client and helpers for API requests."""

    double: GitHubDouble
    client: DoubleClient
    clock: FakeClock

    def api(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = TOKEN,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """An API request with the headers the runner sends (05 §12.1)."""
        sent = {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}
        if token is not None:
            sent["authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        return self.client.request(method, path, body, sent)

    def get(self, path: str, **kwargs: Any) -> Any:
        """``GET`` that must answer 200; the decoded body."""
        response = self.api("GET", path, **kwargs)
        assert response.status == 200, (response.status, response.body)
        return response.json()

    def control(self, method: str, path: str, body: Any = None) -> Response:
        return self.client.request(method, f"/_double/{path}", body)

    def state(self) -> dict[str, Any]:
        return self.client.state()


@pytest.fixture
def start_github(
    serve: Callable[[GitHubDouble], GitHubDouble], client_for: Callable[[GitHubDouble], DoubleClient]
) -> Callable[..., GitHub]:
    """Start a GitHub double with `seed` (default `base_seed`, None for an empty world)."""

    def start(seed: dict[str, Any] | bool | None = True, **kwargs: Any) -> GitHub:
        clock = FakeClock()
        double = serve(GitHubDouble(clock=clock, **kwargs))
        if seed is True:
            double.seed(base_seed())
        elif isinstance(seed, dict):
            double.seed(seed)
        return GitHub(double, client_for(double), clock)

    return start


@pytest.fixture
def github(start_github: Callable[..., GitHub]) -> GitHub:
    """A double with `base_seed`."""
    return start_github()


@pytest.fixture
def seed() -> Callable[[], dict[str, Any]]:
    """`base_seed` as a fixture (test modules cannot import this conftest)."""
    return base_seed


@pytest.fixture
def fixture_json() -> Callable[[str], Any]:
    """A shape fixture (``{"source", "route", "body"}``)."""

    def load(name: str) -> Any:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

    return load

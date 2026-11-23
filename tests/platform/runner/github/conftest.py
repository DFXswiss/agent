"""Fixtures of the GitHub suites (RUN-09): a migrated store and the GitHub double (DBL-02) as a real HTTP server on a
loopback port of the package range, seeded with one organisation installation, and helpers for its journal and its
control routes. The double answers with `https://github.example` URLs (`public_url`), as GitHub answers with https."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.github.server import GitHubDouble
from agent_runner.store.migrate import migrate

from ..credentials.kit import github_port

TOKEN = "double-user-token"  # not a secret: the double only compares it
OTHER_TOKEN = "double-other-token"
REPO = "octo-org/hello"
PUBLIC = "https://github.example"
_PORTS = itertools.count()


class DoubleClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def seed() -> dict[str, Any]:
    """Two users; installation 31 on `octo-org` covering `hello` and `second` (not `hidden`); on `hello` an open pull
    request #1 `feature` -> `main` with one comment, and a branch `topic` without a pull request."""
    return {
        "users": [
            {"login": "octo-user", "id": 101, "name": "Octo User", "email": None, "tokens": [TOKEN]},
            {"login": "other-user", "id": 102, "name": None, "email": None, "tokens": [OTHER_TOKEN]},
        ],
        "repositories": [
            {
                "full_name": REPO,
                "branches": [
                    {
                        "name": "main",
                        "commits": [
                            {"message": "Initial commit.", "files": {"README.md": "hello\n", "src/app.py": "x = 1\n"}},
                            {"message": "Second commit.", "files": {"README.md": "hello\nworld\n"}},
                        ],
                    },
                    {"name": "feature", "from": "main", "commits": [{"files": {"src/app.py": "x = 2\n"}}]},
                    {"name": "topic", "from": "main", "commits": [{"files": {"docs/a.md": "a\n"}}]},
                ],
                "pulls": [{"title": "Feature", "head": "feature", "base": "main", "draft": False, "user": "octo-user"}],
                "comments": [{"number": 1, "body": "Looks good.", "user": "other-user"}],
            },
            {"full_name": "octo-org/second", "private": False, "branches": [{"name": "main", "commits": [{}]}]},
            {"full_name": "octo-org/hidden", "branches": [{"name": "main", "commits": [{}]}]},
        ],
        "installations": [
            {"id": 31, "account": "octo-org", "repositories": [REPO, "octo-org/second"], "users": ["octo-user"]}
        ],
    }


@dataclass
class GitHub:
    double: GitHubDouble
    control: DoubleClient
    clock: DoubleClock

    @property
    def url(self) -> str:
        return self.double.url

    def journal(self) -> list[str]:
        """`METHOD path[?query]` of every journaled request, in order."""
        return [
            f"{r['method']} {r['path']}" + (f"?{r['query']}" if r["query"] else "")
            for r in self.double.journal.records()
        ]

    def paths(self) -> list[str]:
        """`METHOD path` without queries."""
        return [f"{r['method']} {r['path']}" for r in self.double.journal.records()]

    def faults(self, *faults: dict[str, Any]) -> None:
        answer = self.control.request("POST", "/_double/faults", {"faults": list(faults)})
        assert answer.status == 200, answer.body

    def decide(self, action: str, **extra: Any) -> None:
        grant = self.double.device.grants()[-1]
        body = {"user_code": grant["user_code"], "action": action, **extra}
        answer = self.control.request("POST", "/_double/device", body)
        assert answer.status == 200, answer.body

    def repo(self, name: str = REPO) -> dict[str, Any]:
        result: dict[str, Any] = self.control.request("GET", f"/_double/repos/{name}").json()
        return result

    def failures(self) -> list[str]:
        return self.double.failures


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture
def start_github() -> Iterator[Callable[..., GitHub]]:
    started: list[GitHubDouble] = []

    def start(*, public: str | None = PUBLIC, world: dict[str, Any] | None = None) -> GitHub:
        clock = DoubleClock()
        double = GitHubDouble(port=github_port(next(_PORTS)), clock=clock, public_url=public).start()
        started.append(double)
        double.seed(world if world is not None else seed())
        return GitHub(double, DoubleClient(double.url), clock)

    yield start
    for double in started:
        double.stop()


@pytest.fixture
def github(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github()

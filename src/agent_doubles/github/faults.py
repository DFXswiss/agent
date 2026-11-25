"""Fault injection and call expectations of the GitHub double.

A fault rule (``POST /_double/faults`` with ``{"faults": [...]}`` or ``"faults"`` in the seed)::

    {"route": "POST /repos/{owner}/{repo}/git/refs", "repo": "octo-org/hello", "kind": "error", "status": 502,
     "skip": 0, "times": 1}

- ``route``: a route template of the double (`agent_doubles.github.server.ROUTES`) or ``*`` for every route;
  ``repo`` (optional) limits it to one repository (case-insensitive).
- ``skip``: matching requests that pass unharmed before the fault fires; ``times``: how many requests it hits.
- ``kind``:
  - ``error``: ``status`` (500, 502, 503 or 504; default 502) with a GitHub error body;
  - ``retry_after``: ``429`` with ``Retry-After: <retry_after>`` seconds (default 1);
  - ``secondary_rate_limit``: ``403`` with the secondary-rate-limit message and ``Retry-After`` when
    ``retry_after`` is given;
  - ``primary_rate_limit``: ``403`` (or ``status: 429``) with ``x-ratelimit-remaining: 0`` and
    ``x-ratelimit-reset`` ``reset_in`` seconds (default 1) ahead;
  - ``exhaust``: the normal answer, but with ``x-ratelimit-remaining: 0`` and the reset ``reset_in`` seconds ahead;
  - ``slow``: the normal answer after ``delay_ms`` milliseconds (default 1000);
  - ``disconnect``: the connection is closed without an answer;
  - ``tree_mismatch`` (only ``POST /repos/{owner}/{repo}/git/trees``): the tree is created with an extra file
    ``.double-tree-mismatch``, so its id differs from the one the client computed (03 §9 ``tree_mismatch``);
  - ``truncated`` (only ``GET /repos/{owner}/{repo}/git/trees/{tree}``): ``truncated: true`` with only the first
    ``limit`` entries (default 1).
  - ``mergeable_unknown`` (only ``GET /repos/{owner}/{repo}/pulls/{number}``, KOR-48): the pull request answers
    ``mergeable: null`` and ``mergeable_state: "unknown"`` although its mergeability is computed (GitHub still
    computing); ``times`` reads stay unknown, so a client's re-reads can be tested up to "being checked".

Faults on API routes are applied after the header checks, so a retried request is still checked; their answers carry
the rate-limit headers like every API answer.

Every armed fault is an expectation: ``state.done`` stays false until it has fired ``times`` times.

An expectation (``POST /_double/expect`` with ``{"expectations": [...]}`` or ``"expectations"`` in the seed)::

    {"route": "POST /repos/{owner}/{repo}/issues/{number}/comments", "repo": "octo-org/hello", "count": 1}

is met when exactly ``count`` matching requests arrived; a further matching request is a failure.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

KINDS = frozenset(
    {
        "error",
        "retry_after",
        "secondary_rate_limit",
        "primary_rate_limit",
        "exhaust",
        "slow",
        "disconnect",
        "tree_mismatch",
        "truncated",
        "mergeable_unknown",
    }
)
# Kinds that change the normal answer of one route instead of replacing it.
ROUTE_BOUND = {
    "tree_mismatch": "POST /repos/{owner}/{repo}/git/trees",
    "truncated": "GET /repos/{owner}/{repo}/git/trees/{tree}",
    "mergeable_unknown": "GET /repos/{owner}/{repo}/pulls/{number}",
}
# Kinds after which the request is answered normally (possibly altered).
CONTINUING = frozenset({"slow", "exhaust", "tree_mismatch", "truncated", "mergeable_unknown"})
ERROR_STATUSES = frozenset({500, 502, 503, 504})
ANY_ROUTE = "*"


class FaultError(ValueError):
    """A fault rule or expectation the double refuses."""


def _int(data: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FaultError(f"{key} must be an integer >= {minimum}")
    return value


def _route_and_repo(data: dict[str, Any], routes: frozenset[str]) -> tuple[str, str | None]:
    route = data.get("route")
    if route != ANY_ROUTE and route not in routes:
        raise FaultError(f"unknown route {route!r}")
    repo = data.get("repo")
    if repo is not None and not isinstance(repo, str):
        raise FaultError("repo must be a string")
    return str(route), repo.lower() if repo is not None else None


@dataclass
class Fault:
    """One armed fault rule and how often it fired."""

    route: str
    repo: str | None
    kind: str
    status: int
    retry_after: int | None
    reset_in: int
    delay_ms: int
    limit: int
    skip: int
    times: int
    seen: int = 0
    fired: int = 0

    def matches(self, route: str, repo: str | None) -> bool:
        return self.route in {ANY_ROUTE, route} and (self.repo is None or self.repo == repo)

    @property
    def spent(self) -> bool:
        return self.fired >= self.times

    def describe(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "repo": self.repo,
            "kind": self.kind,
            "status": self.status,
            "fired": self.fired,
            "times": self.times,
        }


def parse_fault(raw: Any, routes: frozenset[str]) -> Fault:
    """A fault rule from its JSON form; `routes` are the known route templates."""
    if not isinstance(raw, dict):
        raise FaultError("a fault must be an object")
    allowed = {"route", "repo", "kind", "status", "retry_after", "reset_in", "delay_ms", "limit", "skip", "times"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise FaultError(f"unknown fault keys {unknown}")
    route, repo = _route_and_repo(raw, routes)
    kind = raw.get("kind")
    if kind not in KINDS:
        raise FaultError(f"kind must be one of {sorted(KINDS)}")
    if kind in ROUTE_BOUND and route != ROUTE_BOUND[kind]:
        raise FaultError(f"a {kind} fault applies to {ROUTE_BOUND[kind]} only")
    defaults = {"error": 502, "retry_after": 429, "secondary_rate_limit": 403, "primary_rate_limit": 403}
    status = _int(raw, "status", defaults.get(kind, 200), 100)
    if kind == "error" and status not in ERROR_STATUSES:
        raise FaultError(f"an error fault needs a status in {sorted(ERROR_STATUSES)}")
    if kind == "primary_rate_limit" and status not in {403, 429}:
        raise FaultError("a primary rate limit answers 403 or 429")
    if kind in {"retry_after", "secondary_rate_limit"} and status != defaults[kind]:
        raise FaultError(f"a {kind} fault answers {defaults[kind]}")
    retry_after = _int(raw, "retry_after", 1, 0) if kind == "retry_after" or "retry_after" in raw else None
    return Fault(
        route,
        repo,
        kind,
        status,
        retry_after,
        _int(raw, "reset_in", 1, 0),
        _int(raw, "delay_ms", 1000, 0),
        _int(raw, "limit", 1, 0),
        _int(raw, "skip", 0, 0),
        _int(raw, "times", 1, 1),
    )


@dataclass
class Expectation:
    """A route expected to be called exactly `count` times."""

    route: str
    repo: str | None
    count: int
    seen: int = 0

    def matches(self, route: str, repo: str | None) -> bool:
        return self.route in {ANY_ROUTE, route} and (self.repo is None or self.repo == repo)

    def describe(self) -> dict[str, Any]:
        return {"route": self.route, "repo": self.repo, "count": self.count, "seen": self.seen}


def parse_expectation(raw: Any, routes: frozenset[str]) -> Expectation:
    """An expectation from its JSON form."""
    if not isinstance(raw, dict):
        raise FaultError("an expectation must be an object")
    unknown = sorted(set(raw) - {"route", "repo", "count"})
    if unknown:
        raise FaultError(f"unknown expectation keys {unknown}")
    route, repo = _route_and_repo(raw, routes)
    return Expectation(route, repo, _int(raw, "count", 1, 0))


class Plan:
    """The armed faults and the expectations, thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.faults: list[Fault] = []
        self.expectations: list[Expectation] = []

    def add_faults(self, faults: list[Fault]) -> None:
        with self._lock:
            self.faults.extend(faults)

    def add_expectations(self, expectations: list[Expectation]) -> None:
        with self._lock:
            self.expectations.extend(expectations)

    def clear(self) -> None:
        with self._lock:
            self.faults.clear()
            self.expectations.clear()

    def take(self, route: str, repo: str | None) -> Fault | None:
        """The fault that hits this request, if any (the first armed matching rule past its ``skip``)."""
        with self._lock:
            for fault in self.faults:
                if fault.spent or not fault.matches(route, repo):
                    continue
                fault.seen += 1
                if fault.seen > fault.skip:
                    fault.fired += 1
                    return fault
            return None

    def observe(self, route: str, repo: str | None) -> list[str]:
        """Count a request against the expectations; texts of the expectations it exceeds."""
        exceeded = []
        with self._lock:
            for expectation in self.expectations:
                if expectation.matches(route, repo):
                    expectation.seen += 1
                    if expectation.seen > expectation.count:
                        exceeded.append(
                            f"{route} called {expectation.seen} times, expected {expectation.count}"
                            + (f" for {expectation.repo}" if expectation.repo else "")
                        )
        return exceeded

    def pending(self) -> list[str]:
        """Faults that have not fired as often as armed and expectations not yet met, as readable texts."""
        with self._lock:
            out = [
                f"fault {f.kind} on {f.route} fired {f.fired} of {f.times} times" for f in self.faults if not f.spent
            ]
            out += [f"expected {e.route} {e.count} times, seen {e.seen}" for e in self.expectations if e.seen < e.count]
            return out

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "faults": [fault.describe() for fault in self.faults],
                "expectations": [expectation.describe() for expectation in self.expectations],
            }

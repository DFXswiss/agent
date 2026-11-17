"""Security-issue counts from a Plane project. Tests inject FakeSecurityTracker."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

CACHE_SECONDS = 60.0
PAGE_SIZE = 100
MAX_PAGES = 50

# Plane state groups: backlog and unstarted are open, started is in progress, completed is done.
# Cancelled items are not vulnerabilities any more and are not counted.
_GROUP_BUCKET = {
    "backlog": "open",
    "unstarted": "open",
    "started": "in_progress",
    "completed": "done",
}


class SecurityTrackerError(ValueError):
    pass


@dataclass(frozen=True)
class SecurityCounts:
    open: int
    in_progress: int
    done: int

    def as_dict(self) -> dict[str, int]:
        return {"open": self.open, "in_progress": self.in_progress, "done": self.done}


class SecurityTracker:
    def counts(self) -> SecurityCounts:
        raise NotImplementedError


def count_by_group(states: list[dict[str, Any]], items: list[dict[str, Any]]) -> SecurityCounts:
    group_of: dict[str, str] = {}
    for state in states:
        sid = state.get("id")
        group = state.get("group")
        if isinstance(sid, str) and isinstance(group, str):
            group_of[sid] = group
    totals = {"open": 0, "in_progress": 0, "done": 0}
    for item in items:
        state = item.get("state")
        if not isinstance(state, str) or state not in group_of:
            raise SecurityTrackerError("work item has an unknown state")
        bucket = _GROUP_BUCKET.get(group_of[state])
        if bucket is not None:
            totals[bucket] += 1
    return SecurityCounts(**totals)


class PlaneSecurityTracker(SecurityTracker):
    """Reads one Plane project. `api_url` is the project's API base:
    `<plane>/api/v1/workspaces/<slug>/projects/<project-id>`."""

    def __init__(self, api_url: str, token: str, client: httpx.Client | None = None) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=20.0)
        self._lock = threading.Lock()
        self._cached: tuple[float, SecurityCounts] | None = None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(
            self._api_url + path,
            params=params,
            headers={"X-API-Key": self._token, "Accept": "application/json", "User-Agent": "agent-core"},
        )
        if resp.status_code != 200:
            raise SecurityTrackerError(f"security tracker request failed: HTTP {resp.status_code}")
        return resp.json()

    @staticmethod
    def _results(body: Any) -> list[dict[str, Any]]:
        rows = body.get("results") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise SecurityTrackerError("security tracker returned no results")
        return [r for r in rows if isinstance(r, dict)]

    def _fetch(self) -> SecurityCounts:
        states = self._results(self._get("/states/"))
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"per_page": PAGE_SIZE, "fields": "id,state"}
            if cursor:
                params["cursor"] = cursor
            body = self._get("/issues/", params)
            items.extend(self._results(body))
            if not (isinstance(body, dict) and body.get("next_page_results")):
                return count_by_group(states, items)
            nxt = body.get("next_cursor")
            if not isinstance(nxt, str) or nxt == "":
                raise SecurityTrackerError("security tracker returned no next cursor")
            cursor = nxt
        raise SecurityTrackerError("security tracker has more pages than allowed")

    def counts(self) -> SecurityCounts:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            fresh = self._fetch()
            self._cached = (now, fresh)
            return fresh


class FakeSecurityTracker(SecurityTracker):
    """Fixed counts. Used only by tests."""

    def __init__(self, counts: SecurityCounts | None = None, error: str = "") -> None:
        self._counts = counts or SecurityCounts(open=0, in_progress=0, done=0)
        self.error = error

    def counts(self) -> SecurityCounts:
        if self.error:
            raise SecurityTrackerError(self.error)
        return self._counts

"""U-HUB-analysis (usage): `GET /v1/device/workgroups/{wg}/usage` (ADR-095 section 4). The sessions of the workgroup
started in the period with their whole counters, charges, runtime and operation counts, per task (default) or per
session, newest first and paged; the total of the period only on the first page; `device:wg`."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.analysis.support import NOT_FOUND, Reads, invalid
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World, new_id

ZERO = {
    "requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_micro_usd": 0,
    "charged_micro_usd": 0,
}
HOUR_MS = 3_600_000


@pytest.fixture
def reads(world: World, fleet: Fleet) -> Reads:
    return Reads.build(world, fleet)


def _scene(reads: Reads) -> dict[str, str]:
    """Task `a` (main `s1` ended after an hour, child `s2` still working), task `b` (`s3`, one expired operation), an
    old task and a session of another workgroup."""
    a = reads.task(reads.ago(hours=3))
    s1 = reads.session(a, reads.ago(hours=3), state="stopped", ended=reads.ago(hours=2), model="claude-opus-4-1")
    s2 = reads.session(a, reads.ago(hours=2, minutes=30), parent=s1)
    reads.usage(s1, 3, 800, 1000)
    reads.usage(s2, 2, 400, 500)
    reads.charge(s1, 700, 1)
    reads.charge(s1, 300, 2)
    for state in ("succeeded", "failed", "denied"):
        reads.operation(s1, reads.ago(hours=2, minutes=50), state=state)
    reads.operation(s2, reads.ago(hours=2), state="rejected")
    b = reads.task(reads.ago(hours=1))
    s3 = reads.session(b, reads.ago(hours=1))
    reads.operation(s3, reads.ago(minutes=50), state="expired")
    reads.operation(s3, reads.ago(minutes=40), state="executing")
    old = reads.task(reads.ago(days=10))
    reads.session(old, reads.ago(days=10))
    foreign = reads.task(reads.ago(hours=1), wg=reads.other_wg)
    reads.session(foreign, reads.ago(hours=1), wg=reads.other_wg)
    return {"a": a, "b": b, "s1": s1, "s2": s2, "s3": s3}


def _figures(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in (*ZERO, "runtime_ms", "operations", "failed_operations")}


def test_usage_per_task_with_the_total_of_the_period(reads: Reads) -> None:
    ids = _scene(reads)
    since = format_time(reads.ago(days=7))
    body = reads.ok("usage", since=since)
    assert (body["workgroup_id"], body["since"], body["until"], body["by"]) == (
        reads.wg,
        since,
        "2026-11-21T12:00:00.123Z",
        "task",
    )
    assert [row["task_id"] for row in body["rows"]] == [ids["b"], ids["a"]]
    b, a = body["rows"]
    assert (a["created_at"], a["sessions"], b["sessions"]) == (format_time(reads.ago(hours=3)), 2, 1)
    assert _figures(a) == {
        "requests": 5,
        "input_tokens": 1200,
        "output_tokens": 600,
        "cache_read_tokens": 300,
        "cache_write_tokens": 150,
        "cost_micro_usd": 1500,
        "charged_micro_usd": 1000,
        "runtime_ms": HOUR_MS + int(2.5 * HOUR_MS),
        "operations": 4,
        "failed_operations": 3,
    }
    assert _figures(b) == {**ZERO, "runtime_ms": HOUR_MS, "operations": 2, "failed_operations": 1}
    assert body["total"] == {
        "sessions": 3,
        "tasks": 2,
        **{key: a[key] + b[key] for key in _figures(a)},
    }
    assert body["next_before"] is None


def test_usage_per_session_is_the_subagent_tree(reads: Reads) -> None:
    ids = _scene(reads)
    body = reads.ok("usage", since=format_time(reads.ago(days=7)), by="session")
    assert body["by"] == "session"
    assert [row["session_id"] for row in body["rows"]] == [ids["s3"], ids["s2"], ids["s1"]]
    s3, s2, s1 = body["rows"]
    assert {key: s1[key] for key in ("task_id", "parent_session_id", "depth", "state", "model_id", "ended_at")} == {
        "task_id": ids["a"],
        "parent_session_id": None,
        "depth": 0,
        "state": "stopped",
        "model_id": "claude-opus-4-1",
        "ended_at": format_time(reads.ago(hours=2)),
    }
    assert (s2["parent_session_id"], s2["depth"], s2["ended_at"], s2["model_id"]) == (ids["s1"], 1, None, None)
    assert (s1["charged_micro_usd"], s1["runtime_ms"], s3["task_id"]) == (1000, HOUR_MS, ids["b"])
    assert "sessions" not in s1
    assert body["total"]["sessions"] == 3


@pytest.mark.parametrize(("by", "key", "order"), [("task", "task_id", ("b", "a")), ("session", "session_id", None)])
def test_usage_pages_newest_first_and_totals_only_the_first_page(
    reads: Reads, by: str, key: str, order: tuple[str, ...] | None
) -> None:
    ids = _scene(reads)
    expected = [ids[name] for name in order or ("s3", "s2", "s1")]
    since = format_time(reads.ago(days=7))
    seen: list[str] = []
    page = reads.ok("usage", since=since, by=by, limit="1")
    assert page["total"] is not None
    while True:
        seen += [row[key] for row in page["rows"]]
        if page["next_before"] is None:
            break
        page = reads.ok("usage", since=since, by=by, limit="1", before=page["next_before"])
        assert page["total"] is None
    assert seen == expected


def test_the_period_bounds_the_sessions_by_their_start(reads: Reads) -> None:
    ids = _scene(reads)
    body = reads.ok("usage", since=format_time(reads.ago(days=7)), until=format_time(reads.ago(minutes=90)))
    assert [row["task_id"] for row in body["rows"]] == [ids["a"]]
    assert body["total"]["sessions"] == 2
    old = reads.ok("usage", since=format_time(reads.ago(days=11)), until=format_time(reads.ago(days=9)))
    assert (len(old["rows"]), old["total"]["tasks"], old["total"]["requests"]) == (1, 1, 0)
    empty = reads.ok("usage", since=format_time(reads.ago(minutes=10)))
    assert (empty["rows"], empty["total"]["sessions"], empty["next_before"]) == ([], 0, None)


def test_usage_of_a_workgroup_the_device_does_not_serve_is_not_found(reads: Reads) -> None:
    since = format_time(reads.ago(days=1))
    for wg in (reads.other_wg, new_id(), "not-an-id", new_id().upper()):
        assert reads.refused("usage", wg=wg, since=since) == NOT_FOUND, wg
    elsewhere = reads.fleet.org_device(reads.scene.draft, {reads.other_wg: "device"})
    response = reads.get("usage", device=elsewhere, since=since)
    assert response.status_code == 404
    assert reads.get("usage", wg=reads.other_wg, device=elsewhere, since=since).status_code == 200


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({}, "/since"),
        ({"since": "yesterday"}, "/since"),
        ({"since": "2026-11-21T12:00:00.123Z"}, "/since"),
        ({"since": "2026-08-21T12:00:00.122Z"}, "/since"),
        ({"since": "2026-11-20T00:00:00.000Z", "until": "now"}, "/until"),
        ({"since": "2026-11-20T00:00:00.000Z", "by": "day"}, "/by"),
        ({"since": "2026-11-20T00:00:00.000Z", "limit": "0"}, "/limit"),
        ({"since": "2026-11-20T00:00:00.000Z", "limit": "201"}, "/limit"),
        ({"since": "2026-11-20T00:00:00.000Z", "before": "a+b"}, "/before"),
        ({"since": "2026-11-20T00:00:00.000Z", "before": "abc"}, "/before"),
    ],
)
def test_usage_parameter_errors_name_the_parameter(reads: Reads, query: dict[str, str], pointer: str) -> None:
    assert reads.refused("usage", **query) == invalid(pointer)

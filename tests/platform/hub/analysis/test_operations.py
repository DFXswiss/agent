"""U-HUB-analysis (operations): `GET /v1/device/workgroups/{wg}/operations` (ADR-097 section 4). The operations of
every task of the workgroup newest first (`created_at`, then ID), filtered by period, type and state, paged; with
`group_by=type` the counts per type, state and error code instead."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.analysis import reads as analysis_reads
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.analysis.support import NOT_FOUND, Reads, invalid
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World, new_id


@pytest.fixture
def reads(world: World, fleet: Fleet) -> Reads:
    return Reads.build(world, fleet)


def _scene(reads: Reads) -> dict[str, str]:
    a = reads.task(reads.ago(hours=4))
    s = reads.session(a, reads.ago(hours=4))
    b = reads.task(reads.ago(hours=4))
    t = reads.session(b, reads.ago(hours=4))
    found = {
        "old": reads.operation(s, reads.ago(days=10)),
        "o1": reads.operation(s, reads.ago(hours=2), state="failed", error="timeout", finished=reads.ago(hours=1)),
        "o2": reads.operation(t, reads.ago(hours=1)),
        "o3": reads.operation(s, reads.ago(hours=1), op_type="github.comment", state="failed", error="timeout"),
    }
    foreign = reads.task(reads.ago(hours=1), wg=reads.other_wg)
    reads.operation(reads.session(foreign, reads.ago(hours=1), wg=reads.other_wg), reads.ago(hours=1))
    return {"a": a, "b": b, "s": s, "t": t, **found}


def _ids(body: dict[str, Any]) -> list[str]:
    return [row["operation_id"] for row in body["operations"]]


def test_the_operations_of_the_workgroup_newest_first(reads: Reads) -> None:
    ids = _scene(reads)
    newest = sorted([ids["o2"], ids["o3"]], reverse=True)
    body = reads.ok("operations")
    assert _ids(body) == [*newest, ids["o1"], ids["old"]]
    assert body["next_before"] is None
    assert body["operations"][2] == {
        "operation_id": ids["o1"],
        "task_id": ids["a"],
        "session_id": ids["s"],
        "type": "github.push",
        "state": "failed",
        "error_code": "timeout",
        "created_at": format_time(reads.ago(hours=2)),
        "finished_at": format_time(reads.ago(hours=1)),
    }
    assert body["operations"][3]["finished_at"] is None
    week = format_time(reads.ago(days=7))
    assert _ids(reads.ok("operations", since=week)) == [*newest, ids["o1"]]
    assert _ids(reads.ok("operations", until=format_time(reads.ago(minutes=90)))) == [ids["o1"], ids["old"]]
    assert _ids(reads.ok("operations", type="github.push")) == [ids["o2"], ids["o1"], ids["old"]]
    assert _ids(reads.ok("operations", state="failed")) == [ids["o3"], ids["o1"]]
    assert _ids(reads.ok("operations", since=week, type="github.push", state="succeeded")) == [ids["o2"]]


def test_the_operations_page_by_time_and_id(reads: Reads) -> None:
    ids = _scene(reads)
    seen: list[str] = []
    page = reads.ok("operations", limit="1")
    while True:
        seen += _ids(page)
        if page["next_before"] is None:
            break
        page = reads.ok("operations", limit="1", before=page["next_before"])
    assert seen == [*sorted([ids["o2"], ids["o3"]], reverse=True), ids["o1"], ids["old"]]


def test_the_counts_per_type_state_and_error(reads: Reads, monkeypatch: pytest.MonkeyPatch) -> None:
    _scene(reads)
    body = reads.ok("operations", group_by="type")
    assert body == {
        "groups": [
            {"type": "github.push", "state": "succeeded", "error_code": None, "count": 2},
            {"type": "github.comment", "state": "failed", "error_code": "timeout", "count": 1},
            {"type": "github.push", "state": "failed", "error_code": "timeout", "count": 1},
        ],
        "truncated": False,
    }
    week = reads.ok("operations", since=format_time(reads.ago(days=7)), state="failed", group_by="type")
    assert [group["type"] for group in week["groups"]] == ["github.comment", "github.push"]
    assert reads.ok("operations", group_by="type", before="abc") == body  # counts are not paged
    monkeypatch.setattr(analysis_reads, "LIST_MAX", 2)
    assert reads.ok("operations", group_by="type") == {"groups": body["groups"][:2], "truncated": True}


def test_the_operations_of_a_workgroup_the_device_does_not_serve_are_not_found(reads: Reads) -> None:
    for wg in (reads.other_wg, new_id(), "x"):
        assert reads.refused("operations", wg=wg) == NOT_FOUND, wg


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({"group_by": "action"}, "/group_by"),
        ({"since": "x"}, "/since"),
        ({"type": "GitHub.push"}, "/type"),
        ({"type": "push"}, "/type"),
        ({"type": "github." + "a" * 1100}, "/type"),
        ({"state": "done"}, "/state"),
        ({"before": "abc"}, "/before"),
        ({"before": "a b"}, "/before"),
        ({"limit": "0"}, "/limit"),
    ],
)
def test_operation_parameter_errors_name_the_parameter(reads: Reads, query: dict[str, str], pointer: str) -> None:
    assert reads.refused("operations", **query) == invalid(pointer)


def test_without_since_the_longest_period_before_until_is_read(reads: Reads) -> None:
    """No list or count spans more than 92 days: without `since` the period starts 92 days before `until`."""
    task = reads.task(reads.ago(days=200))
    session = reads.session(task, reads.ago(days=200))
    ancient = reads.operation(session, reads.ago(days=100))
    recent = reads.operation(session, reads.ago(days=91))
    assert _ids(reads.ok("operations")) == [recent]
    assert reads.ok("operations", group_by="type")["groups"] == [
        {"type": "github.push", "state": "succeeded", "error_code": None, "count": 1}
    ]
    assert _ids(reads.ok("operations", until=format_time(reads.ago(days=95)))) == [ancient]

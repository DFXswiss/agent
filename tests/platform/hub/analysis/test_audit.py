"""U-HUB-analysis (audit): `GET /v1/device/workgroups/{wg}/audit` (ADR-095 section 4). The audit rows of the
workgroup newest first, filtered by period, action or action group, task and session, paged by the cursor of the
audit view; with `group_by=action` the counts per action and result instead (largest first, bounded, `truncated`).
Rows of other workgroups and of the organization level never appear."""

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


def _scene(reads: Reads) -> dict[str, Any]:
    a, b = reads.task(reads.ago(hours=4)), reads.task(reads.ago(hours=4))
    s = reads.session(a, reads.ago(hours=4))
    rows = {
        "old": reads.audit("operation.failed", reads.ago(days=10)),
        "r1": reads.audit("operation.accepted", reads.ago(hours=3), task=a, session=s),
        "r2": reads.audit("operation.failed", reads.ago(hours=2), result="failed", task=a, session=s),
        "r3": reads.audit("operation.failed", reads.ago(hours=2), result="failed", task=b),
        "r4": reads.audit("hook.denied", reads.ago(hours=1), result="denied", session=s),
    }
    reads.audit("operation.failed", reads.ago(hours=1), wg=reads.other_wg)
    reads.audit("task.create", reads.ago(hours=1), wg="")
    return {"a": a, "b": b, "s": s, **rows}


def _seqs(body: dict[str, Any]) -> list[int]:
    return [row["seq"] for row in body["rows"]]


def _before_now(reads: Reads) -> str:
    """An end that leaves out the rows the hub writes at its own time (the scene's and the read log's)."""
    return format_time(reads.ago(seconds=1))


def test_the_rows_of_the_workgroup_newest_first(reads: Reads) -> None:
    ids = _scene(reads)
    body = reads.ok("audit", until=_before_now(reads))
    assert _seqs(body) == [ids["r4"], ids["r3"], ids["r2"], ids["r1"], ids["old"]]
    assert body["next_before"] is None
    row = body["rows"][2]
    assert {key: row[key] for key in ("action", "result", "org_id", "wg_id", "task_id", "session_id")} == {
        "action": "operation.failed",
        "result": "failed",
        "org_id": reads.scene.org,
        "wg_id": reads.wg,
        "task_id": ids["a"],
        "session_id": ids["s"],
    }
    assert (row["at"], row["actor_id"], row["target_kind"]) == (
        format_time(reads.ago(hours=2)),
        reads.scene.writer.id,
        "test",
    )
    everything = reads.ok("audit", limit="200")
    assert {row["wg_id"] for row in everything["rows"]} == {reads.wg}
    assert set(_seqs(body)) <= set(_seqs(everything))


def test_filters_by_period_action_task_and_session(reads: Reads) -> None:
    ids = _scene(reads)
    until = _before_now(reads)
    week = format_time(reads.ago(days=7))
    assert _seqs(reads.ok("audit", since=week, until=until)) == [ids["r4"], ids["r3"], ids["r2"], ids["r1"]]
    assert _seqs(reads.ok("audit", until=until, action="operation")) == [ids["r3"], ids["r2"], ids["r1"], ids["old"]]
    assert _seqs(reads.ok("audit", until=until, action="operation.failed")) == [ids["r3"], ids["r2"], ids["old"]]
    assert _seqs(reads.ok("audit", until=until, action="operation.fail")) == []
    assert _seqs(reads.ok("audit", until=until, task=ids["a"])) == [ids["r2"], ids["r1"]]
    assert _seqs(reads.ok("audit", until=until, session=ids["s"])) == [ids["r4"], ids["r2"], ids["r1"]]
    assert _seqs(reads.ok("audit", since=week, until=until, task=ids["b"], action="operation")) == [ids["r3"]]


def test_the_rows_page_with_the_audit_cursor(reads: Reads) -> None:
    ids = _scene(reads)
    until = _before_now(reads)
    seen: list[int] = []
    page = reads.ok("audit", until=until, limit="2")
    while True:
        seen += _seqs(page)
        if page["next_before"] is None:
            break
        page = reads.ok("audit", until=until, limit="2", before=page["next_before"])
    assert seen == [ids["r4"], ids["r3"], ids["r2"], ids["r1"], ids["old"]]


def test_the_counts_per_action_and_result(reads: Reads, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _scene(reads)
    until = _before_now(reads)
    body = reads.ok("audit", until=until, group_by="action")
    assert body == {
        "groups": [
            {"action": "operation.failed", "result": "failed", "count": 2},
            {"action": "hook.denied", "result": "denied", "count": 1},
            {"action": "operation.accepted", "result": "ok", "count": 1},
            {"action": "operation.failed", "result": "ok", "count": 1},
        ],
        "truncated": False,
    }
    week = format_time(reads.ago(days=7))
    filtered = reads.ok("audit", since=week, until=until, action="operation", group_by="action")
    assert filtered["groups"] == [
        {"action": "operation.failed", "result": "failed", "count": 2},
        {"action": "operation.accepted", "result": "ok", "count": 1},
    ]
    cursor = reads.ok("audit", until=until, limit="1")["next_before"]
    assert reads.ok("audit", until=until, group_by="action", before=cursor) == body  # counts are not paged
    assert reads.ok("audit", until=until, task=ids["b"], group_by="action")["groups"] == [
        {"action": "operation.failed", "result": "failed", "count": 1}
    ]
    monkeypatch.setattr(analysis_reads, "LIST_MAX", 1)
    cut = reads.ok("audit", until=until, group_by="action")
    assert cut == {"groups": body["groups"][:1], "truncated": True}


def test_the_audit_of_a_workgroup_the_device_does_not_serve_is_not_found(reads: Reads) -> None:
    for wg in (reads.other_wg, new_id(), "not-an-id"):
        assert reads.refused("audit", wg=wg) == NOT_FOUND, wg


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({"group_by": "type"}, "/group_by"),
        ({"since": "2026-11-21"}, "/since"),
        ({"until": "2026-11-21"}, "/until"),
        ({"since": "2026-11-21T12:00:00.123Z"}, "/since"),
        ({"since": "2026-01-01T00:00:00.000Z", "until": "2026-11-01T00:00:00.000Z"}, "/since"),
        ({"action": "Operation"}, "/action"),
        ({"task": "general"}, "/task"),
        ({"session": new_id().upper()}, "/session"),
        ({"before": "a+b"}, "/before"),
        ({"before": "abc"}, "/before"),
        ({"limit": "201"}, "/limit"),
    ],
)
def test_audit_parameter_errors_name_the_parameter(reads: Reads, query: dict[str, str], pointer: str) -> None:
    assert reads.refused("audit", **query) == invalid(pointer)


def test_without_since_the_longest_period_before_until_is_read(reads: Reads) -> None:
    """No list or count spans more than 92 days: without `since` the period starts 92 days before `until`."""
    ancient = reads.audit("operation.failed", reads.ago(days=100))
    recent = reads.audit("operation.failed", reads.ago(days=91))
    until = format_time(reads.ago(seconds=1))
    assert _seqs(reads.ok("audit", until=until)) == [recent]
    groups = reads.ok("audit", until=until, group_by="action")["groups"]
    assert groups == [{"action": "operation.failed", "result": "ok", "count": 1}]
    assert _seqs(reads.ok("audit", until=format_time(reads.ago(days=95)))) == [ancient]

"""U-HUB-attention (reads): `GET /v1/orgs/{org}/attention` and `GET /v1/orgs/{org}/attention/summary` - the order of
each category, the cursor in both orders, the filters, the counters and the rights: a member who cannot read a
workgroup never sees its tasks, not even in a counter (03-api.md section 4.6, ADR-064 sections 4 and 6; KOR-39)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from agent_hub.attention import read
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.orgs.support import Actor, new_id

NOT_CONTRACT = {"contract": False}


def _second_workgroup(situation: Situation) -> str:
    """A workgroup of the org that `writer` writes and `reader` cannot read."""
    scene = situation.scene
    wg = new_id()
    scene.draft.add("wg_create", scene.admin, wg=wg, epoch=1)
    scene.draft.add("grant", scene.admin, subject=scene.writer.id, wg=wg, access="write")
    response = scene.draft.post(scene.admin, scene.draft.wraps(scene.admin, "wg", wg, 1, [scene.admin, scene.writer]))
    assert response.status_code == 200, response.text
    return wg


def _get(actor: Actor, org: str, suffix: str = "", **params: str) -> httpx.Response:
    return actor.browser.get(f"/v1/orgs/{org}/attention{suffix}", params=params)


def _page(situation: Situation, actor: Actor, **params: str) -> dict[str, Any]:
    response = _get(actor, situation.desk.org, **params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _all(situation: Situation, actor: Actor, **params: str) -> list[str]:
    """Every task ID of a category, page by page."""
    found: list[str] = []
    query = dict(params)
    while True:
        body = _page(situation, actor, **query)
        found.extend(item["task"]["task_id"] for item in body["items"])
        if body["next_before"] is None:
            return found
        query["before"] = body["next_before"]


def _records(situation: Situation, wg: str, owner: str, kind: tuple[str, str], times: list[datetime]) -> list[str]:
    tasks = []
    for since in times:
        task = situation.insert_task(wg, owner)
        situation.insert_record(task, wg, owner, kind, since)
        tasks.append(task)
    return tasks


def test_lists_are_ordered_per_category_and_paged_in_both_orders(situation: Situation) -> None:
    scene, wg = situation.scene, situation.desk.wg
    now = situation.hub.clock()
    times = [now - timedelta(minutes=minutes) for minutes in (5, 1, 9, 1, 3, 7, 1)]
    waiting = _records(situation, wg, scene.writer.id, ("needs_decision", "question"), times)
    running = _records(situation, wg, scene.writer.id, ("working", "working"), times)
    ascending = [task for _since, task in sorted(zip(times, waiting, strict=True))]
    descending = [task for _since, task in sorted(zip(times, running, strict=True), reverse=True)]
    assert _all(situation, scene.reader, category="needs_decision", limit="3") == ascending
    assert _all(situation, scene.reader, category="working", limit="2") == descending
    assert _all(situation, scene.reader, category="working") == descending
    assert _all(situation, scene.reader, category="blocked") == []
    first = _page(situation, scene.reader, category="needs_decision", limit="7")
    assert first["next_before"] is None
    item = first["items"][0]
    assert item["task"]["task_id"] == item["attention"]["task_id"] == ascending[0]
    assert item["attention"]["category"] == "needs_decision"
    assert item["task"]["wg_id"] == wg


def test_a_member_never_sees_a_workgroup_it_cannot_read(situation: Situation) -> None:
    scene, wg = situation.scene, situation.desk.wg
    hidden_wg = _second_workgroup(situation)
    now = situation.hub.clock()
    visible = _records(situation, wg, scene.writer.id, ("blocked", "session_failed"), [now])
    hidden = _records(situation, hidden_wg, scene.writer.id, ("blocked", "session_failed"), [now])
    _records(situation, hidden_wg, scene.writer.id, ("needs_decision", "question"), [now])
    assert _all(situation, scene.reader, category="blocked") == visible
    assert sorted(_all(situation, scene.writer, category="blocked")) == sorted(visible + hidden)
    assert _get(scene.reader, situation.desk.org, "/summary").json() == {
        "needs_decision": 0,
        "needs_decision_mine": 0,
        "blocked": 1,
        "working": 0,
        "done_since": 0,
    }
    assert _get(scene.writer, situation.desk.org, "/summary").json()["needs_decision"] == 1
    lonely = situation.scene.world.user()
    situation.scene.world.join(scene.draft, lonely)
    assert _all(situation, lonely, category="blocked") == []
    assert set(_get(lonely, situation.desk.org, "/summary").json().values()) == {0}


def test_filters_mine_and_since(situation: Situation) -> None:
    scene, wg = situation.scene, situation.desk.wg
    now = situation.hub.clock()
    old = _records(situation, wg, scene.writer.id, ("needs_decision", "approval"), [now - timedelta(hours=2)])
    mine = _records(situation, wg, scene.reader.id, ("needs_decision", "question"), [now - timedelta(minutes=1)])
    assert _all(situation, scene.reader, category="needs_decision", mine="1") == mine
    assert _all(situation, scene.reader, category="needs_decision", mine="0") == old + mine
    since = format_time(now - timedelta(hours=1))
    assert _all(situation, scene.reader, category="needs_decision", since=since) == mine
    summary = _get(scene.reader, situation.desk.org, "/summary").json()
    assert (summary["needs_decision"], summary["needs_decision_mine"]) == (2, 1)


def test_the_summary_counts_recently_finished_tasks(situation: Situation) -> None:
    scene, wg = situation.scene, situation.desk.wg
    now = situation.hub.clock()
    _records(situation, wg, scene.writer.id, ("done", "idle"), [now - timedelta(hours=1), now - timedelta(hours=30)])
    _records(situation, wg, scene.writer.id, ("done", "closed"), [now - timedelta(hours=23)])
    _records(situation, wg, scene.writer.id, ("working", "queued"), [now - timedelta(days=9)])
    summary = _get(scene.writer, situation.desk.org, "/summary").json()
    assert (summary["done_since"], summary["working"]) == (2, 1)
    week = _get(scene.writer, situation.desk.org, "/summary", since="2026-11-01T00:00:00.000Z").json()
    assert week["done_since"] == 3
    assert timedelta(hours=24) == read.DONE_WINDOW


@pytest.mark.parametrize(
    ("params", "pointer"),
    [
        ({}, "/category"),
        ({"category": "stuck"}, "/category"),
        ({"category": "done", "mine": "yes"}, "/mine"),
        ({"category": "done", "limit": "101"}, "/limit"),
        ({"category": "done", "limit": "0"}, "/limit"),
        ({"category": "done", "since": "yesterday"}, "/since"),
        ({"category": "done", "since": "2026-02-31T00:00:00.000Z"}, "/since"),
        ({"category": "done", "before": "not-a-cursor"}, "/before"),
    ],
)
def test_malformed_queries_are_refused(situation: Situation, params: dict[str, str], pointer: str) -> None:
    response = situation.scene.reader.browser.get(
        f"/v1/orgs/{situation.desk.org}/attention", params=params, extensions=NOT_CONTRACT
    )
    assert response.status_code == 400, response.text
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}


def test_a_malformed_since_of_the_summary_is_refused(situation: Situation) -> None:
    response = situation.scene.reader.browser.get(
        f"/v1/orgs/{situation.desk.org}/attention/summary", params={"since": "now"}, extensions=NOT_CONTRACT
    )
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/since"]}}


@pytest.mark.parametrize("suffix", ["", "/summary"])
def test_strangers_and_unknown_orgs_get_not_found(situation: Situation, suffix: str) -> None:
    params = {"category": "done"} if not suffix else {}
    for actor, org in (
        (situation.scene.stranger, situation.desk.org),
        (situation.scene.reader, new_id()),
        (situation.scene.reader, "not-an-id"),
    ):
        response = actor.browser.get(f"/v1/orgs/{org}/attention{suffix}", params=params)
        assert response.status_code == 404, (actor, org)
        assert response.json()["error"] == "not_found"

"""U-HUB-attention (volume, E-02/E-19): 5 000 tasks over 50 workgroups, each with a subagent tree of depth 2 (4
children with 2 children each), backfilled and read page by page - every page complete without duplicates, the
counters equal the pages, the subagent counters right, each page within a generous time bound (ADR-076 section 4;
KOR-39)."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest

from agent_hub.attention import jobs, read
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.orgs.support import Actor, new_id

TASKS = 5000
WORKGROUPS = 50
CHILDREN, GRANDCHILDREN = 4, 2
PAGE_SECONDS = 5.0
"""Generous: the work device runs other suites in parallel; a page takes milliseconds on an idle machine."""
STATES = ("working", "idle", "failed", "closed", "scheduling")
EXPECTED = {
    "working": ("working", "subagents"),
    "idle": ("done", "idle"),
    "failed": ("blocked", "session_failed"),
    "closed": ("done", "closed"),
    "scheduling": ("needs_decision", "device_missing"),
}

V7 = "overlay(gen_random_uuid()::text placing '7' from 15 for 1)::uuid"
"""A random UUID with the version digit of a UUIDv7 (the contracts accept only those IDs)."""
_TASKS = """
INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, state_reason, created_at, state_at, last_activity_at,
  closed_at)
SELECT {V7}, %(org)s, (%(wgs)s::uuid[])[1 + i %% %(groups)s],
  CASE WHEN i %% 2 = 0 THEN %(writer)s::uuid ELSE %(reader)s::uuid END, s.state,
  CASE s.state WHEN 'failed' THEN 'agent_exited' WHEN 'scheduling' THEN 'device_offline' END,
  %(now)s - i * interval '1 second', %(now)s - i * interval '1 second', %(now)s - i * interval '1 second',
  CASE WHEN s.state = 'closed' THEN %(now)s - i * interval '1 second' END
FROM generate_series(0, %(last)s) i
CROSS JOIN LATERAL (SELECT (%(states)s::text[])[1 + i %% 5] AS state) s
""".replace("{V7}", V7)
_MAIN = """
INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, depth, state, state_reason, created_at)
SELECT {V7}, t.id, t.org_id, t.wg_id, t.owner_id, 0,
  CASE t.state WHEN 'failed' THEN 'failed' WHEN 'closed' THEN 'stopped' WHEN 'scheduling' THEN 'requested'
  ELSE 'idle' END,
  CASE t.state WHEN 'failed' THEN 'agent_exited' END, t.created_at
FROM hub.task t WHERE t.org_id = %(org)s AND t.wg_id = ANY(%(wgs)s::uuid[])
""".replace("{V7}", V7)
_CHILDREN = """
INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, parent_session_id, depth, state, created_at)
SELECT {V7}, p.task_id, p.org_id, p.wg_id, p.owner_id, p.id, p.depth + 1,
  CASE WHEN t.state = 'working' AND c = 1 THEN 'working' WHEN t.state = 'working' THEN 'idle' ELSE 'stopped' END,
  p.created_at + c * interval '1 millisecond'
FROM hub.session p JOIN hub.task t ON t.id = p.task_id CROSS JOIN generate_series(1, %(count)s) c
WHERE p.org_id = %(org)s AND p.wg_id = ANY(%(wgs)s::uuid[]) AND p.depth = %(depth)s
""".replace("{V7}", V7)


def _estate(situation: Situation) -> tuple[list[str], set[str]]:
    """The workgroups (all written by `writer`, the first half read by `reader`) and their tasks."""
    scene, desk = situation.scene, situation.desk
    wgs = [new_id() for _ in range(WORKGROUPS)]
    with situation.hub.pool.connection() as connection, connection.transaction():
        for index, wg in enumerate(wgs):
            connection.execute(
                "INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, false, 0)", (wg, desk.org)
            )
            connection.execute(
                "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'write', 0)",
                (wg, scene.writer.id),
            )
            if index < WORKGROUPS // 2:
                connection.execute(
                    "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'read', 0)",
                    (wg, scene.reader.id),
                )
        values: dict[str, Any] = {
            "org": desk.org,
            "wgs": wgs,
            "groups": WORKGROUPS,
            "writer": scene.writer.id,
            "reader": scene.reader.id,
            "now": situation.hub.clock() - timedelta(seconds=1),
            "last": TASKS - 1,
            "states": list(STATES),
        }
        connection.execute(_TASKS, values)
        connection.execute(_MAIN, values)
        connection.execute(_CHILDREN, {**values, "count": CHILDREN, "depth": 0})
        connection.execute(_CHILDREN, {**values, "count": GRANDCHILDREN, "depth": 1})
    tasks = {str(row[0]) for row in desk.rows("SELECT id FROM hub.task WHERE wg_id = ANY(%s::uuid[])", wgs)}
    return wgs, tasks


def _pages(situation: Situation, actor: Actor, category: str, **params: str) -> list[dict[str, Any]]:
    """Every page of one category (or, with `category` empty and `overview`, of one overview of the open tasks)."""
    items: list[dict[str, Any]] = []
    which = {"category": category} if category else {}
    query = {**which, "limit": str(read.LIST_MAX), **params}
    while True:
        started = time.monotonic()
        response = actor.browser.get(f"/v1/orgs/{situation.desk.org}/attention", params=query)
        assert time.monotonic() - started < PAGE_SECONDS
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) <= read.LIST_MAX
        items.extend(body["items"])
        if body["next_before"] is None:
            return items
        assert len(body["items"]) == read.LIST_MAX
        query["before"] = body["next_before"]


@pytest.mark.timing
def test_thousands_of_tasks_with_deep_subagent_trees(situation: Situation) -> None:
    wgs, tasks = _estate(situation)
    assert jobs.backfill(situation.hub.app.state.hub) == TASKS
    with situation.hub.pool.connection() as connection:  # what autovacuum does after a bulk load
        connection.execute("ANALYZE hub.task, hub.session, hub.task_attention")
    sessions = situation.desk.rows("SELECT count(*) FROM hub.session WHERE wg_id = ANY(%s::uuid[])", wgs)[0][0]
    assert sessions == TASKS * (1 + CHILDREN + CHILDREN * GRANDCHILDREN)
    scene = situation.scene
    readable = {"writer": set(wgs), "reader": set(wgs[: WORKGROUPS // 2])}
    for name, actor in (("writer", scene.writer), ("reader", scene.reader)):
        seen: set[str] = set()
        counts: dict[str, int] = {}
        closed = 0
        for category in ("needs_decision", "blocked", "working", "done"):
            items = _pages(situation, actor, category)
            ids = [item["task"]["task_id"] for item in items]
            assert len(ids) == len(set(ids)), (name, category)
            keys = [(item["attention"]["since"], item["attention"]["task_id"]) for item in items]
            assert keys == sorted(keys, reverse=category != "needs_decision"), (name, category)
            for item in items:
                assert item["task"]["wg_id"] in readable[name]
                assert EXPECTED[item["task"]["state"]] == (category, item["attention"]["reason"])
                total = CHILDREN * (1 + GRANDCHILDREN)
                assert item["attention"]["subagents_total"] == total
                assert item["attention"]["subagents_active"] == (total if item["task"]["state"] == "working" else 0)
            seen |= set(ids)
            counts[category] = len(ids)
            if category == "done":
                closed = sum(1 for item in items if item["attention"]["reason"] == "closed")
        assert seen <= tasks
        assert len(seen) == (TASKS if name == "writer" else TASKS // 2)
        mine = _pages(situation, actor, "needs_decision", mine="1")
        assert {item["task"]["owner_id"] for item in mine} <= {actor.id}
        # the overview of the open tasks: the list "No agent" holds the stuck and the idle tasks, never a closed one
        no_agent = _pages(situation, actor, "", overview="no_agent")
        reasons = [(item["attention"]["category"], item["attention"]["reason"]) for item in no_agent]
        assert set(reasons) == {("blocked", "session_failed"), ("done", "idle")}
        keys = [(item["attention"]["since"], item["attention"]["task_id"]) for item in no_agent]
        assert keys == sorted(keys, reverse=True)
        summary = actor.browser.get(f"/v1/orgs/{situation.desk.org}/attention/summary").json()
        assert summary == {
            "needs_decision": counts["needs_decision"],
            "needs_decision_mine": len(mine),
            "blocked": counts["blocked"],
            "working": counts["working"],
            "done_since": counts["done"],
            "open_working": counts["working"],
            "open_waiting": counts["needs_decision"],
            "open_no_agent": len(no_agent),
        }
        # every task in exactly one number, or closed
        assert summary["open_working"] + summary["open_waiting"] + summary["open_no_agent"] + closed == len(seen)

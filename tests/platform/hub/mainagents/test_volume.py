"""U-HUB-mainagents (volume, E-02/E-19): 6 000 open and 3 000 closed tasks of 300 people in 50 workgroups, each open
task with 4 subagents (2 of them running), read page by page - the people of the caller complete and in order without
duplicates, their counters equal to the stored records, a person's main agents complete and newest first, each page
within a generous time bound, and the plans of both reads use the partial index `task_open_owner` (ADR-095 section 5;
KOR-52)."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.mainagents import read
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.orgs.support import Actor, new_id

OPEN, CLOSED = 6000, 3000
PEOPLE = 300
WORKGROUPS = 50
CHILDREN = 4
PAGE_SECONDS = 5.0
"""Generous: the work device runs other suites in parallel; a page takes milliseconds on an idle machine."""
INDEX = "task_open_owner"
NAME_INDEX = "event_task_agent_name"
EVENTS = 1000
"""Session events of each open task of the busy person: its name event lies far behind them."""
CATEGORIES = ("needs_decision", "blocked", "working", "done")

V7 = "overlay(gen_random_uuid()::text placing '7' from 15 for 1)::uuid"
"""A random UUID with the version digit of a UUIDv7 (the contracts accept only those IDs)."""
_TASKS = """
INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at, closed_at)
SELECT {V7}, %(org)s, (%(wgs)s::uuid[])[1 + (i + i / %(people)s) %% %(groups)s],
  (%(owners)s::uuid[])[1 + i %% %(people)s],
  CASE WHEN i < %(open)s THEN 'working' ELSE 'closed' END,
  %(now)s - i * interval '1 second', %(now)s - i * interval '1 second', %(now)s - (i %% 997) * interval '1 second',
  CASE WHEN i >= %(open)s THEN %(now)s END
FROM generate_series(0, %(last)s) i
""".replace("{V7}", V7)
_MAIN = """
INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, depth, state, created_at)
SELECT {V7}, t.id, t.org_id, t.wg_id, t.owner_id, 0, 'working', t.created_at
FROM hub.task t WHERE t.org_id = %(org)s AND t.state <> 'closed'
""".replace("{V7}", V7)
_CHILDREN = """
INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, parent_session_id, depth, state, created_at)
SELECT {V7}, p.task_id, p.org_id, p.wg_id, p.owner_id, p.id, 1, CASE WHEN c <= 2 THEN 'working' ELSE 'stopped' END,
  p.created_at + c * interval '1 millisecond'
FROM hub.session p CROSS JOIN generate_series(1, %(count)s) c WHERE p.org_id = %(org)s AND p.depth = 0
""".replace("{V7}", V7)
_RECORDS = """
INSERT INTO hub.task_attention (task_id, org_id, wg_id, owner_id, category, reason, since, seq, subagents_total,
  subagents_active, updated_at)
SELECT t.id, t.org_id, t.wg_id, t.owner_id, (%(categories)s::text[])[1 + abs(hashtext(t.id::text)) %% 4], 'working',
  t.last_activity_at, 1, %(count)s, 2, t.last_activity_at
FROM hub.task t WHERE t.org_id = %(org)s AND t.state <> 'closed'
"""


_EVENTS = """
INSERT INTO hub.event (scope_id, scope_seq, id, scope, org_id, wg_id, task_id, author_id, author_kind, kind, ref,
  epoch, ts, lh, header_text, ct, sig, size, received_at)
SELECT t.wg_id, row_number() OVER (PARTITION BY t.wg_id ORDER BY t.id, e) + 1000000, {V7}, 'wg', t.org_id, t.wg_id,
  t.id, t.owner_id, 'user', CASE WHEN e = 0 THEN 'task.agent_name' ELSE 'session.output' END, '', 1, %(now)s, 0, '{}',
  '\\x00'::bytea, decode(repeat('00', 64), 'hex'), 1, %(now)s
FROM hub.task t CROSS JOIN generate_series(0, %(events)s) e
WHERE t.org_id = %(org)s AND t.owner_id = %(busy)s AND t.state <> 'closed'
""".replace("{V7}", V7)


def _estate(situation: Situation) -> tuple[list[str], list[str]]:
    """The workgroups (every other one read by `reader`) and the people; `reader` is the first of them. Each person's
    open tasks lie in 20 consecutive workgroups, so half of them are readable."""
    scene, desk = situation.scene, situation.desk
    wgs = [new_id() for _ in range(WORKGROUPS)]
    owners = [scene.reader.id, *(new_id() for _ in range(PEOPLE - 1))]
    with situation.hub.pool.connection() as connection, connection.transaction():
        for index, wg in enumerate(wgs):
            connection.execute(
                "INSERT INTO hub.wg (id, org_id, is_default, created_seq) VALUES (%s, %s, false, 0)", (wg, desk.org)
            )
            if index % 2 == 0:
                connection.execute(
                    "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'read', 0)",
                    (wg, scene.reader.id),
                )
        values: dict[str, Any] = {
            "org": desk.org,
            "wgs": wgs,
            "groups": WORKGROUPS,
            "owners": owners,
            "people": PEOPLE,
            "open": OPEN,
            "now": situation.hub.clock() - timedelta(seconds=1),
            "last": OPEN + CLOSED - 1,
            "count": CHILDREN,
            "categories": list(CATEGORIES),
        }
        connection.execute(_TASKS, values)
        connection.execute(_MAIN, values)
        connection.execute(_CHILDREN, values)
        connection.execute(_RECORDS, values)
        connection.execute(_EVENTS, {**values, "busy": owners[2], "events": EVENTS})
        connection.execute(
            "ANALYZE hub.task, hub.session, hub.task_attention, hub.event"
        )  # what autovacuum does after a load
    return wgs, owners


def _timed(actor: Actor, path: str, params: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    response = actor.browser.get(path, params=params)
    assert time.monotonic() - started < PAGE_SECONDS
    assert response.status_code == 200, response.text
    return dict(response.json())


def _expected(situation: Situation, wgs: list[str]) -> list[dict[str, Any]]:
    rows = situation.desk.rows(
        "SELECT t.owner_id::text, count(*), count(*) FILTER (WHERE a.category = 'needs_decision'),"
        " count(*) FILTER (WHERE a.category = 'blocked'), count(*) FILTER (WHERE a.category = 'working'),"
        " sum(a.subagents_active) FROM hub.task t JOIN hub.task_attention a ON a.task_id = t.id"
        " WHERE t.state <> 'closed' AND t.wg_id = ANY(%s::uuid[]) GROUP BY t.owner_id",
        wgs,
    )
    return [dict(zip(read.PERSON_FIELDS, (row[0], *map(int, row[1:])), strict=True)) for row in rows]


def _plan(situation: Situation, statement: str, params: Any) -> str:
    with situation.hub.pool.connection() as connection:
        found = connection.execute(f"EXPLAIN {statement}", params).fetchall()
    return "\n".join(str(row[0]) for row in found)


@pytest.mark.timing
def test_hundreds_of_people_with_thousands_of_main_agents(situation: Situation) -> None:
    wgs, owners = _estate(situation)
    reader, org = situation.scene.reader, situation.desk.org
    readable = wgs[::2]
    expected = _expected(situation, readable)
    mine = next(row for row in expected if row["owner_id"] == reader.id)
    ordered = [mine, *sorted((row for row in expected if row is not mine), key=lambda row: row["owner_id"])]
    found: list[dict[str, Any]] = []
    query = {"limit": str(read.LIST_MAX)}
    while True:
        body = _timed(reader, f"/v1/orgs/{org}/people/agents", query)
        assert len(body["people"]) <= read.LIST_MAX
        found.extend(body["people"])
        if body["next_after"] is None:
            break
        assert len(body["people"]) == read.LIST_MAX
        query["after"] = body["next_after"]
    assert found == ordered
    assert len(found) == PEOPLE  # every person has open tasks in the readable half
    assert sum(row["agents"] for row in found) == OPEN // 2
    assert sum(row["subagents_active"] for row in found) == OPEN // 2 * 2
    # the main agents of the caller and of another person, page by page
    for owner in (reader.id, owners[-1]):
        items: list[dict[str, Any]] = []
        params = {"owner": owner, "limit": "7"}
        while True:
            body = _timed(reader, f"/v1/orgs/{org}/main-agents", params)
            items.extend(body["items"])
            if body["next_before"] is None:
                break
            params["before"] = body["next_before"]
        keys = [(item["task"]["last_activity_at"], item["task"]["task_id"]) for item in items]
        assert keys == sorted(keys, reverse=True)
        assert len(set(keys)) == len(keys)
        assert {item["task"]["wg_id"] for item in items} <= set(readable)
        listed = next(row for row in found if row["owner_id"] == owner)
        assert len(items) == listed["agents"]
        assert all(item["attention"] is not None for item in items)
    # both reads use the partial index of the open tasks
    with situation.hub.pool.connection() as connection:
        values = read.people_values(connection, UUID(org), UUID(reader.id), UUID(owners[1]))
        statement, arguments = read.main_agents_query(
            connection, UUID(org), UUID(reader.id), owner=UUID(owners[2]), before=None, limit=read.LIST_MAX
        )
    for condition in (read.OWN, read.OTHERS, read.OTHERS_AFTER):
        plan = _plan(situation, read.people_statement(condition), {**values, "limit": read.LIST_MAX + 1})
        assert INDEX in plan, plan
    plan = _plan(situation, statement, arguments)
    assert INDEX in plan, plan
    assert NAME_INDEX in plan, plan  # the name of a task is found without walking its event history
    # the busy person's main agents name their (oldest) name event within the bound of a page
    busy = _timed(reader, f"/v1/orgs/{org}/main-agents", {"owner": owners[2], "limit": str(read.LIST_MAX)})
    names = situation.desk.rows(
        "SELECT task_id::text, id::text FROM hub.event WHERE kind = 'task.agent_name' AND task_id = ANY(%s::uuid[])",
        [item["task"]["task_id"] for item in busy["items"]],
    )
    assert busy["items"]
    assert {item["task"]["task_id"]: item["task"]["name_event_id"] for item in busy["items"]} == dict(names)

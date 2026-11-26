"""Test support of the main agents suites (U-HUB-mainagents, KOR-52): rows written directly for the read suites (open
and closed tasks of several owners, their attention records with active subagents) and a workgroup the reader cannot
read, on top of the situation helper of the attention suites (`tests/platform/hub/attention/support.py`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from tests.platform.hub.attention.support import Situation
from tests.platform.hub.orgs.support import Actor, new_id

NOT_CONTRACT = {"contract": False}


def hidden_workgroup(situation: Situation) -> str:
    """A workgroup of the org that `writer` writes and `reader` cannot read."""
    scene = situation.scene
    wg = new_id()
    scene.draft.add("wg_create", scene.admin, wg=wg, epoch=1)
    scene.draft.add("grant", scene.admin, subject=scene.writer.id, wg=wg, access="write")
    response = scene.draft.post(scene.admin, scene.draft.wraps(scene.admin, "wg", wg, 1, [scene.admin, scene.writer]))
    assert response.status_code == 200, response.text
    return wg


def task(
    situation: Situation,
    owner: str,
    *,
    wg: str | None = None,
    state: str = "working",
    at: datetime | None = None,
    category: str | None = None,
    active: int = 0,
) -> str:
    """A task of `owner` (in `wg`, default the scene's) whose activity is at `at`, with an attention record of
    `category` and `active` running subagents when a category is given."""
    target = wg or situation.desk.wg
    created = situation.insert_task(target, owner, state=state, at=at)
    if category is not None:
        since = at or situation.hub.clock()
        situation.desk.execute(
            "INSERT INTO hub.task_attention (task_id, org_id, wg_id, owner_id, category, reason, since, seq,"
            " subagents_total, subagents_active, updated_at) VALUES (%s, %s, %s, %s, %s, 'working', %s, 1, %s, %s,"
            " %s)",
            created,
            situation.desk.org,
            target,
            owner,
            category,
            since,
            active + 1,
            active,
            since,
        )
    return created


def people(actor: Actor, org: str, **params: str) -> httpx.Response:
    return actor.browser.get(f"/v1/orgs/{org}/people/agents", params=params)


def agents(actor: Actor, org: str, **params: str) -> httpx.Response:
    return actor.browser.get(f"/v1/orgs/{org}/main-agents", params=params)


def all_people(situation: Situation, actor: Actor, **params: str) -> list[dict[str, Any]]:
    """Every person row, page by page (each page checked against the contract by the test client)."""
    found: list[dict[str, Any]] = []
    query = dict(params)
    while True:
        response = people(actor, situation.desk.org, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        found.extend(body["people"])
        if body["next_after"] is None:
            return found
        assert body["next_after"] == body["people"][-1]["owner_id"]
        query["after"] = body["next_after"]


def all_agents(situation: Situation, actor: Actor, owner: str, **params: str) -> list[dict[str, Any]]:
    """Every main agent of `owner`, page by page."""
    found: list[dict[str, Any]] = []
    query = {"owner": owner, **params}
    while True:
        response = agents(actor, situation.desk.org, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        found.extend(body["items"])
        if body["next_before"] is None:
            return found
        query["before"] = body["next_before"]

"""The main-agent views of the hub fake as the hub runs them (KOR-52, ADR-095 section 5; 03-api.md section 4.6):

- `GET /v1/orgs/{org}/people/agents?after&limit` (members, else `404`; `limit` 1-100, default 50; any other parameter
  `400 request_invalid`): per owner the open tasks (state not `closed`) of the workgroups the caller may read, with the
  counts of the attention records (`hub.attention`: `needs_decision`, `blocked`, `working`) and the sum of their active
  subagents; the caller first, then by owner ID; `after` = the last owner ID of the previous page.
- `GET /v1/orgs/{org}/main-agents?owner&before&limit` (`owner` required): the open tasks of `owner` in the readable
  workgroups, newest activity first (`last_activity_at`, then task ID), each with its attention record or null;
  `before` = `<last_activity_at>|<task_id>` of the last item.
"""

from __future__ import annotations

from typing import Any, cast

from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import WG

from ..hubfake import HubFake, Json, Request


def install(hub: HubFake) -> None:
    def open_tasks(request: Request, org: str) -> list[Json] | None:
        state = hub.orgs[org].log.state if org in hub.orgs else None
        user = cast(str, request.user)
        if state is None or state.member(user) is None:
            return None
        return [
            found["task"]
            for found in hub.tasks.values()
            if found["task"]["org_id"] == org
            and found["task"]["state"] != "closed"
            and state.can_read(user, WG, found["task"]["wg_id"])
        ]

    def limit_of(query: dict[str, str]) -> int | None:
        text = query.get("limit", "50")
        return int(text) if text.isdigit() and 1 <= int(text) <= 100 else None

    def people(request: Request, body: Any, org: str) -> tuple[int, Any]:
        rows = open_tasks(request, org)
        if rows is None:
            return 404, {"error": "not_found"}
        query, limit = request.query, limit_of(request.query)
        if set(query) - {"after", "limit"} or limit is None or ("after" in query and not is_uuid7(query["after"])):
            return 400, {"error": "request_invalid"}
        counts: dict[str, Json] = {}
        for task in rows:
            person = counts.setdefault(
                task["owner_id"],
                {"owner_id": task["owner_id"], "agents": 0, "needs_decision": 0, "blocked": 0, "working": 0}
                | {"subagents_active": 0},
            )
            person["agents"] += 1
            attention = hub.attention.get(task["task_id"])
            if attention is not None:
                if attention["category"] in ("needs_decision", "blocked", "working"):
                    person[attention["category"]] += 1
                person["subagents_active"] += attention["subagents_active"]
        ordered = sorted(counts.values(), key=lambda row: (row["owner_id"] != request.user, row["owner_id"]))
        if "after" in query:
            index = next(i for i, row in enumerate(ordered) if row["owner_id"] == query["after"])
            ordered = ordered[index + 1 :]
        page = ordered[:limit]
        following = page[-1]["owner_id"] if len(ordered) > limit else None
        return 200, {"people": page, "next_after": following}

    def main_agents(request: Request, body: Any, org: str) -> tuple[int, Any]:
        rows = open_tasks(request, org)
        if rows is None:
            return 404, {"error": "not_found"}
        query, limit = request.query, limit_of(request.query)
        if set(query) - {"owner", "before", "limit"} or limit is None or not is_uuid7(query.get("owner", "")):
            return 400, {"error": "request_invalid"}
        mine = sorted(
            (task for task in rows if task["owner_id"] == query["owner"]),
            key=lambda task: (task["last_activity_at"], task["task_id"]),
            reverse=True,
        )
        if "before" in query:
            at, _, last = query["before"].partition("|")
            mine = [task for task in mine if (task["last_activity_at"], task["task_id"]) < (at, last)]
        page = mine[:limit]
        following = f"{page[-1]['last_activity_at']}|{page[-1]['task_id']}" if len(mine) > limit else None
        items = [{"task": task, "attention": hub.attention.get(task["task_id"])} for task in page]
        return 200, {"items": items, "next_before": following}

    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/people/agents", people)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/main-agents", main_agents)

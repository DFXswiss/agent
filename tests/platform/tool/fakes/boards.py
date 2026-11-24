"""`POST /v1/events` and the whiteboard routes of the hub fake as the hub runs them (`agent_hub.events.store`,
`agent_hub.whiteboards.projection` and `read`):

- `POST /v1/events` `{events: [{event, notify?}]}` (1-100, all or nothing): header format, author = the caller (`403
  forbidden`), `notify` only on `whiteboard.entry` (`400 request_invalid`), E9 by `id` (identical: `replayed`, else
  `409 event_conflict`), the org exists (`404 not_found`), `check_event_hub` (rights at `lh` and at receipt, E6 with the
  receipt time of the scope's epoch, decertified devices excluded); stored events are projected into the whiteboard
  entries (the first entry of an ID wins; on the task level only for a task of the event's workgroup; edit/delete only
  for an existing, not deleted entry of the same org, level, workgroup and task);
- `GET /v1/whiteboards?org&wg&task&level&before&limit`: the entries the caller may read (org level: member; below:
  workgroup read), newest first, `next_before` paging; a named org, workgroup or task the caller cannot read is `404`;
- `GET /v1/whiteboards/{entry}/history`: the projected events of the entry, ascending.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from agent_runner.crypto.encoding import canonical
from agent_runner.crypto.event import check_header
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import ORG, WG

from ..hubfake import HubFake, Json, Request, now_ms_text
from .membership import check_events, extra

ENTRY, EDIT, DELETE = "whiteboard.entry", "whiteboard.edit", "whiteboard.delete"
LEVELS = ("org", "workgroup", "task")


def level_of(header: Json) -> str:
    if header["scope"] == ORG:
        return "org"
    return "workgroup" if header["task"] == "" else "task"


class Boards:
    def __init__(self, hub: HubFake) -> None:
        self.hub = hub
        self.state = extra(hub)

    def stored(self, event_id: str) -> Json | None:
        for items in self.hub.events.values():
            for item in items:
                if item["event"].get("header", {}).get("id") == event_id:
                    return item
        return None

    def project(self, item: Json, notify: bool) -> None:
        header = item["event"]["header"]
        if header["kind"] not in (ENTRY, EDIT, DELETE):
            return
        ref, level = header["ref"], level_of(header)
        wg, task = header["wg"] or None, header["task"] or None
        row = self.state.entries.get(ref)
        if header["kind"] == ENTRY:
            if row is not None or (
                task is not None and self.hub.tasks.get(task, {}).get("task", {}).get("wg_id") != wg
            ):
                return
            self.state.entries[ref] = {
                "entry_id": ref,
                "org_id": header["org"],
                "level": level,
                "wg_id": wg,
                "task_id": task,
                "author_id": header["author"],
                "created_at": now_ms_text(),
                "updated_at": now_ms_text(),
                "deleted": False,
                "notify": notify,
                "scope_id": header["scope_id"],
                "created": item["scope_seq"],
                "latest": item["scope_seq"],
            }
            return
        place = (header["org"], level, wg, task)
        if row is None or row["deleted"] or (row["org_id"], row["level"], row["wg_id"], row["task_id"]) != place:
            return
        row |= {"latest": item["scope_seq"], "updated_at": now_ms_text(), "deleted": header["kind"] == DELETE}

    def post_events(self, request: Request, body: Json) -> tuple[int, Any]:
        items = body.get("events")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            return 400, {"error": "request_invalid"}
        now = datetime.now(UTC)
        results: list[Json] = []
        staged: list[tuple[int, Json, bool]] = []
        for index, item in enumerate(items):
            event, notify = item["event"], item.get("notify", False)
            header = check_header(event.get("header"))
            if header["author"] != request.user:
                return 403, {"error": "forbidden"}
            if notify and header["kind"] != ENTRY:
                return 400, {"error": "request_invalid"}
            known = self.stored(cast(str, header["id"]))
            if known is not None:
                if canonical(known["event"]) != canonical(event):
                    return 409, {"error": "event_conflict"}
                results.append({"id": header["id"], "scope_id": header["scope_id"], "scope_seq": known["scope_seq"]})
                continue
            org = self.hub.orgs.get(cast(str, header["org"]))
            if org is None:
                return 404, {"error": "not_found"}
            check_events(self.hub, [event], org.log, now, self.state.rotated_at)
            staged.append((index, event, notify))
            results.append({"id": header["id"], "scope_id": header["scope_id"], "scope_seq": None})
        for index, event, notify in staged:
            scope = event["header"]["scope_id"]
            self.hub.store_event(scope, event)
            item = self.hub.events[scope][-1]
            self.project(item, notify)
            results[index]["scope_seq"] = item["scope_seq"]
        return 200, {"results": [{**result, "replayed": False} for result in results]}

    def can_read_wg(self, user: str, wg: str) -> bool:
        org = self.hub.org_of_wg(wg)
        return org is not None and self.hub.orgs[org].log.state.can_read(user, WG, wg)

    def readable(self, user: str, row: Json) -> bool:
        org = self.hub.orgs[row["org_id"]].log.state
        if org.member(user) is None:
            return False
        return row["level"] == "org" or org.can_read(user, WG, row["wg_id"])

    def list_entries(self, request: Request, body: Any) -> tuple[int, Any]:
        query, user = request.query, cast(str, request.user)
        level = query.get("level")
        if level is not None and level not in LEVELS:
            return 400, {"error": "request_invalid"}
        if any(name in query and not is_uuid7(query[name]) for name in ("org", "wg", "task")):
            return 400, {"error": "request_invalid"}
        if "org" in query and (
            query["org"] not in self.hub.orgs or self.hub.orgs[query["org"]].log.state.member(user) is None
        ):
            return 404, {"error": "not_found"}
        if "wg" in query and not self.can_read_wg(user, query["wg"]):
            return 404, {"error": "not_found"}
        if "task" in query:
            task = self.hub.tasks.get(query["task"])
            if task is None or not self.can_read_wg(user, task["task"]["wg_id"]):
                return 404, {"error": "not_found"}
        rows = [
            row
            for row in self.state.entries.values()
            if self.readable(user, row)
            and all(row[f"{name}_id"] == query[name] for name in ("org", "wg", "task") if name in query)
            and (level is None or row["level"] == level)
        ]
        rows.sort(key=lambda row: (row["created_at"], row["entry_id"]), reverse=True)
        if "before" in query:
            at, last = query["before"].split("|")
            rows = [row for row in rows if (row["created_at"], row["entry_id"]) < (at, last)]
        limit = min(int(query.get("limit", "50")), 200)
        page = [
            {
                **{key: row[key] for key in ("entry_id", "org_id", "level", "wg_id", "task_id", "author_id")},
                **{key: row[key] for key in ("created_at", "updated_at", "deleted")},
                "latest_event": {"scope_id": row["scope_id"], "scope_seq": row["latest"]},
            }
            for row in rows[:limit]
        ]
        following = f"{rows[limit - 1]['created_at']}|{rows[limit - 1]['entry_id']}" if len(rows) > limit else None
        return 200, {"entries": page, "next_before": following}

    def history(self, request: Request, body: Any, entry: str) -> tuple[int, Any]:
        row = self.state.entries.get(entry)
        if row is None or not self.readable(cast(str, request.user), row):
            return 404, {"error": "not_found"}
        events = [
            item
            for item in self.hub.events.get(row["scope_id"], [])
            if item["scope_seq"] == row["created"]
            or (
                item["event"]["header"]["kind"] in (EDIT, DELETE)
                and item["event"]["header"]["ref"] == entry
                and (item["event"]["header"]["task"] or None) == row["task_id"]
                and row["created"] < item["scope_seq"] <= row["latest"]
            )
        ]
        return 200, {"events": events}


def install(hub: HubFake) -> None:
    boards = Boards(hub)
    hub.add_route("POST", r"/v1/events", boards.post_events)
    hub.add_route("GET", r"/v1/whiteboards", boards.list_entries)
    hub.add_route("GET", r"/v1/whiteboards/(?P<entry>[0-9a-f-]+)/history", boards.history)

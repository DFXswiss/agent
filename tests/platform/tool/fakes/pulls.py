"""The pull-request index of the hub fake as the hub runs it (KOR-48, ADR-084 section 1, 03-api.md section 4.4,
`contracts/changes/KOR-48.md`):

- `GET /v1/workgroups/{wg}/pr-links?task&cursor&limit` (`wg:read`; `limit` 1-200, default 100, else `400`): the rows
  `{epoch, tag, sealed, task_id, session_id, linked_by, created_at}` newest first, `next_cursor` an opaque position;
- `POST /v1/workgroups/{wg}/pr-links {epoch, tag, sealed, task_id}` (`wg:write`, `403 forbidden` otherwise; the task
  in that workgroup, `404`; the current epoch only, `400 epoch_stale`): `201 {created: true, link}`, a tag linked
  under that epoch already `200 {created: false, link}`;
- `DELETE /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` (`wg:write`): a person's link `{deleted: true}`, a device's
  `409 state_conflict`, none `404`;
- `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` (`wg:read`): the row without `epoch` and `tag`, none `404`.

`device_link` seeds what a runner registers after `github.create_pull_request` (sealed under the workgroup's key with
`origin = device:<session>`, or without `sealed` like an older runner).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto import context as crypto
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from ..hubfake import HubFake, Json, Request, User, now_ms_text

ROW_KEYS = ("epoch", "tag", "sealed", "task_id", "session_id", "linked_by", "created_at")


@dataclass
class Index:
    rows: list[Json] = field(default_factory=list)
    """Every link with its `wg`, oldest first."""


def index(hub: HubFake) -> Index:
    found = getattr(hub, "kor48_pr_index", None)
    if found is None:
        found = Index()
        setattr(hub, "kor48_pr_index", found)  # noqa: B010
    return cast(Index, found)


def device_link(
    hub: HubFake, user: User, task: str, repo: str, number: int, *, sealed: bool = True, session: str | None = None
) -> Json:
    """A link the device of the task registered under the workgroup's current epoch (`user` holds the key)."""
    row = hub.tasks[task]["task"]
    wg, org = row["wg_id"], row["org_id"]
    epoch = hub.orgs[org].log.state.epoch(WG, wg)
    key = hub.org_key(org, user, WG, wg)
    session = session or uuid7()
    tag = crypto.pr_link_tag(crypto.pr_link_key(key), repo, number)
    body = crypto.seal_pr_link(
        crypto.pr_link_body_key(key),
        wg_id=wg,
        epoch=epoch,
        tag=tag,
        task_id=task,
        origin=f"device:{session}",
        repo=repo,
        number=number,
    )
    link = {
        "wg": wg,
        "epoch": epoch,
        "tag": tag,
        "sealed": body if sealed else None,
        "task_id": task,
        "session_id": session,
        "linked_by": None,
        "created_at": now_ms_text(),
    }
    index(hub).rows.append(link)
    return link


def public(row: Json) -> Json:
    return {key: row[key] for key in ROW_KEYS}


def install(hub: HubFake) -> None:
    found = index(hub)

    def place(request: Request, wg: str, write: bool) -> tuple[int, Json] | None:
        org = hub.org_of_wg(wg)
        member = None if org is None else hub._member(request, org)
        if member is None or member.log.state.wg_access(cast(str, request.user), wg) is None:
            return 404, {"error": "not_found"}
        if write and not member.log.state.can_write_wg(cast(str, request.user), wg):
            return 403, {"error": "forbidden"}
        return None

    def find(wg: str, epoch: str, tag: str) -> Json | None:
        return next((r for r in found.rows if (r["wg"], str(r["epoch"]), r["tag"]) == (wg, epoch, tag)), None)

    def listing(request: Request, body: Any, wg: str) -> tuple[int, Any]:
        refused = place(request, wg, False)
        if refused is not None:
            return refused
        limit = int(request.query.get("limit", "100"))
        if not 1 <= limit <= 200 or set(request.query) - {"limit", "task", "cursor"}:
            return 400, {"error": "request_invalid"}
        task = request.query.get("task")
        rows = [r for r in reversed(found.rows) if r["wg"] == wg and (task is None or r["task_id"] == task)]
        start = int(request.query.get("cursor", "0"))
        page = rows[start : start + limit]
        following = str(start + limit) if start + limit < len(rows) else None
        return 200, {"items": [public(r) for r in page], "next_cursor": following}

    def create(request: Request, body: Json, wg: str) -> tuple[int, Any]:
        refused = place(request, wg, True)
        if refused is not None:
            return refused
        if set(body) != {"epoch", "tag", "sealed", "task_id"}:
            return 400, {"error": "request_invalid"}
        task = hub.tasks.get(body["task_id"])
        if task is None or task["task"]["wg_id"] != wg:
            return 404, {"error": "not_found"}
        org = cast(str, hub.org_of_wg(wg))
        if body["epoch"] != hub.orgs[org].log.state.epoch(WG, wg):
            return 400, {"error": "epoch_stale"}
        known = find(wg, str(body["epoch"]), body["tag"])
        if known is not None:
            return 200, {"created": False, "link": public(known)}
        row = {"wg": wg, **body, "session_id": None, "linked_by": request.user, "created_at": now_ms_text()}
        found.rows.append(row)
        return 201, {"created": True, "link": public(row)}

    def delete(request: Request, body: Any, wg: str, epoch: str, tag: str) -> tuple[int, Any]:
        refused = place(request, wg, True)
        if refused is not None:
            return refused
        known = find(wg, epoch, tag)
        if known is None:
            return 404, {"error": "not_found"}
        if known["linked_by"] is None:
            return 409, {"error": "state_conflict"}
        found.rows.remove(known)
        return 200, {"deleted": True}

    def single(request: Request, body: Any, wg: str, epoch: str, tag: str) -> tuple[int, Any]:
        refused = place(request, wg, False)
        if refused is not None:
            return refused
        known = find(wg, epoch, tag)
        if known is None:
            return 404, {"error": "not_found"}
        return 200, {key: known[key] for key in ROW_KEYS if key not in ("epoch", "tag")}

    links = r"/v1/workgroups/(?P<wg>[0-9a-f-]+)/pr-links"
    hub.add_route("GET", links, listing)
    hub.add_route("POST", links, create)
    hub.add_route("DELETE", links + r"/(?P<epoch>[0-9]+)/(?P<tag>[A-Za-z0-9_-]+)", delete)
    hub.add_route("GET", links + r"/(?P<epoch>[0-9]+)/(?P<tag>[A-Za-z0-9_-]+)", single)

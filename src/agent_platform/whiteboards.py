"""Whiteboards (11-agent-access.md section 8.4; 06-web.md section 6.1; 03-api.md sections 4.4 and 4.9; 04-crypto.md
sections 4.3 and 5.12; twin of the web client's `whiteboards/location.ts`, `project.ts` and `actions.ts`).

One board on each of three levels: the org (events in the org scope, `wg` and `task` empty; `--org`), a workgroup
(the workgroup scope, `task` empty; `--org --wg`) and a task (the scope of the task's workgroup with `task` set;
`--org --task`, the workgroup comes from the task). Reading the org board needs org membership, the others read access
to the workgroup; writing needs `org:admin` on the org level and `wg:write` below (an archived workgroup is not
writable), checked before anything is sealed.

`whiteboard list` reads the board's events (`GET /v1/scopes/{scope}/events`) through the reader (`reader.settle`), then
every page of the hub's entry rows (`GET /v1/whiteboards`), and projects them with the hub's rules (`project`): an
entry creates the entry `ref` (the first one in the scope wins), an edit replaces the text and a delete ends the entry,
both only for an existing entry that is not deleted. Unverifiable events on the board are neutral rows with their code,
locked events neutral rows without content; an entry whose ID an earlier entry claimed elsewhere, or that the hub does
not list although it existed when the rows were listed, is a neutral `event_conflict` row; a listed entry without
events is `missing`. Items are newest first. `whiteboard history` adds `GET /v1/whiteboards/{entry}/history`.
`add`, `edit` and `delete` seal the body with the scope's current key, sign it (`lh` = the verified head) and send it
with `POST /v1/events`; an edit or delete names the newest applied event of the entry as `base_event_id`; only a new
entry may carry `notify`.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree, text_argument
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.orgs import now_text
from agent_platform.reader import VERIFIED, Settled, scope_events, settle
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.state import ORG, WG

ENTRY, EDIT, DELETE = "whiteboard.entry", "whiteboard.edit", "whiteboard.delete"
KINDS = (ENTRY, EDIT, DELETE)
TEXT_MAX = 65536
ROWS_PAGE = 200
BODY_KEYS = {ENTRY: {"level", "text"}, EDIT: {"base_event_id", "text"}, DELETE: {"base_event_id"}}


@dataclass(frozen=True)
class Location:
    level: str
    org: str
    wg: str | None = None
    task: str | None = None

    @property
    def scope(self) -> tuple[str, str]:
        return (ORG, self.org) if self.level == "org" else (WG, cast(str, self.wg))


def _id(value: str) -> str:
    if not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def locate(account: Account, org: str, wg: str | None, task: str | None) -> Location:
    """The board of the options; the workgroup of a task board comes from the task row."""
    _id(org)
    if task is not None:
        answer = account.call("GET", f"/v1/tasks/{segment(_id(task))}")
        row = answer.get("task")
        if not isinstance(row, dict) or row.get("task_id") != task or not is_uuid7(row.get("wg_id")):
            raise unverified("response_invalid")
        if wg is not None and wg != row["wg_id"]:
            raise PlatformError("invalid_argument", reason="task_not_in_wg")
        wg = str(row["wg_id"])
    if wg is not None and _id(wg) not in account.org_log(org).state.workgroups:
        raise PlatformError("not_found")
    level = "task" if task is not None else "workgroup" if wg is not None else "org"
    return Location(level, org, wg, task)


def level_of(header: dict[str, Any]) -> str:
    if header.get("scope") == ORG:
        return "org"
    return "workgroup" if header.get("task") == "" else "task"


def on_board(header: dict[str, Any], location: Location) -> bool:
    scope, scope_id = location.scope
    return (
        header.get("org") == location.org
        and header.get("scope") == scope
        and header.get("scope_id") == scope_id
        and level_of(header) == location.level
        and (location.level != "task" or header.get("task") == location.task)
    )


def valid_text(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= TEXT_MAX


def _valid_body(kind: str, body: object, location: Location) -> bool:
    if not isinstance(body, dict) or set(body) != BODY_KEYS[kind]:
        return False
    if kind == ENTRY:
        return body["level"] == location.level and valid_text(body["text"])
    return is_uuid7(body["base_event_id"]) and (kind == DELETE or valid_text(body["text"]))


# --- projection ---


@dataclass
class Draft:
    id: str
    seq: int
    author: str
    created_at: str
    updated_at: str
    text: str
    latest_event_id: str
    edited: bool = False
    deleted: bool = False
    versions: list[dict[str, Any]] = field(default_factory=list)

    def item(self, names: dict[str, str]) -> dict[str, Any]:
        return {
            "type": "entry",
            "entry_id": self.id,
            "seq": self.seq,
            "author": self.author,
            "author_name": names.get(self.author),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "text": self.text,
            "edited": self.edited,
            "deleted": self.deleted,
            "latest_event_id": self.latest_event_id,
            "versions": len(self.versions),
        }


def _apply(drafts: dict[str, Draft], event: Settled) -> None:
    header = event.header
    kind, ref, body = str(header["kind"]), str(header["ref"]), cast(dict[str, Any], event.body)
    version = {
        "event_id": header["id"],
        "seq": event.seq,
        "kind": kind,
        "author": header["author"],
        "ts": header["ts"],
        "text": None if kind == DELETE else body["text"],
    }
    draft = drafts.get(ref)
    if kind == ENTRY:
        if draft is None:
            drafts[ref] = Draft(
                ref,
                event.seq,
                str(header["author"]),
                str(header["ts"]),
                str(header["ts"]),
                body["text"],
                str(header["id"]),
                versions=[version],
            )
        return
    if draft is None or draft.deleted:
        return
    draft.updated_at, draft.latest_event_id = str(header["ts"]), str(header["id"])
    draft.versions.append(version)
    if kind == DELETE:
        draft.deleted = True
    else:
        draft.text, draft.edited = body["text"], True


@dataclass
class Board:
    items: list[dict[str, Any]]
    entries: dict[str, Draft]


def project(events: list[Settled], location: Location, rows: list[dict[str, Any]], checked: int) -> Board:
    """The board from settled events and the hub's rows (`whiteboards/project.ts` `projectBoard`)."""
    drafts: dict[str, Draft] = {}
    neutral: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for event in sorted(events, key=lambda item: item.seq):
        header = event.header or event.raw
        if header.get("kind") not in KINDS:
            continue
        ref = header.get("ref") if isinstance(header.get("ref"), str) and header.get("ref") else None
        here = on_board(header, location)
        if event.status != VERIFIED:
            if here:
                neutral.append({"type": event.status, "seq": event.seq, "code": event.code, "entry_id": ref})
            continue
        kind = header["kind"]
        elsewhere = kind == ENTRY and ref in claimed and ref not in drafts
        if kind == ENTRY:
            claimed.add(cast(str, ref))
        if not here:
            continue
        if elsewhere:
            neutral.append({"type": "unverifiable", "seq": event.seq, "code": "event_conflict", "entry_id": ref})
        elif not _valid_body(kind, event.body, location):
            neutral.append({"type": "unverifiable", "seq": event.seq, "code": "header_invalid", "entry_id": ref})
        else:
            _apply(drafts, event)
    listed = {str(row["entry_id"]) for row in rows}
    entries: dict[str, Draft] = {}
    for draft in drafts.values():
        if draft.seq <= checked and draft.id not in listed:
            neutral.append({"type": "unverifiable", "seq": draft.seq, "code": "event_conflict", "entry_id": draft.id})
        else:
            entries[draft.id] = draft
    shown = {*entries, *(item["entry_id"] for item in neutral if item["entry_id"] is not None)}
    missing = [
        {"type": "missing", "seq": row["latest_event"]["scope_seq"], "entry_id": str(row["entry_id"])}
        for row in rows
        if str(row["entry_id"]) not in shown
    ]
    items: list[dict[str, Any]] = [*({"key": f"e{d.id}", "draft": d} for d in entries.values())]
    items += [{"key": f"n{item['seq']}", "item": item} for item in neutral]
    items += [{"key": f"p{item['entry_id']}", "item": item} for item in missing]
    items.sort(key=lambda found: found["key"])
    items.sort(key=lambda found: -(found["draft"].seq if "draft" in found else found["item"]["seq"]))
    return Board(items, entries)


# --- reading ---


def _rows(account: Account, location: Location) -> list[dict[str, Any]]:
    params = {"org": location.org, "level": location.level}
    params |= {"wg": location.wg} if location.wg is not None else {}
    params |= {"task": location.task} if location.task is not None else {}
    rows: list[dict[str, Any]] = []
    before: str | None = None
    while True:
        query = {**params, "limit": str(ROWS_PAGE), **({"before": before} if before is not None else {})}
        page = account.call("GET", f"/v1/whiteboards?{urlencode(query)}")
        found = page.get("entries")
        valid = isinstance(found, list) and all(
            isinstance(row, dict)
            and isinstance(row.get("entry_id"), str)
            and isinstance(row.get("latest_event"), dict)
            and isinstance(row["latest_event"].get("scope_seq"), int)
            for row in found
        )
        if not valid:
            raise unverified("response_invalid")
        rows += [
            row for row in cast(list[dict[str, Any]], found) if all(r["entry_id"] != row["entry_id"] for r in rows)
        ]
        following = page.get("next_before")
        if not isinstance(following, str):
            return rows
        before = following


def read_board(account: Account, location: Location, extra: Sequence[Any] = ()) -> Board:
    """The board's events (and `extra` stored items, e.g. a history), then the rows, projected."""
    query = {"kinds": ",".join(KINDS), **({"task": location.task} if location.task is not None else {})}
    items = scope_events(account, location.scope[1], query)
    checked = max([0, *(item.get("scope_seq", 0) for item in items if isinstance(item, dict))])
    rows = _rows(account, location)
    known = {item.get("scope_seq") for item in items if isinstance(item, dict)}
    items += [item for item in extra if not isinstance(item, dict) or item.get("scope_seq") not in known]
    ring = account.keys(location.org)
    return project([settle(account, location.org, item, ring) for item in items], location, rows, checked)


def board_list(account: Account, location: Location) -> dict[str, Any]:
    board = read_board(account, location)
    names = account.names(location.org).members
    items = [found["draft"].item(names) if "draft" in found else found["item"] for found in board.items]
    return {**_where(location), "items": items}


def _where(location: Location) -> dict[str, Any]:
    return {"level": location.level, "org_id": location.org, "wg_id": location.wg, "task_id": location.task}


def board_history(account: Account, location: Location, entry: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/whiteboards/{segment(_id(entry))}/history")
    events = answer.get("events")
    if not isinstance(events, list):
        raise unverified("response_invalid")
    board = read_board(account, location, events)
    names = account.names(location.org).members
    draft = board.entries.get(entry)
    if draft is None:
        neutral = [found["item"] for found in board.items if "item" in found and found["item"]["entry_id"] == entry]
        if not neutral:
            raise PlatformError("not_found")
        return {**_where(location), "entry_id": entry, "entry": None, "items": neutral}
    versions = [{**version, "author_name": names.get(version["author"])} for version in draft.versions]
    return {**_where(location), "entry_id": entry, "entry": draft.item(names), "versions": versions}


# --- writing ---


def require_writer(account: Account, location: Location) -> None:
    """The board's write right (`org:admin` on the org level, `wg:write` below), before anything is sealed."""
    state = account.org_log(location.org).state
    if location.level == "org" and not state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    if location.level != "org" and not state.can_write_wg(account.user_id, cast(str, location.wg)):
        raise PlatformError("no_write_access")


def _seal(account: Account, location: Location, kind: str, ref: str, body: dict[str, object]) -> dict[str, object]:
    require_writer(account, location)
    log = account.org_log(location.org)
    state = log.state
    scope, scope_id = location.scope
    epoch = state.epoch(scope, scope_id)
    key = account.keys(location.org).get((scope, scope_id, epoch))
    if key is None:
        raise PlatformError("hub_refused", reason="epoch_stale")
    header = new_header(
        id=uuid7(),
        org=location.org,
        scope=scope,
        scope_id=scope_id,
        epoch=epoch,
        author=account.user_id,
        kind=kind,
        ts=now_text(),
        lh=log.head.seq,
        wg="" if location.wg is None or location.level == "org" else location.wg,
        task=location.task or "",
        ref=ref,
    )
    return seal_event(key, header, body_json(body), account.identity.sig)


def _send(account: Account, location: Location, event: dict[str, object], notify: bool) -> dict[str, Any]:
    item = {"event": event, "notify": True} if notify else {"event": event}
    answer = account.call("POST", "/v1/events", {"events": [item]})
    results = answer.get("results")
    result = results[0] if isinstance(results, list) and results else None
    if not isinstance(result, dict) or not isinstance(result.get("scope_seq"), int):
        raise unverified("response_invalid")
    header = cast(dict[str, Any], event["header"])
    return {**_where(location), "entry_id": header["ref"], "event_id": header["id"], "scope_seq": result["scope_seq"]}


def _text(text: str) -> str:
    if not valid_text(text):
        raise PlatformError("invalid_argument")
    return text


def board_add(account: Account, location: Location, text: str, notify: bool) -> dict[str, Any]:
    body: dict[str, object] = {"level": location.level, "text": _text(text)}
    return _send(account, location, _seal(account, location, ENTRY, uuid7(), body), notify)


def _entry(account: Account, location: Location, entry: str) -> Draft:
    require_writer(account, location)
    found = read_board(account, location).entries.get(_id(entry))
    if found is None or found.deleted:
        raise PlatformError("not_found")
    return found


def board_edit(account: Account, location: Location, entry: str, text: str) -> dict[str, Any]:
    _text(text)
    draft = _entry(account, location, entry)
    body: dict[str, object] = {"base_event_id": draft.latest_event_id, "text": text}
    return _send(account, location, _seal(account, location, EDIT, entry, body), False)


def board_delete(account: Account, location: Location, entry: str) -> dict[str, Any]:
    draft = _entry(account, location, entry)
    body: dict[str, object] = {"base_event_id": draft.latest_event_id}
    return _send(account, location, _seal(account, location, DELETE, entry, body), False)


# --- command line ---


def _location(account: Account, args: argparse.Namespace) -> Location:
    return locate(account, args.org, args.wg, args.task)


def _list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return board_list(account, _location(account, args))


def _history(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return board_history(account, _location(account, args), args.entry)


def _add(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return board_add(account, _location(account, args), text_argument(args, context), args.notify)


def _edit(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return board_edit(account, _location(account, args), args.entry, text_argument(args, context))


def _delete(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return board_delete(account, _location(account, args), args.entry)


def register(tree: Tree) -> None:
    group = tree.group("whiteboard", "whiteboards of an organization, workgroup or task")
    for name, work, text in (
        ("list", _list, "the entries of a board (verified, newest first)"),
        ("history", _history, "every version of an entry"),
        ("add", _add, "a new entry"),
        ("edit", _edit, "replace the text of an entry"),
        ("delete", _delete, "delete an entry"),
    ):
        sub = tree.command(group, name, work, text)
        sub.add_argument("--org", required=True)
        sub.add_argument("--wg", help="the workgroup board")
        sub.add_argument("--task", help="the task board")
        if name in ("history", "edit", "delete"):
            sub.add_argument("--entry", required=True)
        if name in ("add", "edit"):
            sub.add_argument("--text", required=True, help="Markdown, or - to read it from stdin")
        if name == "add":
            sub.add_argument("--notify", action="store_true", help="notify the board's readers")

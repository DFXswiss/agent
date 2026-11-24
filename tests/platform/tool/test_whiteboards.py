"""Whiteboards (stage a, 11-agent-access.md section 8.4; 06-web.md section 6.1): `whiteboard list|history|add|edit|
delete` on the org, workgroup and task boards, against the hub fake that checks events like the hub and projects the
entries. Texts exist only inside sealed events; what does not verify is shown neutrally, never with content."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import whiteboards
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import ORG, WG

from .conftest import Tool
from .fakes.membership import extra
from .hubfake import HubFake, User, now_ms_text
from .teams import join, team, wg_create


def board_event(  # noqa: PLR0917
    hub: HubFake,
    user: User,
    org: str,
    kind: str,
    ref: str,
    body: object,
    *,
    wg: str = "",
    task: str = "",
    lh: int | None = None,
    key: bytes | None = None,
) -> dict[str, Any]:
    """A whiteboard event sealed by `user` with the scope's current key (made by hand, outside the tool)."""
    scope, scope_id = (WG, wg) if wg else (ORG, org)
    log = hub.orgs[org].log
    header = new_header(
        id=uuid7(),
        org=org,
        scope=scope,
        scope_id=scope_id,
        epoch=log.state.epoch(scope, scope_id),
        author=user.id,
        kind=kind,
        ts=now_ms_text(),
        lh=log.head.seq if lh is None else lh,
        wg=wg,
        task=task,
        ref=ref,
    )
    key = hub.org_key(org, user, scope, scope_id) if key is None else key
    return seal_event(key, header, body_json(body), user.identity.sig)


def post(tool: Tool, event: dict[str, Any]) -> dict[str, Any]:
    return tool.run("api", "POST", "/v1/events", "--body", "-", stdin=json.dumps({"events": [{"event": event}]}))[1]


def texts(document: dict[str, Any]) -> list[str | None]:
    return [item.get("text") for item in document["items"]]


def test_round_trip_on_the_org_board(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    bob_tool, bob = join(hub, tool, org, tmp_path, "Bob")
    code, added = tool.run("whiteboard", "add", "--org", org, "--text", "Hello **team**", "--notify")
    assert code == 0, added
    entry = added["entry_id"]
    assert (added["level"], added["org_id"], added["wg_id"], added["task_id"]) == ("org", org, None, None)
    assert extra(hub).entries[entry]["notify"] is True
    listed = bob_tool.run("whiteboard", "list", "--org", org)[1]
    item = listed["items"][0]
    assert (item["type"], item["entry_id"], item["text"], item["author"], item["author_name"]) == (
        "entry",
        entry,
        "Hello **team**",
        account.id,
        "Ada",
    )
    assert (item["edited"], item["deleted"], item["versions"]) == (False, False, 1)
    edited = tool.run("whiteboard", "edit", "--org", org, "--entry", entry, "--text", "-", stdin="Hello again")[1]
    assert edited["entry_id"] == entry
    history = bob_tool.run("whiteboard", "history", "--org", org, "--entry", entry)[1]
    assert [(v["kind"], v["text"], v["author_name"]) for v in history["versions"]] == [
        ("whiteboard.entry", "Hello **team**", "Ada"),
        ("whiteboard.edit", "Hello again", "Ada"),
    ]
    assert history["entry"]["latest_event_id"] == edited["event_id"]
    assert tool.run("whiteboard", "delete", "--org", org, "--entry", entry)[1]["entry_id"] == entry
    item = bob_tool.run("whiteboard", "list", "--org", org)[1]["items"][0]
    assert (item["deleted"], item["text"], item["versions"]) == (True, "Hello again", 3)
    for argv in (("edit", "--text", "x"), ("delete",)):
        assert tool.run("whiteboard", argv[0], "--org", org, "--entry", entry, *argv[1:])[1]["error"] == "not_found"
    assert tool.run("whiteboard", "delete", "--org", org, "--entry", uuid7())[1]["error"] == "not_found"
    # the hub received ciphertext only
    sent = b"".join(hub.bodies)
    assert b"Hello" not in sent
    # without the right: the tool refuses before sealing, the hub refuses a hand-made event
    assert bob_tool.run("whiteboard", "add", "--org", org, "--text", "x")[1]["error"] == "not_admin"
    refused = post(bob_tool, board_event(hub, bob, org, "whiteboard.entry", uuid7(), {"level": "org", "text": "x"}))
    assert refused["hub_error"] == "author_not_writer"


def test_workgroup_and_task_boards(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    bob_tool, _ = join(hub, tool, org, tmp_path, "Bob", "--wg", f"{wg}:write")
    carol_tool, _ = join(hub, tool, org, tmp_path, "Carol", "--wg", f"{wg}:read")
    assert bob_tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "from Bob")[1]["level"] == "workgroup"
    task = tool.run("task", "create", "--wg", wg, "--text", "A task")[1]["task_id"]
    on_task = tool.run("whiteboard", "add", "--org", org, "--task", task, "--text", "on the task")[1]
    assert (on_task["level"], on_task["wg_id"], on_task["task_id"]) == ("task", wg, task)
    board = carol_tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]
    assert texts(board) == ["from Bob"]
    assert board["items"][0]["author_name"] == "Bob"
    assert texts(carol_tool.run("whiteboard", "list", "--org", org, "--wg", wg, "--task", task)[1]) == ["on the task"]
    assert carol_tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "x")[1]["error"] == "no_write_access"
    # location errors
    other = wg_create(tool, org, "Other")
    assert tool.run("whiteboard", "list", "--org", org, "--wg", other, "--task", task)[1]["reason"] == "task_not_in_wg"
    assert tool.run("whiteboard", "list", "--org", org, "--wg", uuid7())[1]["error"] == "not_found"
    for argv in (("--org", "x"), ("--org", org, "--wg", "x"), ("--org", org, "--task", "x")):
        assert tool.run("whiteboard", "list", *argv)[1]["error"] == "invalid_argument", argv
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda b, r: (200, {"task": {"task_id": task}})
    assert tool.run("whiteboard", "list", "--org", org, "--task", task)[1]["reason"] == "response_invalid"
    for text in ("", "x" * (whiteboards.TEXT_MAX + 1)):
        assert tool.run("whiteboard", "add", "--org", org, "--text", text)[1]["error"] == "invalid_argument"


def test_what_does_not_verify_is_shown_neutrally(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    task = tool.run("task", "create", "--wg", wg, "--text", "A task")[1]["task_id"]
    first = tool.run("whiteboard", "add", "--org", org, "--text", "genuine")[1]
    # a forged copy (another position, tampered ciphertext)
    stored = hub.events[org]
    forged: Any = json.loads(json.dumps(stored[-1]))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    stored.append({**forged, "scope_seq": len(stored) + 1})
    # an event that names a log head beyond the verified one (the reader reads the log again, then refuses it)
    beyond = board_event(hub, account, org, "whiteboard.entry", uuid7(), {"level": "org", "text": "x"}, lh=999)
    hub.store_event(org, beyond)
    # a body that does not fit the board, and a row the hub lists without any event
    post(tool, board_event(hub, account, org, "whiteboard.entry", uuid7(), {"level": "task", "text": "x"}))
    ghost = uuid7()
    extra(hub).entries[ghost] = {
        **extra(hub).entries[first["entry_id"]],
        "entry_id": ghost,
        "latest": 99,
        "created_at": "2000-01-01T00:00:00.000Z",
    }
    items = tool.run("whiteboard", "list", "--org", org)[1]["items"]
    assert [(item["type"], item.get("code"), item.get("text")) for item in items] == [
        ("missing", None, None),
        ("unverifiable", "header_invalid", None),
        ("unverifiable", "log_fork", None),
        ("unverifiable", "signature_invalid", None),
        ("entry", None, "genuine"),
    ]
    # an entry ID claimed first on the task board and then reused on the workgroup board
    on_task = tool.run("whiteboard", "add", "--org", org, "--task", task, "--text", "first")[1]
    reused = {"level": "workgroup", "text": "second"}
    assert post(tool, board_event(hub, account, org, "whiteboard.entry", on_task["entry_id"], reused, wg=wg))["ok"]
    wg_items = tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]["items"]
    assert [(item["type"], item["code"]) for item in wg_items] == [("unverifiable", "event_conflict")]
    # and an entry ID of the workgroup board reused on the task board: the hub did not project it
    on_wg = tool.run("whiteboard", "add", "--org", org, "--wg", wg, "--text", "board")[1]
    reused = {"level": "task", "text": "copy"}
    post(tool, board_event(hub, account, org, "whiteboard.entry", on_wg["entry_id"], reused, wg=wg, task=task))
    task_items = tool.run("whiteboard", "list", "--org", org, "--task", task)[1]["items"]
    assert [(item["type"], item.get("code"), item.get("text")) for item in task_items] == [
        ("unverifiable", "event_conflict", None),
        ("entry", None, "first"),
    ]
    history = tool.run("whiteboard", "history", "--org", org, "--task", task, "--entry", on_wg["entry_id"])[1]
    assert (history["entry"], [item["code"] for item in history["items"]]) == (None, ["event_conflict"])
    assert tool.run("whiteboard", "history", "--org", org, "--wg", wg, "--entry", first["entry_id"])[1]["error"] == (
        "not_found"
    )
    # without the keys the events are locked
    hub.wraps[:] = [w for w in hub.wraps if not (w["scope"] == ORG and w["recipient_id"] == account.id)]
    locked = tool.run("whiteboard", "list", "--org", org)[1]["items"]
    assert {item["type"] for item in locked} >= {"locked"}
    assert all("text" not in item for item in locked if item["type"] != "missing")
    assert tool.run("whiteboard", "add", "--org", org, "--text", "x")[1]["reason"] == "epoch_stale"


def test_paging_and_malformed_answers(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whiteboards, "ROWS_PAGE", 1)
    org = team(tool)
    for text in ("one", "two", "three"):
        tool.run("whiteboard", "add", "--org", org, "--text", text)
    assert texts(tool.run("whiteboard", "list", "--org", org)[1]) == ["three", "two", "one"]
    original = hub.r_events
    hub.override("r_events", lambda r, b, scope: (200, {**original(r, b, scope)[1], "events": ["junk"]}))
    assert tool.run("whiteboard", "list", "--org", org)[1]["items"][0]["type"] == "missing"
    hub.restore("r_events")
    hub.hooks[("GET", "/v1/whiteboards")] = lambda b, r: (200, {"entries": [{"entry_id": 5}]})
    assert tool.run("whiteboard", "list", "--org", org)[1]["reason"] == "response_invalid"
    del hub.hooks[("GET", "/v1/whiteboards")]
    entry = tool.run("whiteboard", "list", "--org", org)[1]["items"][0]["entry_id"]
    hub.hooks[("GET", f"/v1/whiteboards/{entry}/history")] = lambda b, r: (200, {"events": None})
    assert tool.run("whiteboard", "history", "--org", org, "--entry", entry)[1]["reason"] == "response_invalid"
    hub.hooks[("POST", "/v1/events")] = lambda b, r: (200, {"results": [{"scope_seq": "1"}]})
    assert tool.run("whiteboard", "add", "--org", org, "--text", "x")[1]["reason"] == "response_invalid"


def test_reader_and_projection_edges(tool: Tool, hub: HubFake, account: User) -> None:
    org = team(tool)
    wg = wg_create(tool, org)
    task = tool.run("task", "create", "--wg", wg, "--text", "A task")[1]["task_id"]
    first = tool.run("whiteboard", "add", "--org", org, "--text", "first")[1]["entry_id"]
    for kind, ref, body, key in (
        ("whiteboard.entry", first, {"level": "org", "text": "again"}, None),  # the first entry of an ID wins
        ("whiteboard.edit", uuid7(), {"base_event_id": uuid7(), "text": "x"}, None),  # an unknown entry
        ("whiteboard.edit", first, {"text": "x"}, None),  # keys of the wrong kind
        ("whiteboard.entry", uuid7(), ["x"], None),  # no object
        ("whiteboard.entry", uuid7(), {"level": "org", "text": "x"}, b"z" * 32),  # not the scope's key
    ):
        assert post(tool, board_event(hub, account, org, kind, ref, body, key=key))["ok"] is True
    items = tool.run("whiteboard", "list", "--org", org)[1]["items"]
    assert [item["type"] for item in items] == ["unverifiable", "unverifiable", "unverifiable", "entry"]
    assert [item["code"] for item in items[1:3]] == ["header_invalid", "header_invalid"]
    assert items[0]["code"] not in (None, "header_invalid")
    assert (items[3]["text"], items[3]["versions"]) == ("first", 1)
    # an unverifiable event of the task board is not shown on the workgroup board
    tool.run("whiteboard", "add", "--org", org, "--task", task, "--text", "on the task")
    forged: Any = json.loads(json.dumps(hub.events[wg][-1]))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    hub.events[wg].append({**forged, "scope_seq": len(hub.events[wg]) + 1})
    assert tool.run("whiteboard", "list", "--org", org, "--wg", wg)[1]["items"] == []
    # event pages are followed; a malformed page is refused
    original = hub.r_events

    def paged(request: Any, body: Any, scope: str) -> tuple[int, Any]:
        status, answer = original(request, body, scope)
        page = answer["events"][:1]
        more = bool(answer["events"][1:])
        after = int(request.query.get("after", "0"))
        return status, {**answer, "events": page, "next_after": page[0]["scope_seq"] if more else after}

    hub.override("r_events", paged)
    assert len(tool.run("whiteboard", "list", "--org", org)[1]["items"]) == 4
    hub.override("r_events", lambda r, b, scope: (200, {"events": None}))
    assert tool.run("whiteboard", "list", "--org", org)[1]["reason"] == "response_invalid"

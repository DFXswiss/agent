"""U-HUB-whiteboards (reads): `GET /v1/whiteboards` lists the readable entries newest first with filters and cursor,
`GET /v1/whiteboards/{entry}/history` the projected events ascending; both refuse foreign objects with `404` and are
read-logged (03-api.md section 4.9; every response is validated against its contract by the harness)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.whiteboards import read
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.whiteboards.support import Board, id_of, ref_of


def _list(actor: Actor, **query: str) -> Any:
    response = actor.browser.get("/v1/whiteboards", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(actor: Actor, **query: str) -> list[str]:
    return [item["entry_id"] for item in _list(actor, **query)["entries"]]


def _status(actor: Actor, url: str, **query: str) -> tuple[int, Any]:
    response = actor.browser.get(url, params=query)
    return response.status_code, response.json()


def _setup(board: Board) -> dict[str, str]:
    """An org entry, a workgroup entry, a task entry and an entry of the other workgroup, one second apart."""
    scene, clock = board.scene, board.scene.world.hub.clock
    task, _session = scene.start_session()
    refs = {}
    for name, author, event in (
        ("org", scene.admin, board.entry(scene.admin, level="org")),
        ("wg", scene.writer, board.entry(scene.writer)),
        ("task", scene.writer, board.entry(scene.writer, level="task", task=task)),
        ("other", scene.writer, board.entry(scene.writer, wg=board.other_wg)),
    ):
        clock.advance(1)
        board.posted(author, event)
        refs[name] = ref_of(event)
    refs["task_id"] = task
    return refs


def test_each_user_lists_the_entries_it_may_read(board: Board) -> None:
    scene = board.scene
    refs = _setup(board)
    assert _ids(scene.writer) == [refs["other"], refs["task"], refs["wg"], refs["org"]]
    assert _ids(scene.reader) == [refs["task"], refs["wg"], refs["org"]]
    assert _ids(board.outsider) == [refs["org"]]
    assert _ids(scene.stranger) == []
    item = _list(scene.reader, level="task")["entries"][0]
    assert item == {
        "entry_id": refs["task"],
        "org_id": scene.org,
        "level": "task",
        "wg_id": scene.wg,
        "task_id": refs["task_id"],
        "author_id": scene.writer.id,
        "created_at": item["created_at"],
        "updated_at": item["created_at"],
        "deleted": False,
        "latest_event": {"scope_id": scene.wg, "scope_seq": 2},
    }


def test_filters_narrow_the_list(board: Board) -> None:
    scene = board.scene
    refs = _setup(board)
    assert _ids(scene.writer, org=scene.org, level="org") == [refs["org"]]
    assert _ids(scene.writer, wg=scene.wg) == [refs["task"], refs["wg"]]
    assert _ids(scene.writer, wg=scene.wg, level="workgroup") == [refs["wg"]]
    assert _ids(scene.writer, task=refs["task_id"]) == [refs["task"]]
    assert _ids(scene.writer, wg=board.other_wg) == [refs["other"]]


def test_named_objects_the_caller_may_not_read_are_not_found(board: Board) -> None:
    scene = board.scene
    refs = _setup(board)
    missing = (404, {"error": "not_found"})
    assert _status(scene.stranger, "/v1/whiteboards", org=scene.org) == missing
    assert _status(scene.reader, "/v1/whiteboards", wg=board.other_wg) == missing
    assert _status(board.outsider, "/v1/whiteboards", task=refs["task_id"]) == missing
    assert _status(scene.writer, "/v1/whiteboards", task=new_id()) == missing


def test_malformed_queries_are_refused(board: Board) -> None:
    for query in ({"org": "nope"}, {"wg": new_id().upper()}, {"level": "project"}, {"before": "abc"}, {"limit": "0"}):
        response = board.scene.writer.browser.get("/v1/whiteboards", params=query, extensions={"contract": False})
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid"), query


def test_pages_follow_the_cursor(board: Board) -> None:
    scene, clock = board.scene, board.scene.world.hub.clock
    refs = []
    for _ in range(3):
        clock.advance(1)
        refs.append(ref_of(board.posted(scene.writer, board.entry(scene.writer))[0]))
    first = _list(scene.writer, limit="2")
    assert [item["entry_id"] for item in first["entries"]] == [refs[2], refs[1]]
    second = _list(scene.writer, limit="2", before=first["next_before"])
    assert [item["entry_id"] for item in second["entries"]] == [refs[0]]
    assert second["next_before"] is None


def test_history_lists_the_projected_events_ascending(board: Board) -> None:
    scene = board.scene
    entry = board.posted(scene.writer, board.entry(scene.writer))[0]
    ref = ref_of(entry)
    edit = board.event(scene.writer, "whiteboard.edit", ref)
    foreign = board.event(scene.writer, "whiteboard.edit", ref, wg=board.other_wg)
    board.posted(scene.writer, edit, foreign, board.event(scene.writer, "whiteboard.entry", ref))
    delete = board.posted(scene.writer, board.event(scene.writer, "whiteboard.delete", ref))[0]
    board.posted(scene.writer, board.event(scene.writer, "whiteboard.edit", ref))  # after the delete
    response = scene.reader.browser.get(f"/v1/whiteboards/{ref}/history")
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert [item["event"]["header"]["id"] for item in events] == [id_of(entry), id_of(edit), id_of(delete)]
    assert [item["scope_seq"] for item in events] == sorted(item["scope_seq"] for item in events)
    listed = _list(scene.reader)["entries"][0]
    assert (listed["deleted"], listed["latest_event"]["scope_seq"]) == (True, events[-1]["scope_seq"])


def _history(board: Board) -> tuple[str, list[int]]:
    """An entry with four edits; its ref and the `scope_seq` of its five events, ascending."""
    scene = board.scene
    entry = board.posted(scene.writer, board.entry(scene.writer))[0]
    ref = ref_of(entry)
    for _ in range(4):
        board.posted(scene.writer, board.event(scene.writer, "whiteboard.edit", ref))
    events = scene.reader.browser.get(f"/v1/whiteboards/{ref}/history").json()["events"]
    return ref, [item["scope_seq"] for item in events]


def test_history_pages_newest_first(board: Board) -> None:
    """ADR-086 section 2: `before`/`limit` read one page newest first with `next_before` (KOR-35)."""
    ref, seqs = _history(board)
    assert len(seqs) == 5
    url = f"/v1/whiteboards/{ref}/history"
    first = _status(board.scene.reader, url, limit="2")[1]
    assert ([item["scope_seq"] for item in first["events"]], first["next_before"]) == (seqs[:2:-1], seqs[3])
    second = _status(board.scene.reader, url, limit="2", before=str(first["next_before"]))[1]
    assert ([item["scope_seq"] for item in second["events"]], second["next_before"]) == ([seqs[2], seqs[1]], seqs[1])
    last = _status(board.scene.reader, url, before=str(second["next_before"]))[1]
    assert last == {"events": [_status(board.scene.reader, url)[1]["events"][0]], "next_before": None}
    assert _status(board.scene.reader, url, before=str(seqs[0])) == (200, {"events": [], "next_before": None})
    assert "next_before" not in _status(board.scene.reader, url)[1]


def test_history_is_capped(board: Board, monkeypatch: pytest.MonkeyPatch) -> None:
    ref, seqs = _history(board)
    monkeypatch.setattr(read, "HISTORY_MAX", 3)
    body = _status(board.scene.reader, f"/v1/whiteboards/{ref}/history")[1]
    assert ([item["scope_seq"] for item in body["events"]], body["truncated"]) == (seqs[:3], True)
    monkeypatch.setattr(read, "HISTORY_MAX", 5)
    assert "truncated" not in _status(board.scene.reader, f"/v1/whiteboards/{ref}/history")[1]


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "201"}, {"before": "-1"}, {"before": "01"}])
def test_history_refuses_bad_paging(board: Board, query: dict[str, str]) -> None:
    ref, _seqs = _history(board)
    response = board.scene.reader.browser.get(
        f"/v1/whiteboards/{ref}/history", params=query, extensions={"contract": False}
    )
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_history_needs_the_right_of_the_level(board: Board) -> None:
    scene = board.scene
    refs = _setup(board)
    missing = (404, {"error": "not_found"})
    assert _status(board.outsider, f"/v1/whiteboards/{refs['wg']}/history") == missing
    assert _status(scene.stranger, f"/v1/whiteboards/{refs['org']}/history") == missing
    assert _status(scene.writer, f"/v1/whiteboards/{new_id()}/history") == missing
    assert _status(scene.writer, "/v1/whiteboards/not-an-id/history") == missing
    assert _status(board.outsider, f"/v1/whiteboards/{refs['org']}/history")[0] == 200


def test_reads_are_read_logged(board: Board, hub: HubHarness) -> None:
    scene = board.scene
    refs = _setup(board)
    _list(scene.reader)
    assert _status(scene.reader, f"/v1/whiteboards/{refs['wg']}/history")[0] == 200
    hub.flush_read_log()
    actions = [
        row[0] for row in board.rows("SELECT action FROM hub.audit WHERE actor_id = %s ORDER BY seq", scene.reader.id)
    ]
    assert actions[-2:] == ["read.whiteboards", "read.whiteboard.history"]

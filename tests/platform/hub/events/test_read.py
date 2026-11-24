"""U-HUB-events (read): `GET /v1/scopes/{scope_id}/events` - filters, `session.terminal` only on request, pagination,
head, rights (a reader of a foreign workgroup gets `404`), parameter checks and the read log (03-api.md sections 1 and
4.4)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.crypto.encoding import format_time
from agent_hub.events import read
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id

Json = dict[str, Any]


def _seqs(response: Any) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["scope_seq"] for item in response.json()["events"]]


def _fill(scene: Scene) -> dict[str, Any]:
    """seq 1 settings, 2 prompt (task A), 3 output (session of A), 4 terminal (same session), 5 whiteboard (ref),
    6 prompt (task B)."""
    task, session = scene.start_session()
    entry = new_id()
    settings = scene.wg_event(scene.writer)
    prompt = scene.prompt(scene.writer, task)
    assert scene.post(scene.writer, settings, prompt).status_code == 200
    scene.append(scene.device, scene.session_event(scene.device, session))
    scene.append(scene.device, scene.session_event(scene.device, session, "session.terminal"))
    board = scene.wg_event(scene.writer, "whiteboard.entry", ref=entry)
    other_task = new_id()
    assert scene.post(scene.writer, board, scene.prompt(scene.writer, other_task)).status_code == 200
    return {"task": task, "session": session, "entry": entry, "settings": settings, "other_task": other_task}


def test_read_returns_stored_events_without_terminal(scene: Scene) -> None:
    filled = _fill(scene)
    response = scene.read(scene.reader)
    assert _seqs(response) == [1, 2, 3, 5, 6]
    body = response.json()
    assert (body["next_after"], body["head"]) == (None, 6)
    first = body["events"][0]
    assert first == {
        "scope_seq": 1,
        "received_at": format_time(scene.world.hub.clock()),
        "event": filled["settings"],
    }


def test_read_filters(scene: Scene) -> None:
    filled = _fill(scene)
    assert _seqs(scene.read(scene.reader, kinds="prompt")) == [2, 6]
    assert _seqs(scene.read(scene.reader, kinds="session.terminal,session.output")) == [3, 4]
    assert _seqs(scene.read(scene.reader, task=filled["task"])) == [2, 3]
    assert _seqs(scene.read(scene.reader, task=filled["task"], kinds="session.terminal")) == [4]
    assert _seqs(scene.read(scene.reader, session=filled["session"])) == [3]
    assert _seqs(scene.read(scene.reader, ref=filled["entry"])) == [5]
    assert _seqs(scene.read(scene.reader, ref=new_id())) == []


def test_read_paginates(scene: Scene) -> None:
    _fill(scene)
    page = scene.read(scene.reader, limit="2")
    assert _seqs(page) == [1, 2]
    assert (page.json()["next_after"], page.json()["head"]) == (2, 6)
    page = scene.read(scene.reader, limit="2", after="2")
    assert _seqs(page) == [3, 5]
    assert page.json()["next_after"] == 5
    page = scene.read(scene.reader, limit="2", after="5")
    assert (_seqs(page), page.json()["next_after"]) == ([6], None)
    exact = scene.read(scene.reader, limit="5")
    assert (_seqs(exact), exact.json()["next_after"]) == ([1, 2, 3, 5, 6], None)
    assert _seqs(scene.read(scene.reader, after="6")) == []


def test_read_newest_first(scene: Scene) -> None:
    filled = _fill(scene)
    page = scene.read(scene.reader, limit="2", before="7")
    assert _seqs(page) == [6, 5]
    assert page.json() | {"events": []} == {"events": [], "next_after": None, "head": 6, "next_before": 5}
    page = scene.read(scene.reader, limit="2", before="5")
    assert (_seqs(page), page.json()["next_before"]) == ([3, 2], 2)
    page = scene.read(scene.reader, limit="2", before="2")
    assert (_seqs(page), page.json()["next_before"]) == ([1], None)
    assert _seqs(scene.read(scene.reader, before="1")) == []
    assert _seqs(scene.read(scene.reader, before="0")) == []
    # the filters and the watched kinds apply as with `after`
    assert _seqs(scene.read(scene.reader, before="7", kinds="session.terminal,prompt")) == [6, 4, 2]
    assert _seqs(scene.read(scene.reader, before="7", task=filled["task"])) == [3, 2]
    assert "next_before" not in scene.read(scene.reader).json()
    both = scene.read(scene.reader, before="7", after="1")
    assert (both.status_code, both.json()) == (400, {"error": "request_invalid"})
    invalid = scene.world.hub.client(scene.reader.browser.key).get(
        f"/v1/scopes/{scene.wg}/events", params={"before": "x"}, extensions={"contract": False}
    )
    assert (invalid.status_code, invalid.json()) == (400, {"error": "request_invalid"})


def test_read_org_scope_and_empty_scopes(scene: Scene) -> None:
    org = scene.read(scene.writer, scene.org)
    assert [item["event"]["header"]["kind"] for item in org.json()["events"]] == [
        "org.profile",
        "wg.profile",
        "member.profile",
    ]
    assert org.json()["head"] == 3
    empty = scene.read(scene.writer, scene.draft.default_wg)
    assert empty.json() == {"events": [], "next_after": None, "head": 0}


def test_read_rights(scene: Scene) -> None:
    _fill(scene)
    stranger = scene.stranger
    assert scene.read(stranger).json() == {"error": "not_found"}
    assert scene.read(stranger, scene.org).status_code == 404
    assert scene.read(scene.reader, new_id()).status_code == 404
    assert scene.read(scene.reader, "not-a-uuid").status_code == 404
    assert scene.read(scene.reader, scene.wg.upper()).status_code == 404
    hidden = new_id()
    draft, admin = scene.draft, scene.admin
    draft.add("wg_create", admin, wg=hidden, epoch=1)
    assert draft.post(admin, draft.wraps(admin, "wg", hidden, 1, [admin])).status_code == 200
    assert scene.read(scene.writer, hidden).status_code == 404  # an org member without access to the workgroup
    assert scene.read(admin, hidden).status_code == 200
    draft.add("ungrant", admin, subject=scene.reader.id, wg=scene.wg)
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    wraps = draft.wraps(admin, "wg", scene.wg, 2, [admin, scene.writer, scene.device, scene.other_device])
    assert draft.post(admin, wraps).status_code == 200
    assert scene.read(scene.reader).status_code == 404


def test_read_refuses_bad_parameters(scene: Scene) -> None:
    for query in (
        {"after": "-1"},
        {"after": "01"},
        {"after": "x"},
        {"limit": "0"},
        {"limit": "1001"},
        {"kinds": "Prompt"},
        {"kinds": "prompt,nothing.known"},
        {"task": "x"},
        {"session": new_id().upper()},
        {"ref": "entry"},
    ):
        response = scene.writer.browser.get(
            f"/v1/scopes/{scene.wg}/events", params=query, extensions={"contract": False}
        )
        assert response.json() == {"error": "request_invalid"}, query
    assert scene.read(scene.writer, limit="1000").status_code == 200


def test_read_is_logged(scene: Scene) -> None:
    assert scene.read(scene.reader).status_code == 200
    scene.world.hub.flush_read_log()
    rows = scene.rows(
        "SELECT actor_id::text, result, target_kind, target_id FROM hub.audit WHERE action = 'read.scope.events'"
    )
    assert rows == [(scene.reader.id, "ok", "scope_id", scene.wg)]


def test_a_read_racing_an_append_serves_events_and_head_of_one_state(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The events are read before the head: an event committed in between must not raise the served head beyond the
    # last served event (the browser would move its cursor past the event and lose it).
    assert scene.post(scene.writer, scene.wg_event(scene.writer)).status_code == 200
    read_head = read.head

    def append_first(*args: Any, **kwargs: Any) -> int:
        monkeypatch.setattr(read, "head", read_head)
        assert scene.post(scene.writer, scene.wg_event(scene.writer)).status_code == 200
        return read_head(*args, **kwargs)

    monkeypatch.setattr(read, "head", append_first)
    raced = scene.read(scene.reader).json()
    assert ([item["scope_seq"] for item in raced["events"]], raced["next_after"], raced["head"]) == ([1], None, 1)
    later = scene.read(scene.reader, after="1").json()
    assert ([item["scope_seq"] for item in later["events"]], later["head"]) == ([2], 2)


def test_read_newest_first_with_before(scene: Scene) -> None:
    """ADR-086 section 2 (KOR-35): `before` pages newest first with `next_before`, with every filter."""
    filled = _fill(scene)
    newest = scene.read(scene.reader, before="9999999999999999", limit="2")
    assert (_seqs(newest), newest.json()) == ([6, 5], {**newest.json(), "next_before": 5, "head": 6})
    older = scene.read(scene.reader, before="5", limit="2")
    assert (_seqs(older), older.json()["next_before"]) == ([3, 2], 2)
    last = scene.read(scene.reader, before="2")
    assert (_seqs(last), last.json()["next_before"]) == ([1], None)
    assert _seqs(scene.read(scene.reader, before="1")) == []
    assert _seqs(scene.read(scene.reader, before="0")) == []
    assert _seqs(scene.read(scene.reader, before="7", kinds="prompt")) == [6, 2]
    assert _seqs(scene.read(scene.reader, before="7", kinds="session.terminal,session.output")) == [4, 3]
    assert _seqs(scene.read(scene.reader, before="7", task=filled["task"])) == [3, 2]
    assert _seqs(scene.read(scene.reader, before="7", session=filled["session"])) == [3]
    assert _seqs(scene.read(scene.reader, before="7", session=filled["session"], kinds="session.terminal")) == [4]
    assert _seqs(scene.read(scene.reader, before="7", ref=filled["entry"])) == [5]


def test_after_and_before_exclude_each_other(scene: Scene) -> None:
    for query in ({"after": "1", "before": "5"}, {"before": "-1"}, {"before": "05"}, {"before": "1" * 17}):
        response = scene.reader.browser.get(
            f"/v1/scopes/{scene.wg}/events", params=query, extensions={"contract": False}
        )
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid"), query


class Explaining:
    """A connection that records the plan of every event read (EXPLAIN of the same text and parameters)."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.plans: list[str] = []

    def execute(self, query: str, params: Any = None) -> Any:
        if query.startswith("SELECT scope_seq"):
            rows = self.connection.execute("EXPLAIN " + query, params).fetchall()
            self.plans.append("\n".join(row[0] for row in rows))
        return self.connection.execute(query, params)


def test_every_page_is_read_in_index_order(scene: Scene) -> None:
    """Pages in both directions come from an index in the order of the page (no scan of the table, no sort), with
    each filter (ADR-086 section 2; `event_session_seq` of migration 0030 for a session's events)."""
    filled = _fill(scene)
    with scene.world.hub.pool.connection() as connection, connection.transaction():
        for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            connection.execute(f"SET LOCAL {setting} = off")
        explaining = Explaining(connection)
        wg = UUID(scene.wg)
        for before in (None, 7):
            for filters in (
                {},
                {"task": UUID(filled["task"])},
                {"session": UUID(filled["session"])},
                {"session": UUID(filled["session"]), "kinds": {"session.terminal"}},
                {"kinds": {"prompt", "session.output"}},
                {"ref": filled["entry"]},
            ):
                read.read_events(cast(Any, explaining), wg, before=before, **cast(Any, filters))
    assert len(explaining.plans) == 12
    for plan in explaining.plans:
        assert "Index" in plan, plan
        assert "Sort" not in plan, plan
        assert "Seq Scan" not in plan, plan
    session_plans = [explaining.plans[2], explaining.plans[8]]
    assert all("event_session_seq" in plan for plan in session_plans), session_plans


def test_event_pages_serialise_their_direction() -> None:
    ascending = read.EventPage([], 4, 9)
    newest = read.EventPage([], None, 9, next_before=3, backwards=True)
    assert ascending.to_json() == {"events": [], "next_after": 4, "head": 9}
    assert newest.to_json() == {"events": [], "next_after": None, "head": 9, "next_before": 3}

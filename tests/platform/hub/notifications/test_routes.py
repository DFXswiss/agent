"""U-HUB-notifications (routes): `GET /v1/notifications` lists the caller's own rows newest first with a cursor,
`POST /v1/notifications/read` marks own rows by ID or all of them, foreign IDs change nothing, and `GET /v1/me` counts
the unread rows (03-api.md sections 4.1 and 4.9; contracts validated by the harness)."""

from __future__ import annotations

from typing import Any

from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.whiteboards.support import Board


def _notify(board: Board, count: int) -> None:
    """`count` notified workgroup entries by the writer, one second apart (the reader and the admin are notified)."""
    scene = board.scene
    for _ in range(count):
        scene.world.hub.clock.advance(1)
        board.posted(scene.writer, board.entry(scene.writer), notify=[True])


def _page(actor: Actor, **query: str) -> Any:
    response = actor.browser.get("/v1/notifications", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _unread(actor: Actor) -> int:
    count: int = actor.browser.get("/v1/me").json()["unread_notifications"]
    return count


def test_the_list_is_the_callers_own_newest_first(board: Board) -> None:
    scene = board.scene
    _notify(board, 3)
    rows = board.rows(
        "SELECT id::text, ref FROM hub.notification WHERE recipient_id = %s ORDER BY created_at DESC, id DESC",
        scene.reader.id,
    )
    first = _page(scene.reader, limit="2")
    assert [item["notification_id"] for item in first["notifications"]] == [rows[0][0], rows[1][0]]
    second = _page(scene.reader, limit="2", before=first["next_before"])
    assert [item["notification_id"] for item in second["notifications"]] == [rows[2][0]]
    assert second["next_before"] is None
    item = first["notifications"][0]
    assert item == {
        "notification_id": rows[0][0],
        "kind": "whiteboard.entry",
        "org_id": scene.org,
        "wg_id": scene.wg,
        "task_id": None,
        "ref": rows[0][1],
        "created_at": item["created_at"],
        "read_at": None,
    }
    assert _page(scene.writer) == {"notifications": [], "next_before": None}
    assert _page(board.outsider)["notifications"] == []


def test_marking_read_by_id_and_all(board: Board, hub: HubHarness) -> None:
    scene = board.scene
    _notify(board, 3)
    assert _unread(scene.reader) == 3
    mine = [item["notification_id"] for item in _page(scene.reader)["notifications"]]
    theirs = [item["notification_id"] for item in _page(scene.admin)["notifications"]]
    response = scene.reader.browser.post("/v1/notifications/read", json={"ids": [mine[0], theirs[0], new_id()]})
    assert (response.status_code, response.json()) == (200, {})
    assert _unread(scene.reader) == 2
    assert _unread(scene.admin) == 3  # a foreign ID is ignored
    read = _page(scene.reader)["notifications"]
    assert read[0]["read_at"] is not None
    assert [item["read_at"] for item in read[1:]] == [None, None]
    marked_at = hub.clock()
    hub.clock.advance(5)
    assert scene.reader.browser.post("/v1/notifications/read", json={"all": True}).status_code == 200
    assert _unread(scene.reader) == 0
    assert board.rows("SELECT read_at FROM hub.notification WHERE id = %s", mine[0]) == [(marked_at,)]  # kept


def test_invalid_read_bodies_are_refused(board: Board) -> None:
    bodies: list[dict[str, Any]] = [{}, {"ids": []}, {"all": False}, {"ids": [new_id()], "all": True}, {"ids": ["x"]}]
    for body in bodies:
        response = board.scene.reader.browser.post("/v1/notifications/read", json=body, extensions={"contract": False})
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid"), body
    response = board.scene.reader.browser.get(
        "/v1/notifications", params={"before": "x"}, extensions={"contract": False}
    )
    assert response.status_code == 400


def test_the_list_is_read_logged(board: Board, hub: HubHarness) -> None:
    _page(board.scene.reader)
    hub.flush_read_log()
    assert board.rows("SELECT count(*) FROM hub.audit WHERE action = 'read.notifications'") == [(1,)]

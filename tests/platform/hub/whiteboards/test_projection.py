"""U-HUB-whiteboards (projection): `whiteboard_entry` follows the whiteboard events in the storing transaction, the
rights of each level come from the event rules, and the rebuild gives the incremental state (02-data-model.md 1.3)."""

from __future__ import annotations

from typing import Any

from agent_hub.events import hooks
from agent_hub.registry import PROJECTION_REBUILDERS, merge_mappings
from agent_hub.whiteboards import projection
from agent_hub.whiteboards.event_hooks import project
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.whiteboards.support import Board, id_of, ref_of

COLUMNS = (
    "id, org_id, level, wg_id, task_id, author_id, created_event_id, latest_event_id, created_at, updated_at,"
    " deleted, notify"
)


def _entry(board: Board, ref: str) -> tuple[Any, ...] | None:
    found = board.rows(f"SELECT {COLUMNS} FROM hub.whiteboard_entry WHERE id = %s", ref)
    return found[0] if found else None


def _all(board: Board) -> list[tuple[Any, ...]]:
    return board.rows(f"SELECT {COLUMNS} FROM hub.whiteboard_entry ORDER BY id")


def test_the_hook_and_the_rebuilder_are_registered() -> None:
    assert project in hooks.configure()
    assert merge_mappings(PROJECTION_REBUILDERS)["whiteboards"].__module__ == "agent_hub.whiteboards.projections"


def test_an_entry_on_each_level_creates_its_row(board: Board) -> None:
    scene = board.scene
    task, _session = scene.start_session()
    org_entry = board.entry(scene.admin, level="org")
    wg_entry = board.entry(scene.writer)
    task_entry = board.entry(scene.writer, level="task", task=task)
    board.posted(scene.admin, org_entry)
    board.posted(scene.writer, wg_entry, task_entry)
    now = scene.world.hub.clock()
    for event, level, wg, task_id, author in (
        (org_entry, "org", None, None, scene.admin),
        (wg_entry, "workgroup", scene.wg, None, scene.writer),
        (task_entry, "task", scene.wg, task, scene.writer),
    ):
        row = _entry(board, ref_of(event))
        assert row is not None
        assert [str(value) if value is not None else None for value in row[:8]] == [
            ref_of(event),
            scene.org,
            level,
            wg,
            task_id,
            author.id,
            id_of(event),
            id_of(event),
        ]
        assert row[8:] == (now, now, False, False)


def test_writers_edit_and_delete_entries_of_their_level(board: Board) -> None:
    scene, clock = board.scene, board.scene.world.hub.clock
    entry = board.posted(scene.writer, board.entry(scene.writer))[0]
    ref = ref_of(entry)
    created = clock()
    clock.advance(5)
    edit = board.posted(scene.writer, board.event(scene.writer, "whiteboard.edit", ref))[0]
    row = _entry(board, ref)
    assert row is not None
    assert (str(row[6]), str(row[7]), row[8], row[9], row[10]) == (
        id_of(entry),
        id_of(edit),
        created,
        clock(),
        False,
    )
    clock.advance(5)
    delete = board.posted(scene.writer, board.event(scene.writer, "whiteboard.delete", ref))[0]
    row = _entry(board, ref)
    assert row is not None
    assert (str(row[7]), row[9], row[10]) == (id_of(delete), clock(), True)
    clock.advance(5)
    late = board.posted(scene.writer, board.event(scene.writer, "whiteboard.edit", ref))[0]  # stored, not projected
    assert board.rows("SELECT count(*) FROM hub.event WHERE id = %s", id_of(late)) == [(1,)]
    assert _entry(board, ref) == row


def test_org_level_entries_need_an_admin_and_workgroup_entries_a_writer(board: Board) -> None:
    scene = board.scene
    refused = (400, {"error": "author_not_writer"})

    def answer(author: Any, event: dict[str, Any]) -> tuple[int, Any]:
        response = board.post(author, event)
        return response.status_code, response.json()

    assert answer(scene.writer, board.entry(scene.writer, level="org")) == refused
    assert answer(scene.reader, board.entry(scene.reader)) == refused
    assert answer(board.outsider, board.entry(board.outsider)) == refused
    assert answer(scene.reader, board.entry(scene.reader, wg=board.other_wg)) == refused
    assert answer(scene.writer, board.entry(scene.writer, wg=board.other_wg))[0] == 200
    entry = board.posted(scene.admin, board.entry(scene.admin, level="org"))[0]
    assert answer(scene.writer, board.event(scene.writer, "whiteboard.edit", ref_of(entry), level="org")) == refused
    assert board.rows("SELECT count(*) FROM hub.whiteboard_entry") == [(2,)]


def test_events_that_do_not_fit_an_entry_change_nothing(board: Board) -> None:
    scene = board.scene
    task, _session = scene.start_session()
    entry = board.posted(scene.writer, board.entry(scene.writer))[0]
    ref = ref_of(entry)
    before = _entry(board, ref)
    board.posted(
        scene.writer,
        board.event(scene.writer, "whiteboard.edit", ref, wg=board.other_wg),  # another workgroup
        board.event(scene.writer, "whiteboard.delete", ref, level="task", task=task),  # another level
        board.event(scene.writer, "whiteboard.entry", ref, wg=board.other_wg),  # the ID exists: first entry wins
        board.event(scene.writer, "whiteboard.edit", board.entry(scene.writer)["header"]["ref"]),  # unknown entry
    )
    assert _entry(board, ref) == before
    assert board.rows("SELECT count(*) FROM hub.whiteboard_entry") == [(1,)]


def test_entry_and_edit_in_one_call_are_projected_in_order(board: Board) -> None:
    scene = board.scene
    entry = board.entry(scene.writer)
    edit = board.event(scene.writer, "whiteboard.edit", ref_of(entry))
    board.posted(scene.writer, entry, edit)
    row = _entry(board, ref_of(entry))
    assert row is not None
    assert (str(row[6]), str(row[7])) == (id_of(entry), id_of(edit))
    assert board.posted(scene.writer, entry)  # a replay changes nothing
    assert _entry(board, ref_of(entry)) == row


def test_other_events_skip_the_projection(board: Board, hub: HubHarness) -> None:
    scene = board.scene
    board.posted(scene.writer, scene.wg_event(scene.writer))
    with hub.pool.connection() as connection:
        project(connection, [])
    assert board.rows("SELECT count(*) FROM hub.whiteboard_entry") == [(0,)]


def test_rebuild_equals_the_incremental_state(board: Board, hub: HubHarness) -> None:
    scene, clock = board.scene, board.scene.world.hub.clock
    task, _session = scene.start_session()
    org_entry = board.posted(scene.admin, board.entry(scene.admin, level="org"), notify=[True])[0]
    clock.advance(1)
    wg_entry = board.posted(scene.writer, board.entry(scene.writer), notify=[False])[0]
    task_entry = board.entry(scene.writer, level="task", task=task)
    board.posted(
        scene.writer,
        task_entry,
        board.event(scene.writer, "whiteboard.edit", ref_of(task_entry), level="task", task=task),
    )
    clock.advance(1)
    board.posted(scene.writer, board.event(scene.writer, "whiteboard.delete", ref_of(wg_entry)))
    board.posted(scene.writer, board.event(scene.writer, "whiteboard.edit", ref_of(wg_entry)))  # after the delete
    board.posted(scene.writer, board.event(scene.writer, "whiteboard.entry", ref_of(org_entry), wg=board.other_wg))
    incremental = _all(board)
    assert len(incremental) == 3
    assert [row[11] for row in incremental if str(row[0]) == ref_of(org_entry)] == [True]
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.whiteboard_entry SET deleted = NOT deleted, notify = NOT notify")
        assert merge_mappings(PROJECTION_REBUILDERS)["whiteboards"](connection, hub.config) == 3
    assert _all(board) == incremental
    with hub.pool.connection() as connection, connection.transaction():
        assert projection.rebuild(connection) == 3
    assert _all(board) == incremental


def test_levels() -> None:
    assert projection.level_of("org", None) == "org"
    assert projection.level_of("wg", None) == "workgroup"

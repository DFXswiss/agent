"""U-HUB-contexts (pull-request links): the opaque tags of ADR-072 section 8 and the pull-request index of ADR-084
section 1 - sealed bodies, a person's link by address and its removal, the device's link replacing a person's, the
list of a workgroup or task with its cursor, the rights (also of the user's agent, ADR-056) and the audit rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto import context
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.contexts.support import NOT_CONTRACT, WK, Checkpoints, error
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import audit_rows

TAG = context.pr_link_tag(context.pr_link_key(WK), "example-org/example-repo", 17)
OTHER = context.pr_link_tag(context.pr_link_key(WK), "example-org/example-repo", 18)


def post(checkpoints: Checkpoints, session: str, device: Actor | None = None, **changes: Any) -> tuple[int, Any]:
    body = {"session_id": session, "epoch": 1, "tag": TAG, **changes}
    return error(checkpoints.client(device).post("/v1/device/pr-links", json=body))


def test_a_pull_request_leads_to_its_task(checkpoints: Checkpoints) -> None:
    hub, scene = checkpoints.outbox.hub, checkpoints.scene
    task, session = checkpoints.outbox.session()
    path = f"/v1/workgroups/{scene.wg}/pr-links/1/{TAG}"
    assert error(scene.writer.browser.get(path)) == (404, {"error": "not_found"})
    at = format_time(hub.clock())
    assert post(checkpoints, session) == (200, {"replayed": False})
    hub.clock.advance(5)
    assert post(checkpoints, session) == (200, {"replayed": True})
    # one row of its own for the registration; the repeat changed nothing (ADR-063: the generic row)
    linked = audit_rows(checkpoints.outbox.desk, "session.pr_linked")
    assert [(row[0], row[2], row[3]) for row in linked] == [("device", TAG, {"epoch": 1})]
    found = {"task_id": task, "session_id": session, "linked_by": None, "sealed": None, "created_at": at}
    for client in (scene.writer.browser, scene.reader.browser, agent_client(hub, UUID(scene.reader.id))):
        assert error(client.get(path)) == (200, found)
    for client in (scene.stranger.browser, agent_client(hub, UUID(scene.stranger.id))):
        assert client.get(path).status_code in (403, 404)
    # the first task keeps the tag
    _task, second = checkpoints.outbox.session()
    assert post(checkpoints, second) == (409, {"error": "id_conflict"})
    assert error(scene.writer.browser.get(path)) == (200, found)
    assert post(checkpoints, second, tag=OTHER) == (200, {"replayed": False})


def test_refused_registrations(checkpoints: Checkpoints) -> None:
    _task, session = checkpoints.outbox.session()
    assert post(checkpoints, new_id()) == (404, {"error": "not_found"})
    assert post(checkpoints, session, checkpoints.scene.other_device) == (403, {"error": "forbidden"})
    _task, outside = checkpoints.scene.start_session(wg=checkpoints.scene.draft.default_wg)
    assert post(checkpoints, outside) == (403, {"error": "forbidden"})
    assert post(checkpoints, session, epoch=2) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/epoch"]}},
    )
    assert checkpoints.outbox.desk.rows("SELECT count(*) FROM hub.pr_link") == [(0,)]


@pytest.mark.parametrize(("epoch", "tag"), [("0", TAG), ("2147483648", TAG), ("x", TAG), ("1", "short"), ("2", TAG)])
def test_a_malformed_or_unknown_path_names_nothing(checkpoints: Checkpoints, epoch: str, tag: str) -> None:
    _task, session = checkpoints.outbox.session()
    assert post(checkpoints, session) == (200, {"replayed": False})
    path = f"/v1/workgroups/{checkpoints.scene.wg}/pr-links/{epoch}/{tag}"
    found = checkpoints.scene.writer.browser.get(path, extensions=NOT_CONTRACT)
    assert error(found) == (404, {"error": "not_found"})


# --- ADR-084 section 1: the pull-request index ---------------------------------------------------------------------

BODY_KEY = context.pr_link_body_key(WK)


def sealed(task: str, origin: str, *, tag: str = TAG, epoch: int = 1, wg: str | None = None) -> str:
    """A real sealed body (the hub only checks its form)."""
    return context.seal_pr_link(
        BODY_KEY,
        wg_id=wg or "01930033-0000-7000-8000-000000000003",
        epoch=epoch,
        tag=tag,
        task_id=task,
        origin=origin,
        repo="Example-Org/Example-Repo",
        number=17 if tag == TAG else 18,
    )


def links(checkpoints: Checkpoints) -> str:
    return f"/v1/workgroups/{checkpoints.scene.wg}/pr-links"


def link(checkpoints: Checkpoints, task: str, client: Any = None, **changes: Any) -> tuple[int, Any]:
    scene = checkpoints.scene
    body = {"epoch": 1, "tag": TAG, "sealed": sealed(task, f"user:{scene.writer.id}"), "task_id": task, **changes}
    return error((client or scene.writer.browser).post(links(checkpoints), json=body))


def test_a_device_link_carries_its_sealed_body_and_is_listed(checkpoints: Checkpoints) -> None:
    hub, scene = checkpoints.outbox.hub, checkpoints.scene
    task, session = checkpoints.outbox.session()
    body = sealed(task, f"device:{session}")
    at = format_time(hub.clock())
    assert post(checkpoints, session, sealed=body) == (200, {"replayed": False})
    row = {
        "epoch": 1,
        "tag": TAG,
        "sealed": body,
        "task_id": task,
        "session_id": session,
        "linked_by": None,
        "created_at": at,
    }
    single = {key: value for key, value in row.items() if key not in ("epoch", "tag")}
    assert error(scene.reader.browser.get(f"{links(checkpoints)}/1/{TAG}")) == (200, single)
    for client in (scene.reader.browser, agent_client(hub, UUID(scene.reader.id))):
        assert error(client.get(links(checkpoints))) == (200, {"items": [row], "next_cursor": None})
    for client in (scene.stranger.browser, agent_client(hub, UUID(scene.stranger.id))):
        assert error(client.get(links(checkpoints))) == (404, {"error": "not_found"})
    # a body that is not of the sealed form is refused at its pointer, as an older hub refuses the field
    malformed = {"session_id": session, "epoch": 1, "tag": OTHER, "sealed": "AAAA"}
    refused = checkpoints.client().post("/v1/device/pr-links", json=malformed, extensions=NOT_CONTRACT)
    assert error(refused) == (400, {"error": "request_invalid", "detail": {"pointers": ["/sealed"]}})


def test_a_person_links_a_pull_request_by_its_address(checkpoints: Checkpoints) -> None:
    hub, scene, desk = checkpoints.outbox.hub, checkpoints.scene, checkpoints.outbox.desk
    task, _session = checkpoints.outbox.session()
    at = format_time(hub.clock())
    body = sealed(task, f"user:{scene.writer.id}")
    row = {
        "epoch": 1,
        "tag": TAG,
        "sealed": body,
        "task_id": task,
        "session_id": None,
        "linked_by": scene.writer.id,
        "created_at": at,
    }
    assert link(checkpoints, task, sealed=body) == (201, {"created": True, "link": row})
    hub.clock.advance(5)
    # the same tag again - by anybody, for any task - shows the existing link and moves nothing
    other_task, _ = checkpoints.outbox.session()
    assert link(checkpoints, other_task) == (200, {"created": False, "link": row})
    assert [(r[0], r[2], r[3]) for r in audit_rows(desk, "pr.linked")] == [
        ("user", TAG, {"epoch": 1, "replaced": False})
    ]
    assert error(scene.reader.browser.get(f"{links(checkpoints)}/1/{TAG}"))[1]["linked_by"] == scene.writer.id
    # the user's agent may link like its user (ADR-056)
    agent = agent_client(hub, UUID(scene.writer.id))
    assert (
        link(checkpoints, task, agent, tag=OTHER, sealed=sealed(task, f"user:{scene.writer.id}", tag=OTHER))[0] == 201
    )


def test_refused_person_links(checkpoints: Checkpoints) -> None:
    scene, desk = checkpoints.scene, checkpoints.outbox.desk
    task, _session = checkpoints.outbox.session()
    assert link(checkpoints, task, scene.reader.browser) == (403, {"error": "forbidden"})
    assert link(checkpoints, task, agent_client(checkpoints.outbox.hub, UUID(scene.reader.id))) == (
        403,
        {"error": "forbidden"},
    )
    # a write on a workgroup the caller cannot write is refused as such (`rights.require(write=True)`)
    assert link(checkpoints, task, scene.stranger.browser) == (403, {"error": "forbidden"})
    unknown = f"/v1/workgroups/{new_id()}/pr-links"
    assert error(
        scene.writer.browser.post(
            unknown, json={"epoch": 1, "tag": TAG, "sealed": sealed(task, "user:" + scene.writer.id), "task_id": task}
        )
    ) == (404, {"error": "not_found"})
    invalid_task = (400, {"error": "request_invalid", "detail": {"pointers": ["/task_id"]}})
    assert link(checkpoints, new_id()) == invalid_task
    outside, _ = scene.start_session(wg=scene.draft.default_wg)
    assert link(checkpoints, outside) == invalid_task
    assert link(checkpoints, task, epoch=2) == (400, {"error": "epoch_stale"})
    # after a rotation only the new epoch takes a person's link; the device may still name an older one
    desk.execute(
        "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('wg', %s, 2, %s)",
        scene.wg,
        checkpoints.outbox.hub.clock(),
    )
    assert link(checkpoints, task) == (400, {"error": "epoch_stale"})
    body = {"epoch": 2, "tag": TAG, "sealed": None, "task_id": task}
    refused = scene.writer.browser.post(links(checkpoints), json=body, extensions=NOT_CONTRACT)
    assert error(refused) == (400, {"error": "request_invalid", "detail": {"pointers": ["/sealed"]}})
    assert desk.rows("SELECT count(*) FROM hub.pr_link") == [(0,)]
    assert link(checkpoints, task, epoch=2)[0] == 201


def test_the_device_link_replaces_a_persons_link(checkpoints: Checkpoints) -> None:
    hub, scene, desk = checkpoints.outbox.hub, checkpoints.scene, checkpoints.outbox.desk
    linked_task, _ = checkpoints.outbox.session()
    assert link(checkpoints, linked_task)[0] == 201
    hub.clock.advance(5)
    task, session = checkpoints.outbox.session()
    at = format_time(hub.clock())
    body = sealed(task, f"device:{session}")
    assert post(checkpoints, session, sealed=body) == (200, {"replayed": False})
    single = {"task_id": task, "session_id": session, "linked_by": None, "sealed": body, "created_at": at}
    assert error(scene.writer.browser.get(f"{links(checkpoints)}/1/{TAG}")) == (200, single)
    assert [(r[0], r[1], r[2], r[3]) for r in audit_rows(desk, "pr.linked")][1:] == [
        ("device", UUID(checkpoints.outbox.device.id), TAG, {"epoch": 1, "replaced": True})
    ]
    assert audit_rows(desk, "session.pr_linked") == []
    # from now on it is a device link: replayed for its task, refused for another, never removed by a person
    assert post(checkpoints, session) == (200, {"replayed": True})
    assert post(checkpoints, checkpoints.outbox.session()[1]) == (409, {"error": "id_conflict"})
    assert error(scene.writer.browser.delete(f"{links(checkpoints)}/1/{TAG}")) == (409, {"error": "id_conflict"})


def test_a_person_removes_a_persons_link(checkpoints: Checkpoints) -> None:
    hub, scene, desk = checkpoints.outbox.hub, checkpoints.scene, checkpoints.outbox.desk
    task, _ = checkpoints.outbox.session()
    assert link(checkpoints, task)[0] == 201
    path = f"{links(checkpoints)}/1/{TAG}"
    assert error(scene.reader.browser.delete(path)) == (403, {"error": "forbidden"})
    assert error(scene.stranger.browser.delete(path)) == (403, {"error": "forbidden"})
    assert error(agent_client(hub, UUID(scene.writer.id)).delete(path)) == (200, {"deleted": True})
    assert [(r[0], r[2], r[3]) for r in audit_rows(desk, "pr.unlinked")] == [("user", TAG, {"epoch": 1})]
    assert error(scene.writer.browser.delete(path)) == (404, {"error": "not_found"})
    for epoch, tag in (("0", TAG), ("2147483648", TAG), ("1", "short")):
        found = scene.writer.browser.delete(f"{links(checkpoints)}/{epoch}/{tag}", extensions=NOT_CONTRACT)
        assert error(found) == (404, {"error": "not_found"})


def test_the_list_pages_newest_first_and_filters_by_task(checkpoints: Checkpoints) -> None:
    hub, scene = checkpoints.outbox.hub, checkpoints.scene
    first, session = checkpoints.outbox.session()
    second, _ = checkpoints.outbox.session()
    tags = [context.pr_link_tag(context.pr_link_key(WK), "o/r", number) for number in range(1, 5)]
    assert post(checkpoints, session, tag=tags[0]) == (200, {"replayed": False})
    for tag, task in ((tags[1], second), (tags[2], first), (tags[3], second)):
        hub.clock.advance(1)
        assert link(checkpoints, task, tag=tag)[0] == 201
    hub.clock.advance(1)
    # two links at the same moment are ordered by epoch, then tag (descending, byte order)
    assert link(checkpoints, first, tag=OTHER)[0] == 201
    same = [OTHER, TAG]
    assert link(checkpoints, first)[0] == 201
    ordered = sorted(same, reverse=True) + tags[:0:-1] + tags[:1]
    seen: list[str] = []
    cursor = None
    while True:
        query = {"limit": "2"} if cursor is None else {"limit": "2", "cursor": cursor}
        status, page = error(scene.reader.browser.get(links(checkpoints), params=query))
        assert status == 200
        seen += [item["tag"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ordered
    status, page = error(scene.reader.browser.get(links(checkpoints), params={"task": second}))
    assert (status, [item["tag"] for item in page["items"]]) == (200, [tags[3], tags[1]])
    status, page = error(scene.reader.browser.get(links(checkpoints), params={"task": second, "limit": "1"}))
    rest = scene.reader.browser.get(links(checkpoints), params={"task": second, "cursor": page["next_cursor"]})
    assert [item["tag"] for item in rest.json()["items"]] == [tags[1]]


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({"task": "x"}, "/task"),
        ({"cursor": "a+b"}, "/cursor"),
        ({"cursor": "x"}, "/cursor"),  # not b64u
        ({"cursor": "eyJ"}, "/cursor"),  # not JSON
        ({"cursor": "W10"}, "/cursor"),  # [] instead of an object
        ({"cursor": "e30"}, "/cursor"),  # {} without the keys
        ({"cursor": "eyJhdCI6MSwiZXBvY2giOiIxIiwidGFnIjoieCJ9"}, "/cursor"),  # a text epoch
        ({"cursor": "eyJhdCI6MSwiZXBvY2giOjEsInRhZyI6MX0"}, "/cursor"),  # a number tag
        ({"cursor": "eyJ0YWciOiJ4IiwiZXBvY2giOjEsImF0IjoxfQ"}, "/cursor"),  # not canonical
        ({"cursor": "eyJhdCI6OTk5OTk5OTk5OTk5OTk5OTk5LCJlcG9jaCI6MSwidGFnIjoieCJ9"}, "/cursor"),  # out of range
        ({"limit": "0"}, None),
        ({"limit": "201"}, None),
    ],
)
def test_malformed_list_queries_are_refused(
    checkpoints: Checkpoints, query: dict[str, str], pointer: str | None
) -> None:
    found = checkpoints.scene.reader.browser.get(links(checkpoints), params=query, extensions=NOT_CONTRACT)
    detail = {} if pointer is None else {"detail": {"pointers": [pointer]}}
    assert error(found) == (400, {"error": "request_invalid", **detail})


def test_exactly_one_origin_per_row(checkpoints: Checkpoints) -> None:
    import psycopg  # noqa: PLC0415

    task, session = checkpoints.outbox.session()
    insert = (
        "INSERT INTO hub.pr_link (wg_id, epoch, tag, task_id, session_id, linked_by, created_at)"
        " VALUES (%s, 1, %s, %s, %s, %s, now())"
    )
    scene = checkpoints.scene
    for session_id, linked_by in ((None, None), (session, scene.writer.id)):
        with pytest.raises(psycopg.errors.CheckViolation):
            checkpoints.outbox.desk.execute(insert, scene.wg, TAG, task, session_id, linked_by)
    with pytest.raises(psycopg.errors.CheckViolation):
        checkpoints.outbox.desk.execute(
            "INSERT INTO hub.pr_link (wg_id, epoch, tag, task_id, session_id, sealed, created_at)"
            " VALUES (%s, 1, %s, %s, %s, 'short', now())",
            scene.wg,
            TAG,
            task,
            session,
        )

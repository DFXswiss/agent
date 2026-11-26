"""U-HUB-support (ADR-096, KOR-56): the support roster from the platform log, releases of a workgroup to support in
the membership log, what a support person may read and write at the hub, revocation with rotation, a person leaving
support, audit and notifications."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent_hub.orgs import rights
from agent_hub.support import notify, projections, store
from agent_hub.support import routes as support_routes
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.support.conftest import Roots
from tests.platform.hub.tasks.support import Desk

Json = dict[str, Any]


def release(desk: Desk, by: Actor, access: str, people: list[Actor], *, epochs: int = 1) -> Any:
    """`support_grant` with the wraps of every epoch to the people and the `support.info` event, in one batch."""
    draft, wg = desk.scene.draft, desk.wg
    draft.add("support_grant", by, wg=wg, access=access)
    wraps = [wrap for epoch in range(1, epochs + 1) for wrap in draft.wraps(by, "wg", wg, epoch, people)]
    info = draft.event(by, "support.info", scope="wg", scope_id=wg, epoch=epochs, wg=wg)
    return draft.post(by, wraps, [info])


def readers(desk: Desk) -> list[Actor]:
    scene = desk.scene
    return [scene.admin, scene.writer, scene.reader, scene.device, scene.other_device]


def revoke(desk: Desk, by: Actor, *, rotate: bool = True, epoch: int = 2) -> Any:
    draft, wg = desk.scene.draft, desk.wg
    draft.add("support_revoke", by, wg=wg)
    if not rotate:
        return draft.post(by)
    draft.add("rotate", by, scope="wg", scope_id=wg, epoch=epoch)
    return draft.post(by, draft.wraps(by, "wg", wg, epoch, readers(desk)))


def kinds(desk: Desk, recipient: Actor, kind: str) -> list[tuple[Any, ...]]:
    return desk.rows(
        "SELECT ref, wg_id FROM hub.notification WHERE recipient_id = %s AND kind = %s ORDER BY created_at, id",
        recipient.id,
        kind,
    )


def test_a_person_gets_the_role_and_a_name(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person, "Sam")
    assert desk.rows("SELECT active, name FROM hub.support_person WHERE user_id = %s", person.id) == [(True, "Sam")]
    assert person.browser.get("/v1/support/releases").json() == {"releases": [], "truncated": False}
    assert desk.scene.writer.browser.get("/v1/support/releases").status_code == 404
    assert desk.hub.root.put(f"/v1/platform/support/{new_id()}", json={"name": "Nobody"}).status_code == 404
    assert desk.hub.root.put("/v1/platform/support/not-a-uuid", json={"name": "X"}).status_code == 404
    assert desk.hub.root.put(f"/v1/platform/support/{person.id}", json={"name": "a\nb"}).status_code == 400
    assert desk.rows("SELECT target_id FROM hub.audit WHERE action = 'support.name'") == [(person.id,)]


def test_a_role_needs_a_certification(roots: Roots, world: Any) -> None:
    person = world.user()
    refused = roots.post({"type": "role_grant", "subject": person.id, "role": "support"})
    assert refused.status_code == 409 or refused.json()["error"] == "log_rights", refused.text


def test_without_a_release_a_person_sees_nothing(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    scene = desk.scene
    assert person.browser.get(f"/v1/workgroups/{desk.wg}/tasks").status_code == 404
    assert person.browser.get(f"/v1/orgs/{desk.org}/log").status_code == 404
    assert person.browser.get(f"/v1/scopes/{desk.wg}/events").status_code == 404
    wrapped = scene.draft.wraps(scene.admin, "wg", desk.wg, 1, [person])
    refused = scene.admin.browser.post("/v1/wraps", json={"wraps": wrapped})
    assert refused.json()["error"] == "wrap_unauthorized"
    assert desk.rows("SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s", person.id) == [(0,)]


def test_a_read_release_lets_a_person_read_and_is_noticed(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person, "Sam")
    scene = desk.scene
    response = release(desk, scene.admin, "read", [person])
    assert response.status_code == 200, response.text
    assert desk.rows("SELECT access FROM hub.support_release WHERE wg_id = %s", desk.wg) == [("read",)]
    assert kinds(desk, scene.admin, "support.release") == [("grant:read", uuid.UUID(desk.wg))]
    assert kinds(desk, person, "support.release") == [("grant:read", uuid.UUID(desk.wg))]
    listed = scene.writer.browser.get(f"/v1/orgs/{desk.org}/support").json()
    assert listed["releases"][0]["access"] == "read"
    assert listed["people"] == [{"user_id": person.id, "name": "Sam"}]
    assert scene.stranger.browser.get(f"/v1/orgs/{desk.org}/support").status_code == 404
    assert scene.writer.browser.get("/v1/orgs/not-a-uuid/support").status_code == 404
    assert scene.writer.browser.get(f"/v1/orgs/{desk.org.upper()}/support").status_code == 404
    mine = person.browser.get("/v1/support/releases").json()["releases"]
    assert [(item["org_id"], item["wg_id"], item["access"]) for item in mine] == [(desk.org, desk.wg, "read")]
    for path in (f"/v1/workgroups/{desk.wg}/tasks", f"/v1/orgs/{desk.org}/log", f"/v1/scopes/{desk.wg}/events"):
        assert person.browser.get(path).status_code == 200, path
    assert [(ref,) for ref, _wg in kinds(desk, scene.admin, "support.access")] == [(person.id,)]
    wraps = person.browser.get("/v1/wraps").json()["wraps"]
    assert {(item["scope_id"], item["epoch"]) for item in wraps} == {(desk.wg, 1)}
    assert person.browser.get(f"/v1/workgroups/{scene.draft.default_wg}/tasks").status_code == 404
    reads = desk.rows(
        "SELECT action, org_id FROM hub.audit WHERE actor_id = %s AND action LIKE 'read.%%' ORDER BY seq", person.id
    )
    assert ("read.workgroup.tasks", uuid.UUID(desk.org)) in reads
    task, _session = scene.start_session()
    refused = desk.prompt(person, task, scene.prompt(person, task))
    assert refused.status_code == 403, refused.text


def test_a_write_release_lets_a_person_prompt_and_write_the_whiteboard(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    scene = desk.scene
    assert release(desk, scene.writer, "read", [person]).status_code == 200
    scene.draft.add("support_grant", scene.writer, wg=desk.wg, access="write")
    assert scene.draft.post(scene.writer).status_code == 200
    task, _session = scene.start_session()
    event = scene.prompt(person, task)
    response = desk.prompt(person, task, event)
    assert response.status_code == 200, response.text
    assert desk.rows("SELECT author_id, author_kind FROM hub.event WHERE id = %s", event["header"]["id"]) == [
        (uuid.UUID(person.id), "user")
    ]
    assert desk.rows("SELECT count(*) FROM hub.audit WHERE action = 'support.write' AND actor_id = %s", person.id) == [
        (1,)
    ]
    board = scene.post(person, scene.wg_event(person, "whiteboard.entry", ref=new_id()))
    assert board.status_code == 200, board.text
    settings = scene.post(person, scene.wg_event(person, "settings.workgroup"))
    assert settings.json()["error"] == "kind_not_allowed"
    created = desk.create(person)
    assert created.status_code == 403, created.text


def test_revocation_rotates_and_shuts_the_person_out(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    scene = desk.scene
    assert release(desk, scene.admin, "read", [person]).status_code == 200
    assert revoke(desk, scene.admin, rotate=False).json()["error"] == "rotation_required"
    scene.draft.drop_pending()
    response = revoke(desk, scene.admin)
    assert response.status_code == 200, response.text
    assert desk.rows("SELECT count(*) FROM hub.support_release") == [(0,)]
    assert desk.rows("SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s", person.id) == [(0,)]
    assert person.browser.get(f"/v1/workgroups/{desk.wg}/tasks").status_code == 404
    assert ("revoke", uuid.UUID(desk.wg)) in kinds(desk, scene.admin, "support.release")
    late = scene.draft.wraps(scene.admin, "wg", desk.wg, 2, [person])
    assert scene.admin.browser.post("/v1/wraps", json={"wraps": late}).json()["error"] == "wrap_unauthorized"


def test_catch_up_offers_every_epoch_to_the_support_persons(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    scene = desk.scene
    assert release(desk, scene.admin, "read", []).status_code == 200
    plan = scene.admin.browser.get(f"/v1/orgs/{desk.org}/catch-up").json()
    slots = [item for item in plan["missing"] if item["recipient_id"] == person.id]
    assert slots == [{"scope": "wg", "scope_id": desk.wg, "epoch": 1, "recipient_id": person.id}]


def test_a_person_leaving_support_owes_a_rotation(desk: Desk, roots: Roots, world: Any) -> None:
    leaving, staying = world.user(), world.user()
    roots.support(leaving)
    roots.support(staying)
    scene = desk.scene
    assert release(desk, scene.admin, "read", [leaving, staying]).status_code == 200
    roots.leave(leaving)
    assert desk.rows("SELECT active FROM hub.support_person WHERE user_id = %s", leaving.id) == [(False,)]
    pending = desk.rows("SELECT rotation_pending FROM hub.org WHERE id = %s", desk.org)[0][0]
    assert {"scope": "wg", "scope_id": desk.wg} in pending
    assert desk.rows("SELECT detail->>'change' FROM hub.audit WHERE action = 'support.people'") == [("left",)]
    assert kinds(desk, scene.admin, "support.people") == [(f"left:{leaving.id}", None)]
    assert leaving.browser.get(f"/v1/workgroups/{desk.wg}/tasks").status_code == 404
    assert staying.browser.get(f"/v1/workgroups/{desk.wg}/tasks").status_code == 200
    draft = scene.draft
    draft.add("rotate", scene.admin, scope="wg", scope_id=desk.wg, epoch=2)
    assert draft.post(scene.admin, draft.wraps(scene.admin, "wg", desk.wg, 2, readers(desk))).status_code == 200
    after = desk.rows("SELECT rotation_pending FROM hub.org WHERE id = %s", desk.org)[0][0]
    assert {"scope": "wg", "scope_id": desk.wg} not in after
    # coming back: the existing row is active again, nothing is owed
    assert roots.post({"type": "role_grant", "subject": leaving.id, "role": "support"}).status_code == 200
    assert desk.rows("SELECT active FROM hub.support_person WHERE user_id = %s", leaving.id) == [(True,)]
    assert (f"joined:{leaving.id}", None) in kinds(desk, scene.admin, "support.people")


def test_a_release_row_without_its_org_is_skipped(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    stray = new_id()
    desk.execute(
        "INSERT INTO hub.support_release (wg_id, org_id, access, granted_seq, granted_at)"
        " VALUES (%s, %s, 'read', 1, %s)",
        new_id(),
        stray,
        desk.hub.clock(),
    )
    roots.leave(person)
    assert desk.rows("SELECT count(*) FROM hub.audit WHERE action = 'support.people'") == [(0,)]


def test_rights_of_a_support_person(desk: Desk, roots: Roots, world: Any) -> None:
    person = world.user()
    roots.support(person)
    assert release(desk, desk.scene.admin, "write", [person]).status_code == 200
    with desk.hub.pool.connection() as connection:
        assert rights.can(connection, person.id, rights.WG_READ, wg_id=desk.wg)
        assert not rights.can(connection, person.id, rights.WG_WRITE, wg_id=desk.wg)
        assert rights.can(connection, person.id, rights.TASK_WRITE, wg_id=desk.wg, support_write=True)
        assert rights.can(connection, person.id, rights.ORG_SUPPORT, org_id=desk.org)
        assert not rights.can(connection, person.id, rights.ORG_MEMBER, org_id=desk.org)
        assert not rights.can(connection, desk.scene.writer.id, rights.ORG_SUPPORT, org_id=desk.org)
        assert store.released(connection, desk.org) == [(desk.wg, "write")]


def test_first_access_without_a_member_releaser_and_pending_of_an_unknown_org(desk: Desk) -> None:
    with desk.hub.pool.connection() as connection, connection.transaction():
        written = notify.first_access(
            connection, uuid.UUID(desk.org), uuid.UUID(desk.wg), uuid.UUID(new_id()), desk.hub.clock()
        )
        assert [item.recipient_id for item in written] == [uuid.UUID(desk.scene.admin.id)]
        store.merge_pending(connection, uuid.UUID(new_id()), [("wg", desk.wg)])
        assert notify.people_notices(connection, [], [], desk.hub.clock()) == []


def test_the_rebuild_derives_the_same_roster(desk: Desk, roots: Roots, world: Any) -> None:
    kept, left, renewed = world.user(), world.user(), world.user()
    for person in (kept, left, renewed):
        roots.support(person)
    roots.leave(left)
    assert roots.post({"type": "user_decertify", "user": renewed.id}).status_code == 200
    with desk.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.support_person SET active = NOT active")
        assert projections.rebuild(connection) == len(roots.entries)
    assert desk.rows("SELECT user_id FROM hub.support_person WHERE active") == [(uuid.UUID(kept.id),)]


@pytest.mark.parametrize(
    ("bodies", "expected"),
    [
        ([], set()),
        ([{"type": "user_certify", "user": "a"}, {"type": "role_grant", "subject": "a", "role": "support"}], {"a"}),
        (
            [
                {"type": "user_certify", "user": "a"},
                {"type": "role_grant", "subject": "a", "role": "support"},
                {"type": "user_decertify", "user": "a"},
                {"type": "user_certify", "user": "a"},
            ],
            set(),
        ),
        (
            [
                {"type": "user_certify", "user": "a"},
                {"type": "role_grant", "subject": "a", "role": "inviter"},
                {"type": "role_grant", "subject": "a", "role": "support"},
                {"type": "role_revoke", "subject": "a", "role": "inviter"},
                {"type": "role_revoke", "subject": "a", "role": "support"},
            ],
            set(),
        ),
    ],
)
def test_roster_of_the_replayed_log(bodies: list[Json], expected: set[str]) -> None:
    assert projections.roster_of(bodies) == expected


def test_the_release_list_is_capped(desk: Desk, roots: Roots, world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    person = world.user()
    roots.support(person)
    assert release(desk, desk.scene.admin, "read", [person]).status_code == 200
    monkeypatch.setattr(support_routes, "RELEASES_MAX", 0)
    assert person.browser.get("/v1/support/releases").json() == {"releases": [], "truncated": True}


def test_a_reader_member_that_is_support_writes_under_the_release(desk: Desk, roots: Roots) -> None:
    """ADR-096 §4: a member with read access that is also support prompts under a write release (either right
    suffices); the write is audited as support's."""
    scene = desk.scene
    roots.support(scene.reader)
    assert release(desk, scene.admin, "write", [scene.reader]).status_code == 200
    task, _session = scene.start_session()
    response = desk.prompt(scene.reader, task, scene.prompt(scene.reader, task))
    assert response.status_code == 200, response.text
    assert desk.rows(
        "SELECT count(*) FROM hub.audit WHERE action = 'support.write' AND actor_id = %s", scene.reader.id
    ) == [(1,)]
    # a member's own write stays a member's write
    own = desk.prompt(scene.writer, task, scene.prompt(scene.writer, task))
    assert own.status_code == 200, own.text
    assert desk.rows("SELECT count(*) FROM hub.audit WHERE action = 'support.write'") == [(1,)]

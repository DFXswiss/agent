"""U-HUB-audit (view): `GET /v1/orgs/{org}/audit` - admin and member rights, filters, newest first, paging and
validation (03-api.md section 4.11, 06-web.md section 9.2; R-6.7, R-8.9)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.query import decode_before, encode_before
from agent_hub.errors import HubError
from tests.platform.contracts.audit_details import valid_detail
from tests.platform.hub.monitoring.support import Watch
from tests.platform.hub.orgs.support import Actor, new_id


def add(watch: Watch, action: str, actor: Actor, *, wg: str | None = None, org: str | None = None, **ids: str) -> int:
    """One audit row of the org (`mine` unless `org`); its `seq`."""
    with watch.hub.pool.connection() as connection:
        stored = append_audit(
            connection,
            AuditRow(
                at=watch.now(),
                actor_kind=actor.kind,
                actor_id=UUID(actor.id),
                action=action,
                result="ok",
                org_id=UUID(org or watch.mine.id),
                wg_id=None if wg is None else UUID(wg),
                task_id=UUID(ids["task"]) if "task" in ids else None,
                session_id=UUID(ids["session"]) if "session" in ids else None,
                target_kind="test",
                target_id=action,
                detail=valid_detail(action),
            ),
        )
    return stored.seq


def rows(watch: Watch, who: Actor, org: str | None = None, **query: str) -> list[dict[str, Any]]:
    response = who.browser.get(f"/v1/orgs/{org or watch.mine.id}/audit", params={"limit": "200", **query})
    assert response.status_code == 200, response.text
    assert response.json()["next_before"] is None
    return list(response.json()["rows"])


def seqs(found: list[dict[str, Any]]) -> list[int]:
    return [row["seq"] for row in found]


def test_the_admin_sees_every_row_of_the_org_newest_first(watch: Watch) -> None:
    default = watch.mine.default_wg
    mine = [
        add(watch, "operation.accepted", watch.admin, wg=default),
        add(watch, "operation.failed", watch.member, wg=watch.hidden),
        add(watch, "hook.denied", watch.own, wg=watch.hidden),
    ]
    foreign = add(watch, "task.create", watch.other.founder, org=watch.other.id)
    found = rows(watch, watch.admin)
    assert seqs(found) == sorted(seqs(found), reverse=True)
    assert set(mine) <= set(seqs(found))
    assert foreign not in seqs(found)
    assert {row["org_id"] for row in found} == {watch.mine.id}
    assert any(row["action"].startswith("log.") for row in found)  # the org's own log appends
    first = next(row for row in found if row["seq"] == mine[0])
    assert first == {
        "seq": mine[0],
        "at": first["at"],
        "actor_id": watch.admin.id,
        "actor_kind": "user",
        "action": "operation.accepted",
        "result": "ok",
        "org_id": watch.mine.id,
        "wg_id": default,
        "task_id": None,
        "session_id": None,
        "device_id": None,
        "target_kind": "test",
        "target_id": "operation.accepted",
        "detail": valid_detail("operation.accepted"),
        "event_id": None,
    }


def test_a_member_sees_readable_workgroups_and_own_actions(watch: Watch) -> None:
    default, hidden = watch.mine.default_wg, watch.hidden
    readable = add(watch, "operation.accepted", watch.admin, wg=default)
    own_hidden = add(watch, "operation.failed", watch.member, wg=hidden)
    own_org = add(watch, "task.create", watch.member)
    foreign_hidden = add(watch, "hook.denied", watch.own, wg=hidden)
    foreign_org = add(watch, "task.close", watch.admin)
    found = seqs(rows(watch, watch.member))
    assert {readable, own_hidden, own_org} <= set(found)
    assert foreign_hidden not in found
    assert foreign_org not in found
    # the member's read itself is recorded before its answer (ADR-063 section 3): actions only here
    actions = [row for row in rows(watch, watch.admin) if not row["action"].startswith("read.")]
    assert seqs(actions)[:5] == [foreign_org, foreign_hidden, own_org, own_hidden, readable]


def test_a_revoked_grant_hides_the_workgroup_rows(watch: Watch) -> None:
    readable = add(watch, "operation.accepted", watch.admin, wg=watch.mine.default_wg)
    assert readable in seqs(rows(watch, watch.member))
    watch.desk.execute(
        "UPDATE hub.wg_access SET revoked_seq = 99 WHERE wg_id = %s AND user_id = %s",
        watch.mine.default_wg,
        watch.member.id,
    )
    assert readable not in seqs(rows(watch, watch.member))


def test_filters(watch: Watch) -> None:
    default = watch.mine.default_wg
    task, session = watch.session(watch.own, default)
    accepted = add(watch, "operation.accepted", watch.own, wg=default, task=task, session=session)
    failed = add(watch, "operation.failed", watch.member, wg=watch.hidden)
    other_task = add(watch, "task.create", watch.member, wg=default, task=new_id())
    assert seqs(rows(watch, watch.admin, action="operation.accepted")) == [accepted]
    assert seqs(rows(watch, watch.admin, action="operation")) == [failed, accepted]
    assert seqs(rows(watch, watch.admin, action="operation.accept")) == []
    assert seqs(rows(watch, watch.admin, action="operation.accepted.more")) == []
    assert seqs(rows(watch, watch.admin, actor=watch.member.id, action="operation")) == [failed]
    assert seqs(rows(watch, watch.admin, task=task)) == [accepted]
    assert seqs(rows(watch, watch.admin, session=session)) == [accepted]
    assert seqs(rows(watch, watch.admin, wg=watch.hidden, action="operation")) == [failed]
    assert seqs(rows(watch, watch.member, wg=default, action="task")) == [other_task]
    assert seqs(rows(watch, watch.member, wg=watch.hidden)) == [failed]


def test_paging(watch: Watch) -> None:
    added = [add(watch, "operation.accepted", watch.admin, wg=watch.mine.default_wg) for _ in range(5)]
    client = watch.admin.browser
    path = f"/v1/orgs/{watch.mine.id}/audit"
    first = client.get(path, params={"limit": "2", "action": "operation"}).json()
    assert seqs(first["rows"]) == added[:2:-1]
    second = client.get(path, params={"limit": "2", "action": "operation", "before": first["next_before"]}).json()
    assert seqs(second["rows"]) == added[2:0:-1]
    third = client.get(path, params={"limit": "2", "action": "operation", "before": second["next_before"]}).json()
    assert (seqs(third["rows"]), third["next_before"]) == ([added[0]], None)
    default = client.get(path).json()
    assert len(default["rows"]) == 50 or default["next_before"] is None


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({"before": "abc"}, "/before"),
        ({"action": "Operation"}, "/action"),
        ({"action": "a" * 97}, "/action"),
        ({"actor": "nope"}, "/actor"),
        ({"task": "01930014-0000-7000-8000-00000000001A"}, "/task"),
        ({"session": "1"}, "/session"),
        ({"wg": ""}, "/wg"),
    ],
)
def test_invalid_queries(watch: Watch, query: dict[str, str], pointer: str) -> None:
    response = watch.admin.browser.get(f"/v1/orgs/{watch.mine.id}/audit", params=query, extensions={"contract": False})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": [pointer]}},
    )


def test_limits_and_refusals(watch: Watch) -> None:
    for limit in ("0", "201", "01"):
        response = watch.admin.browser.get(
            f"/v1/orgs/{watch.mine.id}/audit", params={"limit": limit}, extensions={"contract": False}
        )
        assert response.status_code == 400
    for org in (watch.mine.id, new_id(), "nope"):
        response = watch.other.founder.browser.get(f"/v1/orgs/{org}/audit")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    watch.desk.execute(
        "UPDATE hub.org_member SET removed_seq = 99 WHERE org_id = %s AND principal_id = %s",
        watch.mine.id,
        watch.member.id,
    )
    assert watch.member.browser.get(f"/v1/orgs/{watch.mine.id}/audit").status_code == 404


@pytest.mark.parametrize(
    "text",
    [
        encode_before(0),
        encode_before(-3),
        "eyJzZXEiOnRydWV9",  # {"seq":true}
        "eyJzZXEiOiIxIn0",  # {"seq":"1"}
        "eyJzZXEiOjEuNX0",  # {"seq":1.5}
        "eyAic2VxIjogMX0",  # { "seq": 1} (not canonical)
        "WzFd",  # [1]
        "e30",  # {}
        "!!",
    ],
)
def test_cursors(text: str) -> None:
    with pytest.raises(HubError) as raised:
        decode_before(text)
    assert raised.value.code == "request_invalid"
    assert decode_before(encode_before(17)) == 17


def test_rows_are_logged_as_reads(watch: Watch) -> None:
    rows(watch, watch.member)
    watch.hub.flush_read_log()
    found = rows(watch, watch.admin, action="read.org.audit", actor=watch.member.id)
    assert [(row["result"], row["target_kind"], row["target_id"]) for row in found] == [("ok", "org", watch.mine.id)]

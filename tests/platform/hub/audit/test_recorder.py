"""U-HUB-audit: the rows a request appended (`agent_hub.audit.recorder`) and the placement of request rows
(`agent_hub.audit.placement`, ADR-063 sections 1 and 3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.placement import place
from agent_hub.audit.recorder import RECORDER, Noted, committed, deferred, recording, unchanged
from tests.platform.hub.conftest import FIXED_NOW, uuid7
from tests.platform.hub.core.test_read_log import seed_scope


def row(actor: UUID | None = None, *, org_id: UUID | None = None) -> AuditRow:
    return AuditRow(
        at=FIXED_NOW, actor_kind="user", actor_id=actor or uuid7(), action="agent.pairing", result="ok", org_id=org_id
    )


def test_outside_a_request_nothing_is_noted(writer: psycopg.Connection[Any]) -> None:
    assert RECORDER.get() is None
    append_audit(writer, row())
    unchanged()
    deferred()
    assert RECORDER.get() is None


def test_a_recording_notes_rows_and_marks(writer: psycopg.Connection[Any]) -> None:
    with recording() as found:
        stored = append_audit(writer, row())
        unchanged()
        deferred()
    assert found.rows == [Noted(seq=stored.seq, hash=stored.hash, result="ok")]
    assert (found.unchanged, found.deferred) == (True, True)
    assert RECORDER.get() is None


def rolled_back_append(writer: psycopg.Connection[Any]) -> None:
    with writer.transaction():
        append_audit(writer, row())
        raise RuntimeError("rolled back")


def test_committed_keeps_only_stored_rows_with_their_hash(writer: psycopg.Connection[Any]) -> None:
    assert committed(writer, []) == []
    with recording() as found:
        kept = append_audit(writer, row())
        with pytest.raises(RuntimeError):
            rolled_back_append(writer)
    rolled_back = found.rows[1]
    # a later row takes the rolled-back seq, with another hash
    later = append_audit(writer, row())
    assert later.seq == rolled_back.seq
    assert committed(writer, found.rows) == [Noted(seq=kept.seq, hash=kept.hash, result="ok")]


def member(connection: psycopg.Connection[Any], org: UUID, principal: UUID) -> None:
    connection.execute(
        "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq)"
        " VALUES (%s, %s, 'user', 'member', 1)",
        (org, principal),
    )


def refused(
    actor: UUID,
    *,
    org_id: UUID | None = None,
    wg_id: UUID | None = None,
    task_id: UUID | None = None,
    session_id: UUID | None = None,
) -> AuditRow:
    return AuditRow(
        at=FIXED_NOW,
        actor_kind="user",
        actor_id=actor,
        action="request.refused",
        result="denied",
        org_id=org_id,
        wg_id=wg_id,
        task_id=task_id,
        session_id=session_id,
    )


def scope(found: AuditRow) -> tuple[UUID | None, ...]:
    return found.org_id, found.wg_id, found.task_id, found.session_id


def test_a_refusal_keeps_the_ids_of_the_actors_organization(writer: psycopg.Connection[Any]) -> None:
    org, wg, task, session, _ = seed_scope(writer)
    actor = uuid7()
    member(writer, org, actor)
    assert scope(place(writer, refused(actor, task_id=task))) == (None, None, task, None)
    assert scope(place(writer, refused(actor, org_id=org))) == (org, None, None, None)
    assert scope(place(writer, refused(actor, org_id=org, session_id=session))) == (org, None, None, session)
    assert scope(place(writer, refused(actor, wg_id=wg))) == (None, wg, None, None)
    # nothing named: nothing to confine
    assert scope(place(writer, refused(actor))) == (None, None, None, None)


def test_a_refusal_of_a_stranger_names_no_organization(writer: psycopg.Connection[Any]) -> None:
    org, wg, task, _session, _ = seed_scope(writer)
    other_org, *_ = seed_scope(writer)
    stranger, insider = uuid7(), uuid7()
    member(writer, other_org, insider)
    cleared = (None, None, None, None)
    assert scope(place(writer, refused(stranger, task_id=task))) == cleared
    assert scope(place(writer, refused(stranger, org_id=org))) == cleared
    # an ID of an unknown object or one that contradicts the hub's row is no proof of the organization
    assert scope(place(writer, refused(insider, wg_id=uuid7()))) == cleared
    assert scope(place(writer, refused(insider, org_id=other_org, wg_id=wg))) == cleared
    # a removed member is a stranger
    writer.execute("UPDATE hub.org_member SET removed_seq = 2 WHERE principal_id = %s", (insider,))
    assert scope(place(writer, refused(insider, org_id=other_org))) == cleared

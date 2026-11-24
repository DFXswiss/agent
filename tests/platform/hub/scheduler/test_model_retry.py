"""U-HUB-scheduler (model-access continuation, KOR-49, ADR-077 section 4, rule E-05): a main session that failed
`model_access_unavailable` gets the hub's next attempt once its owner stores a model credential - in the transaction
that stores the credential, or at once when the credential arrived while the start ran - which takes over the
undelivered prompts exactly as the capacity retry does; nothing else starts an attempt."""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

import pytest

from agent_hub.crypto.ids import uuid7
from agent_hub.sessions import model_retry
from agent_hub.sessions.effects import Effects
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.scheduler.test_capacity_refusal import first_prompt, mains, pending
from tests.platform.hub.tasks.support import Desk, hints

REASON = "model_access_unavailable"


def _credential(desk: Desk, owner: str, kind: str, at: datetime) -> None:
    """A stored credential row of the owner (the record's content does not matter to the hub's continuation)."""
    desk.execute(
        "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at)"
        " VALUES (%s, %s, %s, 1, 1, '{}', %s, %s, %s) ON CONFLICT (owner_id, kind) DO UPDATE SET updated_at = %s",
        uuid7(),
        owner,
        kind,
        b"ct",
        secrets.token_bytes(64),
        at,
        at,
    )


def _store(desk: Desk, owner: str, kind: str) -> int:
    """What `PUT /v1/me/credentials/{kind}` does after storing the record: the continuation, then the effects."""
    now = desk.hub.clock.advance(1)
    _credential(desk, owner, kind, now)
    effects = Effects()
    with desk.hub.pool.connection() as connection, connection.transaction():
        started = model_retry.after_credential(connection, UUID(owner), kind, now, effects)
    effects.flush()
    return started


def _failed(desk: Desk) -> tuple[str, str]:
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    desk.hub.clock.advance(1)
    desk.transition(first, "failed", REASON)
    assert (desk.task(task)["state"], desk.task(task)["state_reason"]) == ("failed", REASON)
    return task, first


@pytest.mark.parametrize("kind", ["claude_subscription", "anthropic_api_key"])
def test_a_stored_model_credential_continues_the_task(desk: Desk, published: MemoryPublisher, kind: str) -> None:
    task, first = _failed(desk)
    published.clear()
    assert _store(desk, desk.scene.writer.id, kind) == 1
    found = mains(desk, task)
    assert [(state, reason) for _, state, _, reason in found] == [("failed", None), ("requested", REASON)]
    second = found[1][0]
    assert pending(desk, task) == {first: [first_prompt(desk, task)], second: [first_prompt(desk, task)]}
    assert hints(published, kind="session.start") == [{"type": "session.start", "session_id": second}]
    assert desk.task(task)["state"] == "starting"
    assert _store(desk, desk.scene.writer.id, kind) == 0  # the task is no longer failed: nothing more


def test_other_credentials_and_other_owners_start_nothing(desk: Desk) -> None:
    task, _first = _failed(desk)
    assert _store(desk, desk.scene.writer.id, "github") == 0
    assert _store(desk, desk.scene.reader.id, "claude_subscription") == 0
    assert len(mains(desk, task)) == 1


def test_a_credential_that_arrived_while_the_start_ran_continues_at_once(desk: Desk) -> None:
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    _credential(desk, desk.scene.writer.id, "claude_subscription", desk.hub.clock.advance(1))
    desk.hub.clock.advance(1)
    desk.transition(first, "failed", REASON)
    found = mains(desk, task)
    assert [(state, reason) for _, state, _, reason in found] == [("failed", None), ("requested", REASON)]
    assert desk.task(task)["state"] == "starting"


def test_an_older_credential_waits_for_the_next_write(desk: Desk) -> None:
    """A credential stored before the session started is the one the device could not use: no loop."""
    _credential(desk, desk.scene.writer.id, "claude_subscription", desk.hub.clock())
    desk.hub.clock.advance(1)
    task, _first = _failed(desk)
    assert len(mains(desk, task)) == 1


def test_a_failure_without_pending_prompts_or_a_closed_task_starts_nothing(desk: Desk) -> None:
    task, first = _failed(desk)
    # the prompt counts as delivered (a transition into `working`): nothing is pending, so there is no attempt
    desk.execute(
        "INSERT INTO hub.session_transition (session_id, seq, state, at, received_at)"
        " SELECT %s, max(seq) + 1, 'working', now(), now() FROM hub.session_transition WHERE session_id = %s",
        first,
        first,
    )
    assert _store(desk, desk.scene.writer.id, "claude_subscription") == 0
    desk.execute("UPDATE hub.task SET state = 'closed' WHERE id = %s", task)
    assert _store(desk, desk.scene.writer.id, "anthropic_api_key") == 0
    assert len(mains(desk, task)) == 1

"""U-HUB-events (signals): the notification payload carries identifiers only, `parse` refuses anything else, the
signal leaves out text that is no identifier, and replays announce nothing (03-api.md 4.4, ADR-027 section 4)."""

from __future__ import annotations

import json
import uuid

import pytest

from agent_hub.events import notify
from agent_hub.events.store import StoredEvent
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import new_id


def _stored(**changes: object) -> StoredEvent:
    values: dict[str, object] = {
        "id": new_id(),
        "org_id": new_id(),
        "scope": "wg",
        "scope_id": new_id(),
        "scope_seq": 3,
        "kind": "platform.read",
        "author_id": new_id(),
        "event": {"header": {}, "ct": "", "sig": ""},
        "replayed": False,
        "wg_id": None,
        "ref": "repo:org/name",
    }
    values.update(changes)
    return StoredEvent(**values)  # type: ignore[arg-type]


def test_payload_holds_only_identifiers_order_kind_and_session() -> None:
    stored = _stored()
    assert json.loads(notify.payload(stored)) == {
        "org_id": stored.org_id,
        "scope": "wg",
        "scope_id": stored.scope_id,
        "scope_seq": 3,
        "kind": "platform.read",
        "session": None,
    }
    session = new_id()
    parsed = notify.parse(notify.payload(_stored(kind="browser.action", session_id=session)))
    assert (parsed.kind, parsed.session_id) == ("browser.action", uuid.UUID(session))
    assert notify.parse(notify.payload(stored)).session_id is None


def _payload(**changes: object) -> str:
    body: dict[str, object] = {
        "org_id": new_id(),
        "scope": "org",
        "scope_id": new_id(),
        "scope_seq": 1,
        "kind": "org.profile",
        "session": None,
    }
    body.update(changes)
    return json.dumps(body)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '{"org_id": "x"}',
        json.dumps({"org_id": new_id(), "scope": "org", "scope_id": new_id(), "scope_seq": 1}),
        _payload(scope="cred"),
        _payload(scope_seq=0),
        _payload(scope_seq=True),
        _payload(scope_seq="1"),
        _payload(org_id=new_id().upper()),
        _payload(session=new_id().upper()),
        json.dumps({"org_id": new_id(), "scope": "org", "scope_id": new_id(), "scope_seq": 1, "kind": "prompt"}),
        _payload(kind="Prompt"),
        _payload(kind=1),
        _payload(session="x"),
        _payload(extra=1),
    ],
)
def test_parse_refuses_other_payloads(text: str) -> None:
    with pytest.raises(ValueError, match=r"notification|UUID|badly formed"):
        notify.parse(text)


def test_signal_ids_leave_out_text_that_is_no_identifier() -> None:
    stored = _stored(session_id=new_id(), task_id=new_id(), wg_id=new_id())
    ids = notify.signal_ids(stored)
    assert "ref" not in ids
    assert ids["session_id"] == uuid.UUID(stored.session_id or "")
    assert notify.signal_ids(_stored(ref=""))["notify"] == 0
    assert "ref" not in notify.signal_ids(_stored(ref=""))


def test_replays_announce_nothing(emitted: list[object], published: MemoryPublisher) -> None:
    notify.announce([_stored(replayed=True)])
    assert (emitted, published.items()) == ([], [])

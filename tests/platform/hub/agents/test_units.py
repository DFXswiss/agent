"""U-HUB-agents (guards): the helpers that take the agent of a principal accept only an agent principal with a UUID
`agent_id` (what the resolver of `agent_hub.agents.keys` builds) and refuse or ignore anything else, so a principal
of another kind can never be mistaken for an agent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_hub.agents import notify, routes
from agent_hub.auth.keys import Principal
from agent_hub.errors import HubError
from agent_hub.http.pipeline import acting_agent
from agent_hub.http.readlog import read_row

AGENT = uuid.UUID("01930050-0000-7000-8000-000000000050")
USER = uuid.UUID("01930051-0000-7000-8000-000000000051")


def principal(kind: Any, **attributes: object) -> Principal:
    return Principal(
        kind=kind, key_id=f"{kind}." + "0" * 32, principal_id=USER, actor_kind="user", attributes=attributes
    )


def test_only_an_agent_principal_with_a_uuid_is_an_agent() -> None:
    proper = principal("agent", agent_id=AGENT)
    assert acting_agent(proper) == AGENT
    assert acting_agent(principal("agent", agent_id=str(AGENT))) is None
    assert acting_agent(principal("session", agent_id=AGENT)) is None
    assert routes._agent_id(proper) == AGENT
    for wrong in (principal("agent", agent_id="x"), principal("session", agent_id=AGENT)):
        with pytest.raises(HubError) as refused:
            routes._agent_id(wrong)
        assert refused.value.code == "forbidden"


def test_only_sessions_and_agents_of_a_user_manage_agents() -> None:
    assert routes._session_user(principal("session")) == USER
    assert routes._session_user(principal("agent", agent_id=AGENT)) == USER
    for wrong in (
        principal("device"),
        Principal(kind="session", key_id=None, principal_id=USER, actor_kind="user"),
        Principal(kind="agent", key_id="agent." + "0" * 32, principal_id=None, actor_kind="user"),
    ):
        with pytest.raises(HubError) as refused:
            routes._session_user(wrong)
        assert refused.value.code == "forbidden"


def test_the_read_row_names_only_a_real_agent() -> None:
    at = datetime(2026, 11, 21, tzinfo=UTC)
    common: dict[str, Any] = {"resource": "me", "status": 200, "path_params": {}, "query": {}, "at": at}
    assert read_row(principal("agent", agent_id=AGENT), **common).agent_id == AGENT
    assert read_row(principal("agent", agent_id="x"), **common).agent_id is None
    assert read_row(principal("session", agent_id=AGENT), **common).agent_id is None


def test_an_unknown_account_change_or_target_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown account change"):
        notify.account_change(None, USER, "agent_revoked", datetime.now(UTC), target="a b", agent_id=AGENT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown account change"):
        notify.account_change(None, USER, "nothing", datetime.now(UTC), agent_id=AGENT)  # type: ignore[arg-type]
    assert notify.account_change(None, USER, "nothing", datetime.now(UTC)) == []  # type: ignore[arg-type]


def test_only_a_browser_session_or_an_agent_confirms_a_pairing() -> None:
    """`POST /v1/me/agents` is for a browser session or an agent (KOR-62: `store.confirm` decides for the agent);
    the handler refuses any other principal and one without a key ID before it touches the database."""
    body = routes.AgentConfirm.model_validate(
        {"pairing_id": str(AGENT), "bundle_ct": "A" * 64, "label_ct": "A" * 64, "claim_hash": "A" * 43}
    )
    keyless = Principal(kind="session", key_id=None, principal_id=USER, actor_kind="user")
    for who in (principal("device"), keyless):
        with pytest.raises(HubError) as refused:
            routes._confirm(None, who, body)  # type: ignore[arg-type]
        assert refused.value.code == "forbidden"

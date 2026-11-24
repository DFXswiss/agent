"""Test support of the own-identity agent (ADR-082): the browser's confirmation batches, the agent as a log actor and
a confirmed and claimed own-identity agent of a scene user.

`agent_actor(agent)` is the agent as an `Actor` of the org drafts (kind `agent`, its own keys, a client signing with
its key). `add_batch(draft, user, agent, ...)` appends `agent_add` by the user to the draft (pending) and returns the
batch of the confirmation with the wraps of every epoch of the draft the user holds, wrapped to the agent.
`own_agent(hub, user, drafts)` pairs, confirms (passkey gesture) and claims an own-identity agent of `user` that is in
every given org.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_hub.crypto import agent as agent_format
from tests.platform.hub.agents.support import (
    Handover,
    TestAgent,
    certificate,
    gesture_client,
    handover,
    new_agent,
)
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import Actor, OrgDraft

Json = dict[str, Any]


def paired(hub: HubHarness) -> TestAgent:
    """An open loopback pairing."""
    agent = new_agent(hub)
    response = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body())
    assert response.status_code == 200, response.text
    return agent


def agent_actor(hub: HubHarness, agent: TestAgent) -> Actor:
    """The agent as an actor of the drafts (wraps to it, entries and events by it)."""
    return Actor(str(agent.agent_id), "agent", agent.identity, agent.client(hub))


def readable(draft: OrgDraft) -> list[tuple[str, str, int]]:
    """The slots `(scope, scope_id, epoch)` of every key of the draft (its founder reads all of them)."""
    return sorted(draft.keys)


def add_batch(
    draft: OrgDraft,
    user: Actor,
    agent: TestAgent,
    *,
    gen: int = 1,
    cert: str | None = None,
    slots: Sequence[tuple[str, str, int]] | None = None,
    signer: Any = None,
) -> Json:
    """`agent_add` by the user (pending in the draft) and the batch with the agent's wraps."""
    hub = draft.world.hub
    draft.add(
        "agent_add",
        user,
        signer=signer,
        subject=str(agent.agent_id),
        user=user.id,
        pub=agent.identity.public(),
        gen=gen,
        cert=cert or certificate(agent, user.id, user.identity, gen),
    )
    recipient = agent_actor(hub, agent)
    wraps = [
        draft.wrap(user, scope, scope_id, epoch, recipient) for scope, scope_id, epoch in (slots or readable(draft))
    ]
    return {"org": draft.id, "expected_seq": draft.submitted, "entries": draft.pending(), "wraps": wraps}


def submitted(*drafts: OrgDraft) -> None:
    """The pending entries of the drafts were stored by a confirmation."""
    for draft in drafts:
        draft.submitted = len(draft.entries)


@dataclass
class OwnAgent:
    """A claimed own-identity agent of `user` and what its confirmation sent."""

    agent: TestAgent
    user: Actor
    actor: Actor
    confirm: Handover


def confirm_body(
    hub: HubHarness, agent: TestAgent, user: Actor, drafts: Sequence[OrgDraft], *, gen: int = 1, **options: Any
) -> Handover:
    """The new-form confirmation of `agent` for `user` with one batch per draft."""
    batches = [add_batch(draft, user, agent, gen=gen) for draft in drafts]
    return handover(agent, user.id, user.identity, gen=gen, batches=batches, **options)


def own_agent(hub: HubHarness, user: Actor, drafts: Sequence[OrgDraft], *, gen: int = 1) -> OwnAgent:
    """Pair, confirm and claim an own-identity agent of `user` that is added to every draft's log."""
    agent = paired(hub)
    confirm = confirm_body(hub, agent, user, drafts, gen=gen)
    response = gesture_client(hub, user_uuid(user), agent.agent_id).post("/v1/me/agents", json=confirm.body)
    assert response.status_code == 200, response.text
    submitted(*drafts)
    agent.user_id = user_uuid(user)
    claimed = agent.client(hub).post("/v1/agent/claim", json=confirm.claim_body)
    assert claimed.status_code == 200, claimed.text
    opened = agent_format.open_user(
        agent.identity.kem, confirm.secret, str(agent.agent_id), user.id, claimed.json()["user_ct"]
    )
    assert opened["sig"] == user.identity.public()["sig"]
    return OwnAgent(agent, user, agent_actor(hub, agent), confirm)


def user_uuid(user: Actor) -> uuid.UUID:
    return uuid.UUID(user.id)

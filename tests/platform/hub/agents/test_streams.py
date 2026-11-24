"""U-HUB-agents (live stream, 11-agent-access.md sections 5.3 and 8, decisions KOR-24): an active agent follows its
user's live stream; revoking the agent (or ending a browser session) ends its open streams at once; account
notifications reach only streams opened with `account=1`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest

from agent_hub.notifications.rules import publish_rows, row
from agent_hub.stream.publish import MemoryPublisher, close_keys
from tests.platform.hub.agents.support import stored_agent
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.orgs.support import World
from tests.platform.hub.stream.support import LiveHub, Refused, browser_key, free_port, live_hub

LIVE_PORT_OFFSET = 18


@pytest.fixture
def live(hub: HubHarness) -> Iterator[LiveHub]:
    with live_hub(hub, free_port(LIVE_PORT_OFFSET)) as served:
        yield served


def test_revoking_an_agent_ends_its_streams_but_not_the_users(hub: HubHarness, live: LiveHub) -> None:
    world = World(hub)
    founder = world.user()
    draft = world.public_org(founder)
    agent = stored_agent(hub, uuid.UUID(founder.id))
    agent_stream = live.stream(agent.signing_key(), org=draft.id)
    browser_stream = live.stream(browser_key(founder), org=draft.id)
    for stream in (agent_stream, browser_stream):
        hello = stream.frame()
        assert hello is not None
        assert hello.event == "hello"
    assert founder.browser.delete(f"/v1/me/agents/{agent.agent_id}").json() == {}
    assert agent_stream.ended()
    assert browser_stream.quiet() == []
    assert not browser_stream.ended(0.2)
    # a new stream of the revoked agent is refused
    with pytest.raises(Refused) as refused:
        live.stream(agent.signing_key(), org=draft.id)
    assert (refused.value.status, refused.value.body) == (401, {"error": "key_revoked"})
    # the browser session's end ends its stream; a new main orchestrator ends its own by revoking all
    assert founder.browser.post("/v1/session/end", json={}).status_code == 200
    assert browser_stream.ended()
    successor = stored_agent(hub, uuid.UUID(founder.id))
    successor_stream = live.stream(successor.signing_key(), org=draft.id)
    hello = successor_stream.frame()
    assert hello is not None
    assert successor.client(hub).delete("/v1/me/agents").json() == {"revoked": 1}
    assert successor_stream.ended()


def test_account_notifications_reach_only_streams_with_the_account_flag(hub: HubHarness, live: LiveHub) -> None:
    world = World(hub)
    founder = world.user()
    draft = world.public_org(founder)
    agent = stored_agent(hub, uuid.UUID(founder.id))
    plain = live.stream(browser_key(founder), org=draft.id)
    flagged = live.stream(agent.signing_key(), org=draft.id, account="1")
    for stream in (plain, flagged):
        assert stream.frame() is not None
    now = hub.clock()
    account_row = row(
        (uuid7(now), uuid.UUID(founder.id), "agent.account_change", None, None, None, "agent_revoked::", now, None)
    )
    org_row = replace(account_row, id=uuid7(now), kind="task.idle", org_id=uuid.UUID(draft.id), ref=None)
    publish_rows([account_row, org_row])
    received = flagged.frames(2)
    assert sorted(frame.data["kind"] for frame in received) == ["agent.account_change", "task.idle"]
    only = plain.frame()
    assert only is not None
    assert (only.event, only.data["kind"]) == ("notification", "task.idle")
    assert plain.quiet() == []
    with pytest.raises(Refused) as refused:
        live.stream(browser_key(founder), org=draft.id, account="yes")
    assert refused.value.status == 400


def test_closing_no_keys_publishes_nothing(published: MemoryPublisher) -> None:
    close_keys(uuid.uuid4(), [])
    assert published.items() == []

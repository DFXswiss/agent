"""Fixtures of the org-invitation suites: the world, the fake browser, recorded frames and fresh caches."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from agent_hub.device import signals as device_signals
from agent_hub.invites import signals as invite_signals
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.stream import publish
from tests.platform.hub.auth.authenticator import FakeBrowser
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.invites.support import Invitation, accept, create_invitation, registered_actor
from tests.platform.hub.orgs.support import Actor, OrgDraft, World


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    """No cached logs and no bound hub context from another test."""
    for cache in (logs.ORG_LOGS, logs.PLATFORM_LOGS, platform_log.CACHE):
        cache.clear()
    invite_signals._context.clear()
    device_signals._context.clear()
    yield
    invite_signals.wait()
    device_signals.wait()
    invite_signals._context.clear()
    device_signals._context.clear()


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def fleet(world: World) -> Fleet:
    return Fleet(world)


@pytest.fixture
def browser(hub: HubHarness) -> FakeBrowser:
    return FakeBrowser(hub)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


AcceptFlow = Callable[[OrgDraft], tuple[Invitation, Actor]]


@pytest.fixture
def browser_accept(browser: FakeBrowser) -> AcceptFlow:
    """A new user is invited (API), registers through the invitation, logs in and accepts (04-crypto.md 5.1, 5.2)."""

    def run(draft: OrgDraft) -> tuple[Invitation, Actor]:
        invitation = create_invitation(draft)
        assert invitation.response.status_code == 200, invitation.response.text
        actor, _account = registered_actor(browser, invitation.invite_id)
        response = accept(draft, actor, invitation)
        assert response.status_code == 200, response.text
        return invitation, actor

    return run

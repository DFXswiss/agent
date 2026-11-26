"""Fixtures of the support suite (ADR-096): the tasks desk (an org with admin, writer, reader, two devices and the
workgroup `wg`) plus a platform log written through the test hub with the test root key, in which support persons get
their certification and the role `support`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from agent_hub import signals
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.keys import ed25519_public_raw
from agent_hub.log.entries import entry_hash, sign_platform_entry
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness, root_private_key
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.hub.tasks.support import Desk

Json = dict[str, Any]


@dataclass
class Roots:
    """The platform log of the test hub (support roles)."""

    hub: HubHarness
    entries: list[Json] = field(default_factory=list)

    def post(self, *bodies: Json) -> httpx.Response:
        root = root_private_key()
        if not self.entries and bodies[0]["type"] != "root":
            bodies = ({"type": "root", "pub": b64u_encode(ed25519_public_raw(root))}, *bodies)
        chained: list[Json] = []
        prev = entry_hash(self.entries[-1]) if self.entries else ""
        for offset, body in enumerate(bodies):
            full = {"seq": len(self.entries) + offset, "prev": prev, "by": "platform-root", "v": 1}
            entry = sign_platform_entry({**full, "ts": format_time(self.hub.clock()), **body}, root)
            chained.append(entry)
            prev = entry_hash(entry)
        response = self.hub.root.post("/v1/platform/log", json={"expected_seq": len(self.entries), "entries": chained})
        if response.status_code == 200:
            self.entries.extend(chained)
        return response

    def ready(self) -> None:
        """Every device announced the features the support release needs (ADR-060, ADR-096)."""
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.device SET features = features || ARRAY['log.admit', 'log.support']")

    def support(self, person: Actor, name: str | None = None) -> None:
        """Certify the person's identity and give it the role `support` (and a display name)."""
        self.ready()
        response = self.post(
            {"type": "user_certify", "user": person.id, "pub": person.pub},
            {"type": "role_grant", "subject": person.id, "role": "support"},
        )
        assert response.status_code == 200, response.text
        if name is not None:
            named = self.hub.root.put(f"/v1/platform/support/{person.id}", json={"name": name})
            assert named.status_code == 200, named.text

    def leave(self, person: Actor) -> None:
        response = self.post({"type": "role_revoke", "subject": person.id, "role": "support"})
        assert response.status_code == 200, response.text


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    for cache in (logs.ORG_LOGS, logs.PLATFORM_LOGS, platform_log.CACHE):
        cache.clear()
    signals.configure()
    yield
    signals.configure()


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def desk(world: World) -> Desk:
    found = Desk.build(world)
    found.online(found.scene.device)
    return found


@pytest.fixture
def roots(hub: HubHarness) -> Roots:
    return Roots(hub)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)

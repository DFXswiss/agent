"""Fixtures of the community suite (KOR-26, 12-community.md): members with real identities on the running test hub.

A `Member` is a user with a registered identity and a browser client that does what a web client does: join, create
the first epoch or request the key, serve requests of others, open its own request wrap, post sealed messages.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_hub.crypto import community as fmt
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.keys import x25519_public_raw
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness, SignedClient
from tests.platform.hub.orgs.support import Actor, World, new_id

Json = dict[str, Any]


@dataclass
class Member:
    """A user of the community (module docstring)."""

    hub: HubHarness
    actor: Actor
    key: bytes | None = None
    request_key: X25519PrivateKey | None = None
    request_id: str | None = None
    sent: list[Json] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.actor.id

    @property
    def client(self) -> SignedClient:
        return self.actor.browser

    def get(self, path: str, **params: str) -> httpx.Response:
        return self.client.get(path, params=params or None)

    def state(self) -> Json:
        response = self.client.get("/v1/community")
        assert response.status_code == 200, response.text
        body: Json = response.json()
        return body

    def join(self) -> None:
        response = self.client.post("/v1/community/join", json={})
        assert response.status_code == 200, response.text

    def self_wrap(self, key: bytes, epoch: int = 1, recipient: str | None = None) -> Json:
        wrap_text = fmt.wrap_community_key(
            self.actor.identity.kem_public(), epoch=epoch, recipient=recipient or self.id, key=key
        )
        sig = fmt.sign_community_wrap(
            self.actor.identity.sig, epoch=epoch, recipient=recipient or self.id, wrap_text=wrap_text
        )
        return {"epoch": epoch, "wrap": wrap_text, "sig": sig}

    def create_epoch(self, key: bytes | None = None) -> httpx.Response:
        key = key or secrets.token_bytes(32)
        body = {**self.self_wrap(key), "key_check": b64u_encode(fmt.key_check(key, 1))}
        response = self.client.post("/v1/community/epochs", json=body)
        if response.status_code == 200:
            self.key = key
        return response

    def request(self) -> httpx.Response:
        self.request_key = X25519PrivateKey.generate()
        self.request_id = new_id()
        kem = b64u_encode(x25519_public_raw(self.request_key))
        sig = fmt.sign_request(self.actor.identity.sig, id=self.request_id, kem_pub=kem, user=self.id)
        return self.client.put("/v1/community/request", json={"id": self.request_id, "kem_pub": kem, "sig": sig})

    def serve(self, key: bytes | None = None) -> httpx.Response:
        """Wrap the key (or `key`) for every open request this holder is offered."""
        listed = self.client.get("/v1/community/requests")
        assert listed.status_code == 200, listed.text
        wraps = []
        for item in listed.json()["requests"]:
            wrap_text = fmt.wrap_community_key(
                b64u_decode(item["kem_pub"], 32), epoch=1, recipient=item["id"], key=key or self.key or b""
            )
            sig = fmt.sign_community_wrap(self.actor.identity.sig, epoch=1, recipient=item["id"], wrap_text=wrap_text)
            wraps.append({"request": item["id"], "epoch": 1, "wrap": wrap_text, "sig": sig})
        if not wraps:
            return listed
        return self.client.post("/v1/community/requests/wraps", json={"wraps": wraps})

    def open_request(self) -> bytes:
        """Open the wrapped request, check the key and store the self-wrap (the web client's step)."""
        state = self.state()
        wrapped = state["request"]["wrapped"]
        assert self.request_key is not None
        assert wrapped is not None
        key = fmt.unwrap_community_key(
            self.request_key, epoch=1, recipient=state["request"]["id"], wrap_text=wrapped["wrap"]
        )
        fmt.verify_community_wrap(
            b64u_decode(wrapped["wrapper_sig"], 32),
            epoch=1,
            recipient=state["request"]["id"],
            wrap_text=wrapped["wrap"],
            signature=wrapped["sig"],
        )
        assert b64u_encode(fmt.key_check(key, 1)) == state["epoch"]["key_check"]
        response = self.client.post("/v1/community/wraps", json=self.self_wrap(key))
        assert response.status_code == 200, response.text
        self.key = key
        return key

    def message(
        self,
        text: str = "Hallo zusammen",
        name: str = "Anna",
        *,
        key: bytes | None = None,
        epoch: int = 1,
        author: str | None = None,
        message_id: str | None = None,
    ) -> Json:
        header = fmt.new_header(
            id=message_id or new_id(), author=author or self.id, epoch=epoch, ts=format_time(self.hub.clock())
        )
        return fmt.seal_message(
            key or self.key or secrets.token_bytes(32), header, fmt.body_bytes(name, text), self.actor.identity.sig
        )

    def post(self, message: Json | None = None, **options: Any) -> httpx.Response:
        sealed = message or self.message(**options)
        self.sent.append(sealed)
        return self.client.post("/v1/community/messages", json={"message": sealed})


class Community:
    """Members on one hub."""

    def __init__(self, hub: HubHarness) -> None:
        self.hub = hub
        self.world = World(hub)

    def member(self, *, join: bool = True) -> Member:
        member = Member(self.hub, self.world.user())
        if join:
            member.join()
        return member

    def founder(self) -> Member:
        member = self.member()
        response = member.create_epoch()
        assert response.status_code == 200, response.text
        return member

    def holder(self, founder: Member) -> Member:
        """A member who got the key through a request served by `founder`."""
        member = self.member()
        assert member.request().status_code == 200
        served = founder.serve()
        assert served.status_code == 200, served.text
        member.open_request()
        return member

    def moderator(self, member: Member) -> None:
        response = self.hub.root.post("/v1/community/moderators", json={"user": member.id, "granted": True})
        assert response.status_code == 200, response.text


@pytest.fixture
def community(hub: HubHarness) -> Community:
    return Community(hub)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


@contextmanager
def unchecked(hub: HubHarness) -> Iterator[None]:
    """Requests that break the contract on purpose (the hub's own refusal is under test)."""
    hooks = hub.base.event_hooks
    hub.base.event_hooks = {"request": [], "response": hooks["response"]}
    try:
        yield
    finally:
        hub.base.event_hooks = hooks


def frames(recorder: publish.MemoryPublisher) -> list[tuple[str, str, Json]]:
    """`(audience kind, audience id, data)` of every `community` frame."""
    return [(i.audience.kind, str(i.audience.id), dict(i.data)) for i in recorder.items() if i.frame == "community"]

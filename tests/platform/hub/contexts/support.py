"""Test support of the contexts suites (U-HUB-contexts): sealed checkpoints of a session of the outbox scene, uploads
and commits signed by the device's own key (`tests/platform/hub/outbox/support.py`)."""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from agent_hub.crypto import context
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.ids import uuid7
from tests.platform.hub.events.support import Json
from tests.platform.hub.orgs.support import Actor
from tests.platform.hub.outbox.support import Outbox

NOT_CONTRACT = {"contract": False}
WK = bytes(range(32))
Limits = Callable[..., None]
"""The fixture `limits`: run the hub with other quota settings (`HubConfig.context_*`)."""


@dataclass
class Sealed:
    """One checkpoint ready to upload: its object ID, manifest, signature and sealed chunks."""

    object_id: str
    manifest: dict[str, object]
    sig: str
    chunks: list[bytes] = field(default_factory=list)

    def resigned(self, device: Actor, **changes: object) -> Sealed:
        """The same checkpoint with a changed (and newly signed) manifest."""
        manifest = {**self.manifest, **changes}
        return replace(self, manifest=manifest, sig=context.sign_manifest(device.identity.sig, manifest))


class Checkpoints:
    """Uploads of the outbox scene's devices (module docstring)."""

    def __init__(self, outbox: Outbox) -> None:
        self.outbox = outbox
        # object IDs in the order of sealing (UUIDv7 of strictly increasing milliseconds), as one device makes them
        self._ms = itertools.count(time.time_ns() // 1_000_000)

    @property
    def scene(self) -> Any:
        return self.outbox.scene

    def client(self, device: Actor | None = None) -> Any:
        return self.outbox.fleet.client(device or self.outbox.device)

    def seal(self, task: str, session: str, parts: list[bytes] | None = None, device: Actor | None = None) -> Sealed:
        """A checkpoint of the session sealed under `WK` (one chunk per part); a later seal has the greater ID."""
        author = device or self.outbox.device
        object_id = uuid7(next(self._ms))
        salt = os.urandom(32)
        key = context.context_key(WK, salt, object_id)
        found = [
            context.seal_chunk(key, object_id, index, part) for index, part in enumerate(parts or [b"one", b"two"])
        ]
        manifest: dict[str, object] = {
            "v": 1,
            "object": object_id,
            "org": self.scene.org,
            "wg": self.scene.wg,
            "task": task,
            "session": session,
            "author": author.id,
            "epoch": 1,
            "salt": b64u_encode(salt),
            "chunks": len(found),
            "size": sum(len(item) for item in found),
            "digest": context.digest(found),
            "ts": self.outbox.now(),
        }
        return Sealed(object_id, manifest, context.sign_manifest(author.identity.sig, manifest), found)

    def put(
        self,
        object_id: str,
        index: int | str,
        session: str,
        ct: bytes | str,
        device: Actor | None = None,
        **options: Any,
    ) -> httpx.Response:
        text = ct if isinstance(ct, str) else b64u_encode(ct)
        response: httpx.Response = self.client(device).put(
            f"/v1/device/contexts/{object_id}/chunks/{index}", json={"session_id": session, "ct": text}, **options
        )
        return response

    def commit(self, sealed: Sealed, device: Actor | None = None, **options: Any) -> httpx.Response:
        response: httpx.Response = self.client(device).post(
            f"/v1/device/contexts/{sealed.object_id}/commit",
            json={"manifest": sealed.manifest, "sig": sealed.sig},
            **options,
        )
        return response

    def upload(self, sealed: Sealed, session: str, device: Actor | None = None) -> None:
        for index, chunk in enumerate(sealed.chunks):
            response = self.put(sealed.object_id, index, session, chunk, device)
            assert response.status_code == 200, response.text

    def saved(self, task: str, session: str, parts: list[bytes] | None = None, device: Actor | None = None) -> Sealed:
        """A committed checkpoint."""
        sealed = self.seal(task, session, parts, device)
        self.upload(sealed, session, device)
        response = self.commit(sealed, device)
        assert response.status_code == 200, response.text
        return sealed


def error(response: httpx.Response) -> tuple[int, Json]:
    return response.status_code, dict(response.json())

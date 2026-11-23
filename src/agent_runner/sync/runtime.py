"""The sync objects of one daemon (05-runner.md section 2.3).

`runtime(context)` builds them once per daemon context and keeps them on the context as `sync_runtime`, so the
`sync` and `ws` tasks, the shutdown hook, the hint handlers and other areas share one hub client, one pusher (with
its lock and wake event) and one WebSocket client. Other areas write outbox items with
`runtime(context).enqueue(conn, item_kind, body)` inside their own transaction.

The hub URL and the pinned hub key come from `device.json` (the key pinned at enrollment belongs to that hub); the
storage key is derived from the device's Ed25519 seed (`crypto.storage.storage_key`). A device that is not enrolled
has no runtime (None): the sync tasks then end at once.

Tests may set `sync_options` on the context (`SyncOptions`) to inject the HTTP transport, the wall clock, the jitter
source, the WebSocket connector, the discovery package and the sealer.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from psycopg import AsyncConnection

from agent_runner import registry
from agent_runner.crypto.storage import storage_key
from agent_runner.encoding import b64u_decode
from agent_runner.sync import extensions, outbox
from agent_runner.sync.backoff import Backoff
from agent_runner.sync.http import HubClient
from agent_runner.sync.push import Pusher
from agent_runner.sync.seal import Sealer, StoreSealer
from agent_runner.sync.ws import Connector, WsClient, websocket_connect

ATTRIBUTE = "sync_runtime"


@dataclass
class SyncOptions:
    transport: httpx.AsyncBaseTransport | None = None
    wall: Callable[[], float] = time.time
    random: Callable[[], float] = random.random
    connect: Connector = websocket_connect
    sealer: Sealer | None = None
    package: str | None = None
    pusher_settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SyncRuntime:
    client: HubClient
    pusher: Pusher
    ws: WsClient
    storage_key: bytes
    wall: Callable[[], float]

    async def enqueue(self, conn: AsyncConnection[Any], item_kind: str, body: Mapping[str, object]) -> int:
        """Append an outbox item in the caller's transaction (`outbox.enqueue`; 0 when it joined a scope queue)."""
        return await outbox.enqueue(conn, self.storage_key, item_kind, body)

    async def close(self) -> None:
        await self.client.aclose()


def build(context: Any, options: SyncOptions) -> SyncRuntime:
    identity = context.identity
    host = context.host
    package: str = options.package or host.package
    key = storage_key(identity.keys.sig)
    signing_key = identity.keys.signing_key()
    client = HubClient(
        hub_url=identity.device.hub_url,
        key_id=identity.device.key_id,
        signing_key=signing_key,
        hub_key=b64u_decode(identity.device.hub_key),
        transport=options.transport,
        wall=options.wall,
    )
    sealer = options.sealer or StoreSealer(
        storage_key=key, device_id=identity.device.device_id, signing_key=signing_key
    )
    pusher = Pusher(
        pool=context.pool,
        client=client,
        storage_key=key,
        sealer=sealer,
        log=context.log,
        clock=host.clock,
        sleep=host.sleep,
        backoff=Backoff(random=options.random),
        **dict(options.pusher_settings),
    )
    ws = WsClient(
        context=context,
        signer=client.signer,
        ws_base_url=client.origin.ws_base_url,
        handlers=registry.hint_handlers(package),
        hooks=extensions.connect_hooks(package),
        sections=extensions.heartbeat_sections(package),
        pusher=pusher,
        log=context.log,
        clock=host.clock,
        sleep=host.sleep,
        backoff=Backoff(random=options.random),
        connect=options.connect,
    )
    return SyncRuntime(client=client, pusher=pusher, ws=ws, storage_key=key, wall=options.wall)


def runtime(context: Any) -> SyncRuntime | None:
    """The sync runtime of `context`, built on first use; None while the device is not enrolled."""
    existing = getattr(context, ATTRIBUTE, None)
    if isinstance(existing, SyncRuntime):
        return existing
    if context.identity is None:
        return None
    options = getattr(context, "sync_options", None)
    built = build(context, options if isinstance(options, SyncOptions) else SyncOptions())
    setattr(context, ATTRIBUTE, built)
    return built

"""The live service of one hub process: browser streams, device sockets and the bus (03-api.md sections 5 and 6.2).

The app's lifespan creates one `StreamService` (`app.state.stream`), starts it (the bus listens before the first
request is served) and attaches it to `agent_hub.stream.publish.LIVE`, so every frame published in this process
reaches it. All registries live on the event loop; publishers and the bus threads hand frames over with
`call_soon_threadsafe`.

- `publish(item)` (any thread): sends the frame over the bus to the other processes and delivers it here.
- Bus callbacks: a committed event (`hub_events`) is read once per process - only when a stream of its org is open
  here - and offered to the org's streams; a frame of another process is delivered like a local one; an oversized
  frame of another process becomes a `resync`/`reconcile` for its audience; a lost bus connection resyncs every
  stream and sends every device `reconcile`.
- A `close_keys` frame (`agent_hub.stream.publish.CLOSE_KEYS`, from any process) ends the user's streams opened with
  one of its keys (a revoked agent, an ended browser session; KOR-24); it is never sent to a browser.
- `close_all()` (shutdown): every stream gets `resync {hub_restart}` and ends, every device socket is closed with
  1012; new streams are refused with `503 unavailable` and new sockets with 1012 from then on. The command `agent-hub`
  calls it before uvicorn waits for open connections (`close_streams`), so the hub stops promptly with open streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID

import psycopg
from fastapi import FastAPI
from starlette.websockets import WebSocket, WebSocketDisconnect

from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.errors import HubError
from agent_hub.events.notify import Appended
from agent_hub.http.context import HubContext
from agent_hub.stream import presence
from agent_hub.stream.bus import Bus, BusMessage, Connect, encode
from agent_hub.stream.publish import CLOSE_KEYS, LIVE, Audience, Published
from agent_hub.stream.sse import (
    BUFFER_FRAMES,
    HEARTBEAT,
    HEARTBEAT_SECONDS,
    BrowserStream,
    LiveEvent,
    StreamQuery,
    opening,
    read_event,
    readable_scopes,
    watchable,
)
from agent_hub.stream.ws import HINT_BUFFER, INTERNAL, POLICY, RESTART, DeviceSocket

LOG = logging.getLogger("agent_hub.stream")
FLUSH_SECONDS = 5.0
T = TypeVar("T")


class StreamService:
    """The live channels of one hub process (module docstring)."""

    def __init__(
        self,
        context: HubContext,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        buffer: int = BUFFER_FRAMES,
        hint_buffer: int = HINT_BUFFER,
        connect: Connect = psycopg.connect,
    ) -> None:
        self.context = context
        self.instance = uuid7()
        self.heartbeat_seconds = heartbeat_seconds
        self.buffer = buffer
        self.hint_buffer = hint_buffer
        self.closing = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.by_org: dict[UUID, set[BrowserStream]] = {}
        self.by_user: dict[UUID, set[BrowserStream]] = {}
        self.devices: dict[UUID, DeviceSocket] = {}
        self._connected: dict[UUID, datetime] = {}
        self._orgs: frozenset[UUID] = frozenset()
        self.bus = Bus(
            context.config.database.connect_kwargs(),
            context.pool,
            self.instance,
            on_event=self._on_event,
            on_message=self._on_message,
            on_gap=self._on_gap,
            connect=connect,
        )

    # --- lifecycle ---

    async def start(self) -> None:
        """Listen on the bus and attach to the process's publisher."""
        self.loop = asyncio.get_running_loop()
        self.bus.start()
        if not await asyncio.to_thread(self.bus.wait_ready):
            LOG.error("live bus: not listening yet; frames of other hub processes arrive once it is")
        LIVE.attach(self)

    async def stop(self) -> None:
        """Close every stream and socket, detach and stop the bus."""
        self.close_all()
        LIVE.detach(self)
        await asyncio.to_thread(self.bus.stop)

    def is_closing(self) -> bool:
        """`close_all` has run (read again after every await of a connect)."""
        return self.closing

    def close_all(self) -> None:
        """End every stream (`resync {hub_restart}`) and socket (1012); refuse new ones (event-loop thread)."""
        self.closing = True
        for streams in list(self.by_org.values()):
            for stream in list(streams):
                stream.close("hub_restart")
        for socket in list(self.devices.values()):
            socket.close(RESTART)

    # --- publishing (any thread) ---

    def _call(self, function: Callable[..., object], *args: Any) -> None:
        loop = self.loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile: nobody is connected any more
            loop.call_soon_threadsafe(function, *args)

    def publish(self, item: Published) -> None:
        """A frame published in this process: to the other processes and to the receivers here."""
        if item.audience.kind != "device" and item.frame in ("event", "terminal"):
            return  # events reach every process through hub_events (agent_hub.stream.bus)
        self.bus.send(encode(self.instance, item))
        self._call(self._deliver, item)

    # --- bus callbacks (listener thread) ---

    def _on_event(self, appended: Appended) -> None:
        if appended.org_id not in self._orgs:
            return
        with self.context.pool.connection() as connection:
            found = read_event(connection, appended.scope_id, appended.scope_seq)
        if found is not None:
            self._call(self._deliver_event, found)

    def _on_message(self, message: BusMessage) -> None:
        if message.oversized:
            self._call(self._deliver_resync, message.item)
        else:
            self._call(self._deliver, message.item)

    def _on_gap(self) -> None:
        self._call(self._gap)

    # --- delivery (event-loop thread) ---

    def _browsers(self, audience: Audience) -> list[BrowserStream]:
        if audience.kind == "all":
            return [stream for streams in self.by_user.values() for stream in streams]
        table = self.by_org if audience.kind == "org" else self.by_user
        return list(table.get(audience.id, ()))

    def _deliver(self, item: Published) -> None:
        if item.frame == CLOSE_KEYS:
            keys = set(item.data.get("key_ids", ()))
            for stream in self._browsers(item.audience):
                if stream.key_id in keys:
                    stream.close()
            return
        if item.audience.kind == "device":
            socket = self.devices.get(item.audience.id)
            if socket is not None:
                socket.offer(item.frame, item.data)
            return
        for stream in self._browsers(item.audience):
            stream.offer(item)

    def _deliver_event(self, item: LiveEvent) -> None:
        for stream in list(self.by_org.get(item.org_id, ())):
            stream.offer_event(item)

    def _deliver_resync(self, item: Published) -> None:
        if item.audience.kind == "device":
            socket = self.devices.get(item.audience.id)
            if socket is not None:
                socket.offer("reconcile", {})
            return
        for stream in self._browsers(item.audience):
            stream.resync_for(item)

    def _gap(self) -> None:
        for streams in list(self.by_org.values()):
            for stream in list(streams):
                stream.resync("buffer_overflow")
        for socket in list(self.devices.values()):
            socket.offer("reconcile", {})

    # --- browser streams ---

    def _transaction(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self.context.pool.connection() as connection, connection.transaction():
            return function(connection, *args, **kwargs)

    def _rights(self, connection: psycopg.Connection[Any], org: UUID, user: UUID, sessions: frozenset[UUID]) -> Any:
        scopes = readable_scopes(connection, org, user)
        watched = frozenset() if scopes is None else watchable(connection, org, sessions, scopes)
        return scopes, watched

    def _add(self, stream: BrowserStream) -> None:
        self.by_org.setdefault(stream.org, set()).add(stream)
        self.by_user.setdefault(stream.user_id, set()).add(stream)
        self._orgs = frozenset(self.by_org)

    def abandon(self, stream: BrowserStream) -> None:
        """Forget a stream that was opened but will never be served (its route failed after `open_browser`)."""
        self._remove(stream)

    def _remove(self, stream: BrowserStream) -> None:
        for table, key in ((self.by_org, stream.org), (self.by_user, stream.user_id)):
            streams = table.get(key)
            if streams is not None:
                streams.discard(stream)
                if not streams:
                    del table[key]
        self._orgs = frozenset(self.by_org)

    async def open_browser(self, user_id: UUID, query: StreamQuery, key_id: str | None = None) -> BrowserStream:
        """Check the rights, register the stream and queue its opening frames (`HubError` before streaming); `key_id`
        is the key that signed the request (its revocation ends the stream)."""
        if self.closing:
            raise HubError("unavailable")
        scopes, watched = await asyncio.to_thread(self._transaction, self._rights, query.org, user_id, query.terminals)
        if scopes is None or watched != query.terminals:
            raise HubError("not_found")
        if self.is_closing():  # the hub began to shut down during the read: never register after `close_all`
            raise HubError("unavailable")
        stream = BrowserStream(user_id, query, scopes, watched, buffer=self.buffer, key_id=key_id)
        self._add(stream)
        try:
            opened = await asyncio.to_thread(
                self._transaction,
                opening,
                stream.stream_id,
                query,
                scopes=scopes,
                watched=watched,
                now=self.context.clock(),
            )
        except BaseException:
            self._remove(stream)
            raise
        stream.prime(opened)
        return stream

    async def _refresh(self, stream: BrowserStream) -> None:
        try:
            scopes, watched = await asyncio.to_thread(
                self._transaction, self._rights, stream.org, stream.user_id, stream.watched
            )
        except Exception:
            LOG.error("stream %s: the rights could not be read again", stream.stream_id, exc_info=True)
            return
        stream.update_rights(scopes, watched)

    async def browser_body(self, stream: BrowserStream) -> AsyncIterator[bytes]:
        """The bytes of an open stream: frames, a heartbeat comment every `heartbeat_seconds`, until it ends."""
        loop = asyncio.get_running_loop()
        beat = loop.time() + self.heartbeat_seconds
        try:
            while True:
                chunks = await stream.take(max(0.0, beat - loop.time()))
                if chunks:
                    yield b"".join(chunks)
                if stream.finished:
                    return
                if loop.time() >= beat:
                    yield HEARTBEAT
                    beat = loop.time() + self.heartbeat_seconds
                    stream.refresh = True
                if stream.refresh:
                    stream.refresh = False
                    await self._refresh(stream)
        finally:
            self._remove(stream)

    # --- device sockets ---

    async def run_device(self, websocket: WebSocket, device_id: UUID, features: Sequence[str] = ()) -> None:
        """Serve one device socket from the upgrade to the disconnect (agent_hub.stream.ws); `features` are the
        capabilities the device announced (`agent_hub.device.features`)."""
        if self.closing:
            await websocket.close(RESTART)
            return
        window = self.context.config.device_online_seconds
        now = self._connect_time(device_id)
        was_online = await asyncio.to_thread(
            self._transaction, presence.connect, device_id, self.instance, now, window, features=features
        )
        if was_online is None:  # no longer usable (changed since the gate resolved the key)
            await websocket.close(POLICY)
            return
        if self.is_closing():  # the hub began to shut down during the write
            await websocket.close(RESTART)
            await asyncio.to_thread(self._transaction, presence.disconnect, device_id, self.instance, now, now)
            return
        await websocket.accept()
        socket = DeviceSocket(device_id, websocket, now, buffer=self.hint_buffer)
        previous = self.devices.get(device_id)
        self.devices[device_id] = socket
        socket.offer("hello", {"hub_time": format_time(now)})
        if previous is not None:
            previous.close(POLICY)
        try:
            if not was_online:
                await asyncio.to_thread(presence.announce_presence, device_id, True)
            await self._serve(socket, window)
        finally:
            if self.devices.get(device_id) is socket:
                del self.devices[device_id]
            offline = await asyncio.to_thread(
                self._transaction, presence.disconnect, device_id, self.instance, now, self.context.clock()
            )
            if offline:
                await asyncio.to_thread(presence.announce_presence, device_id, False)

    def _connect_time(self, device_id: UUID) -> datetime:
        """The connection time of a new socket, strictly after the last one of the device in this process: it
        identifies the presence row of the socket (with the instance), so an old socket never removes a new row."""
        now = self.context.clock()
        last = self._connected.get(device_id)
        if last is not None and now <= last:
            now = last + timedelta(microseconds=1)
        self._connected[device_id] = now
        return now

    async def _serve(self, socket: DeviceSocket, window: float) -> None:
        async def on_heartbeat(capacity: dict[str, int]) -> bool:
            result = await asyncio.to_thread(
                self._transaction,
                presence.heartbeat,
                socket.device_id,
                instance=self.instance,
                connected_at=socket.connected_at,
                now=self.context.clock(),
                capacity=capacity,
            )
            if result.capacity_changed:
                await asyncio.to_thread(presence.announce_capacity, socket.device_id)
            return result.current

        sender = asyncio.create_task(socket.send_loop())
        receiver = asyncio.create_task(socket.receive_loop(window, on_heartbeat))
        done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if receiver in done and receiver.exception() is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(sender), FLUSH_SECONDS)
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        failures = [task.exception() for task in (sender, receiver) if not task.cancelled()]
        gone = any(isinstance(failure, WebSocketDisconnect) for failure in failures)
        for failure in failures:
            if failure is not None and not gone:
                LOG.error("device socket %s failed", socket.device_id, exc_info=failure)
                socket.close(INTERNAL)
        if socket.close_code is not None and not gone:
            with contextlib.suppress(Exception):
                await socket.websocket.close(socket.close_code)


def service_of(app: FastAPI) -> StreamService | None:
    """The live service of a running app."""
    found = getattr(app.state, "stream", None)
    return found if isinstance(found, StreamService) else None


async def close_streams(app: FastAPI) -> None:
    """Close every stream and socket of the app before the server waits for its connections (shutdown)."""
    service = service_of(app)
    if service is not None:
        service.close_all()

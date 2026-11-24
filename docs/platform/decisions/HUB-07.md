# HUB-07 – Live channels: SSE `/v1/stream`, device WebSocket, presence

Decisions taken where `03-api.md` sections 5, 6.2 and 12, `02-data-model.md` section 2.1, `06-web.md` section 1,
ADR-013, ADR-016 and ADR-027 were silent or ambiguous.

## Publishing interface

1. **`agent_hub.stream.publish.publish(audience, frame, data, *, wg_id=None)` stays the interface.** The package text
   names `publish(kind, org_id, payload, audience)` in `stream/__init__.py`. HUB-02 already published the interface as
   the module `agent_hub.stream.publish` with `publish(Audience, frame, data)`, and HUB-04/05/06 call it. A function
   `publish` in the package `__init__` would collide with that module name (`from agent_hub.stream import publish`
   returns the module or the function depending on import order). The module keeps its path and signature; it gains
   the keyword `wg_id=` (restricts a browser frame to readers of one workgroup) and the shorthand
   `hint(device_id, type, body)` named in the package text. The package `__init__` documents the modules only.
2. **Closed frame lists at publish time.** `publish` refuses (`ValueError`) a frame the audience cannot receive:
   devices get the hints of 03 §6.2 except `hello` (sent by the socket), browsers the frames of 03 §5 except `hello`
   and `resync` (made by the stream). A wrong frame is a programming error of the publishing area and shows in its
   tests.
3. **Default fan-out `LIVE`, services attach.** The previous default (`MemoryPublisher`) is replaced by
   `LivePublisher`; each running app attaches its `StreamService` in its lifespan (no `set_publisher`, so suites that
   install a fake keep working). Without a running app (CLI processes) a frame has no receiver in the process and is
   dropped - the hub never queues frames or hints (03 §6.2).
4. **Workgroup of a frame, fail closed.** `task`, `session` and `operation` frames carry their row; the stream takes
   `wg_id` from it (`session.wg_id` for `session`); a row without a valid `wg_id` reaches nobody. A `metrics` frame
   without `wg_id=` is delivered only when all its samples are device samples (VM and session samples belong to a
   workgroup, 03 §4.11) - HUB-16 publishes VM/session samples per workgroup with `wg_id=`. Other org frames are
   org-wide. User frames (`wraps`, `device_response`) go to every stream of that user; a `notification` only to the
   user's streams of its `org_id` that read its `wg_id`. Hint bodies must not contain `type`.

## Bus between processes

5. **Events through `hub_events`, everything else through `hub_stream`.** Events are announced by HUB-06 inside the
   storing transaction on `hub_events` (identifiers only). Every process - the storing one included - reads the
   committed event once from `hub.event` when it has a stream of that org open and offers it to its streams; the
   `event`/`terminal` frames that `announce` publishes in-process are ignored by the live service. One source keeps
   the commit order and needs no de-duplication between a local and a remote path. All other frames and hints are
   delivered locally at once and sent as JSON on `hub_stream` (`{"i","a","f","d","w"}`); a process skips its own
   messages by its instance ID.
6. **Oversized frames.** PostgreSQL limits a notification to 8000 bytes. A frame over 7900 bytes travels as
   `{"i","a","f","w","r":true}` (without data): the other processes send `resync {buffer_overflow}` to the audience's
   streams that the frame type and explicit workgroup do not exclude (e.g. `metrics` only to `monitoring=1`) or
   `reconcile` to the device, and the receivers pull. No extra table is needed; rows of 03 §5 are far below the limit, so in practice
   this affects only large `metrics` frames (HUB-16 should keep a frame below it, e.g. one frame per entity kind).
7. **Compact JSON, not the canonical form.** The canonical JSON of `agent_hub.crypto` refuses non-integers;
   `metrics` frames carry `cpu_ratio`. The bus and the SSE frames use compact ASCII JSON (`allow_nan=False`).
8. **Lost bus connection.** The listener reconnects (0.2 s doubling to 5 s) and then resyncs every local stream with
   `resync {buffer_overflow}` and sends every local device `reconcile`; notifications sent meanwhile are lost.

## Browser stream

9. **Rights per stream, re-read.** Readable scopes = the org scope (active member) and the workgroups with an active
   `wg_access` row. They are read at connect, again on every `log` frame of the org and with every heartbeat (15 s).
   A user who left the org loses the stream (it ends; the reconnect gets `404 not_found`); watched sessions that are
   no longer readable are dropped from the watch list.
10. **Refusals.** Not a member, or a watched session that is unknown, of another org or in an unreadable workgroup →
   `404 not_found` (one code, no oracle; 03 §1). Cursors of scopes the user cannot read are ignored, not refused (a
   revoked workgroup must not make the client's reconnect fail for ever). Query errors → `400 request_invalid`
   (limits of `contracts/ws/sse/query.json`: ≤ 3 distinct terminals, ≤ 256 distinct cursors, `monitoring=1`).
11. **Replay.** After `hello` the events after each readable cursor are replayed in ascending order per scope, scopes in
   ID order; watched sessions' `session.terminal` events are part of the replay (consistent with the live frames).
   More than 1000 events after one cursor, more than 2000 events or 4 MiB stored bytes over all cursors (counted
   before anything is loaded) → no replay at all, `resync {replay_limit}` (the client re-pulls every cursor anyway). `hello.heads` lists every readable scope (0 without events).
12. **No duplicates within one stream.** Per scope the stream remembers the highest `scope_seq` it sent (the head in
   `hello` or the last replayed event); live frames at or below it are dropped. Frames that arrive while the opening
   is read are held back (bounded like the buffer) and filtered afterwards.
13. **Buffer and heartbeat.** 4096 frames and 8 MiB per stream; on overflow the queue is replaced by
   `resync {buffer_overflow}`, keeping a `hello` that was not sent yet (the client needs it first).
   The heartbeat comment `: heartbeat` is sent every 15 s regardless of traffic (the web client times out after
   45 s without one).
14. **Hub restart.** On shutdown every stream gets `resync {hub_restart}` and ends; the client reconnects with its
   cursors and the replay fills the gap. A process that dies without a shutdown cannot send it; the reconnect with
   cursors covers that case.

## Device socket and presence

15. **Device must be usable.** The pipeline refuses unknown keys and wrong key kinds (HUB-02); the decertified/removed
   refusal (`key_revoked`) is the device resolver's job (HUB-08). The socket applies the same rule (`active`, principal
   enabled, an org device still an active member of its owning org) at the upgrade and with every heartbeat, so a
   removal while connected closes the socket at the next heartbeat. Pending devices (admitted by the resolver for
   `device:any`) are refused here, the socket needs right `device`. A refused upgrade is closed before it is accepted (HTTP 403, code 1008 at the ASGI level).
16. **One presence row per device, owned by one socket.** `device_presence` (`hub_instance`, `connected_at`) identifies
   the socket that owns the row; `connected_at` is strictly increasing per device within a process. A heartbeat or a
   disconnect touches the row only while it still belongs to that socket, so an old socket never removes a newer row,
   in this or another process. A newer socket of the same device closes the older one (1008).
17. **Offline.** Online ⇔ the row exists and `last_heartbeat_at` is less than `AGENT_HUB_DEVICE_ONLINE_SECONDS` ago
   (`presence.is_online`, `presence.online_devices` for HUB-08/09/13). A socket without a heartbeat for the window is
   closed (1008) by its process; rows left by a dead process are deleted by the job `stream.presence_expire` (every
   5 s, every process).
18. **Signals instead of `presence.subscribe`.** The package text mentions `presence.subscribe`; ADR-027 §4 and the
   package's interface note define the mechanism as `agent_hub.signals`: `device.presence` (`device_id`, `online`
   1/0) on every change of the online state and `device.capacity` (`device_id`) when a heartbeat changes
   `device.capacity`. HUB-09 subscribes in `scheduler/signals.py`. A reconnect within the window is no presence
   change; a disconnect followed by a reconnect gives `online=0` then `online=1`.
19. **Heartbeat content.** Capacity, session and VM lists are validated against `contracts/ws/device/heartbeat.json`;
   only the capacity is stored (`device.capacity`). Session and VM states reach the hub through the outbox
   (HUB-10). An invalid frame closes the socket (1008; binary 1003; > 64 KiB 1009); an internal failure closes it with
   1011 and is logged.
20. **Hint buffer.** 256 hints per socket; on overflow the queue is replaced by one `reconcile`. After `decertified`
   the socket is closed (1008). Shutdown closes sockets with 1012.
21. **Audit.** `device.connect` and `device.disconnect` (detail `{"channel":"ws"}`) with the device as actor and
   target and, for org devices, `org_id` = the owning org (so org audits list them); `device.disconnect` only when the
   socket still owned its presence row (a superseded or expired row is not audited twice); the expiry job writes it
   for rows of dead processes.
21a. **Known limits.** A socket superseded by a newer socket in another process keeps receiving hints until its next
   heartbeat (≤ 15 s; hints are content-free). Presence signals from the expiry job and a reconnect run on different
   threads and may arrive out of order: subscribers re-read `presence.is_online` instead of trusting the order. A hub
   that shuts down while a stream or socket is being opened refuses it (`503` / 1012) instead of registering it after
   `close_all`.
21b. **No read-log row for the streams.** `GET /v1/stream` and the WebSocket upgrade are streaming routes; the HUB-02
   route declaration forbids `read_resource` on them (03 §1 read log is about resource reads; the data a stream
   delivers is read-logged when the client pulls it).

## Shutdown (open point of ACC-M0)

22. **Exit 0 after SIGTERM.** The command uses `HubServer`, a uvicorn server that first closes the live channels
   (otherwise uvicorn waits for open streams for ever) and does not re-raise the captured signal after the orderly
   shutdown (it clears uvicorn's `_captured_signals`, a private attribute pinned by the test of the exit status). SIGINT
   exits with 0 as well. Supervisors see exit status 0 instead of −15. The integration branch had no other fix for this point.
   `app.py` (HUB-02) starts and stops the live service in its lifespan; this is the wiring the package text asks for
   ("replaces the interface/fake of HUB-02").

## Tests

23. **Real servers.** SSE and WebSocket tests run against uvicorn on loopback ports of the package range
   (`WP_PORT_BASE + 14…17`, ephemeral ports when unset); the TestClient cannot stream. Two apps in one process stand
   for two hub processes (each with its own service and bus connection); one test also starts `python -m agent_hub`
   as a real second process with the system clock. Measured locally: hub receive → browser frame p50 3.3 ms, p95
   3.8 ms over 200 events.

# RUN-02 decisions (runner sync base: outbox, signed HTTP client, WebSocket client)

Small decisions of the sync package (05-runner.md §2.3, 03-api.md §1, §6.1–§6.2, 04-crypto.md §5.12,
02-data-model.md §4, ADR-013, ADR-022, ADR-027 §4).

## Modules
1. `src/agent_runner/sync/` holds, besides the owned `outbox.py`, `push.py`, `http.py`, `ws.py`, `hints.py` and
   `tasks.py`: `seal.py` (sealing of event drafts), `backoff.py` (1 s → 30 s, ±20 %), `codes.py` (copies of the
   closed lists `error-codes.json` (origin `hub`) and `outbox-item-kinds.json`; a test keeps them equal to
   `contracts/`), `extensions.py` (two discovered points, see 6), `runtime.py` (per-daemon objects) and
   `shutdown.py` (the outbox flush as `ShutdownHook`, as RUN-01 suggested).
2. Legacy reuse: `agent_cli.keys.request_text`/`response_text` are imported read-only (00-overview.md §5). The host
   line rules of `agent_cli.keys.hub_origin` and the answer checks of `agent_cli.keys.verify_answer` are copied and
   adapted in `http.py` (they are not in the import allow-list).

## HTTP client
3. Only the pinned hub **public key** of `device.json` is checked; `Agent-Hub-Key` must be present once and match
   `[a-z0-9-]{1,64}` (the `HubSigner` rule) but is not compared, because `device.json` (02-data-model.md §4) stores
   no hub key ID.
4. Failures that never reach `HubRefusal` (all are `HubTransportError` with a closed `reason`, retried by the caller):
   unreachable hub, unsigned answer, a repeated or missing signature header, a malformed time or signature, a
   signature by another key or over other bytes, an `Agent-Hub-Time` more than 120 s from the local clock, a
   `Content-Encoding`, an answer over 8 MiB, any 3xx (redirects are never followed), a non-object 2xx body and a
   verified refusal whose code is not in the closed list. The unsigned 401s of the hub gate (`time_skew`,
   `key_unknown`, …) only yield a `claimed` code for the log line.
5. The hub URL and the hub key come from `device.json` (the key pinned at enrollment belongs to that hub), not from
   `AGENT_RUNNER_HUB_URL`.

## Extension points (ADR-027 §4, ADR-028)
6. Two discovered points (ADR-028) are added in the sync area without editing `registry.py` (RUN-01): `<area>/connect.py:HOOKS`
   (`ConnectHook(phase, name, run)`, phases `reconcile` and `pull`, run after every WebSocket (re)connect around the
   outbox push; RUN-03 registers its reconcile engine and pull loop there) and `<area>/heartbeat.py:SECTIONS`
   (`HeartbeatSection(field, provide)` for `capacity`, `sessions`, `vms`; VM-02 and RUN-04 register providers). A
   heartbeat field without a provider is sent empty, so the device is online before those packages land.
7. `sync/hints.py` is both the dispatcher and a registration module: its `HANDLERS` registers `hello` (logs a clock
   skew of more than 5 s). Other hint types (`reconcile`, `events`, `log`, …) are registered by their owners.
8. Other areas reach the sync objects through `agent_runner.sync.runtime.runtime(context)` (built once, kept on the
   daemon context as `sync_runtime`, None while not enrolled): `runtime(context).enqueue(conn, item_kind, body)`.
9. Two RUN-01 tests asserted that the real package registers nothing (`test_registry.py`) and that the daemon runs
   zero tasks (`test_cli.py`); they now check only that the lookups succeed, that the sync registrations are present
   and that the task count is a number, so later areas do not have to edit them again.

## Outbox
10. `enqueue(conn, storage_key, item_kind, body)` runs in the caller's transaction; an autocommit connection outside a
    transaction block is refused (the advisory lock would end with the statement). The insert sends
    `NOTIFY runner_outbox`, delivered on commit; the `sync` task listens on its own connection and otherwise polls
    every 5 s.
11. An `event` item stores a draft `{header, body}`: the header fields without `epoch`, `author`, `lh`, `salt`,
    `nonce`. The header formats and the E8 size limit are checked at enqueue time with placeholders, so a draft that
    could never be sealed never takes an `origin_seq` (a gapless stream cannot skip an item locally).
12. Sealing chooses the highest epoch of the scope the device holds a key for (`runner.scope_key`) and the verified
    log head (`runner.log_head`), both maintained by RUN-03; without them the item waits (`seal_waiting`) and the
    items before it are still pushed. After `reseal` the item is sealed with an epoch newer than the refused one and
    waits until such a key exists. The set of items to reseal is kept in memory: after a restart the stored sealed
    bytes are sent again and the hub answers `reseal` again (no conflict, the hub never stored them).
13. Wipe continuation: while `pushed_origin_seq` is unknown (fresh or wiped store) nothing is pushed. The position is
    taken from `GET /v1/device/state.last_origin_seq` — by the push loop itself, and through
    `outbox.adopt_hub_position(conn, storage_key, last_origin_seq)` for RUN-03's reconcile step. Items written before
    the position was known (numbered from 1) are renumbered to follow it; their bodies are re-sealed for the new
    AAD context. Once known, later values are ignored (the local position is authoritative). A device that is not
    `active` pushes nothing.
14. `origin_seq_gap` rewinds `pushed_origin_seq` to `expected - 1`. An `expected` that is missing, below 1 or above
    the local maximum + 1 cannot be served and is treated as fatal like a conflict (`origin_seq_gap`).
15. Fatal conditions are persisted as `sync_state.sync_fatal`, logged as `runner.alert <code>`, kept as
    `Pusher.fatal` for the metrics sink of RUN-15 (`agent_runner_device_sync_fatal`), and end the `sync` task (a
    returning task is not restarted). `outbox.clear_fatal` is the operator action for `agent-runner doctor`.
16. Batching: at most 500 items and 4 MiB of the encoded request body (the first item is always sent); at most one
    push per 250 ms on the monotonic clock; another batch follows at once while the previous one advanced
    `acked_through`. A verified answer whose `acked_through` or result sequence lies outside the batch is a
    transport error and changes nothing.
17. Push retries (transport errors and refusals other than gap/conflict) use the same backoff as the WebSocket;
    `429` waits `Retry-After` (bounded 1 s – 1 h).

## WebSocket
18. The socket is opened with the `websockets` client with redirects refused (`process_redirect` returns the error),
    no proxy, no compression and frames of at most 64 KiB. Order after connect: record `hub_ws_last_connect`, run
    the `reconcile` hooks, `Pusher.drain()`, the `pull` hooks, then dispatch hints in arrival order. A failing hook,
    push or handler is logged by exception type only and does not end the connection.
19. Backoff 1 s doubling to 30 s with a factor drawn from [0.8, 1.2]; it is reset when the previous connection stayed
    up for at least 30 s (measured on the monotonic clock).

## Review (independent subagent) – resolutions
20. Request bodies are encoded with sorted keys, so a retried sealed event read back from `jsonb` is sent byte for
    byte as on the first attempt (test compares the raw request bytes).
21. An outbox row that does not open (`decrypt_failed`) is fatal `outbox_corrupt` (alert, task ends); batch-level
    `400`/`413` refusals are retried with backoff and each raises `runner.alert push_refused_<status>`.
22. An event that cannot be sealed logs `seal_waiting origin_seq=N` once; the stream waits, as strict order requires
    (03-api.md §6.1). Unassigning a device removes its sessions, so no new device events for that scope are written.
    Superseded by KOR-08 (ADR-047): such an event waits in a scope queue and holds back only its own scope.
23. Verified answers are checked further: unknown result statuses and a `reseal` that is not the item right after
    `acked_through` are `response_invalid`; a rejection code outside the closed list is logged as `invalid`, never
    verbatim. After a `reseal` the next batch follows at once.
24. `sync_fatal` is a `sync_state` key the data model does not list; `sync_state` is a key/value table, so no schema
    change is needed. The hub URL scheme rule (`https` outside test mode) is enforced when `device.json` is written
    (enrollment, RUN-03); the client accepts the stored value.
25. Not changed (low): a cleared fatal flag takes effect at the next daemon start; a database failure during the
    connect sequence restarts the `ws` task through the supervisor instead of the socket backoff.

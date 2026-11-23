# HUB-06 – Event appends, scope reads, scope heads, fan-out signal

Decisions taken where `03-api.md` sections 1, 4.4 and 12, `04-crypto.md` sections 4.3, 5.12 and 6.4,
`02-data-model.md` sections 2.3 and 3, ADR-003 and ADR-027 were silent or ambiguous.

## One way into `hub.event`

1. **`agent_hub.events.store.append_events(connection, events, author, *, now, root_key, logs=None, notify=None,
   origin_seqs=None)`** is the only writer of `hub.event` for every area (`POST /v1/events`, genesis and log batches,
   tasks, the device outbox, operations). It runs in the caller's transaction, checks every event before it stores the
   first one (all or nothing) and returns `StoredEvent`s in request order. `logs` lets the orgs area check against the
   log after its batch and restricts the call to that org (an event of another org in a log batch is
   `header_invalid`, as before HUB-06); `origin_seqs` gives the `origin_seq` of every event of an outbox push
   (`origin_device_id` = author), so the outbox stores all `event` items of a push in **one** call with sorted
   locking. The package text lists a `validate.py`; the checks are the reference rules of `agent_hub.log.events`
   plus a few lines of request rules, so they live in `store.py` next to the storage they guard.
2. **Orgs switched over.** The interface `agent_hub/orgs/events.py` (with its minimal implementation and the lookup of
   `agent_hub/events/batch.py`, decision HUB-05, 17) is removed; `orgs/append.py:store_batch_events` calls
   `append_events` with `logs={org: log after the batch}` and the root key, `orgs/routes.py:publish_append` announces
   the stored events through `agent_hub.events.notify.announce`. Behaviour for the orgs routes is unchanged except that
   their events now also send device hints and the signal `event.appended`.

## Checks

3. **Order.** Format (E1/E2) → author = caller (`403 forbidden`) → `notify` only on `whiteboard.entry`
   (`400 request_invalid`) → E9 lookup by `id` → org known (`404 not_found`) → E1–E8 and the hub checks
   (`agent_hub.log.events.check_event_hub`). Codes as in decision HUB-05, 7 (`log_fork` → `header_invalid`).
4. **A replay is not checked again.** Identical bytes under a known `id` answer the stored `(scope_id, scope_seq)` with
   `replayed:true` before the rights and epoch checks: a retry after a rotation, after the grace period or after a
   membership change must get its original result (outbox retries, 04-crypto.md 5.12). The author check comes first,
   so nobody learns about events of others. A repeated `id` inside one call is treated the same way (replay of the
   earlier item, or `event_conflict`).
5. **The org row is held `FOR SHARE`** while the events of a call are checked and stored, so a concurrent membership
   append (which holds `FOR UPDATE`) cannot change rights between the E5 check at receipt and the commit; the event
   append waits for it and then checks against the new log (test `test_append_waits_for_a_running_membership_append`).
6. **Unknown org.** An event whose header names an org the hub does not know is `404 not_found` (the reader vector
   `E1_org_mismatch` gives `header_invalid`, because a reader compares with the log it holds).
7. **E7 data.** The session of a session kind must exist in `hub.session` (rows of the tasks area, HUB-09) with the
   header's `task` and workgroup and run on the author device (`device_id`); otherwise `author_not_writer`. The task and
   workgroup match is a hardening beyond the wording of E7 (a device cannot file output under another task).
8. **Grace period.** E6 uses `scope_epoch.created_at` of the scope's newest epoch as rotation time (decision HUB-05, 17);
   600 s inclusive (`now - rotated_at <= 600 s`), tested at 599.999 s, 600 s and 600.001 s.
9. **`notify` flag.** `notify:true` on any other kind than `whiteboard.entry` is `400 request_invalid` (a request-shape
   rule, not a crypto rule). The flag is not stored; it travels in the signal (`notify` = 1) to the subscribers
   (HUB-15). `notify:false` is allowed on every kind.

## Storage and order

10. **Scope heads.** All `scope_head` rows of a call are created if missing (`INSERT … ON CONFLICT DO NOTHING`) and
    locked in sorted `scope_id` order before the first increment; each event then gets `UPDATE … SET seq = seq + 1
    RETURNING seq` (02-data-model.md 2.3). Sorted locking keeps two calls that touch the same scopes in opposite order
    free of deadlocks; one call's events of one scope get consecutive numbers. A rollback undoes the increments
    (gapless).
11. **Concurrent identical submissions.** E9 is repeated for every new event after its scope head is locked; a
    concurrent call with the same event has committed by then (read committed), so the second call answers
    `replayed` (or `event_conflict`) instead of a unique violation. Two *different* events with the same new `id` in
    different scopes at the same moment can still meet the unique constraint (`503 unavailable`); UUIDv7 client IDs
    make that practically impossible.
11a. **Lock order.** Org rows (`FOR SHARE`, sorted), scope heads (sorted), then the global audit lock of
    `append_audit`. Callers call `append_events` before they write any audit row in the same transaction (the outbox
    of HUB-10: the `event` items of a push in one call before the audit rows of other items; HUB-09/HUB-12: events
    before `task.create`/operation audit rows); otherwise a concurrent `POST /v1/events` holding a scope head and
    waiting for the audit lock can deadlock with them.
11b. **Post-processors inside the transaction.** Effects that must commit with the event (whiteboard projection and
    notification rows of HUB-15, task bookkeeping of HUB-09) register `agent_hub/<area>/event_hooks.py:HOOKS`
    (discovered like every extension point, ADR-027 section 4; the extension point is defined in
    `agent_hub/events/hooks.py`, not in the shared registry list). `append_events` calls them once per call with the
    newly stored events (with the request's `notify` flag); a failing hook refuses the call. The after-commit signal
    `event.appended` stays for best-effort reactions.
12. **Columns.** `wg_id` = `scope_id` for workgroup scope, the header's `wg` for org scope (`wg.profile`), else NULL;
    `task_id`/`session_id` NULL for `""`; `size` = decoded `ct` length; `header_text` = canonical header.
13. **Audit `event.append`.** One row per newly stored event (actor = author, `device_id` for device authors, org,
    workgroup, task, session, target `event`/id, `event_id`; detail `{kind, scope, scope_seq, ct_bytes}`). Replays and
    refusals are not audited (decision HUB-05, 22).

## Signals

14. **`pg_notify` inside the transaction.** `events/notify.py:send` issues `pg_notify('hub_events', payload)` for every
    stored event inside the storing transaction: PostgreSQL delivers notifications only at commit and drops them on a
    rollback, which is exactly "after commit" for every caller (also callers whose transaction is not owned by this
    area). Payload `{"org_id","scope","scope_id","scope_seq"}` (canonical JSON), never content. `notify.parse` checks
    it for HUB-07's bus. The package text also names `events/signals.py` for this; the file list of the package
    (ownership) names `notify.py`, which HUB-07 reads, so the channel lives there and no `events/signals.py` exists
    (that name is the discovery point for signal subscribers, ADR-027 section 4, which this area does not have).
15. **After the commit** the caller calls `notify.announce(stored)` (the events route does so in its worker thread,
    since subscribers may do I/O; a failure is logged and never turns the committed write into an error): browser frame `event` (or `terminal` for
    `session.terminal`) to the org audience, device hint `events {scope_id, scope_seq}` to every active device that
    reads the scope except the author and not for `session.terminal`, which device pulls never return (computed from the verified log inside the transaction, decertified platform
    devices excluded), and `agent_hub.signals.emit("event.appended", …)` with `event_id`, `org_id`, `scope`,
    `scope_id`, `scope_seq`, `kind`, `author_id`, `notify` and, when set, `wg_id`, `task_id`, `session_id`, `ref`.
    `ref` is passed only when it is identifier text in the sense of `agent_hub.signals` (read targets such as
    `repo:org/name` contain `/` and are left out; subscribers read the row when they need them). Replays announce
    nothing.

## Reading

16. **Scope resolution.** `scope_id` is an org (`org:member`) or a workgroup (`wg:read`, archived workgroups stay
    readable); unknown, malformed (non-canonical UUID) and foreign scopes are all `404 not_found`.
17. **Filters.** `kinds` must match the contract pattern and name known kinds only (`request_invalid` otherwise);
    `task`, `session`, `ref` must be canonical UUIDs (contract); `after` default 0, at most 16 digits (contract
    `q_seq`), `limit` default 200, max 1000.
    Without `kinds` `session.terminal` is left out; with `kinds` exactly the listed kinds are returned.
18. **`next_after`** is the last returned `scope_seq` when further matching events exist (looked ahead with
    `limit + 1`), else `null`; `head` is the scope's `scope_head.seq` (0 without events), independent of filters.

## Known limits (review, documented instead of changed)

19. **Decertification window.** The platform log is read without a lock; a `device_decertify` committed while a device
    event of that device is being checked does not stop that one event (the signature gate refuses the device's next
    request, HUB-04). Org rights are protected by the org row lock (decision 5).
20. **Event-ID oracle.** A caller who submits its own event under somebody else's event `id` gets `event_conflict`,
    which reveals that the ID exists (metadata only; UUIDv7 IDs are not guessable in practice).
21. **Lock starvation.** Overlapping `FOR SHARE` locks of event appends could delay a membership append; PostgreSQL
    queues the `FOR UPDATE` request, so later shares wait behind it. A load test belongs to PFM-01.
22. **Page size.** `limit=1000` events of up to 256 KiB each is the specification's bound (03-api.md section 1); no
    byte cap is added. Clients page with smaller limits for large scopes.
23. **Hint volume.** One `events` hint per stored event and device; batching hints per scope is left to HUB-07's
    fan-out, which may coalesce hints of the same scope.

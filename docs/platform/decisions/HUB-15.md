# HUB-15 – Whiteboards, notifications, session pings

Decisions taken where `03-api.md` sections 4.9, 5, 6.1 and 6.2, `02-data-model.md` sections 1.3 and 2.6,
`04-crypto.md` section 4.3, `05-runner.md` section 9, ADR-020 and ADR-027 section 4 were silent or ambiguous.

## Whiteboard projection (`whiteboards/projection.py`, `whiteboards/event_hooks.py`)

1. **Projection in the storing transaction, not on the signal.** The work package lists `event.appended` for the
   projection, but 02 section 1.3 requires projections in the same transaction as their source row. The projection
   (and the notification rows, session pings and the `whiteboard.notify` audit row of a notified entry) therefore run
   as the in-transaction post-processor `whiteboards/event_hooks.py:HOOKS` that HUB-06 provides for exactly this
   (`agent_hub.events.hooks`). Only the live part (frames, `ping` hints) uses the after-commit signal
   `event.appended` (`notifications/signals.py`).
2. **The hub projects, it does not refuse.** Acceptance stays with the kind rules of HUB-06 (04 section 4.3; the
   contract vectors contain edits of entries the hub has never seen). The projection is lenient instead: the first
   `whiteboard.entry` of an ID creates the row (a later entry with the same ID - on any level - is a plain,
   unprojected event); `whiteboard.edit`/`whiteboard.delete` apply only to an existing, not deleted entry of the same
   org, level, workgroup and task. An edit from another workgroup with a foreign entry ID therefore changes nothing:
   it is stored in its own scope, readers of the entry's scope never receive it, and the history ignores it.
3. **Level from the header.** The body (with `level`) is ciphertext; the hub derives the level from the header: scope
   `org` → `org`, scope `wg` without task → `workgroup`, with task → `task`.
4. **Times.** `created_at`/`updated_at` are the hub receive times of the creating/latest projected event (not the
   author's `ts`), so the rebuild reproduces them from `hub.event`.
5. **Rebuild order and the `notify` flag.** The rebuild replays the whiteboard events in the order of their
   `event.append` audit rows (gapless `seq` under the global audit lock, held until commit = the store order, also
   across scopes for colliding entry IDs). The `notify` flag is not part of the stored event; it comes back from the
   `whiteboard.notify` audit row (`event_id` = the creating event), which is written for every notified entry, also
   with zero recipients.
6. **History = projected events.** `GET /v1/whiteboards/{entry}/history` returns the creating event plus the
   edit/delete events of the same scope, `ref` and task between creation and `latest_event_id` - exactly what the
   projection applied (no duplicate entries, no edits after the delete, nothing from other scopes). Cap 10000
   (contract `maxItems`).

## Reads (`whiteboards/routes.py`, `whiteboards/read.py`)

7. **List filters.** All filters are optional; the result is always restricted to what the caller reads (org level:
   active member; workgroup/task level: `wg_access`). A named `org`/`wg`/`task` the caller may not read is
   `404 not_found` (task: `task:read` on its workgroup). Deleted entries are listed with `deleted: true` (the UI hides
   or strikes them). Cursor: the shared opaque list cursor (`device.tasks.encode_cursor`).

## Notifications (`notifications/rules.py`, `notifications/routes.py`)

8. **Recipients** of a notified entry are computed from the projections inside the storing transaction (the org rows
   are held `FOR SHARE` by `append_events`, so a concurrent membership change cannot interleave). The creating admin of
   a workgroup has write on it (log rule), so admins are recipients only where they have workgroup access.
9. **`ref`** of a notification: the entry (`whiteboard.entry`), the accepted subject user/device (`invite.accepted`,
   the invitation ID is not a UUID), none for `task.*` (task in `task_id`) and `rotation.required`.
10. **`task.idle`** uses the device times (`at`) of the main session's `idle` transition and the transition right
    before it, which must be `working`, ≥ 10 s apart; `created_at` is the hub receive time of the `idle` transition.
    `task.failed` uses the time of the newest `failed` task transition. Both are written once per
    (recipient, kind, task, time), so repeated `task.state` signals never duplicate. They go to the owner only while
    the owner still reads the task's workgroup.
11. **`invite.accepted`** goes to `invite.created_by` while that user is still an admin of the org, never when the
    creator is the subject, once per (admin, org, subject).
12. **`rotation.required`** is evaluated on `log.appended` (org) and `device.decertified` (every org the device is a
    member of): every admin of an org with a non-empty `rotation_pending` gets one, unless an unread
    `rotation.required` of that org is still open for that admin (one open reminder per admin and org; after reading
    it, the next change of the org while still pending reminds again).
13. **Marking read** ignores unknown, already read and foreign IDs (no oracle) and keeps the first `read_at`.
14. **The list is not filtered by current membership**; it shows every row addressed to the caller (content-free, and
    consistent with `unread_notifications` of `GET /v1/me`, which already existed). The live frame is filtered by the
    stream (org and workgroup of the stream, HUB-07).
15. **Signal subscribers need a pool.** Like the other areas, the subscribers queue their work on one background thread
    and use the context bound by the job `notifications.bind_context` (first run at process start, hourly after).
    Across several hub processes the dedupe of 10-12 is not serialised; a duplicate reminder in a race of two
    processes is accepted (content-free, harmless).

## Pings (`notifications/pings.py`, `notifications/device_state.py`)

16. **Active sessions** are those not `stopping`, `stopped` or `failed` (a stopping session never takes another turn).
    Sessions without a device yet get their durable row too and receive it with the device state once placed; hints
    go only to devices that run a pinged session, one per device.
17. **`pings` section** lists the undelivered rows of sessions on the device that have not ended (`stopping` included:
    it was pinged while active), oldest first, at most 1000 per answer.
18. **`ping.delivered`** marking stays in the outbox area (HUB-10 already marks the row in `handlers_audit`); this
    package adds no `notifications/outbox.py`.

## Changes outside the package's paths

19. `tests/platform/hub/events/test_hooks.py` asserted that no area registers an event hook yet; it now checks that the
    whiteboard hook is among the discovered hooks (one line, no list for later packages to edit).
20. Migration `0004_notification_ref.sql` adds the lookup indexes of the after-commit fan-out
    (`notification (kind, ref)`, `session_ping (ref) WHERE delivered_at IS NULL`).

## Review (independent subagent)

21. **Task of another workgroup (medium, fixed).** The event rules check the `task` header only for its form. A
    task-level entry is projected only when its task belongs to the event's workgroup; otherwise it stays an
    unprojected event without notifications or pings (pings additionally match workgroup and task).
22. **Repeated entry ID with `notify` (medium, fixed).** The after-commit fan-out runs only when the signalled event
    created the entry, so a later event with a known ID never re-sends the original's frames and hints.
23. **Fan-out lookups (medium, fixed):** decision 20.
24. **Rebuild vs. concurrent appends (low, fixed):** the rebuild takes the audit lock first.
25. **Not changed, by decision:** "non-stopped" read as not `stopping`/`stopped`/`failed` (decision 16); the history
    cap of 10000 follows the contract `maxItems` (an entry with more edits is not a realistic v1 case); old
    notifications stay listed after a loss of access (decision 14). The `ids` of `POST /v1/notifications/read` are
    UUIDv7 as the contract's `common.json#/$defs/uuid` requires.

# HUB-10 – Device outbox: processing of every item kind, device stream

Decisions taken where `03-api.md` sections 1 and 6.1, `02-data-model.md` sections 2.3-2.9, `04-crypto.md` sections
5.12 and 6.4, `05-runner.md` section 2.3, ADR-013 and ADR-027 were silent or ambiguous. Architecture-relevant parts are
ADR-030 (receipts, handler chains).

## Rule set (`outbox/process.py`)

1. **Receipts instead of target tables.** Replay/conflict detection compares the SHA-256 of the canonical item text
   with a receipt per stored item (`hub.outbox_receipt`, migration 0002, ADR-030). Receipts of rejected items count:
   a resent rejected item is `replayed`, other content under its `origin_seq` is `origin_seq_conflict`.
2. **Refusals change nothing.** `409 origin_seq_gap` and `409 origin_seq_conflict` are decided before the first write
   and refuse the whole push; `detail.expected` is always the stored position + 1. A gap in the middle of a batch
   therefore does not store the items before it (the runner resends from `expected`; its outbox is gapless, so the next
   push passes). Items out of order (`[4, 3]`) are a gap.
3. **Items repeated inside one push** are compared with the earlier item of the same push (`replayed` or conflict).
4. **`acked_through`** is the stream position after the push (also when the push only held resends of older items).
5. **Results after a reseal** are left out: processing stops at the item answered `reseal`, later items (also resends)
   get no result entry and are not processed.
6. **Reseal only for E6.** `append_events` reports the first failing rule; E7 follows E6, so an event that breaks both is
   rejected (`author_not_writer`) after the runner re-sealed it. Accepted: the second push costs one round trip.
7. **Events first, one call.** All new `event` items of a push go into one `append_events` call before any other effect
   (lock order of HUB-06 decision 11a). Only when that call refuses are the events stored one by one in savepoints to
   find the refused ones. In that rare path a later event's scope-head lock is taken after an earlier event's audit lock;
   a deadlock with a concurrent `POST /v1/events` is then possible and resolved by PostgreSQL (one side gets `503`, the
   runner retries). Accepted because it only happens for pushes that contain refused events.
8. **Content refusal vs. server failure.** A `HubError` with a status below 500 (including `401 signature_invalid` of a
   bad event signature), a body that breaks its contract (`request_invalid`) and a value the database refuses as data
   (`DataError`, `CheckViolation`, e.g. a counter overflow) make the item `rejected`; a `5xx` refusal or any other error
   fails the whole push (`503`), so a hub-side outage never turns items into poison.
9. **Rejected items are audited** with the new action `outbox.rejected` (contract change, `contracts/changes/HUB-10.md`):
   actor and device = the pushing device, target `outbox_item`/`<origin_seq>`, detail `{origin_seq, item_kind, code}`,
   no org (the hub does not trust IDs of a refused body).
10. **Envelope vs. body.** The envelope (`items`, `origin_seq`, `item_kind`, object `body`) is checked by FastAPI (whole
    push `400`); every item body is checked when its item is processed (`outbox/bodies.py`, strict pydantic models with
    the contract patterns), so one malformed body is a poison item, not a blocked stream. `integer` columns cap
    `memory_mib`, `disk_gib`, `free_vm_slots` at 2^31 - 1 (contract: 2^53 - 1).
11. **Rate.** One push per device per 250 ms (`outbox/pacing.py`, per hub process like the pipeline's limiter),
    otherwise `429 rate_limited` with `Retry-After: 1` (the header has whole seconds); only admitted pushes count.

## Item kinds and rights (`outbox/handlers_*.py`)

12. **event**: E5 is `device:wg`, E7 is "the session belongs to the device" (HUB-06). An event already stored under its
    `id` (store wipe on the device) is `ok` without a second copy and without an announcement.
13. **session_transition**: `sessions.transitions.apply` (HUB-09) with the device as reporter; `claude_session_id_present`
    has no column and is not stored.
14. **vm_state**: a VM belongs to the device that reported it first (another device: `forbidden`); a VM with a
    workgroup needs `device:wg`, a pool VM (`wg_id: null`) only the device. `state_at` = the device time of the state,
    `created_at` = hub time of the first report; rows of `deleted` VMs are kept.
15. **operation_transition**: steps `accepted → executing`, `accepted|executing → succeeded|failed`,
    `received|accepted|executing → expired`; the current state again is no change, everything else `request_invalid`.
    `result_event_id` must be the device's `operation.result` event with `ref` = the operation. `started_at`/
    `finished_at` are hub receive times (like the session milestones of HUB-09), the transition's `at` is the device
    time. The dedupe slot is released on `failed` and `expired`; audit `operation.<state>` with `local_check: null`
    (the registration's local check is HUB-12's). Operation registration and pings are HUB-12/HUB-15 (extensions).
16. **usage**: `device:session`; counters add up, `cost_basis`, context fill and provider are the latest values.
17. **quota**: the owner must be a user with access to a workgroup the device is assigned to (the device may use the
    owner's credentials); `quota_poll_owners` (which device polls) is HUB-14's. A report older than the stored window is
    ignored. `resets_at: null` is stored as NULL (column made nullable in migration 0002, ADR-030); a window listed twice
    in one item is `request_invalid`.
18. **metrics**: every sample must be the device itself, its VM or a session on it; one foreign or unknown entity refuses
    the whole item; a sample with an existing key is ignored. No frame: the `metrics` frames are the stream's (HUB-16).
19. **audit**: scope (org, workgroup, task, session) comes from the hub's rows, never from the body; `actor_kind` is
    `device` for every runner-reported action (also `read.session.*`, which the runner reports for its sessions).
    `ping.delivered` needs `session_id` and must match the ping's session and kind; a second delivery keeps the first
    time and is audited again.
20. **wrap**: exactly the W3 store of `POST /v1/device/wraps` (`wrap_unauthorized` etc. are content refusals).

## Registration

21. **Handler chains** instead of `merge_mappings` (ADR-030): the outbox area's `outbox.py:HANDLERS` holds the base
    handlers; other areas append handlers for existing kinds. The package text names
    `outbox/handlers_{event,session,vm,operation,usage,quota,metrics,audit,wrap}.py`; these files hold the handlers and
    `outbox/outbox.py` registers them.
22. **Shared files touched** (each by its convention): migration `0002_outbox_receipt.sql` (ADR-027 section 5) with
    `tests/platform/hub/test_schema.py` (new table) and the migration tests of HUB-01, which now count the shipped
    migrations instead of assuming one (`test_migrate.py`, `test_cli.py`); the contract change of decision 9 with
    `audit/actions.py` and `tests/platform/contracts/test_enums.py`; the ADR index.

## Not done here

23. `device/tasks.py` still names the first main session (HUB-09 decision 9 follow-up); this package does not touch the
    device task reads, the follow-up stays open for HUB-12.
24. E2E-06, E2E-13 … E2E-17 do not exist yet (SCN packages); they are not run here.

## Review

25. **Lock order with other writers (review M1, kept with a mitigation).** `append_events` writes `event.append` audit
    rows, so a push holds the global audit lock while handlers lock task, session, operation and org rows. This is the
    order HUB-09 decision 16 prescribes (`append_events` → task → session → audit) and the one `POST /v1/tasks/{task}/
    prompts` already uses; writers that lock a task row first and audit later (close, control) can therefore meet a push
    in a deadlock. PostgreSQL aborts one side; the outbox route runs a push that was the deadlock victim once more, so
    the runner sees at most a rare `503`. A hub-wide fix needs a different audit sequencing (outside this package).
26. **Fixed:** the item digest uses `\u` escapes, so a body with a lone surrogate gets a digest and is rejected instead
    of failing every retry (M2); encoding errors count as content refusals; `Infinity`/`NaN` are refused in
    `cpu_ratio` and `used_percent` (M4); optional body fields may be absent but not `null` (L1); the chains are
    discovered when the routes module is imported at app start (L2); the decisions file is committed (L7); tests for
    surrogates, `Infinity`, `null` fields, the in-push repeat of a rejected item, the deadlock retry and the
    `outbox.rejected` detail contract (L8).
27. **Documented, not changed:** only `HubError`, contract errors and data refusals are content refusals; any other
    exception of a handler (also of a later area) fails the push, so extensions must raise `HubError` for refusals
    (M3, ADR-030). An extension handler of `event` that refuses rejects the item but cannot remove the event, which
    the event phase stored already (M5, ADR-030). After an unassignment a late `vm_state`/`usage`/`metrics` report of
    the old workgroup or session is rejected (L3: rights follow the current view; HUB-16 treats stale VMs); a pruned
    receipt answers `replayed` (L4, ADR-030); the device view is read without org row locks, a concurrent unassignment
    may let one item pass on the rights of the moment before (L5, like HUB-06 decision 19 for decertification);
    `integer` column caps (L6, decision 10).

# HUB-12 – Operations: registration H1–H6, dedupe slots, expiry, child sessions, browser reads

Decisions taken where `03-api.md` sections 4.7, 8.3, 9 and 12, `02-data-model.md` sections 2.4, 2.5 and 2.9,
`04-crypto.md` section 4.10, ADR-012 and ADR-017 were silent or ambiguous.

## Registration (`operations/register.py`)

1. **H1 is split.** A session that is unknown (`404 not_found`) or runs on another device (`403 forbidden`) is an
   HTTP refusal of the right `device:session`: nothing is stored, because the operation cannot be attributed to the
   requesting device. A session of the device that has ended (`stopped`/`failed`), or whose task is closed, is a
   recorded decision `denied` with `rights_denied` (the closed list has no better code); `stopping` sessions may still
   register (the final answer of a one-shot child happens while it stops).
2. **H1 before the local check.** `local_check:"rejected"` skips H2–H6 only, as 03-api.md section 8.3 says; an ended
   session is therefore `denied` even when the runner rejected the request locally.
3. **Decision codes.** H2 → `denied`/`rights_denied` (an archived workgroup has no write access); H3 → `rejected`/
   `unknown_type` (same status as a schema refusal of the guest); H4 → `denied`/`limit_exceeded` or `budget_exceeded`
   (ADR-017); H5 → `duplicate`. Every known type is enabled in v1 (no switch exists).
4. **Interface for RUN-07: the params event names the operation.** The `operation.params` event must carry
   `ref` = operation ID, `scope_id` = the request's workgroup, `task` = task ID, `session` = session ID, and the
   request's `dedupe.epoch` must equal the event's `epoch` (both are made with the current workgroup key; E6 checks the
   event's epoch), and its `kind` must be `operation.params` (the request field is a generic object; the kind check
   is ours). Otherwise `400 request_invalid` with the pointers. `requested_at` may lie at most 120 s ahead of the hub
   clock (signature window; it orders the browser list); older values are allowed (the runner retries a registration
   for up to 10 min while the hub is unreachable).
5. **Two transitions per registration.** Transition 1 `received` (`at` = `requested_at`, device clock, `by_id` = device)
   and transition 2 = the decision (`at` = hub time, `by_id` NULL = the hub). `created_at` = `requested_at`,
   `decided_at` = hub time. Only the decision is audited (`operation.<decision>`, actor = the device, detail with
   `local_check`); the `received` step is not audited separately.
6. **Idempotency.** The operation row is inserted first (`ON CONFLICT (id) DO NOTHING`); a repetition compares
   session, task, workgroup, type, `requested_at`, dedupe slot, params event ID and the local check (from the decision's
   audit detail) plus its error code. Equal → the original decision with `"replayed": true` (a duplicate's `original`
   shows the original's *current* state and result event; an accepted `subagent.request` answers the same child);
   different → `409 id_conflict`. Other bytes under the params event's ID are `409 event_conflict` (E9).
7. **Concurrency.** After storing the params event the registration takes the global audit lock explicitly
   (`append_events` already takes it when it stores; a repetition stores nothing), so every registration has the lock
   order events → audit → task → slot. The task row is locked before the checks, so two `subagent.request`s of one task never both take
   the last free place, and the session state cannot change during the decision (session writers lock the task
   first). A deadlock victim is run once more (like the outbox push).

## Dedupe slots (`operations/dedupe.py`)

8. **Claim in one step.** `INSERT … ON CONFLICT DO NOTHING`; on conflict the slot row is locked and judged: held
   (holder `accepted|executing|succeeded` and `expires_at > now`) → duplicate, otherwise taken over. At `now =
   expires_at` the window has ended.
9. **Window without dedupe fields.** For a type without a field set the hub can only have received an
   idempotency-key tag; its window is 24 h (`dedupe-fields.json` window rule). The hub cannot tell an idempotency tag
   from a field tag (transparent), so a type with fields always uses its own window.
10. **Release.** `failed`/`expired` release the slot (outbox base handler of HUB-10, expiry job). `rejected`,
    `denied`, `duplicate` never take one, so there is nothing to release.
11. **Epoch gap.** Tags of different epochs are different slots (documented limitation of 04-crypto.md section 4.10);
    tested at the slot level.

## Limits and budget (`operations/limits.py`)

12. **Counting.** Running children = sessions of the task with `depth > 0` not in `stopped`/`failed` (`stopping`
    still runs); total = every session of the task with `depth > 0`. Order inside H4: depth, running, total, budget.
13. **Budget hook as an extension point.** `budget_check(BudgetRequest(connection, org_id, session_id, now)) -> bool`.
    HUB-14 registers the computation as `agent_hub/<area>/budget.py:CHECK` (discovered like ADR-027 section 4; at most
    one area). Without it the budget is unlimited. Only `subagent.request` consults the budget in v1: it is the only
    type that starts model work; GitHub operations cost no model budget.

## Child sessions (`operations/children.py`)

14. Created in the registration's transaction after the decision: same task, parent's device **and VM**, depth +1,
    owner = task owner, state `requested`, `model_access`/`model_id` NULL (the runner reports them). Audit
    `session.requested` with the device as actor, hint `session.start`, `session` and `task` frames. The existing
    `sessions` section of `GET /v1/device/state` lists the child (it lists every session of the device), so no own
    device-state section is needed. The re-scheduler only touches sessions without a device, so children are never
    moved.

## Expiry (`operations/jobs.py`)

15. Job `operations.expire` every 30 s: `received` 10 min after `created_at` → `expired`/`hub_unreachable`
    (safety net: a registration is decided in its own transaction, so the hub never keeps `received`); `accepted`
    30 min after `decided_at` → `expired`/`timeout` (the outbox contract allows only these two codes for `expired`).
    Audit actor `hub`, result `failed`. The job takes the audit lock first and skips locked rows, so it cannot deadlock
    with outbox pushes. A later runner `executing` for such an operation is rejected by the outbox state rules.

## Signals (inputs of HUB-15)

16. `operation.finished` (`operation_id`, `session_id`, `task_id`, `org_id`, `state`) after `succeeded`, `failed` or
    `expired` (outbox extension and expiry job), not after the decisions (the runner answers those synchronously).
    `subagent.finished` (`session_id` = child, `parent_session_id`, `task_id`, `org_id`) when a child reaches
    `stopped`/`failed` through the outbox. Both outbox extensions detect "changed by this item" by the end time equal
    to the push time; a repeated report in a later push signals nothing. The same final state repeated inside **one**
    push signals twice (signals are at-least-once; HUB-15 pings are content-free and the runner de-duplicates them).
    Accepted rather than adding state to the outbox's push context (HUB-10 file).

## Reads

17. `GET /v1/tasks/{task}/operations` uses the opaque cursor of the task lists; `GET /v1/operations/{id}` returns the
    newest 100 transitions (contract maximum). Both answer `404` for unknown, malformed and foreign IDs.

17a. **Accepted `subagent.request` and expiry.** The runner reports `executing` → `succeeded` with the result
    `{session_id}` for every accepted type (05-runner.md section 12 steps 6-7), so an accepted subagent request does not
    expire while its child starts; if the runner never reports, the operation expires after 30 min and the child keeps
    running (its session has its own lifecycle and runtime limit).

## Other

18. **Foreign test changed.** `tests/platform/hub/outbox/test_registry.py` asserted that no area extends the outbox
    chains; it now asserts that the base handler is first in every chain (the extension mechanism of ADR-030 is meant
    for exactly this package).
19. **No migration.** The expiry query has no partial index on `operation.state`; acceptable for v1 volumes, a later
    package may add one.
20a. **Replay lookup.** The local check of the original is read from its decision audit row (filter on `task_id`,
    served by the index `audit_task`); no new column was added to avoid a migration.
20. **E2E.** E2E-15, E2E-21, E2E-22 and E2E-24 do not exist yet (SCN-04/SCN-07); they are not run here.

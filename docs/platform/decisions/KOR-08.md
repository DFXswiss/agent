# KOR-08 decisions (agent repository: an unsealable outbox item no longer blocks the device)

Item A-01 of the audit AUD-01 (second part). The design is ADR-047; this file records the smaller decisions. Every
case has a test that fails without the correction (`tests/platform/runner/sync/test_scope_queue.py`,
`test_scope_discard.py`).

## Cases

| Case | Before | After |
|---|---|---|
| Wrap not arrived yet (also: wrap rejected, until SCN-08's re-check stores it) | every later item of the device waits until the wrap arrives | only the scope waits; released when the key is there |
| No verified log head of the org yet | as above | as above |
| New epoch without a wrap for this device (`reseal`) | every later item waits until the wrap arrives, for good if it never comes | only the scope waits (`min_epoch` kept) |
| `device_unassign` with rotation, unsealed drafts of the workgroup | keys deleted: every later item waits for good | the scope waits until the next reconcile run, then its queue is discarded with an audit |
| `remove` of the device, unsealed drafts of the org or its workgroups | as above | as above, one audit per scope |
| Archived workgroup, or keys still held after an unassignment | sealed, `rejected` by the hub, the stream goes on | unchanged (already right) |
| Org stopped (`log_rollback`, `log_fork`) | keys and head kept: sealed and sent | unchanged; a queue of a stopped org is never discarded |
| Hub unreachable | every item waits, retried with backoff, nothing lost | unchanged (already right; no scope is singled out) |
| Device revoked (`key_revoked`, `decertified`) | the hub refuses the device's signature: nothing can be pushed | unchanged (not a scope matter) |

## Decisions

1. **Smallest design that solves every case.** Way (a) of the package alone (discard when the key is gone for good)
   leaves the cases in which the key still comes; way (b) with a time limit would drop events of a scope the device
   may still read. Scope queues (ADR-047) cover both: the waiting scope holds back only itself, a lost scope is
   discarded on verified evidence.
2. **`operation_transition` follows its result event.** The hub accepts a `result_event_id` only for a stored
   `operation.result` event of the device (`handlers_operation._check_result_event`), so a transition naming a queued
   result joins that queue. Other item kinds (session transitions, usage, VM states, audits, metrics, quotas, wraps)
   have no such dependency and never wait. A `session_transition` may therefore reach the hub before the output events
   of a waiting scope; the hub shows them when the scope is released.
3. **`enqueue` returns 0** for an item that joined a queue (no number yet) instead of changing its return type: the
   protocols of other areas (`sessions.records.Outbox`) declare `int`, and no caller uses the value.
4. **Discard rule.** The reconcile step uses `OrgState.can_read(device, scope, scope_id)` of the verified log, the same
   right the hub checks at receipt (E5) and the assignment step uses to delete keys. It only looks at queues: a scope
   the device still holds a key for is never queued, so its items are sealed and the hub decides. The audit uses the
   existing action `runner.alert` with a new alert name `outbox_discarded` (the detail schema allows any lower snake
   case name); `value` is the number of dropped items, the target the scope (`org` or `workgroup`) — metadata only.
5. **Order number 45**, between the assignments (40, which delete the keys) and the sessions (50).
6. **Release cost.** Before each batch the pusher calls the sealer once per queue; a missing key or head returns
   before any cryptography, so a waiting queue costs two indexed reads per round.
7. **Storage context of held bodies** `held-<id>:<item_kind>` (purpose `outbox`): it can never equal the context of
   a numbered item (`<origin_seq>:<item_kind>`), so a row cannot be moved between the tables without re-sealing.
8. **Held items are no backlog.** `outbox.backlog` (metric `agent_runner_device_outbox_backlog`, `doctor`, and the
   flow control of the metrics area with `BACKLOG_LIMIT` = 1000) counts numbered items only. Counting a waiting scope
   with, say, a session's terminal output would have stopped the device's metric samples, a cross-scope effect of the
   kind this package removes. A waiting queue is logged when it is created (`scope_held`), at the first push after
   every start and then hourly (`scope_waiting`, review finding); a gauge of held items belongs to the metrics area
   (open point in the report).
9. **Corrupt rows.** A `CryptoError` while releasing, walking or holding (a stored body or scope key that does not
   decrypt) is `outbox_corrupt`, as for the batch load before.
10. **Adjacent change outside the package's paths.** `subagents/results.py` reads the final text of a subagent from the
   local drafts (ADR-038); without reading the queues it would return an empty text for a child whose scope waits.
   It now reads `outbox.event_drafts`: queued drafts first, then the numbered ones, in **one** statement through a
   server-side cursor, so a hold or release committed during the read can neither repeat nor hide a draft (review
   finding). `tests/platform/runner/test_store.py` lists the new table, as every migration does; the docstring of
   `reconcile/wraps.py` no longer names `seal_waiting`.
11. **Existing tests adapted, not weakened.** `test_push.py::test_reseal_after_epoch_stale` and
    `::test_sealing_waits_for_scope_key_and_log_head` asserted that nothing behind a waiting event was sent; they now
    assert that the item behind it is sent and the event follows after its key, with the same checks of the resealed
    bytes as before.

12. **Supersedes RUN-02 decision 22** (`seal_waiting`: the stream waits for the event). The log line is gone; its
    successors are `scope_held`, `scope_waiting`, `scope_released` and `outbox_discarded`.
13. **A discarded `operation.result` leaves its operation to expire.** The final `operation_transition` naming it is
    discarded with the queue; the hub expires the operation (30 min in `accepted`/`executing` without a final state,
    03-api.md §12). The device has lost the workgroup, so it cannot report a result there anyway.
14. **The discard audit has no org.** The hub takes `org_id` of a device audit row only from `session_id`/`task_id`
    (`handlers_audit._scope`); a queue spans many sessions, so the row is a device row with the scope ID as target.
    Visible to the operator and in the device's audit; adding an org target would be a contract change.
15. **A hold re-seals the tail once, under the outbox lock.** Writers wait for it (one AEAD open and seal per item
    behind the held event). It happens once per blockade; with the hub reachable the tail is short (pushed every
    250 ms). Accepted.
16. **Moved items keep their sealed bytes under a new `origin_seq`.** 04-crypto.md §5.12 binds the reuse of sealed bytes
    to the retries of one `origin_seq`; the moved items were never stored by the hub (decision basis of ADR-047 item
    2), so the hub sees them for the first time under the new number. To be stated in §5.12 by DOC-01.

17. **ADR number.** The ADR was numbered 045 and 046 while other packages took those numbers in parallel (ATS-03
    045 in agent, SCN-09 046 in agent-core); it is ADR-047 in both repositories. The comment of the merged migration
    `0010_outbox_held.sql` still says ADR-046: a merged migration is not edited (its checksum is recorded in every
    store that applied it).

## Documentation for DOC-01

03-api.md §6.1 and 05-runner.md §2.3 (scope queues, the discard rule), 02-data-model.md §4 (`runner.outbox_held`),
08-monitoring.md (the alert name `outbox_discarded`; held items are not in the backlog), 04-crypto.md §5.12 (sealed
bytes of a moved item, decision 16).

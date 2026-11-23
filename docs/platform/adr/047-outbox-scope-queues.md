# ADR-047: Scope queues keep an event the runner cannot seal from holding back the device's other items

Status: Accepted (2026-11-22)

## Context
- The runner outbox is numbered gaplessly per device (`origin_seq`, 03-api.md §6.1) and pushed in that order. An
  `event` item is stored as a draft and sealed at its first push attempt with the current epoch of its scope and the
  verified log head of its org (04-crypto.md §5.12, 05-runner.md §2.3); after `status:"reseal"` (E6) it needs a key of
  a newer epoch.
- When the key (or the log head) is missing, the push walk stopped at that item. Every later item of the device
  waited with it: other workgroups and orgs, session transitions, terminal output, operation results (AUD-01 A-01, the
  failures of INF-03). The hub's rule "a poison item must not block the stream" (03-api.md §6.1) has no runner
  counterpart for an item that cannot even be sent.
- A key is missing for a while or for good: the wrap has not arrived (new assignment, a wrap rejected and not yet
  re-checked), the org's log is not verified yet, a rotation the device got no wrap for (the hub answers `reseal`), or
  the device lost the scope: `device_unassign` with its rotation or `remove` of the device make the assignment step
  delete the keys (05-runner.md §3.5) while drafts of that scope may still be unsealed. The hub would refuse such an
  event anyway (`author_not_writer`, E5 at receipt). The specification has no rule for the runner side.

## Decision
1. **Scope queues outside the numbering.** The runner store gets `runner.outbox_held` (migration `0010`). When the push
   walk meets an event it cannot seal, the event and every later item of its scope move there, and the remaining later
   items are renumbered to close the gap. The scope of an item is `(scope, scope_id)` of an `event`; an
   `operation_transition` whose `result_event_id` names a moved event belongs to that event's scope, because the hub
   accepts the transition only after the result event (03-api.md §9). All other item kinds never wait for a key.
   While a scope has a queue, `enqueue` appends its new items (and transitions naming a queued result) to the queue;
   they get no `origin_seq` yet (`enqueue` returns 0). The order within a scope is therefore kept, the numbering seen
   by the hub stays gapless, and an item of one scope never waits for another scope's key.
2. **Only items the hub never stored are moved or renumbered.** The walk stops at the first item it cannot seal, so
   nothing at or behind it was ever sent; the only exception is a `reseal` answer, which says the hub stored nothing
   from that item on (processing stops there, ADR-030). The refused event loses its sealed bytes and keeps the lowest
   epoch it may be sealed with (`min_epoch`); the sealed bytes of the other moved items are kept.
3. **Release.** Before each batch the pusher tries to seal the first item of every queue (with its `min_epoch`). When
   that succeeds, the queue's items are numbered after the current last item in their order and the first keeps the
   new sealed bytes (`runner.sync scope_released`). Moves, releases and discards run under the outbox lock, like
   `enqueue`, and in one transaction each, so a restart in the middle of a blockade loses or repeats nothing.
4. **Discard only on verified evidence.** The reconcile step `outbox` (order 45, after the assignments) discards a queue
   when the verified log shows that the device may not read the scope (`can_read`), the org is not stopped and the
   platform log is not stopped. The discard writes, in the same transaction, the outbox item `audit` `runner.alert`
   `{alert:"outbox_discarded", value:<items>}` with target `org`/`workgroup` and the scope ID (an existing action and
   detail schema; no contract change) and the log line `runner.sync outbox_discarded`. A queue of a stopped org, of a
   scope of no verified org, or of a scope the device may still read stays: it holds back only itself.
5. **Visibility.** The log names each queue when it is created (`runner.sync scope_held`), while it waits (at the
   first push after a start and then hourly, `scope_waiting`) and when it is released; a discard is an audit row at
   the hub. Held items are not part of the outbox backlog (`agent_runner_device_outbox_backlog`): the
   metrics area holds its samples back while the backlog is high, and a waiting scope must not stop them. A queue has
   no time limit, as it holds back only its own scope. Update (KOR-10): the textfile sink shows the queues as
   `agent_runner_device_outbox_held` (items) and `agent_runner_device_outbox_held_scopes` (waiting scopes)
   (`08-monitoring.md` §3).
6. **Local readers.** The final text of a subagent is read from the local drafts (ADR-038); drafts in a queue are newer
   than every numbered item and are read first, in the same snapshot (`outbox.event_drafts`).

## Consequences
- A missing key costs only its own scope; the cases of AUD-01 A-01 and INF-03 no longer stop a device.
- An event of a scope the device lost before sealing it is dropped with an audit instead of being sent for a sure
  refusal; the hub keeps a record (audit row of the device) of how many were dropped.
- A move re-seals the later items' local bodies with their new numbers (the storage context names `origin_seq`), once
  per blockade; new items of a waiting scope go straight to its queue.
- The runner store gains one table beyond 02-data-model.md §4; 03-api.md §6.1 and 05-runner.md §2.3 should describe the
  queues (documentation for DOC-01).

## Alternatives considered
Numbering every item only at its first push (a schema change of `runner.outbox` that every reader of `origin_seq`
would follow), reordering the unsent tail in place on every round (re-seals the waiting items again and again while
their scope stays blocked), a time limit after which the waiting item is dropped (loses events of a scope the device
may still read), placeholder items in the gap (no outbox item kind is free of effects at the hub).

## Deviation from DESIGN.md
None beyond ADR-001.

# ADR-050: An outbox push whose answer is late is sent once more on another connection

Status: Accepted (2026-11-23)

## Context
- PERF-03 (07-testing.md §2.2) requires runner outbox write → browser frame p95 < 300 ms on T1 and T2. After ADR-048
  the exclusive measurements on T2 (hub device and runner device on a wireless link) gave 291.1 ms (KOR-13) and, in
  the final acceptance, 303.2 ms and 287 ms. The decomposition put the time on the way runner → hub (p95 281–297 ms at
  a median of 25 ms); the hub's share (receive → browser frame) was 8 ms.
- The tail comes in clumps every few seconds: one push hangs on the link for about half a second (a lost segment
  waits for the TCP retransmission timer), and since only one push is on the way (ADR-048 §1) every item written
  meanwhile waits behind it. The link is part of T2 and stays so (ADR-048, alternatives).
- Every device endpoint treats a byte-identical repeat as a repeat (`03-api.md` §1 Idempotency): the hub holds the
  device's `device_stream` row `FOR UPDATE` while it processes a push and answers an `origin_seq` it already stored
  with the same bytes as `replayed`. The runner seals events before the first attempt and resends the same bytes.

## Decision
1. **Hedge once.** A push whose verified answer has not arrived `hedge_after` seconds after it left is sent once more
   with the same body and a new signature (time, nonce). The first request holds its connection, so the copy goes over
   another one of the hub client's pool (an idle one or a new one) and does not queue behind the hanging one.
2. **The first answer counts.** The first verified answer of the two is applied as before and the other request is
   cancelled (its connection is closed). When one of them fails (transport error or refusal, e.g. `429`), the answer
   of the other is awaited; when both fail, the error of the first request is handled as before (backoff,
   `Retry-After`, gap, conflict; a refusal of the copy alone costs at most one backoff). The hub stores every
   `origin_seq` once. The request that did not count may still reach the hub later (its bytes are already on the
   way). The hub then answers it `replayed`, **or** refuses it with `409 origin_seq_conflict` when an item of its
   batch was stored meanwhile with other bytes (an event the runner sealed anew after `reseal`, or items it renumbered
   after a realignment, KOR-10), or with `409 origin_seq_gap` when the hub's position is below the batch (a hub
   restored from an older export). It may also never process it at all. Such a refusal comes before any write and changes nothing (position, receipts, audit, frames), and the runner
   never reads it. When the answer of the copy counts and
   the hub had already processed the first request, items the hub rejected come back `replayed`, so the runner's
   `item_rejected` log line is missing for them; the hub's own audit of the rejection is unaffected and is not
   written a second time.
3. **The other request ends before the next push.** The runner cancels the request that did not count and waits
   until it has ended before the push returns (`agent_runner.sync.push`: `_post` cancels and collects both requests
   and the timer). Only then is the answer applied and the next push, for example with the resealed event, started.
   So no push ever reads the answer to an earlier request, and a late refusal never counts as the runner's own:
   `origin_seq_conflict` remains fatal only for a conflict answered to the push that is on the way. A late copy with
   old bytes cannot be stored either. The hub never stores an event of a stale epoch, so the old bytes of a resealed
   event are refused (`reseal`) before the new seal is stored and conflict afterwards. For renumbered items this rests
   on timing, not on a guarantee: a realignment concerns only items the runner pruned, at least an hour after their
   acknowledgement, so a request from before the restore would have to arrive more than an hour late.
4. **Adaptive wait.** `hedge_after` is three times the median time to an answer of the last 16 answered pushes, at
   least 150 ms (`agent_runner.sync.push`: `HEDGE_FACTOR`, `HEDGE_WINDOW`, `HEDGE_MIN_S`). On a quiet link (median
   about 20 ms) a hanging push is copied after 150 ms; a hub that is slow for every push (PERF-05: about 110 ms per
   push) is not sent each push twice, because the wait grows with the usual answer time. The time of a push whose
   copy answered counts from the first request.
5. **Bounded copies.** At most one copy within 2 s (`HEDGE_SPACING_S`) and none for a batch above 256 KiB
   (`HEDGE_MAX_BYTES`, its upload alone may outlast the wait). A hedged push lasts at least 150 ms, longer than the
   100 ms interval, so within any 2 s the starts plus the copy stay below the 20 tokens the hub's pacer refills
   (ADR-048 §2): a conforming runner still never meets `429`.
6. **Unchanged:** at most one batch is on the way and pacing stays start to start at 100 ms (ADR-048); batch limits,
   sealing, order, scope queues and error handling are unchanged. The log names each copy
   (`runner.sync push_hedged after_ms=<ms>`).

## Consequences
- A hanging push costs the items behind it at most `hedge_after` plus one round trip on a fresh connection instead
  of the retransmission time of the link.
- A copy costs the hub one more signature check, one token of the device's pacer (reserve 10, ADR-048 §2) and a
  short transaction that answers `replayed`; with the adaptive wait and the spacing copies are rare (at most one per
  2 s, about one per hanging push).
- When one request is refused (verified) the runner still waits for the other request. In the worst case that is
  the full request timeout (30 s) before the refusal is handled. This is accepted: it only happens when one request
  was refused and the other hangs, and the refusal is handled as before once it is known.
- Hub and contracts are unchanged; old and new runners and hubs work together in any order. The answers to late
  requests (§2) and the order in which the runner ends its requests (§3) are covered by tests on both sides
  (`tests/platform/runner/sync/test_push_hedge.py`, agent-core `tests/platform/hub/outbox/test_late_copies.py`,
  KOR-19).

## Alternatives considered
- Two batches on the way at once (pipelining): the second batch would reach the hub before the first when the first
  hangs and be refused with `409 origin_seq_gap`.
- A shorter TCP retransmission timeout or a wired link for T2: not in the product's hands, and T2 keeps its link.
- A fixed wait of 150 ms: under PERF-05 most pushes take longer than that and the hub would process nearly every
  push twice.

## Deviation from DESIGN.md
None beyond ADR-001.

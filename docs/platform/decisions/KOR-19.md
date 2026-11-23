# KOR-19 – Hedged outbox push (ADR-050): missing evidence, exact statement, confirming measurements

Follow-up to the final acceptance. The hedged push (ADR-050) was the last product change and came after all audits.
This package adds the missing tests (finding 11 of the review of ADR-050), corrects what ADR-050 says about the late
request and repeats the exclusive PERF-03 measurement on T2. **No product behaviour changed.** No new test showed a
defect, so `push.py` and the hub's code are unchanged, apart from documentation (item 4).

## 1. What the tests cover

A request the runner gives up may still reach the hub: its bytes are already on the way. The runner test rig
(`tests/platform/runner/sync/test_push_hedge.py`) can now hold a request before the hub sees it. When such a request
is cancelled, it still reaches the hub later, and nobody reads the answer. The rig can also delay the end of a
cancelled request and hold a refusal on its way back. The KOR-18 repeat is driven by a transport that fails the first
outbox request on a "reused" connection (httpx trace events) after the hub processed it.

| Case | Test | New / present | Fault built in (not committed) | Result with the fault |
|---|---|---|---|---|
| 1a first request refused (`429`), copy answered | `test_a_refused_first_request_does_not_count_when_the_copy_is_answered` | new | the first request that ends counts, even a refusal | red: the push ends with `429` |
| 1b copy answered `reseal`, the first request arrives late with the old bytes | `test_a_late_first_request_after_a_reseal_answered_to_the_copy_is_refused_without_effect` | new | the loser is not cancelled; it is collected at the next push, after that push's request was answered | red: `SyncFatal origin_seq_conflict` (a constructed fault standing for "the runner reads the given-up request's answer later") |
| 1b mirror order (first answered `reseal`, copy late) | `test_a_late_copy_after_a_reseal_answered_to_the_first_request_is_refused_without_effect` | new | same | red: `SyncFatal origin_seq_conflict` |
| 1c collect before going on | `test_the_next_push_starts_only_after_the_given_up_request_has_ended` | new | the loser is cancelled but not collected | red: the push returns while the loser still runs |
| 1d gap answered to both requests | `test_a_gap_answered_to_both_requests_rewinds_once_and_doubles_nothing` | new | when both fail, a generic transport error replaces the first request's error | red: `HubTransportError`, no rewind |
| 1d realignment (KOR-10) with a late request in the old numbering | `test_a_late_request_in_the_old_numbering_after_a_realignment_is_refused_without_effect` | new | as 1b | red at the assertion that the request was given up (without it the push waits for the old request and never ends) |
| 1e real conflict answered to the first request, the copy then fails | `test_a_conflict_answered_to_the_first_request_stays_fatal_while_the_copy_fails` | new | when both fail, the last request's error | red: transport error instead of `SyncFatal` |
| 1f hedge together with the KOR-18 repeat | `test_the_hedge_together_with_the_repeat_on_a_stale_connection` | new | no repeat on a stale connection | red: the copy's answer counts, the given-up request is missing |
| 2a byte-identical copy after later items | agent-core `test_late_copies.py::test_a_copy_arriving_after_later_items_were_stored_is_replayed` | new | a replayed item runs its effect again | red (also 2c) |
| 2b old bytes of a resealed item | `…::test_a_copy_with_the_old_bytes_of_a_resealed_item_is_refused_without_effect` | new | a resent item is compared only within the push | red: `200` instead of `409` |
| 2c copy of rejected items | `…::test_a_copy_of_rejected_items_is_replayed_without_a_second_audit` | new | a replayed rejected item is audited again | red: second `outbox.rejected` row |
| 2d two byte-identical pushes at once | `test_rules.py::test_two_concurrent_pushes_of_one_device_are_serialised` (present, identical bytes) and `…::test_a_push_and_its_copy_arriving_together_are_serialised_by_the_row_lock` (new) | present + new | `FOR UPDATE` removed from `_position` | the present test stays green: its device has no stream row yet, and the `INSERT … ON CONFLICT` serialises the two. The new test (row exists, the hedge's case) is red 3 of 3: `UniqueViolation` on `outbox_receipt` |

The runner tests run against the fake hub of the sync tests. That a late request with old bytes changes nothing at the
*real* hub is shown by the hub tests 2a–2c. The runner tests show that the runner goes on unharmed and never takes
such an answer as its own. The guarantee itself (collect before going on) is tested by 1c.

"Nothing written" in the hub tests compares position, receipts, every audit row, stored events, usage counters,
VM rows and the published live frames before and after the late request. All new tests run without real waiting
time. The hedge timer and the clock are injected. A few tests sleep 10–50 ms of real time, as the present cases do,
only to give a faulty implementation the chance to go on; a correct one does not depend on it.

## 2. ADR-050

§2 said: "the late copy is answered `replayed` or never processed". That is wrong when an item of the batch was
stored meanwhile with other bytes or under another number. The corrected text names `replayed`, the refusals `409
origin_seq_conflict` / `origin_seq_gap` without any effect that the runner never reads, and "never processed". The new
§3 states the guarantee this rests on: the runner cancels the other request and waits for its end before the push
returns. It also explains why a late copy with old bytes can never be *stored*. The hub never stores an event of a
stale epoch. For renumbered items (KOR-10) this is a timing argument, not a guarantee: they are items pruned at least an
hour after their acknowledgement. ADR-050's former §3–§5 are now §4–§6. The behaviour accepted in review finding 9 is now under "Consequences": a verified refusal of one
request still waits for the other, at worst the request timeout of 30 s. The module docstring of
`agent_runner.sync.push` repeated the wrong sentence and was corrected the same way (documentation only, no code).
The hub comments in `outbox/pacing.py` and `outbox/routes.py` now mention the copy. `03-api.md` §1 and §6.1 do not
contain the wrong statement and are unchanged.

## 3. Confirming measurements PERF-03 on T2

Two more exclusive runs on the integration state (agent-core `93f3a346`, agent `7fa866e3` pinned with
`WP_OTHER_REF`, golden `cache-golden-42cc7cdff91c`, E2E slots limited to 8 on the runner device and 4 on the second device), PERF-03 and PERF-04 with the
reconnect scenario. There were 120 pings over the device link right before and after each run. All runs after
ADR-050 are listed, also the one of the final acceptance:

| Run (UTC) | Pair | Load before → after (runner device; second device) | n | median | p95 | max | hub → browser p95 | outbox → hub p95 | PERF-04 p95 | copies | `429` | link before / after (mean, max, σ) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ACC-FINAL attempt 3, 09:56:02–10:01:40 | agent-core `5e2a9e88`, agent `23ef07dd` | 1.57 → 1.90; 2.02 → 2.57 | 1 122 | 25.0 ms | **192.4 ms** | 345.6 ms | 8.1 ms | 184.6 ms | 290.7 ms | 54 | 0 | 68.2 / 69.3 ms, 121 / 124 ms, 31 / 30 ms |
| KOR-19 run 1, 11:19:09–11:25:14 | agent-core `93f3a346`, agent `7fa866e3` | 1.57 → 3.28; 1.18 → 2.39 | 1 122 | 25.4 ms | **230.7 ms** | 347.7 ms | 8.0 ms | 225.4 ms | 238.8 ms | 60 | 0 | 71.6 / 69.6 ms, 122 / 130 ms, 30 / 31 ms |
| KOR-19 run 2, 11:26:23–11:32:13 | same | 2.40 → 1.51; 1.50 → 2.10 | 1 123 | 24.7 ms | **246.8 ms** | 362.7 ms | 7.9 ms | 241.6 ms | 276.5 ms | 57 | 0 | 69.5 / 73.7 ms, 189 / 208 ms, 33 / 36 ms |

All three runs hold the bound of 300 ms. The wait for exclusivity was 0.5 s, 0.5 s and 0.7 s, and no VM was running
before any run. Two of the three runs (192.4 and 230.7 ms) are inside the 20 % reserve of KOR-13 (≤ 240 ms); run 2
(246.8 ms) is not. During run 2 the link was worse than during the others (maximum 189 / 208 ms, σ 33 / 36 ms).
Before ADR-050 the same link gave 291.1, 303.2 and 287 ms. The hub's share stays at 8 ms, the rest is the way from
the runner to the hub.

p95 is the nearest rank, recomputed from `perf-event-latency.csv`. Copies are the `push_hedged` lines and refusals the
`push_refused` lines of the runner log.

Two attempts before the first counted run did not measure anything and are not results. The golden cache was not
ready because the pinned agent ref was also applied to the cache command. Then the Playwright browsers were missing
on the second device: its work directory was removed at the end of the final acceptance. They were installed with the
command in the host notes, and both attempts were repeated unchanged.

## 4. Not done (possible improvements, not part of this package)

- Encode the body once for both requests (today `_post` encodes it for the size check and the client again for each
  request).
- Let a verified final refusal of one request count at once instead of waiting for the other (review finding 9).
- When the copy wins, log `item_rejected` from the hub's receipt instead of losing the line (review finding 5).

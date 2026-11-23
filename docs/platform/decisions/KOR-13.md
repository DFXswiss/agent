# KOR-13 – decisions (event latency: runner push cadence, hub pacing)

Both repositories carry this file with the same text. Architecture: ADR-048.

1. **Runner: leading edge, start to start, 100 ms** (`agent_runner.sync.push`). A push leaves at once when no push
   started within the last `MIN_INTERVAL_S`; otherwise it waits until one interval after the previous push *started*
   and reads the batch again after the wait. The start is recorded right before the request is sent, also when it
   then fails. After a refusal or a transport error the loop waits the whole `Retry-After` or backoff (1 s → 30 s);
   before KOR-13 a new outbox item (`notify()`) ended that wait early, which at the faster interval would have let a
   busy device retry up to ten times a second against a refusing hub (review finding, fixed with a test). A batch that
   is empty after the re-read (its items moved to a scope queue meanwhile) sends nothing. One lock
   serialises `push_once` for the loop, the reconnect drain and the shutdown flush, so one push is on the way at a
   time. A restarted runner pushes at once; the hub's reserve covers it. Unchanged: batch limits, order, scope queues
   (ADR-047), seal once, backoff, `429`/`409`/transport handling.
2. **Hub: token bucket per device** (`agent_hub.outbox.pacing`): rate one push per 100 ms, reserve 10 pushes,
   `Retry-After: 1` when empty (the smallest whole-second value), a refused push spends nothing, a clock step back
   grants nothing and does not lock the device out (same rule as the pipeline's limiter), at most 100 000 devices kept
   (least recently seen dropped, as before). The reserve absorbs a spread of travel times up to 900 ms; arrival
   gaps measured under PERF-05 were 103–139 ms (p5–p95), on T2 the wireless link stalled single pushes for up to
   about 1.8 s, which spends at most the pushes that pile up behind it and refills at once.
3. **Request budget.** 600 pushes per minute + 10 reserve at most, against 6000 requests per minute and device key
   (03-api.md §1, unchanged); the other device requests (state, pulls, requests, operations) stay far below the rest
   (the test `test_the_push_rate_stays_well_inside_the_general_device_limit` keeps the push share at most half).
4. **Choice of the interval (probe runs, not exclusive, other packages running).** PERF-03 p95, `E2E_PERF_RUNS` default:

   | Measurement | 100 ms | 50 ms | before KOR-13 |
   |---|---|---|---|
   | T1 | 18 ms | 17 ms | 270 ms |
   | T2 (wireless link) | 62 ms | 42 ms | 354 ms |
   | during PERF-05 (30 sessions writing) | 222 ms | not run | 483 ms |

   100 ms meets p95 ≤ 240 ms on all three, so the larger interval is taken (fewer, larger pushes). With one writing
   session both candidates give the same push rate (events are further apart than 100 ms, the leading edge sends each
   at once). Hub load: one writing session 6.6 pushes/s, hub process 3.7 % of one core (T1; T2 4.1 %), equal for both
   candidates; 30 writing sessions at 100 ms: 8.8 pushes/s, 19 items per push (p95 28; before about 100), hub process
   34 % of one core. Under PERF-05 the remaining time splits into runner outbox → hub receive p95 150 ms and hub
   receive → browser frame p95 75 ms (before 385 ms and 148 ms).
5. **Hub processing unchanged.** With about a fifth of the items per push the processing per push and the wait for the
   commit before the frames go out shrank with it; the target holds with the reserve without touching
   `agent_hub.outbox.process` (work package rule 3).
6. **Contract text and pairing of the checkouts.** The contract change is the description of
   `api/device-outbox.request.json` only (`contracts/changes/KOR-13.md`); `agent` carries the synced copy on its package
   branch (commit `Sync the contracts from agent-core.`), so the functional runs could pair the two package branches
   (`WP_OTHER_REF`) with equal `contracts/VERSION`. The merge sync after the agent-core merge makes the same change.
7. **Order of updates.** Hub before runners (runbook `hub-operation.md` §1): a new runner at an old hub would meet
   `429` with `Retry-After: 1`. The integration branches were merged in that order.
8. **Review findings not changed (low).** Aggregate pacing across devices is not added: the general per-key limits
   and the per-device bucket bound every device, and the hub runs as one process (ADR-002); the scaling estimate is in
   ADR-048. The two package branches carry the same `contracts/VERSION`; it is checked again after both merges.
9. **Unchanged measuring code.** `e2e/platform/perf/` and `e2e/platform/orchestrator/routes/latency.py` are untouched.
   The heartbeat 250 ms after a VM state change (`agent_runner.sync.ws.EARLY_BEAT_S`, 03-api.md §6.2) is a separate
   rule and stays.

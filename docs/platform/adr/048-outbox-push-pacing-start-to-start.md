# ADR-048: Outbox pushes are paced start to start; the hub paces each device with a token bucket

Status: Accepted (2026-11-23); amended by ADR-050 (a late push is sent once more)

## Context
- PERF-03 (07-testing.md §2.2) requires runner outbox write → browser frame p95 < 300 ms on every topology and while
  30 sessions write (PERF-05). The final performance measurement (PFM-01, decisions PFM-01 items 8 and 12) missed it on
  T2 (354 ms, wireless link between the devices) and under PERF-05 (483 ms); T1 passed with 270 ms.
- The decomposition put the time on the way runner → hub, not on the hub → browser leg (p95 10–13 ms quiet): the
  specification allowed one push per 250 ms (05-runner.md §2.3, 03-api.md §1 and §6.1), the runner waited 250 ms from
  the **end** of the previous push, and the hub admitted one push per device every 250 ms counted by arrival, answering
  an earlier one with `429` and `Retry-After: 1`. An event waited up to 250 ms plus the push in flight and then
  travelled one push (p95 ≈ 237 ms + 1.95 × push duration). Under PERF-05 each push carried about 100 items and took
  about 100 ms at the hub.
- The concept asks for synchronisation "with as little delay as possible" (R-6.2: outbox push on every new item). The
  owner of the specification decided (KOR-13) that the product becomes faster: the bound and the measuring point of
  PERF-03 stay, the wireless link stays part of T2.

## Decision
1. **Runner: leading edge, start to start.** A push starts at once when an item is due and no push started within the
   last interval; otherwise it starts one interval after the **start** of the previous push. Items committed while a
   push is on the way are collected and leave together with the next push (the batch is read again after a wait).
   At most one push is on the way (one lock for the push loop, the reconnect drain and the shutdown flush). The
   interval is **100 ms** (`agent_runner.sync.push.MIN_INTERVAL_S`). Batch limits (500 items, 4 MiB), order, scope
   queues (ADR-047), sealing before sending, backoff and the handling of `429`, `409` and transport errors are
   unchanged; after a refused or failed push the loop waits the whole `Retry-After` or backoff even when new items
   are written meanwhile (only the idle wait ends on a new item). A restarted runner remembers no interval and pushes
   at once.
2. **Hub: token bucket per device.** The outbox admits the pushes of a device with a token bucket of rate one push per
   100 ms and a reserve of **10** pushes (`agent_hub.outbox.pacing`: `MIN_INTERVAL_SECONDS`, `BURST`). A refused push is
   `429 rate_limited` with `Retry-After: 1`, is not processed and spends nothing. Pushes started at least 100 ms apart
   can arrive closer together when the way jitters; the reserve absorbs a spread of travel times up to 900 ms, so a
   conforming runner never sees `429`, while a device that pushes faster for good is held to ten pushes per second.
   The pacer stays per hub process and keeps at most 100 000 devices.
3. **Choice of the interval by measurement.** Candidates 100 ms and 50 ms; the larger interval is taken when it keeps
   PERF-03 p95 ≤ 240 ms (20 % below the bound) on T1, on T2 and under PERF-05, because a larger interval means fewer,
   larger pushes and less load at the hub. The measurements are in `docs/platform/performance/results.md` (agent-core)
   and the decisions KOR-13.
4. **General device limit.** At the full rate a device sends at most 600 pushes per minute plus the reserve of 10; the
   general limit of 6000 requests per minute and device key (03-api.md §1) keeps more than 5000 for the other device
   requests (state, pulls, requests, operations; heartbeats go over the WebSocket) and is unchanged.
5. **Hub processing unchanged.** With pushes every 100 ms a push under PERF-05 carries a fraction of the earlier 100
   items, so the processing per push and the time until the frames go out after the commit shrink with it;
   `agent_hub.outbox.process` keeps its per-item semantics (KOR-13 measured before deciding).
6. **Order of updates.** The new hub admits old runners (one push per 250 ms is well inside the bucket). A new runner
   at an old hub would meet `429` (and wait a second each time). The hub is therefore updated before the runners
   (runbook `hub-operation.md` §1).

## Consequences
- An event waits at most one interval plus the push in flight; typical PERF-01/02 transitions no longer pay about
  0.25 s each.
- The hub receives up to 2.5 times as many pushes per device, each smaller; the per-push overhead (signature check,
  transaction, device row lock) is paid more often. Measured (decisions KOR-13): one writing session 6.6 pushes/s and
  about 4 % of one core for the hub process, 30 writing sessions on one device 8.8 pushes/s and 34 % of one core. The
  push rate follows the event rate (leading edge) and is capped at 10 per second and device, so the hub's push load
  grows with the number of busy devices, at most about 6 ms of hub CPU per push; a hub serving many busy devices at
  once needs a core per about 15–20 saturated devices for the pushes alone (one hub process, ADR-002).
- `contracts/api/device-outbox.request.json` describes the new pacing (description text only, `contracts/changes/KOR-13.md`).

## Alternatives considered
- Moving the measuring point of PERF-03 to the hub receive (R-6.9) or wiring T2: rejected by the specification owner.
- Start-to-start pacing at 250 ms with jitter tolerance: estimated p95 ≈ 305 ms on T2 (PFM-01), still missing.
- A push per item without any interval: unbounded request rate at the hub during terminal bursts.

## Deviation from DESIGN.md
None beyond ADR-001.

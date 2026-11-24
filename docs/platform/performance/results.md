# Performance results (PERF-01..PERF-05, final acceptance PFM-01, re-measured after KOR-13)

Status: final (KOR-13, 2026-11-23). **PERF-01 to PERF-05 pass** on the exclusive measurement of the merged state.
PERF-03 (runner outbox write → browser frame, p95 < 300 ms) holds on T1 (27 ms), on T2 (291 ms) and while 30
sessions write (181 ms). The target reserve of KOR-13 (p95 ≤ 240 ms) is met on T1 and under PERF-05, not on T2: the
tail there comes from single pushes held up on the wireless link between the devices (section 3.3). Before KOR-13
(PFM-01) PERF-03 missed on T2 (354 ms) and under PERF-05 (483 ms); KOR-13 replaced the fixed push cadence of 250 ms
(ADR-048).

**Update (ACC-FINAL, ADR-050):** on the end state of the final acceptance PERF-03 on T2 gave 303.2 ms and 287 ms
(link mean about 70 ms instead of 26 ms). The runner now sends a push whose answer is late once more on another
connection (hedged push, ADR-050). Exclusive re-measurement on the final pair: PERF-03 T2 **192 ms**, T1 17 ms,
during PERF-05 177 ms; all other PERF criteria pass (section 7). Two more exclusive T2 runs (KOR-19): 230.7 ms and
246.8 ms (section 8).

This document holds the final performance evidence of `09-milestones.md` WP-M3-03 against the criteria of
`07-testing.md` section 2.2. Devices are named by role only (runner device, second device). Raw data (CSV, logs,
`exclusive.json`) are kept with the run artefacts of the work package; the CSV columns are described below. Each
table gives the PFM-01 value "before KOR-13" for comparison.

## 1. Conditions

Every counted run held all E2E and gate slots of the devices it used (`tools/wp/e2e.sh … --exclusive`); no other
work package ran on those devices meanwhile. Device load is the `uptime` load average (1/5/15 min) before and after.
Measured state: agent-core `f92cf9ef` (hub, web, E2E harness) with agent `64388e2b` (runner), the integration state
after KOR-13, KOR-14 and the runner change of KOR-15; golden `cache-golden-37d0a3aae514`.

| Run | Topology | Started – ended (UTC) | Load before | Load after | Running VMs before |
|---|---|---|---|---|---|
| T1 (PERF-01…05) | all on the runner device | 2026-11-23 04:16:21 – 04:40:19 | 1.93 2.42 2.80 | 4.79 5.46 4.25 | 0 |
| T2 (PERF-03, PERF-04, reconnect) | hub, browser and orchestrator on the second device; runner on the runner device | 2026-11-23 04:40:20 – 04:46:23 | runner 4.79 5.46 4.25; second 1.21 1.50 2.50 | runner 2.37 2.96 3.39; second 4.75 2.48 2.52 | 0 / 0 |

Before KOR-13: PFM-01 runs of 2026-11-22 23:16 (T2) and 2026-11-23 00:17 / 01:38 (T1, PERF-05 again after a runner
fix), golden `cache-golden-4b1fe3fab9b3` / `cache-golden-d815a4dd0900`.

Hardware roles: runner device Apple M5 Ultra, 36 cores, 512 GiB (VM budget 480 GiB, 60 slots of 8 GiB); second
device Apple M5 Max, 18 cores, 128 GiB. The link between the two devices is wireless: round trip 6.5–208 ms, mean
26 ms, standard deviation 35 ms (120 pings right after the T2 run; PFM-01: 7–209 ms, mean 46 ms). Clocks: the runner
device's clock is corrected by the offset measured before each evaluation (T2 −6.2 ms at 15 ms round trip); T1 needs
no correction. Runs per series: `E2E_PERF_RUNS=20`, `E2E_PERF_SHORT_RUNS=10`. The model is the deterministic double
of the suite (07-testing.md section 5).

## 2. Method

| Test | What is measured | Clock | Pass criterion | Spec / harness |
|---|---|---|---|---|
| PERF-01 | `session.first_working_at − task.created_at` of 20 first tasks in new private orgs (warm pool filled before each) and 10 first tasks in new workgroups of one org | hub (both ends) | every run and p95 < 10 s | `e2e/platform/perf/perf-01-time-to-working.spec.ts` |
| PERF-02 | same, pool forced to 0 (clone path), 10 runs | hub | every run and p95 < 10 s | `perf-02-clone-fallback.spec.ts` |
| PERF-03 | runner outbox write → browser frame of 1000 terminal events (marker lines written into a session's pane), T1 and T2 | runner device corrected by the measured offset; browser = hub device | p95 < 300 ms | `perf-03-event-latency.spec.ts`, `orchestrator/routes/latency.py` |
| Reconnect (WP-M3-03) | E2E-14 scenario: links runner ↔ hub and browser ↔ hub cut during a turn; after restore the reconnect wait (until the first backlog item arrives) and the catch-up (first to last item) of runner and browser | hub device | wait < 40 s (backoff cap 30 s + 20 % + 4 s), catch-up < 5 s | `perf-03-reconnect-latency.spec.ts` |
| PERF-04 | recorder flush → xterm render of the open terminal view (the flush follows the write into the pane by at most 100 ms, not included) | VM clock corrected by the measured offset | p95 < 1 s | `perf-04-terminal-latency.spec.ts` |
| PERF-05 | 30 workgroups of one org, first tasks sent at once; every session reaches `working` in its own VM; hub health and `agent-runner status` probed during the burst; PERF-03 measured while all 30 sessions write terminal output | hub | all 30 working, 30 distinct VMs, probes within 1 s / 10 s, PERF-03 p95 < 300 ms | `perf-05-parallel-vms.spec.ts` |

Percentiles are nearest-rank. Each time-to-working row carries the per-transition breakdown (hub receive time and
device time of every start state) and the scheduler's start estimate (`session.estimate_ms`).

CSV files of a run: `perf-time-to-working.csv` (PERF-01/02/05 and the A-30 probe), `perf-event-latency.csv`,
`perf-terminal-latency.csv`, `perf-latency.jsonl` (summaries with device load), `perf-reconnect.csv`,
`perf-perf05-appends.csv`, `perf-perf05-responsiveness.csv`, `perf-terminal-storage.csv`.

## 3. Results

All times in seconds (time to working) or milliseconds (latencies); nearest-rank percentiles.

### 3.1 PERF-01 and PERF-02 – time to working (T1)

| Series | Runs | Min | p50 | p95 | Max | Estimate shown | Result |
|---|---|---|---|---|---|---|---|
| PERF-01 first task, new org, warm pool | 20 | 0.61 | 0.62 | 0.64 | 0.64 | 3000 ms | pass |
| PERF-01 first task, new workgroup in an existing org | 10 | 0.61 | 0.65 | 0.77 | 0.77 | 3000 ms | pass |
| PERF-02 pool size 0 (clone path) | 10 | 6.60 | 6.64 | 6.70 | 6.70 | 8000 ms | pass |

Before KOR-13 (p50 / p95): new org 0.60 / 0.60, new workgroup 0.86 / 0.87, clone path 6.86 / 7.14 (PFM-01) and
6.49 / 6.57 (KOR-15 re-run of PERF-02 alone).

Breakdown per transition (hub receive times, p50 / p95 of the step's share):

| Series | → vm_assigning | → vm_ready | → sandbox_ready | → agent_starting | → prompt_delivering | → working |
|---|---|---|---|---|---|---|
| PERF-01 new org | 0.07 / 0.08 | 0.10 / 0.11 | 0.00 / 0.00 | 0.00 / 0.00 | 0.25 / 0.26 | 0.20 / 0.21 |
| PERF-01 new workgroup | 0.08 / 0.14 | 0.10 / 0.11 | 0.00 / 0.10 | 0.00 / 0.00 | 0.27 / 0.33 | 0.20 / 0.21 |
| PERF-02 clone | 0.09 / 0.13 | 6.07 / 6.14 | 0.00 / 0.00 | 0.00 / 0.10 | 0.25 / 0.27 | 0.20 / 0.21 |

Before KOR-13 most steps cost 0.26–0.27 s each at the hub: the push cadence of 250 ms. With the leading edge a
transition reaches the hub as soon as it is written; the remaining shares are the device's own work (pool assignment
about 0.1 s, the Claude TUI start about 0.25 s, the accepted prompt about 0.2 s; device times in
`perf-time-to-working.csv`). Transitions written within one push arrive with the same hub time (share 0.00). The
clone path is dominated by `limactl start` (VM boot and Lima's host agent). PFM-01 measured the clone step at
6.17–6.36 s device time, above the ≤ 6 s of 05-runner.md section 6.1; KOR-15 removed two listings of every Lima
instance of the device from that step (agent: decisions/KOR-15.md), and its exclusive re-run of PERF-02 measured
5.85–5.96 s device time. The budget is judged on the device time; the hub-time share above also contains the push of
the transition.

### 3.2 PERF-03 – event latency runner outbox write → browser frame

| Topology | Events | p50 | p95 | p99 | Max | Outbox → hub receive p50 / p95 | Hub receive → browser p50 / p95 | Result |
|---|---|---|---|---|---|---|---|---|
| T1 | 1128 | 10.6 | 26.6 | 93.8 | 118.2 | 6.5 / 22.0 | 4.0 / 6.3 | pass |
| T2 | 1125 | 24.5 | 291.1 | 339.8 | 440.0 | 18.9 / 284.8 | 5.5 / 7.8 | pass (reserve of 240 ms not met) |
| T1 while 30 sessions write (PERF-05) | 1149 | 97.3 | 180.8 | 311.3 | 542.0 | 70.4 / 133.1 | 27.8 / 48.2 | pass |

Before KOR-13 (p95): T1 270.4, T2 354.0 (miss), during PERF-05 482.8 (miss); hub receive → browser p95 10.2, 13.0
and 148.

### 3.3 Where the time of PERF-03 goes after KOR-13

The runner pushes at once when no push started within the last 100 ms and otherwise 100 ms after the start of the
previous push; one push is on the way at a time; the hub admits ten pushes per second and device with a reserve of
ten (ADR-048). No push was refused with `429` in any KOR-13 run (about 7600 pushes in the functional runs, none in
the measurements).

- **T1:** the terminal events of one session are further apart than 100 ms, so each leaves at once (one push per
  event); p95 27 ms is the push and the delivery.
- **T2:** the median event takes 25 ms. The tail comes in short bursts every few seconds: in those windows the hub
  received nothing for 480–570 ms, so one push was held up on the wireless link and the events written meanwhile
  waited for it (one push on the way at a time) and then travelled with the next. The runner's own wait is at most
  100 ms, the hub's part (receive → browser) 8 ms at p95; the remaining up to about 200–450 ms is the link (ping round
  trips up to 208 ms, standard deviation 35 ms). The probe runs of KOR-13 on the same link at another hour gave p95
  62 ms (decisions KOR-13). The bound holds; the 20 % reserve depends on the link's state.
- **During PERF-05:** 30 sessions write about 300 terminal events per second from one device. Pushes carry about 19
  items (p95 28; before about 100) and arrive every 103–139 ms (p5–p95, probe); runner outbox → hub receive is 133 ms
  at p95 (wait up to 100 ms plus the push), hub receive → browser 48 ms at p95 (before 148 ms). The hub's outbox
  processing was not changed (decisions KOR-13 5).

### 3.4 Reconnect latency (E2E-14 scenario, three cuts per topology)

| Topology | Outage | Runner: wait / catch-up | Browser: wait / catch-up |
|---|---|---|---|
| T1 | 12.3 / 12.3 / 12.3 s | p50 1832, max 3587 ms; 0 / 0 / 0 ms | p50 968, max 17842 ms; p50 3, max 895 ms |
| T2 | 12.4 / 12.4 / 13.3 s | p50 2225, max 4044 ms; 0 / 0 / 0 ms | p50 16045, max 16230 ms; 3 / 3 / 3 ms |

Wait = restore of the link until the first backlog item arrives (bounded by the clients' reconnect backoff, ≤ 40 s);
catch-up = first to last backlog item (≤ 5 s). The runner's backlog of a cut is pushed in one batch (catch-up 0).
Before KOR-13 (max): runner wait 132 / 175 ms, browser wait 3116 / 3527 ms (T1 / T2). The waits depend on where in
its backoff (1 s → 30 s ± 20 %) a client is when the link returns; since KOR-13 a refused or failed push waits its
whole backoff even when new items arrive (decisions KOR-13 1), so the runner's first push after a cut follows the
backoff or the reconnect of its socket, whichever comes first. All within the bound of 40 s.

### 3.5 PERF-04 – terminal latency recorder flush → xterm render

| Topology | Chunks | Min | p50 | p95 | Max | Result |
|---|---|---|---|---|---|---|
| T1 | 349 | 11.7 | 31.7 | 99.7 | 258.7 | pass |
| T2 | 345 | 27.6 | 46.6 | 306.6 | 373.6 | pass |

Before KOR-13 (p95): T1 287.9, T2 362.3. Adding the flush interval of 100 ms as worst case still leaves the p95 below
0.5 s against the bound of 1 s.

### 3.6 PERF-05 – 30 parallel first tasks (T1)

| Measure | Value | Bound | Result |
|---|---|---|---|
| Sessions working / distinct VMs | 30 / 30 | 30 / 30 | pass |
| Time to working of the 30 (hub clock) | p95 14.91, max 14.94 s | every session ≤ 300 s | pass |
| 30 posts sent / all working | 0.98 s / 17.3 s after the first post | – | – |
| Hub health during the start | p50 2.7, max 9.1 ms (idle max 5.9 ms) | 1000 ms | pass |
| `agent-runner status` during the start | p50 192, max 223 ms | 10000 ms | pass |
| PERF-03 while all 30 sessions write | 1149 events, p50 97.3, p95 180.8, max 542.0 ms | p95 < 300 ms | pass |

Before KOR-13: time to working p95 14.08 s, hub health max 11.2 ms, runner status max 248 ms, PERF-03 p95 482.8 ms
(miss). 24 tasks were shown the warm-pool estimate (3000 ms) and 6 the clone estimate (8000 ms). The device ran 35 VMs
(286 720 MiB of 491 520 MiB) and the hub at a load of about 8–10. 10 browser frames of other scopes had no device
event of the measured session and are not counted. During the stop window the hub log holds 2185 requests (2125 ×
`200`, 60 × `404` = the runner's credential look-ups), no `429`/`5xx`, and PostgreSQL logged no deadlock.

## 4. Further measurements (AUD-01)

- **A-30 (start estimate after a pool change; PFM-01 run).** Probe in PERF-05: two tasks empty the pool of 2, a third follows
  1.04 s after the runner reports the pool empty: estimates 3000, 3000, 8000 ms – the third task already shows the
  clone path (the runner's early heartbeat after a VM state change). In the burst of 30 tasks sent within 1.3 s,
  17 got 3000 ms and 13 got 8000 ms although only 2 took the pool: tasks created before the next heartbeat all see the
  last reported `pool_ready` (residual risk, decision PFM-01 5).
- **B-19 (terminal storage at the hub, ADR-016).** The 30 PERF-05 sessions stored 30 098 terminal chunks: 3.49 MB
  ciphertext, 21.4 MB row payload (712 bytes per chunk). Rate at this synthetic load (one marker line per 0.1 s and
  session): 14.0 MB of event rows per session hour (PFM-01: 14.1). Size of `hub.event` after the run: 44.9 MB for
  33 137 rows. This is an upper bound for continuous output; a real session writes less while it waits.
- **B-20 (membership appends against `FOR SHARE` readers; audit-lock deadlocks).** `POST /v1/orgs/{org}/log`: idle
  p50 81 ms, max 95 ms; while 30 sessions push terminal events into the same org p50 169 ms, max 302 ms; all 20
  succeeded without retry (KOR-13 measurement; PFM-01: 63 / 83 and 151 / 212 ms). Stopping all 30 sessions at once while 29 still write: 30 × `200`; the hub log of the
  window holds 1073 requests (1013 × `200`, 60 × `404` = the runner's credential look-ups) and no `429`/`5xx`;
  PostgreSQL logged no deadlock. No starvation observed.

## 5. Web performance budget (06-web.md section 12.2)

The initial JavaScript (≤ 300 KiB gzip) and the time to interactive on the throttled phone profile (≤ 3 s) are
checked by the web gates on every merge (`web/build/budget.ts`, `web/e2e/performance.spec.ts`, decision WEB-13); they
are not repeated here.

## 6. Open points

1. **Closed (KOR-13, ADR-048): PERF-03 on T2.** p95 291 ms (was 354 ms). The 20 % reserve is not met on the wireless
   link: single pushes held up for about half a second dominate the tail (section 3.3); the runner's wait and the hub
   are not the limit. A wired link or a lower jitter of the link would restore the reserve; a further product change
   would have to send several pushes at once, which ADR-048 rules out to keep the order and the hub's load simple.
   Superseded by ADR-050 (section 7): a late push is copied on another connection, which keeps one batch on the way.
2. **Closed (KOR-13, ADR-048): PERF-03 during PERF-05.** p95 181 ms (was 483 ms), with the hub's outbox processing
   unchanged.
3. **Fixed (KOR-14, ADR-049): a session failed with `capacity_exhausted` is re-scheduled.** The hub starts the next
   attempt of the task at once and keeps the refusing device out until it reports a free slot (was: the session stayed
   `failed`, seen in the trial before the runner fix).
4. **Clone step under load (closed for the single clone by KOR-15).** Alone the step now holds its budget: 5.85–5.96 s
   device time (was 6.17–6.36 s; the runner listed every Lima instance of the device twice per clone, agent:
   decisions/KOR-15.md). With 28 parallel clones it takes 10.7–12.8 s: 28 VMs × 4 vCPU boot at once on 36 cores;
   the runner's serial sections are short. Not optimised; the estimate in such a burst stays the residual risk of
   PFM-01 decision 5. The PERF bounds hold (PERF-02 max 6.70 s in the KOR-13 measurement).

## 7. Re-measurement after the hedged outbox push (ACC-FINAL, ADR-050)

Final pair agent-core `5e2a9e88`, agent `23ef07dd`, golden `cache-golden-42cc7cdff91c`; one exclusive run per
topology (`tools/wp/e2e.sh … --exclusive`, no other run on the devices, no running VM before; default sample counts).
Details, loads and the decomposition: `docs/platform/acceptance/FINAL.md` section "Performance".

| Test | Bound | Before (ACC-FINAL end state, two T2 runs) | After ADR-050 | Result |
|---|---|---|---|---|
| PERF-01 new org / new workgroup, p95 | < 10 s | 0.62 / 0.66 s | 0.63 / 0.67 s | pass |
| PERF-02 clone path, p95 | < 10 s | 6.58 s | 6.54 s | pass |
| PERF-03 T1, p95 | < 300 ms | 15.5 ms | 17.1 ms | pass |
| PERF-03 during PERF-05, p95 | < 300 ms | 164.5 ms | 177.2 ms | pass |
| PERF-03 T2, p95 | < 300 ms | 303.2 / 287 ms | 192.4 ms | pass |
| PERF-04 T1 / T2, p95 | < 1 s | 49.7 / 294.4 ms | 54.9 / 290.7 ms | pass |
| PERF-05 | all criteria | pass | 30/30 working, p95 14.4 s, health p95 9.3 ms, status p95 230.7 ms | pass |

On T2 runner outbox write → hub receive p95 fell from 297 / 281 ms to 185 ms at an unchanged median of 25 ms; hub
receive → browser stayed at 8 ms. The link before and after the run: mean 68 / 69 ms, maximum 121 / 124 ms (before
ADR-050: 68–76 ms, maximum up to 217 ms). Copies: 54 on T2, 3 on T1 (during PERF-05), no `429`. The 20 % reserve of
KOR-13 (≤ 240 ms) is met on all three PERF-03 measurements.

## 8. Confirming runs after the hedged outbox push (KOR-19)

Two more exclusive T2 runs (PERF-03, PERF-04, reconnect) on agent-core `93f3a346`, agent `7fa866e3` (no product
change since the final pair), golden `cache-golden-42cc7cdff91c`. The run of section 7 is listed with them. p95 is the
nearest rank recomputed from `perf-event-latency.csv`.

| Run (UTC) | n | median | p95 | max | hub → browser p95 | outbox → hub p95 | PERF-04 p95 | copies | `429` | link mean before / after (max) |
|---|---|---|---|---|---|---|---|---|---|---|
| ACC-FINAL attempt 3, 09:56–10:01 | 1 122 | 25.0 ms | **192.4 ms** | 345.6 ms | 8.1 ms | 184.6 ms | 290.7 ms | 54 | 0 | 68.2 / 69.3 ms (121 / 124) |
| KOR-19 run 1, 11:19–11:25 | 1 122 | 25.4 ms | **230.7 ms** | 347.7 ms | 8.0 ms | 225.4 ms | 238.8 ms | 60 | 0 | 71.6 / 69.6 ms (122 / 130) |
| KOR-19 run 2, 11:26–11:32 | 1 123 | 24.7 ms | **246.8 ms** | 362.7 ms | 7.9 ms | 241.6 ms | 276.5 ms | 57 | 0 | 69.5 / 73.7 ms (189 / 208) |

PERF-03 on T2 holds its bound of 300 ms in all three runs after ADR-050 (before: 291.1, 303.2 and 287 ms). The 20 %
reserve of KOR-13 (≤ 240 ms) is met in two of the three; run 2 (246.8 ms) misses it, on the worst link of the three.
The hub's share stays at 8 ms. Load and waiting time: `docs/platform/decisions/KOR-19.md` section 3.


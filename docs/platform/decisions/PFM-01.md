# PFM-01 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## PFM-01 – decisions in the agent repository (final PERF acceptance)

1. **A hot-path clone occupies one VM slot, not two.** PERF-05 (30 first tasks at once on the runner device) failed one
   session with `capacity_exhausted` although the device had 60 slots and fewer than 40 VMs: the clone's reservation
   (`VmManager.reserved`, needed between the capacity check and the insert) stayed counted after its `assigning` row
   existed, and that row occupies a slot as well (`capacity.SLOT_STATES`, 05-runner.md section 3.4). With 28 parallel
   clones the device counted about 63 occupied slots. The reservation now ends right after the insert
   (`VmManager._slot` yields `release`); a restart keeps its reservation to the end, because its row is `stopped`
   (no slot) until it runs (a `stopping` row it restarts counts twice meanwhile, on the safe side). Test:
   `test_a_hot_path_clone_occupies_one_slot`.
2. **One lock from the capacity check to the claim.** With exact counting the apparent limit is the real one, so an
   old race mattered: the check awaits the store, and two clones of different workgroups (or a clone and a pool
   refill) could both take the last free slot and overbook the VM memory budget. `Pool.slot_lock` is now held from the
   check to the claim by `VmManager._slot` (including a needed LRU stop) and by the refill (`missing` and `filling`).
   Test: `test_two_clones_never_share_the_last_slot` (a barrier makes both checks run before either claims).

## PFM-01 – decisions (final PERF acceptance PERF-01..PERF-05)

1. **PERF-05 after stopping the earlier VMs.** The PERF specs of one iteration share the run's services; PERF-01/02
   leave about 40 workgroup VMs running (idle stop after 30 min, 05-runner.md section 3.5), which would take slots and
   VM memory the 30 workgroups of PERF-05 need (section 3.4, 07-testing.md section 7). PERF-05 first calls the new
   control route `/perf/quiesce`: the run's runners restart with the minimum idle intervals (60 s) until the hub lists
   no running workgroup VM, then restart with their earlier values. The spec then requires the running VM memory plus
   the burst to fit the device's VM memory budget and enough free VM slots, and at the end 30 distinct VMs. A second
   runner instance on the same device (first draft) was rejected in review: it counts the same RAM twice.
2. **30 first tasks at once through the API.** The UI sends one prompt at a time; to start 30 first tasks in parallel
   the spec assigns the device to every workgroup first (the `device_assign` batch of the first-task flow, built with
   the web core like `ensureDevice`) and then posts the 30 tasks concurrently (`PageUser.createTasks`). The app's
   best-effort wrap of the user's own `CK` for the device is left out: the sessions use the platform's model access
   (no user credential), and importing `core/tasks/send.ts` into Node pulls in Vite-only code.
3. **Responsiveness of PERF-05.** 07-testing.md defines it as "PERF-03 holds": after all 30 sessions work, the marker
   writer of SCN-08 (`/latency/marks`) writes into all 30 panes, the browser follows the first session's terminal and
   the event latency of 1000 events is evaluated with the PERF-03 harness (`/latency/events`, p95 < 300 ms). In
   addition the spec probes the hub's health from
   the test process (own connection, not the browser's pool of six HTTP/1.1 connections that carries the 30 task
   posts; every 3 s, inside the anonymous limit of 30/min) and the runner (`agent-runner status --json`, every 5 s)
   during the burst, with bounds of 1 s and 10 s (p95 and maximum). The 30 posts leave the page within `sent_ms`
   (reported); `429` answers of the membership appends under load are retried and counted, not failures.
4. **Reconnect latency filed under PERF-03.** WP-M3-03 names it without a test ID; it extends the event latency
   (R-6.2, R-6.9) and is measured in the scenario of E2E-14 (`perf/perf-03-reconnect-latency.spec.ts`, tag
   `@PERF-03`), so a PERF run selects it with `--only PERF-03` and functional E2E-14 runs stay unchanged. Each side is
   split into the reconnect wait (restore until the first backlog item arrives) and the catch-up (first to last).
   Bounds: the wait by the clients' backoff cap (30 s + 20 % jitter, 03-api.md section 6.2, 06-web.md section 12.5)
   plus 4 s, i.e. 40 s; the catch-up by 5 s (no target in the specification; a turn's backlog is a few items).
5. **A-30 (countdown estimate after a pool change).** The early heartbeat of the runner after every VM state change
   (agent, ACC-M1) addresses it. Evidence: every time-to-working row now carries `session.estimate_ms`; PERF-01 asserts
   3000 ms (pool known filled) and PERF-02 8000 ms. Because PERF-02 onboards for 15 s after emptying the pool (longer
   than the regular heartbeat), PERF-05 adds the real case: two tasks take both pool VMs and a third follows 1 s after
   the runner reports the pool empty; its estimate must be 8000 ms. Not fixed (low, recorded as residual risk): tasks
   created within one heartbeat window (≈0.3 s, e.g. 30 at once) all see the last reported `pool_ready` and get
   3000 ms although only two pool VMs exist; correcting it would need the hub to count pool claims across concurrent
   task transactions (a per-device lock on the task hot path), which the measured countdown error does not justify.
6. **B-19 (terminal storage).** Measured on the 30 PERF-05 sessions after their marker output from `hub.event` (`/perf/terminal-storage`): chunks, ciphertext and row
   payload bytes per session and the rate per session hour, plus the size of `hub.event`; results in
   `docs/platform/performance/results.md`. No retention rule is introduced here (DOC-01/KOR-03).
7. **B-20 (membership appends against `FOR SHARE`, audit-lock deadlocks).** Measured in PERF-05: 10 membership appends
   idle and 10 while 30 sessions push terminal events into the same org, then a stop of all 30 sessions at once while
   29 of them still write (their writers run 1.5 times as long as the measured one); the hub's refusals (429/5xx from the access log) and PostgreSQL's `deadlock detected` lines of that window
   are counted (`/perf/hub-log`). The spec requires every append under load to succeed.
8. **PERF-03 on T2 missed: cause and why no product change.** The exclusive T2 measurement gives p95 354 ms
   (1128 events, p50 210 ms, max 533 ms) against 300 ms. Decomposition from the samples: hub receive → browser frame
   p95 13 ms (the target of R-6.9, 03-api.md section 5 and 09-milestones.md, met by far); runner outbox write → hub
   receive p95 344 ms. The second part is the push cadence: the specification allows at most one push per 250 ms
   (05-runner.md section 2.3, 03-api.md section 6.1); the runner implements it as 250 ms after the previous push
   ended (`agent_runner.sync.push`, the hub's pacer counts by arrival), so an item waits up to 250 ms plus one push round
   trip and then travels one push; the batches arrived every 307 ms (median). The link between the runner device and
   the second device is wireless: round trip 7–209 ms, mean 46 ms (ping during the run window), against well under
   1 ms on T1, where the same harness gives p95 270 ms. With the 250 ms floor, p95 < 300 ms for "outbox write →
   browser frame" needs about twice the push duration plus hub → browser to stay below about 60 ms, which the wireless link does not give.
   Considered and rejected as a fix here: pacing the pushes start to start instead of end to start saves one round
   trip per cycle (estimate p95 ≈ 305 ms on this link, still missed). Without jitter tolerance in the hub's pacer
   (it counts by arrival and answers an early push with `Retry-After: 1`) every arrival closer than 250 ms would cost
   a whole second, so it is a joint change of runner and hub with its own risk, and it would not reach the bound on
   this link either. The wireless link belongs to the test bed, not to the product. `TCP_NODELAY` in the test relay changed nothing measurable.
   The miss is reported as a high-severity defect for the specification owner (choose between the measuring point of
   R-6.9/03-api.md section 5, a wired link for T2, or a faster push cadence with jitter tolerance in the hub).
9. **NB-18: PERF-05 asserts a time bound.** 07-testing.md gives PERF-05 no time target ("all reach `working`"). The
   spec now asserts that every one of the 30 sessions works within 300 s of its task (`PARALLEL_CEILING_S`, evaluated
   by the hub's clock like PERF-01), while it waits up to 900 s for all of them, so a slow start fails on the bound
   with the per-session breakdown instead of a poll timeout.
10. **Review findings not changed (low).** The burst's budget check counts the running pool VMs but not their refill
   (2 VMs): the runner's own budget check (05-runner.md section 3.4) refuses a refill beyond the budget, so the burst
   cannot be starved by it. PERF-04 is measured from the recorder's flush (the event time the runner records), not
   from the write into the pane; the flush interval of 100 ms is an upper bound of the difference and is stated with
   the results. The B-19 rate per session hour is extrapolated from minutes of synthetic marker output and is reported
   as an upper bound for that load. The B-20 stop window's refusals and deadlocks are measured and reported, not
   asserted (AUD-01 asks for the share). The `E2E_PERF_RUNS=20`/`E2E_PERF_SHORT_RUNS=10` of the acceptance are set by
   the caller (`tools` pass every `E2E_*` variable through); the default stays 5 for trial runs (decision SCN-01).
   The Nagle switch in the test relay was dropped again because it changed no measured latency.
11. **PERF-05 lets the session's request budget refill after its set-up.** Creating 33 workgroups and assigning the
   device to each spends the browser session's 600 requests per minute (03-api.md section 1); the first exclusive run
   stopped with `429` on the first measured task. The spec now waits one minute (a full refill of the token bucket)
   before the A-30 probe and retries the first two probe posts on `429` like the set-up; the third is not retried,
   because its timing (1 s after the pool emptied) is what the probe measures.
12. **PERF-03 under PERF-05 is reported, not optimised here.** With 30 writing sessions the event latency is p95
   483 ms; the causes are the push cadence and the hub's per-item processing (results.md section 3.6). Both need a
   change of the runner's pacing together with the hub's pacer or a batched rewrite of the hub's outbox processing,
   each a change of its own with protocol and transaction risk; it is reported as a high-severity defect (open point 2
   of results.md). The runner defect found on the way (a clone counted twice against the VM slots, one of 30 sessions
   `capacity_exhausted`) is fixed in the agent repository (decision PFM-01 1 there).

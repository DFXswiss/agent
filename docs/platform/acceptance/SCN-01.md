# SCN-01 – acceptance evidence (M1 scenarios A: E2E-01/06/07/08, PERF-01/02)

Devices by role only: **runner device** (T1: everything; T2: runner, VMs, doubles) and **second device** (T2: hub,
PostgreSQL, browser, orchestrator). Golden image: shared cache of the integration state, adopted by the run's runner
(`agent-runner image adopt`), warm pool 2 VMs. Model double on the runner device, reached in the VMs through the guest
loopback tunnel of ADR-034.

## E2E (07-testing.md section 2.1)

| Run | Specs | Result |
|---|---|---|
| `e2e.sh SCN-01 --topology t1 --only E2E-01,E2E-06,E2E-07,E2E-08 --repeat 3` | E2E-01, -06, -07, -08 | 3/3 iterations green |
| `e2e.sh SCN-01 --topology t2 --only E2E-01,E2E-06,E2E-08 --repeat 3` | E2E-01, -06, -08 | 3/3 iterations green |

Standard end checks of every iteration: no egress violation (connections only through the relays to hub and model
double, plus the VM operating systems' NTP/DNS to the device's resolver), model double `state.ok`, runners alive, VMs
of the run deleted. The audit-chain check is reported as not available until `agent-hub audit verify` exists (HUB-16).

Time to working from the hub database (`session.first_working_at − task.created_at`), T1, iterations 1/2/3:

| Session | Path | Seconds |
|---|---|---|
| E2E-06 and E2E-08: first task of a new private org | warm pool | 0.82 in every case |
| E2E-08: further task of the same workgroup | running VM | 0.90 / 0.70 / 0.71 |
| E2E-07 (a) clone path; the spec cuts the link to the hub during the start | clone | 6.72 / 6.71 / 6.72 |
| E2E-07 (c) task after the runner restart | running VM | 1.21 / 1.23 / 1.17 |

T2 (hub on the second device, runner across the LAN), iterations 1/2/3: E2E-06 1.04 / 1.04 / 1.04 s (first task of
a new org, warm pool), E2E-08 1.08 / 1.13 / 1.13 s and 0.79 / 0.79 / 0.79 s (further task of the workgroup); no
egress violation.

## PERF (07-testing.md sections 2.2 and 5.3)

`e2e/platform/perf/`, CSV `perf-time-to-working.csv` in the run's artefacts (per task: total and, per start state,
hub receive time and device time after `task.created_at`). Five runs per series here; the acceptance numbers (20/10/10)
are taken by ACC-M1/PFM-01 (`E2E_PERF_RUNS=20 E2E_PERF_SHORT_RUNS=10`).

| Series | Runs | p95 | max | Pass (< 10 s) | Longest step |
|---|---|---|---|---|---|
| PERF-01 new org (warm pool) | 5 | 0.82 s | 0.82 s | yes | `vm_ready` 0.51 s at the hub (device: 0.05 s; push pacing) |
| PERF-01 new workgroup in an existing org | 5 | 0.82 s | 0.82 s | yes | `vm_ready` 0.51 s at the hub |
| PERF-02 clone fallback (pool 0) | 5 | 6.89 s | 6.89 s | yes | `vm_ready` 6.2–6.3 s (clone and boot) |

Run on the merged state (`e2e.sh SCN-01 --topology t1 --only PERF-01,PERF-02`). Every start state's hub receive time
and device time are in the CSV; no run reached 10 s.

## Integration fixes found by the scenarios

- Runner: `session.start` hint wakes the reconcile run (a further task of a workgroup waited up to 30 s); outbox push
  pacing counted from the end of the previous push (`429 Retry-After: 1` inside session starts); `image adopt`.
- Orchestrator: operator key file and T2 operator relay, enrollment route, pool drain, golden adoption, guest tunnels,
  reader role grant, runner-device environment in T2, fresh process log after a restart.

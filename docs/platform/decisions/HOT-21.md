# HOT-21 – The web suite is short again: lanes instead of phases

## Cause

`npm run test:e2e` took about 30 minutes in the package gate (Node sees 3 cores there, so Playwright's default is
one worker) and about the same in the merge gate. Two things made it serial:

- **Project dependencies are phases.** Playwright runs the projects of a run in phases: a project with
  `dependencies` starts only after every project of the earlier phases has finished, not only its own
  dependencies. `volume-seed` depended on the last projects of the other suites, `volume` on `volume-seed`,
  `volume-mobile` on `volume`, and the phones' device sweeps on `devices` and on each other: six phases, one after the
  other (seed 180 s, `volume` 329 s, `volume-mobile` 346 s after the rest).
- **One hub for every spec with the device double.** Specs in `e2e/devices/` run with one worker because every new
  platform device becomes the first scheduling candidate of the hub (WEB-04); two of them at once would steal each
  other's sessions. The two long specs (`attention-volume` 332 s, `chat-volume` 73 s of the measured 624 s of
  `devices`) therefore held up every other device spec.

## Decisions

1. **Lanes** (`web/e2e/fixtures/env.ts`): a lane is a hub test server of its own – own database, device double,
   community keeper and port pair. `main` serves every project not listed in `PROJECT_LANES`; `volume`,
   `volume-mobile`, `mobile-devices`, `mobile-iphone-devices`, `attention-volume` and `chat-volume` each have a lane
   of their own. Projects of different lanes cannot see each other's devices, users or data, so they run at the same
   time with one worker each and nothing depends on anything: every project is in the first (and only) phase.
   The global setup starts the lanes of the selected projects side by side (`lanesOf`: all without `--project`).
2. **Ports**: the main lane keeps `WP_PORT_BASE+2` (hub), `+3` (probe server) and `+4` (device double);
   `e2e/performance.spec.ts` keeps `+5…+7`; lane n ≥ 1 takes `PORT + 6 + 2 (n − 1)` and the port after it. Six lanes
   besides `main` fill the package range exactly up to `WP_PORT_BASE+19` (a Vitest test checks the bound).
3. **The worker's lane**: an automatic worker fixture (`lane` in `fixtures/test.ts`, `enterLane` in the volume
   fixtures) sets `WEB_E2E_LANE` before any test; every helper that calls the device double uses
   `directorOrigin()`, which reads it. The page side needs nothing: each lane project's `baseURL` is its lane's hub.
4. **Order and workers**: the lane projects come first in `projects` (the dispatcher hands out workers in queue order,
   so each lane starts at once); `workers` is Playwright's default (half the cores Node sees) plus one per lane of
   the run, because the lane projects run one test at a time and mostly wait for their pages. The projects whose
   results do not depend on time (`desktop`, `mobile`, `mobile-iphone`) share the default share as before.
5. **The volume data set is seeded per lane** by the first volume worker of the lane (`seed` in `volume/data.ts`,
   inside the worker fixture; a repeat answers the finished seed). Its ids go to
   `test-results/.volume/seed-<project>.json`; the global teardown's completeness check reads every seed file. The
   `volume-seed` setup project is gone.
6. **No quiet machine needed**: the volume gate no longer waits for the rest of the suite. Its time budgets were
   already scaled by the slowdown calibrated in the measured tab (KOR-35 §7, `volume/speed.ts`); structural bounds
   (DOM elements, heap, rows) were never scaled. The measured runs (`board` report) show the slowdown the lanes cause.

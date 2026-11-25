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
   community keeper and port pair. `main` serves `desktop`, `mobile` and `mobile-iphone`; `volume`, `volume-mobile`,
   `mobile-devices`, `mobile-iphone-devices`, `attention-volume` and `devices` (with `chat-volume`, 77 s) each have a
   lane of their own. `devices` left the main lane because desktop specs that append to the platform log were refused
   with `peer_outdated` while a device spec's device without the feature was active (the first lane run showed it; in
   the old serial package run the two never overlapped). Projects of different lanes cannot see each other's devices,
   users or data, so they run at the same time with one worker each and nothing depends on anything: every project is
   in the first (and only) phase. The global setup starts the lanes of the selected projects side by side (`lanesOf`:
   all without `--project` or with a pattern; like Playwright, `--project` takes every value up to the next option,
   without regard to case). A Vitest test checks that every lane names a project of the configuration and that every
   lane project runs one test at a time.
2. **Ports**: the main lane keeps `WP_PORT_BASE+2` (hub), `+3` (probe server) and `+4` (device double);
   `e2e/performance.spec.ts` keeps `+5…+7`; lane n ≥ 1 takes `PORT + 6 + 2 (n − 1)` and the port after it. Six
   lanes besides `main` fill the package range exactly up to `WP_PORT_BASE+19` (a Vitest test checks the bound).
3. **The worker's lane**: an automatic worker fixture (`lane` in `fixtures/test.ts`, `enterLane` in the volume
   fixtures) sets `WEB_E2E_LANE` before any test; every helper that calls the device double uses
   `directorOrigin()`, which reads it. The page side needs nothing: each lane project's `baseURL` is its lane's hub.
4. **Order and workers**: the lane projects come first in `projects` (the dispatcher hands out workers in queue
   order, so each lane starts at once); `workers` is Playwright's default (half the cores Node sees) plus one per
   further lane of the run, because the lane projects run one test at a time and mostly wait for their pages. The
   projects whose results do not depend on time (`desktop`, `mobile`, `mobile-iphone`) share the default share as
   before.
5. **The volume data set is seeded per lane** by the first volume worker of the lane (`seed` in `volume/data.ts`,
   inside the worker fixture; a repeat answers the finished seed). Its ids go to
   `test-results/.volume/seed-<project>.json`; the global teardown's completeness check reads every seed file. The
   `volume-seed` setup project is gone.
6. **No quiet machine needed**: the volume gate no longer waits for the rest of the suite. Its time budgets were
   already scaled by the slowdown calibrated in the measured tab (KOR-35 §7, `volume/speed.ts`); structural bounds
   (DOM elements, heap, rows) were never scaled. The measured runs (`board` report) show the slowdown the lanes cause.
7. **The cached data set** (`scripts/hub_test_volume_cache.py`): a lane of the volume gate starts its hub with
   `--volume-cache DIR` (`WEB_E2E_VOLUME_CACHE`, else `web/node_modules/.cache/volume-data`). The key is a hash over
   the PostgreSQL major version, the migrations, `scripts/hub_test_*.py` and every source file of the packages the
   seed's code imports (loaded with the device double) or reaches over HTTP (`agent_hub/platform`,
   `agent_hub/device`): any change of what writes the data set makes a new entry. An entry is `pg_dump --data-only`
   of the database, taken by the device double right after the seed and before any follow-up route (invitation,
   join, live load) changes it, the device double's state (seed answer, founder, volume device and scope keys, live
   sessions, community key) and the hub signing key (the hub signs audit checkpoints). A data set shrunk by
   `VOLUME_COUNTS` is never stored, and with `VOLUME_COUNTS` set the lanes do not use the cache at all.
   - **Restore**: a usable entry (complete, younger than 12 h) is restored into the freshly migrated database before
     the hub starts: the user triggers are switched off by name (the append-only tables refuse `TRUNCATE` with
     `ENABLE ALWAYS` triggers, which even `session_replication_role = replica` fires), every table is truncated, the
     rows come back with `pg_restore --data-only --single-transaction` as replica (the foreign keys' internal
     triggers stay quiet; the rows come from a consistent database of the same schema), and every trigger gets its
     exact mode back (`ENABLE`, `ENABLE ALWAYS`, …) even when the restore fails. An entry that cannot be restored is
     removed, the database made anew and the data set seeded: a damaged entry costs one seed, never a red run; a
     failed snapshot only costs the next run a seed.
   - **Age**: the data set is placed relative to the time it was made (tasks 26–30 days back, charges 2 days, pull
     request links 1 day, messages over 20 days, invitations open for 20 days). No hub window – the 24 h of the
     situation view's done group, the billing days – has a seeded row near its edge, so 12 h change no view's content.
   - **Pruning**: at most six entries (about 150 MB each), the ones used last kept (a `used` mark is touched on every
     restore), never one used within the last 30 minutes (a lane may be restoring it); an expired entry of the same
     key is replaced by the next seed. Writing goes to a temporary directory that is renamed: two lanes seeding at
     once leave one whole entry.
   - **Tools**: `pg_dump`/`pg_restore` of the server's major version (`WEB_E2E_PG_BIN`, else the versioned
     directories of Homebrew and Debian, else `PATH`): a newer `pg_dump` writes settings an older server refuses.
     The superuser's password, if any, reaches them through `PGPASSWORD`, never the command line. The device double
     gets the superuser connection only in a lane that seeds into the cache (it takes the snapshot at the one moment
     the data set is complete); the cluster is the throwaway test cluster of the run.
   - Rejected: one seed shared by both lanes through a template database (`CREATE DATABASE … TEMPLATE` needs the
     source without connections, so the second lane could only start after the seed – the whole suite would wait),
     restoring under a running hub (its caches would not see the rows), shifting the timestamps of a restored entry
     (signed entries and events carry their time).
8. **The founder holds the community key** in a lane of the volume gate: there is no community keeper (a browser user
   of the suite) in those lanes, because a restored database would hold a keeper whose key the new keeper cannot
   open. The seed's founder creates epoch 1 with its self-wrap (as the first member does) instead of asking the
   keeper, and the device double serves the key requests of the lane's browser users once a second with the
   founder's identity (`serve_community`: open requests, wraps, the `community` frame `key` to the requester), as
   the keeper does in every other lane.

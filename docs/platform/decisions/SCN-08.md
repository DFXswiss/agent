# SCN-08 – decisions (M2f: E2E-29, mobile project of all web scenarios, PERF-03/PERF-04)

## Projects
1. **Two projects, one spec set.** `desktop` (Desktop Chrome) runs every spec except `specs/e2e-29-*`; `mobile`
   (Pixel 7 emulation, touch) runs every web scenario including E2E-29, without `perf/**` and `live/**` (they measure
   devices and live accounts, not the screen). Differences between the projects are expressed only through
   `data-testid` (e.g. the bottom navigation `shell.bottom.*` replaces `shell.nav.*` below `md`); no spec is
   duplicated. `run.sh --project` keeps its two choices.
2. **iPhone 15 and 360 px inside E2E-29.** 07-testing.md section 1 names Pixel 7 and iPhone 15 descriptors. A third
   project would need a further `--project` choice in the orchestrator (not owned by this package) and a full extra
   run; E2E-29 instead checks every screen additionally at the iPhone 15 viewport (393 px) and at 360 px (the M2f
   width) with the layout checks of WEB-13 (`web/e2e/helpers/layout-checks.ts`: horizontal overflow, touch targets
   ≥ 44 px, axe serious/critical). The WEB-13 browser suite runs the iPhone 15 project for all screens.
3. **Notification step of E2E-29.** The single phone user gets the `task.idle` notification of its own first task
   (the scenario lets the model wait 12 s, so the session works longer than the 10 s rule of
   `notifications/rules.py`); a second user only for a whiteboard notification would double the onboardings without
   testing more of the phone layout (E2E-26 covers the whiteboard notification).

## PERF-03 and PERF-04
4. **Load generator.** The orchestrator writes marker lines (`PERF-MARK nnnnn`) to the tty of the session's tmux pane
   (`POST /latency/marks`, a transient systemd unit in the VM), one every 150 ms (> the recorder's 100 ms flush), so
   nearly every line becomes one `session.terminal` event through the real chain recorder → journal → runner outbox
   → hub → SSE → browser. Writing to the pane's tty is terminal *output*; the session reads nothing (no input path).
   1.15 × the wanted count is written so that merged flushes still give enough events.
5. **PERF-03 end points.** Start: the runner outbox row's `created_at` (`now()` of the writing transaction, runner
   device clock; matched via hub `event.origin_seq`). End: arrival of the SSE frame in the page, taken by an init
   script that tees the page's `fetch` of `/v1/stream` (test-only, injected by Playwright; no product code path). Every
   `event`/`terminal` frame of a device event after the start counts.
6. **PERF-04 end points.** Start: the recorder flush (`event.ts`, VM clock), as E2E-12. End: two animation frames
   after the view's `data-chunks` count changed (xterm renders in the frame after the write; WebGL/canvas content is
   not readable from the DOM). Chunk `n` of the view is the `n`-th `session.terminal` event of the session (scope
   order), only chunks the hub received after the view opened count.
7. **Clocks.** The hub, the browser and the orchestrator share one device in both topologies (07 section 5.2). In T1
   the runner device is that device too (offset 0). In T2 the runner device's offset comes from 7 readings of
   `python -I -S -c time.time()` over the orchestrator's node, the VM's from 7 readings of `date` as root in the VM;
   the remote clock is read at the *end* of each round trip (after connection and process start), so the estimate
   is `remote − after` of the shortest round trip (error: the way back, a few ms on the LAN), not the midpoint
   (biased by half the process start; review finding). Offset and round trip are reported with each result.
8. **Artefacts.** `perf-event-latency.csv`, `perf-terminal-latency.csv` (one row per sample) and `perf-latency.jsonl`
   (summary, clock offset, `uptime` of hub and runner device per measurement) in the run's artefacts.
9. **Shared helper `perf/latency.ts`.** Both PERF specs share the setup and the browser probes in a new file next to
   them (the package owns `perf/perf-03-*`/`perf-04-*`; a helper avoids copying the probe code).
10. **Outbox of the right runner.** `POST /latency/events` refuses (409) frames whose device events do not belong to
    sessions of the named runner (`runner.session`), so another device's `origin_seq` can never be paired with them.
11. **PERF-03 measures terminal frames.** The load of the measurement is terminal output, so the samples are
    `terminal` frames (`session.terminal` events); other device events in the window count as well. The path
    outbox → push → hub → SSE is the same for every event kind; only the SSE frame type differs.
12. **Changes outside the owned paths (fixes of defects the full mobile run found, each with its test or run):**
    `fixtures/orchestrator.ts` (`loadScenario('model')` clears the credential check that `POST /model/scenario` set:
    without it every scenario after E2E-16/E2E-27 in one run fails with 401 - a run-isolation defect, not a mobile
    one), `fixtures/platform.ts` and the specs E2E-15/17/19/36 and PERF-01 (`navTo` instead of `shell.nav.*`, the only
    mobile difference, through `data-testid`), E2E-17 (sparkline instead of the uPlot canvas below md, as 06-web.md
    section 12.1 designs it), the T1-only skips of E2E-10/11/16/17/27/28/34/35/36 on T2 (07-testing.md section 2.1),
    E2E-00 (`github` link since the GitHub double runs in every run), `web/src/ui/whiteboard` (touch targets).
    Rebased onto SCN-03…07 without conflicts.
13. **Every spec has a browser part.** The mobile project runs all specs except PERF and live; each spec of
    `specs/` drives a page, so no browser-less scenario runs twice. `navTo(page, 'tasks')` needs a workgroup (the
    bottom bar shows "Tasks" only then); no spec uses it without one.
14. **`devicesOnline` waits only for paired devices.** In a full run E2E-04 leaves its unpaired org device
    (`hub.device.state` stays `active`, the membership has `removed_seq`, its runner is gone); E2E-16 and E2E-27 then
    waited 90 s for it after every runner restart and failed. Platform devices and org devices with a current
    membership are waited for.
15. **The VM clock route reads the offset at the end of the round trip as well.** `GET /sessions/{id}/clock` (E2E-12)
    took the midpoint of `limactl shell … date`; on T2 (ssh to the runner device, then limactl) the reading comes
    long after the midpoint, so the offset was too high and E2E-12's live latency too high (p95 1.12 s in the mobile
    run on T2 although the chain was fast). Same estimate as decision 7.
16. **Load-robust waits in the full mobile run.** E2E-03 waited the default 5 s for the empty invitation list after a
    revoke (several runs on the runner device at once: the signed revoke took longer) → 30 s for that wait only (the UI waits for the signed revoke; KOR-10/KOR-11 changed
    restore and retention, not this path, and ACC-M3 runs parallel lanes again); the reload check after the revoke
    keeps the default, the revoke is confirmed there already. E2E-14 compared the
    hub's acknowledged position with the outbox maximum read just before; the runner can write further items (metrics)
    in between → poll (30 s) until the hub acknowledged at least that position (`≥`). Its check "no `runner.*` audit of the device" failed on a held-over audit of the wrap race of an
    earlier scenario; agent decision SCN-08 4 holds such wraps back instead of auditing them.
17. **Overlap with the milestone-2 acceptance.** The acceptance merged the same suite fixes independently: the model
    double's credential reset (decision 12), the GitHub link of E2E-00, the device wait after a restart (decision 14:
    it now waits for the devices connected before the restart) and the E2E-14 acknowledgement poll (decision 16).
    The rebase keeps the integration branch's versions. This package's own commits for them are dropped or reduced to
    the parts the branch lacked (T1-only skips on T2, the E2E-03 revoke wait).

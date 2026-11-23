# WEB-12 – decisions

Monitoring and audit screens (06-web.md sections 2.1, 9, 10.2, 12.3, 12.4, 13; 08-monitoring.md section 6; 03-api.md
sections 4.11 and 5; ADR-018, ADR-019).

1. **Flows in the core.** `web/src/core/monitoring/` holds the framework-free parts: units (`units.ts`), the overview
   with live samples, freshness, network rates and the tree (`overview.ts`), ranges, windows, series requests, counter
   rates and chart data (`series.ts`), and the audit filters, pages, verification, names and event links (`audit.ts`).
   The slices `monitoring` and `audit` load and keep what the screens show. All of it is covered by Vitest alone; the
   screens only render it.
2. **Metrics frames only while the screen is open.** The live channel took `monitoring` only as a start option, so a
   running channel could not switch it. `LiveChannel.setMonitoring` reopens a running connection when the flag changes
   (as `setTerminals` does for watched terminals), and `AppSession.watchMonitoring(org)` counts the open monitoring
   views of an org and passes the flag to the active channel (and to a channel started later). These two small
   additions in `core/net/sse.ts` and `core/account/session.ts` (WEB-02/WEB-05 files) are the only changes outside the
   owned paths in `web/src`; they come with their own tests (`tests/core/net/monitoring-flag.test.ts`,
   `tests/core/account/monitoring.test.ts`).
3. **Freshness.** The hub's `stale` flag of the load holds until a newer sample arrives; the age is counted on the
   browser clock every second, so a value turns stale (> 60 s) while the screen stays open without new data. Stale
   items are greyed and show "Stale – last value {age} ago"; fresh ones "Updated {age} ago".
4. **Live samples.** A `metrics` frame of the active org's channel replaces an entity's newest sample only with a newer
   one (frames are at-least-once and may repeat, decision HUB-16 13); the replaced sample is kept as `previous` for the
   network rate. A read keeps what the previous overview knew (a newer live sample, the sample before the newest), so
   the rate does not fall back to totals after each read. A sample of an entity the overview does not list (a new VM
   or session) marks the overview for a reload, at most every 15 s after the last read; an entity that the reload
   still does not list (the hub samples more than the overview shows, e.g. a deleted VM) is not asked for again. The
   overview is also read every 60 s (presence, new devices); only the newest of overlapping reads is applied.
5. **Device → VM → session.** The overview's session items carry no VM (03-api.md section 4.11). The session row does
   (`vm_id`, linked by the hub from the device's `vm_state`), so the screen reads `GET /v1/sessions/{id}` once per
   listed session and follows `session` frames; until a row is read, a session is placed under the only VM of its
   workgroup if there is exactly one. Sessions without a VM of the overview and VMs whose device is not listed appear
   in a separate "Not linked" section. A row the hub refuses (`not_found`, `forbidden`) counts as "no VM"; after
   another failure the row is read again on the next overview; overlapping calls read a row once. The hub API (HUB-16)
   is not changed. The session's "Tasks" figure of the package text is its task (title, linked); the sample contract
   has no task count.
6. **Network values.** Samples carry counters. The overview shows the rate between the two newest samples once a live
   sample arrived, else the totals; the network chart shows bytes per second between buckets with a value (averaged
   over empty buckets, nothing after a counter reset).
7. **Ranges and steps.** 15 min and 1 h use 15 s buckets, 6 h 60 s, 24 h 300 s (raw samples); 7, 30 and 90 days use
   1800 s, 7200 s and 21600 s (rollups, multiples of 300 s); the panel names the source and the step ("Raw samples,
   one point per 15 s"). Windows are aligned to the step and fixed when a chart panel opens or the range changes;
   charts are not redrawn live (the overview values are). A series dropped while it loaded is not stored again.
8. **Charts.** Each entity opens CPU, memory (used/limit), disk (used/size; "Workspace" for a session) and network
   (received/sent). uPlot (lazy chunk with its static stylesheet, 06-web.md section 10.2) draws on wide screens with
   its legend and a cursor tooltip (time `HH:MM:SS.mmm` and values, placed in uPlot's cursor layer); x-axis ticks are
   24-hour times of the active locale (with the date beyond a day); the y axis starts at zero. Below `md` (768 px) a
   chart is an SVG sparkline of its first series with the newest value (no chart library loaded). Every chart has the
   "Data table" toggle: a table with caption and header cells, newest bucket first, "–" for a bucket without data.
9. **Units.** Bytes in binary units (KiB … PiB, one decimal below ten), rates per second, CPU in cores
   (`cpu_ratio` 1.0 = one core, 02-data-model.md section 2.8), shares and context in percent, cost in USD (four decimals
   below one dollar), durations in the two largest units (`3 d 4 h`).
10. **Audit time filter.** The hub has no time parameter (decision HUB-16 15). The filter is applied to the loaded
    rows; a load fetches up to five pages on its own while none of them holds a row of the range, and paging stops once
    the oldest row is older than "from". "Until" names a whole second and includes its milliseconds. Filters live in the URL query (`?action=…&actor=…&from=…`), times as UTC header
    times; the form keeps typed values on its own "Apply" and takes a URL from elsewhere (back button, link).
11. **Audit names.** The actor is shown by the display name of its verified `member.profile` (users) or its
    `device.profile` label (devices), else a neutral "User …"/"Device …" with the end of the ID; hub, operator and
    anonymous actors by kind. Targets of kind `user` and `device` are named the same way, others as `kind id`.
    Workgroups and task titles come from the verified profiles and `task.meta` events (pulled once per task of a
    shown row). The actor filter lists the active users and devices of the verified log, the workgroup filter the
    log's workgroups.
12. **Event links.** A row with `event_id` links to the session detail when it names a session, else to the task page
    (where the event is shown); rows without either have no link.
13. **Verification.** Only org admins see "Verify chain" (the route is `org:admin`, 06-web.md section 1.2). The result
    shows intact/broken, the head (row and the first 16 hex digits of the hash) and the newest checkpoint (row,
    publication time, hub key) or "No checkpoint published yet".
14. **Navigation and task tab.** Navigation entries `monitoring` (order 70) and `audit` (71); the task tab `audit`
    (order 50) uses the same view with the task filter fixed and hidden, keeping `?tab=audit` on apply.
15. **Playwright specs and test data.** Specs that use the device double run in the `devices` project (decision WEB-04:
    one at a time, each new device is the first candidate), so the monitoring spec and the audit task-tab spec are
    `web/e2e/devices/{monitoring,audit}.spec.ts`; the audit screen spec is `web/e2e/audit.spec.ts`. The phone layout is
    a `test.describe` with a Pixel-7-sized viewport inside the monitoring spec (the `mobile` project's list in
    `playwright.config.ts` is not edited). The device double gained `scripts/hub_test_metrics.py`: VMs, samples and
    usage are pushed through the real outbox (`POST /v1/device/outbox`, signed by the device, gapless `origin_seq`,
    paced to the hub's 250 ms limit), rollups older than the raw retention are written to `hub.metric_rollup` (no
    ingestion path exists). `scripts/hub_test_director.py` only routes `/devices/<id>/{vms,metrics,usage,rollups}` to
    it (a few lines and the docstring); the specs call it through the new helper `web/e2e/helpers/monitoring.ts`.
16. **Paging in the browser.** In the test hub the read-log rows (`read.*`) of a user did not appear in the org's audit
    list (they seem to be written without `org_id`; `_read_log` reads `path_params` from the request scope – reported
    for the hub, not changed here), so a private org without tasks has only a handful of audit rows. Paging is therefore exercised in the task tab, where 110 session events give 110
    `event.append` rows of the task.
17. **Test IDs.** Items and their parts are scoped by level (`monitoring.device.*`, `monitoring.vm.*`,
    `monitoring.session.*`, including `monitoring.<level>.charts-toggle`); the parts of a chart panel
    (`monitoring.charts`, `monitoring.chart[data-chart]`, `monitoring.range.<id>`, `monitoring.chart.table…`) are
    located inside the item. Audit: `audit.row[data-seq][data-action]`, `audit.row.*`, `audit.filter.*`,
    `audit.verify.*`.
18. **Accessibility.** The range switch is a radio group with arrow keys and a roving tab stop; loading texts are
    status regions; chart titles are `h3` under the device's `h2`; VM states and session states are shown with their
    catalog labels (session states from the task catalog), the device's runner state as reported.
19. **Rights in the browser.** The hub decides what the overview and the series return (HUB-16 tests cover a member of
    the same org without `wg:read`). The Playwright spec checks the case the screen must never show: another org's VMs
    and sessions on a shared platform device, and the hub's `404` for such a series.
20. **Clock.** Ages are counted on the browser clock against the device's sample time (as the hub does, decision
    HUB-16 22). A browser clock far ahead shows values as stale; accepted (the `hello` frame's `hub_time` is not kept by
    the live channel).

## Review (independent subagent: no high, two medium, eleven low findings)

21. Fixed: network totals after each read (medium; `carryOver`, decision 4); the decisions file committed (medium);
    frames only for the active org and no repeated reloads for entities the overview never lists; overlapping reads;
    tooltip position; locale ticks; inclusive "until"; the step in the range label; test IDs per level; radio-group
    keys, status regions, heading levels and state labels; session-row retries and duplicate reads; series answers
    after a panel closed. Recorded instead of changed: browser clock (decision 20), the rights scenario of the browser
    spec (decision 19), the spec locations (decision 15), the "Tasks" figure (decision 5).

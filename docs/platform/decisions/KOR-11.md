# KOR-11 – decisions

Follow-up prompt to an ending session, live state after a reconnect, projection rebuild (audit AUD-02 items NA-04,
NA-05, NA-08, NA-12, NB-10, NB-15).

## NA-04: a follow-up routed to a session that the device ends

1. **Carry-over instead of a refusal.** A main session that ended `stopping`/`stopped` hands the prompts it did not
   deliver to the next main session of the task; they come before that session's own prompts, in event order
   (`agent_hub.sessions.prompts.route`). The routing stays derived (no prompt table). This lifts the "no carry-over"
   rule of HUB-09 decision 11 and KOR-06 decision 4 for sessions that ended without a failure (documentation need for
   KOR-12: `03-api.md` section 4.6.3 and the two decisions get a reference to this one).
2. **Failed sessions keep their prompts.** A `failed` main session makes the task `failed` with its reason; the
   countdown shows the error and "Retry" (`06-web.md` section 4.4), which sends the newest prompt again. Carrying the
   original over as well would deliver it twice, so the undelivered prompts of a failed session stay with it - the
   refusal is visible there.
3. **The next session starts at once.** When the device itself reports `stopped` for the newest main session of an
   open task, that session delivered at least one prompt and prompts are pending there, the hub creates the next main
   session in the same transaction (`carry_over`, called by `sessions.transitions.apply`), placed by the scheduler like
   a follow-up's (`resume_from` on the same device, as the ended session is final). The online window comes from
   `agent_hub.usage.binding` (the outbox has no hub configuration; the same source the `quota` handler uses).
4. **No restart without need.** Nothing is started when the user asked to stop the session (audit
   `session.control` with `action: stop`) or closed the task: the prompt then waits for the next follow-up, whose new
   main session delivers it first. A session that never delivered a prompt (the device ended it during its start,
   e.g. after an unassignment) does not restart itself either, so a device that keeps refusing a workgroup cannot
   cause a chain of sessions; its first prompt is carried to the next follow-up's session (before, it was lost).
5. **Trigger on `stopped`, not on `stopping`.** A session still `stopping` may hold the prompt in its guest; only the
   final state guarantees that it will not be accepted there, and only then can the next session resume from it.
   Known limit: a prompt that Claude Code took while the session was already `stopping` (the transition into
   `working` is refused after `stopping`) counts as undelivered and is delivered again by the next session.
6. **Runner unchanged.** The runner already moves an open `prompt_delivery` row of an ended session to the session the
   hub routes it to next (`agent_runner.prompts.queue`), so no runner or contract change is needed.
7. **Review: locking, the stop signal and the configuration.** `carry_over` runs inside the device's transition,
   which holds the task lock; the per-task advisory lock of the follow-ups is taken before the task lock, so
   `carry_over` cannot take it. A follow-up whose hub time was read before such a new session belongs to the ended
   session and is carried to the new one - delivered once, in order; only a replay of that request answers the ended
   session's ID (documented in `tasks.service._prompt_time`). A user's "Stop" is read from the audit row
   `session.control` (the hub keeps no other record of it; audit rows are append-only and kept); a "Stop" the device
   never received still means the user wanted the session to end, so no session is started. The ID of the new session
   is made like `tasks.service.new_session_id` (that module imports this one). `agent_hub.usage.binding` is the
   existing process-wide holder of the online window for code that gets no hub configuration (also the outbox's
   `quota` handler); it is used, not duplicated.

7a. **Web spec `e2e/devices/chat.spec.ts` (gates of the NA-05 merge).** The spec let the device double report
   `stopping`/`stopped` as soon as the browser had *sent* "End session", so the stop could reach the hub after the
   session ended - with a queued follow-up the hub then (correctly, item 3) started the next main session and the
   session menu stayed. A real runner stops a session only after the hub's `session.control` hint, i.e. after the hub
   stored the stop; the spec now waits for the hub's answer to the stop before the double reports the end.

## NA-08: projection rebuild with missing `hub.wg` rows

8. **The workgroups come from the source as well.** `orgs.projection.clear_org` deleted the workgroup-keyed rows
   (`scope_epoch` of scope `wg`, `wg_access`, `wg_device`) only through the org's `hub.wg` rows; with those rows gone
   (a damaged table, exactly the case a rebuild repairs) the stale rows stayed and the rebuild failed on
   `scope_epoch_pkey`. It now also clears them for every workgroup of the verified log (`log.state.workgroups`,
   archived ones included), the source the rebuild folds anyway, and deletes the `hub.wg` rows of those workgroups
   whatever org they name (review: a row with a wrong `org_id` collided on `wg_pkey`). Rows of a workgroup that
   neither the log nor `hub.wg` names cannot be reached by the rebuild and cannot collide with it either. A workgroup
   ID is global: appending a log entry that names the workgroup of another org fails on `wg_pkey`, so in a store the
   hub wrote the log's workgroups are the org's own; only a tampered store could make one org's rebuild clear
   another's rows, which that org's rebuild (same run) then writes again. Test: the damage case
   "missing workgroups" of `tests/platform/hub/backup/test_rebuild.py` (red before: `UniqueViolation`) and
   "workgroups of another org" (red before the review fix: `wg_pkey`).

## NA-05: live rows after a reconnect or `resync`

9. **One counter, every screen.** The `connection` slice counts the live channel's `hello` (every connect and
   reconnect) and `resync` frames (`round`); `ui/hooks/useLiveReload.ts` runs a screen's load again for each new value
   (not on mount, the screen loads there). The hub replays events after a reconnect but no row frames, so the screens
   that show rows load them again: the task list (first page of the current filter), the task page (`GET
   /v1/tasks/{task}`: the task, its sessions with transitions and their usage), the session page, the notification
   center (first page) and the bell (the unread count of `GET /v1/me`). The operations slice keeps its own round
   (KOR-05); both count the same frames.
10. **Where the reload lives.** Slice frame handlers are synchronous store updates and cannot fetch; what a browser
    must reload depends on what it shows, which only the screens know. So the screens call their existing loaders
    (`loadTasks`, `loadTask`, `loadSession`, `loadNotifications`, `loadMe`) - one line each in the screen files
    (`ui/screens/{tasks,task,session,notifications}`, `ui/shell/header/bell.tsx`), outside the slice files the package
    names but owned by no running package.
12. **Review: paging, sign-out, overlapping loads.** The task list's reload (`loadTasks(…, refresh)`) loads the first
    page again even while another load of the list runs (that one may have been answered before the lost frames),
    replaces the rows of the first page's time range and keeps the rows of further pages already loaded, older than
    the page, with their cursor; a failed reload leaves rows and state alone (no error banner over the rows, the next
    round tries again). Rows are protected against older answers by `staleTask` (last activity). `loadTask` and
    `loadSession` now write nothing after a sign-out during their request (`store.generation`, like `loadTasks`),
    because reloads start on their own. `useLiveReload` reacts only to a higher count (a sign-out resets it). Kept as
    they are: the notification center's reload resets its cursor (rows are merged by ID; "more" loads page 2 again)
    and the first `hello` after a screen mounted loads once more (the screen's load may have been answered before the
    channel was live).
11. **Tests.** Vitest: `tests/core/store/connection-round.test.ts` (the counter) and `tests/ui/live-reload.test.tsx`
    (each screen reloads after `hello`/`resync`, not on mount; red before). Browser: `e2e/devices/live-reload.spec.ts` –
    the channel cannot connect while the session walks to `working`; after the reconnect the task page shows
    `working` (red without the reload).

## NB-15: runner restarts in E2E without the quiet wait

14. **`restart_quietly` removed.** The capacity route and `/monitoring/stack/up` restart the runner at once
    (`capacity.restart_runner`), and E2E-17 no longer waits for a full pool before the capacity change and after
    restoring it. The wait (decisions SCN-07 item 15) covered AUD-01 A-06 (a runner stopped during a pool VM's start
    left that VM booting with egress), which the runner fixed (agent: an interrupted start deletes the VM, no clone is
    started while the daemon stops). That fix removes the leftover VM; why the leftover clone of SCN-07 reached the
    mirrors at all is still not known (AUD-02; KOR-03 measured that a hardened leftover does not), and a runner the
    orchestrator kills after its 10 s stop timeout still leaves a VM until the next start's cleanup, which the
    immediate restart now runs at once. The egress check of each run is the evidence (report KOR-11: 5 runs each of
    E2E-15 and E2E-17 on T1). Documentation need for KOR-12: decisions SCN-07 item 15 and acceptance SCN-07 still
    describe the 30 s wait as current. The `poolReady` before the session start of E2E-17 stays: it is the spec's start
    condition (a warm pool for the session), not a guard against restarts.

## NB-10: browser test of a changed lazily loaded stylesheet

15. **Chromium, monitoring charts.** `web/e2e/devices/lazy-stylesheet.spec.ts` opens the monitoring charts (the uPlot
    chunk needs its own stylesheet) with the stylesheet's bytes changed on the way: the browser refuses it
    (integrity error), the chart chunk does not run, the page ends on the error page with reload, and the changed
    rule never applies; the link carries the table's `sha384` and `crossorigin`. Red when the helper loads stylesheets
    without `integrity`. There is no Firefox project: the web suite runs Chromium only (`web/playwright.config.ts`,
    `07-testing.md` section 1; the phone profiles are Chromium emulations), and adding a browser engine to the suite
    and the gate devices is outside this package. Firefox and Safari stay on the manual release checklist
    (06-web.md section 12.7, KOR-04 decision 7).

## NA-12: holder losses wait for every listed org

16. **Confirmed, no upper bound added.** `holderLosses` (`web/src/core/jobs/credential-rewrap.ts`) returns no loss
    while an org that `GET /v1/me` lists has no verdict (no verified log at the hub's head and not blocked). A log job
    that keeps failing with a hub or network error (`HubError` only fails the run, `log-verify.ts`; only a
    `CryptoError` blocks the org) therefore defers the holder-based check without a limit - the finding holds
    (existing test `tests/core/jobs/credential-rewrap.test.ts`, "waits while an org the hub lists is not verified yet or
    its log is behind the hub"). A deadline after which such an org counts as entitling nobody is **not** added:
    - the holder list is the hub's own statement (`cred_holders`); a hub that wants to keep a device as holder can leave
      it out of the list or fail the log reads - an upper bound gives no protection against the hub, which is the
      trust boundary this check sits behind (KOR-04: "the check can only be as good as the hub");
    - an honest hub serves the user's credential records only to devices its projection entitles, so a holder that
      lost its entitlement cannot read new records while the hub works correctly;
    - the log-based losses (`credentialLosses`, `removalLosses`) of every verified org are independent of the holder
      list and still rotate at once;
    - a deadline would rotate `CK` away from devices legitimately entitled only through the org that cannot be
      verified: the rotation wraps for the readers of the verified logs only, the catch-up job cannot wrap for that org
      until its log verifies, and those devices' sessions would lose model access.
    Remaining (review): a log run that fails with a hub code other than `unavailable`/`rate_limited` is not repeated
    until the next trigger (`log` frame, `interval` every 10 minutes, sign-in), so the deferral lasts as long as that
    failure; the sync indicator shows the failed run. The orchestrator decides whether an upper bound is wanted
    despite the reasons above (report KOR-11).

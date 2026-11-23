# WEB-06 – decisions

Members and rights, workgroups, rotation batches, org switcher, catch-up and pending-rotation jobs (06-web.md §1.2,
§2.4, §7.1, §7.2, §7.5, §11 jobs 3–4, §13; 04-crypto.md §4.6, §4.7, §5.7, §5.10, §5.11, §6.3; 03-api.md §4.2, §4.3;
ADR-005, ADR-024 addendum, ADR-026).

1. **Flows in the core.** `web/src/core/org/` (new directory of this package) holds the framework-free flows, covered
   by Vitest alone: `batch.ts` (the append flow), `members.ts` (rows, rights, planners), `invites.ts` (invitations),
   `workgroups.ts` (rows, creation, rename, archive). The screens only render them.
2. **The batch flow for WEB-07.** `submitBatch(context, org, planner, progress)` (`core/org/batch.ts`) is the one way
   a browser appends membership changes: the planner returns the requested entries from the verified log (or null when
   nothing is left to do), `buildBatch` adds one `rotate` per element of R with fresh keys and the wraps for every
   remaining reader and checks the hub's rules, one `POST /v1/orgs/{org}/log`, the answer must name the new head. On
   `seq_conflict` the log is re-read and the planner runs again (three attempts in total); any other refusal ends the
   flow with its code. After success the verified log of the batch replaces the stored one and only the new keys of
   scopes the user reads are kept. The device screen (WEB-07) passes planners for `device_unassign`/`remove` of a
   device and reuses `ActionDialog` (`ui/screens/members/ActionDialog.tsx`, confirmation, progress "Rotating keys
   2/5", "Wrapping for 7 recipients", "Sending", "attempt 2 of 3") and `createInvite` with `kind: 'device'`
   (`DEVICE_EXPIRY_HOURS` = 24).
3. **Planners re-check on the re-read log.** A change another admin already made is "done" (null, no request): the
   member is gone, the access is already what was asked, the workgroup exists or is archived. A change that became
   impossible is refused with the log rule's code (`last_admin`, `forbidden`, `not_found`). Removing oneself is
   leaving, a separate planner.
4. **Leaving.** A self `remove` is sent alone; the rotation of what the member could read is owed by the remaining
   admins and writers (`OrgState.pending`, decision CRY-02) and sent by their pending-rotation job. The last admin sees
   neither "Leave" nor the role control nor can be demoted (`last_admin`).
5. **Default workgroup.** No `ungrant` and no `grant` on it (ADR-024 addendum); grant on archived workgroups is not
   offered. `grant` also changes read ↔ write. "Also give access to the history" wraps the older epochs the admin holds
   (`buildBatch` `history`).
6. **Invitations.** Expiry choices 1 h, 6 h, 24 h, 72 h (default), 7 d, 14 d, 30 d. The package carries the head
   **after** the `invite` entry, `org_name` and `wg_names` from the verified profiles (empty string if not read yet),
   the pinned founder, the hub key of `/v1/config` and the current `OK` plus the `WK` of every invited workgroup and
   the default workgroup. Because the admin must hold those keys (an admin is not a member of every workgroup), the
   form offers only workgroups whose current key the admin holds; otherwise `epoch_stale`. A `seq_conflict` rebuilds
   entry and package on the re-read log with the **same code**. The code exists only in component state until "Done".
   The open-invitation list is the hub's list cross-checked with the verified log (known, not revoked, not accepted,
   not expired by the signed `expires`); revocation of an invitation that is no longer open is "done".
7. **Workgroup creation.** The workgroup ID is drawn once per dialog, so a retry after a lost answer is "done". The
   batch: `wg_create`, `grant` per chosen member (the creating admin is never granted again), `device_assign` per
   chosen device, `WK_1` for every reader and the current `OK` for a device that had no workgroup left (all by
   `buildBatch`), and the `wg.profile` event sealed with the current `OK` at `lh` = the batch's last entry (the hub
   checks batch events against the log after the batch; the workgroup must exist at `lh`). Devices offered: active
   devices of the org that are not decertified; platform devices already in the org only while it has platform access
   (ADR-026). Choosing new platform candidates is WEB-07's device screen. Rename is a new `wg.profile` event
   (`POST /v1/events`) read into the store at once; names are 1–80 characters.
8. **Catch-up job** (`core/jobs/catch-up.ts`, order 30, triggers `login`, `log`, `interval`). The hub's offer is a
   hint: only slots the verified log confirms are wrapped (`confirmedSlots`, recipient key from the log). The hub also
   offers the user's own `cred` slots (`agent_hub.wraps.catchup`); those are wrapped with the newest own `CK` self-wrap
   that verifies with the own identity, only for the offered epoch equal to that one and for recipients `mayWrapCred`
   allows over every verified, unblocked org of the user. Nothing is sent when nothing is left; a run that started
   before a sign-out sends nothing.
9. **Pending-rotation job** (`core/jobs/rotation.ts`, order 40, triggers `login`, `log`, `interval`). It does not read
   the hub's `rotation_pending`: the hub derives it from the same two facts the browser verifies (a member who left by
   itself, `OrgState.pending`; platform devices whose certification ended while assigned, from the verified platform
   log, refreshed first). An admin first sends `device_unassign` for every workgroup of such a device (+ R), then the
   owed `rotate`s (org: admin; workgroup: admin or writer). At most two batches per run, each with a toast (slice
   `rotation`, header item `ui/shell/header/rotation-toast.tsx`, closes after 10 s or by its button). A `seq_conflict`
   re-reads the log through the log job's function directly (triggering the job would wait for this one).
10. **Interval trigger.** `AppSession` starts one timer after unlock that triggers `interval` for every org of the
    profile every 10 minutes (`INTERVAL_MS`), stopped at sign-out; `setInterval`/`clearInterval` are called as plain
    functions (a browser refuses them as methods of another object). `AppSession.trigger(trigger, org)` is public.
11. **Org switcher** (`ui/shell/header/org-switcher.tsx`). Names from the verified `org.profile` of each org (pulled
    once per org); choosing one navigates to `/o/:org`, and the layout's `activate` reconnects the live channel with
    the other `org`. "Create organization" asks for the organization name and the display name in it (prefilled from
    the active org) and says that platform devices need the operator's grant (ADR-026); it uses `prepareOrg`, a
    non-private twin of `preparePrivateOrg` in `core/account/genesis.ts` (default workgroup named per locale).
12. **Rights in the UI.** Invite, role, grant, ungrant, remove, create, rename and archive controls are rendered only
    for admins; readers and members see the lists only. A user who is not an active member of the org sees "not found".
13. **Navigation.** Entries "Members" (order 60) and "Workgroups" (order 61) by new `nav.ts` files.
14. **Foreign files touched (minimal).** `core/account/session.ts` (interval timer, `trigger`), `core/account/genesis.ts`
    (`prepareOrg`, the private path unchanged), `tests/core/registrations.test.ts` and `tests/ui/registries.test.tsx`
    (they asserted the job list and empty header items exactly; now the first two jobs and containment), `tests/ui/invite.test.tsx` (an assertion on the asynchronous live start now waits: the added sign-in jobs made it timing-dependent under load).
15. **Merged coverage.** Screens reached by only one suite were covered in both (loading and not-found states by
    client-side navigation in the browser specs, copy buttons with clipboard permission) or simplified (no "Try again"
    label variant, no stale-answer guard in the invitation list: React ignores state updates after unmount and every
    change reloads the list), following decision WEB-03 18.
16. **E2E.** E2E-02/18/31/37 (`e2e/platform/specs/`) do not exist yet (only E2E-00); SCN-03 writes them on these
    screens and test IDs. The web specs `web/e2e/{members,workgroups,org-switcher}.spec.ts` run every admin action
    against the real hub; a second user joins with the shown code through `e2e/helpers/members.ts` (lookup, package,
    log check, `accept` with self-wraps, `member.profile`) until WEB-08's invitation screen exists.
17. **Independent review – handled.** No high findings. Medium, fixed: "Create organization" is now idempotent (the
    prepared genesis is kept while name and display name are unchanged; a retry sends the identical body); the grant
    dialog offers only workgroups whose current key the admin holds (the grant carries the `WK` wraps, 04-crypto §5.10;
    the hint "wraps follow by catch-up" would leave a member without keys until a holder is online); the browser spec
    now holds the append back to check the rotation progress (rotate, wrap, send) and runs ungrant through the UI.
    Low, fixed: the unused public `AppSession.trigger` was removed; the ungrant button's label names member and
    workgroup. Low, kept with reasons: catch-up is not triggered by `lh` (the log job handles `lh`, its `log` frame and
    the 10-minute interval follow; adding it changes the request counts the reader tests of WEB-02 pin); `owedPlanner`
    repeats the selection of `pendingRotation` because the job needs a planner for `submitBatch` (pending scopes come
    from this org's log only, so the org-ID check would be a branch no state can reach); invitation creation after a
    failure other than `seq_conflict` draws a new code (an orphan invitation stays revocable in the list); the open
    invitation list is the hub's list checked against the log (an invitation the hub hides is refused by the hub on
    accept anyway); missing names go into the package as empty strings; the user's own `cred` slots offered by the
    hub's catch-up are wrapped here (same verified checks as job 5 of WEB-10, which may reuse `credWraps`); test
    loosening and foreign touches as listed in 14; device assignment at workgroup creation is covered in Vitest only
    (the device double belongs to the `devices` project, WEB-07 covers device screens in the browser).

# WEB-04 – decisions

Home screen, composer, the first task with the countdown, task list (06-web.md §4, §5.1, §12.3, §12.4; 03-api.md §4.5,
§4.6, §4.6.1–§4.6.2, §5; 04-crypto.md §5.9 step 4; ADR-007, ADR-009, ADR-026).

1. **Flows in the core.** The send sequence (`core/tasks/send.ts`), the countdown (`core/tasks/countdown.ts`, a pure
   function of rows, launch, hub clock offset and time), the task list (`list.ts`), pulls (`pull.ts`) and names/titles
   (`directory.ts`) are framework-free and covered by Vitest alone; the screens only render them.
2. **CK wrap after the batch.** 04-crypto §5.9 and 03-api §4.6.1 name "`device_assign` + wraps of `WK`, `OK` and the own
   `CK`" as one batch, but the log batch accepts only `org`/`wg` wraps (W3 of `POST /v1/orgs/{org}/log`, HUB-05) and the
   `cred` epochs live outside the membership log. The client therefore sends the log batch (`device_assign` + `WK` +
   `OK`, built by `buildBatch` from the verified log and the verified platform log) and right after it the `CK` wrap via
   `POST /v1/wraps` (W2 holds once the assignment is stored). The `CK` wrap is best effort: the device needs it only for
   the user's own credentials, and catch-up wrapping (WEB-06/WEB-10 jobs) offers the slot again.
3. **Candidate choice.** The first candidate in the hub's order (online first, then free capacity) that the verified
   platform log certifies – valid certification at `platform_seq`, same device ID and same keys as listed – is used;
   keys always come from the platform log. If none verifies, the platform log is re-verified once (a device certified
   after the last verification), then `no_device_available`. Without a verified platform log (a new org has not run the
   log job yet) the log job runs first. Assigning also requires the user to hold the current `OK` and `WK`
   (`epoch_stale` otherwise), so a device never gets an assignment without its keys.
4. **Conflicts.** `seq_conflict` re-reads the log and rebuilds (3 attempts); if the workgroup got a device meanwhile, the
   assignment is done. `already_assigned` counts as done when the workgroup lists a device. A `409 no_device` from
   `POST /v1/tasks` (unassigned in between) runs the device step once more.
5. **Retry with the same IDs.** The composer keeps the prepared request (task ID, sealed events) after a failure a
   retry can fix (`unavailable`, `rate_limited`, `seq_conflict`); sending the unchanged prompt to the same workgroup
   re-sends the identical body (hub `replayed`). Other refusals (`epoch_stale`, `forbidden`, …) and a changed prompt or
   workgroup prepare a new task. The countdown's Retry likewise keeps its sealed follow-up until the hub took it.
6. **Countdown anchor.** `min(estimate_ms, 10 s)` counts from the moment the `POST /v1/tasks` answer arrived for a task
   this browser launched (`launches` in the slice, also before the session row arrives), from the moment a waiting
   session got its estimate (`estimatedAt`, rescheduling after `device_offline`/`capacity_exhausted`), else from the
   session's creation on the hub clock (offset from `hello.hub_time`). Step elapsed times are `received_at −
   created_at` of the session (both hub clock; for the first session that is the task's creation, after a Retry or
   follow-up the new session's start); the shown step time is the device-reported `at`. Steps a runner skips count as
   done without a time. "Ready in" is `first_working_at − created_at` of the session. Reached states that report work
   in progress (`vm_assigning`, `agent_starting`, `prompt_delivering`) are the current step ("Starting the agent –
   12.4 s" while `agent_starting`, §4.4); the other states complete their step.
7. **Before the task exists.** While the send runs (device step, `POST /v1/tasks`), the composer shows the countdown
   from 10 s with the steps "Assigning execution device" (when an assignment runs) and "Task accepted"; the task page
   continues with the real countdown and shows the assignment step as done with its duration.
8. **Announcements.** The polite live region changes only on a step change and every 5 s (its text is a pure function
   of phase, current step and, while counting, the remaining time rounded up to 5 s, else the elapsed time rounded down
   to 5 s), so screen readers are not flooded.
9. **Retry after `failed`.** The newest verified prompt of the task (pulled if not read yet) is sent again as a follow-up
   (`POST /v1/tasks/{task}/prompts`); the hub creates a new main session and the countdown follows the session the
   answer names.
10. **Frames for slices.** `hello`, `task` and `session` frames reach the store through a new registration point:
    a slice file may export `frames` (`core/net/frames.ts` discovers them, a throwing handler is isolated).
    `AppSession.activate` passes `onFrame` to the live channel, and the channel now also hands `hello` to the frame
    handler after processing it (minimal edits in `core/account/session.ts` and `core/net/sse.ts`, WEB-02/WEB-03).
11. **Task context.** `AppSession.tasks` exposes store, client, clock, the log job and the event reader to the task
    flows (`TasksContext` in `core/tasks/pull.ts`; a type-only module would have no coverage data for the merged gate).
12. **Pulled events.** The live channel starts a new scope at its head, so older events are pulled: the org's profiles
    (`org.profile`, `wg.profile`, `member.profile`) once per org, the titles (`task.meta`) once per workgroup and then
    per task still without one, the task page's `task.meta`/`prompt` once. Everything goes through the event reader;
    only verified bodies are shown, unverifiable titles as "Unverifiable title (code)". Own sealed events are read at
    once with the positions of the `POST /v1/tasks` answer.
13. **Task page frame in WEB-05's directory.** The send sequence navigates to `/o/:org/t/:task` at once; that route did
    not exist. `web/src/ui/screens/task/` now holds a minimal frame (title, prompt, countdown panel with Retry) which
    WEB-05 extends into the chat timeline – the frame lives where it will grow, so no route is defined twice.
14. **Home.** The home screen always shows the composer (for users with write access somewhere) and the five newest
    tasks of the default workgroup with a link to its list; navigation entry "Home" (`screens/home/nav.ts`). The GitHub
    chip shows "Connect GitHub" as a link to `/settings` until WEB-09 provides the connection state. Readers without
    any writable workgroup see no composer; a task list of a workgroup without `wg:write` has none either.
15. **Unknown and unverified.** A workgroup the user cannot read is "not found" (existence not revealed); while the
    org's log is not verified, home and list say so instead of rendering controls from guessed rights.
16. **Device double for the Playwright suite.** The runner and its outbox (HUB-10) are not part of the web suite.
    `scripts/hub_test_director.py` (started by `scripts/hub_test_server.py --director-port`, loopback only) enrolls and
    certifies platform devices with real keys through the hub API and root-signed platform-log appends, keeps presence
    rows with heartbeats, re-runs the scheduler when a device comes back, and applies scripted session transitions
    through `agent_hub.sessions.transitions.apply`; frames reach the hub's browsers over the PostgreSQL bus exactly like
    those of another hub process. Specs using it live in `web/e2e/devices/` and run in the Playwright project
    `devices` with one worker (each new device must be the first candidate); the project discovers the directory, so
    later device specs only add files. The test server waits for the double's `/health`.
17. **API helper as the signed-in user.** `web/e2e/helpers/user.ts` creates workgroups and many tasks where the UI of
    later packages is missing (WEB-06) or a test needs volume (paging): the page signs with its own non-extractable
    session key from IndexedDB, Node holds the identity opened from the recovery slot with the onboarding's recovery
    code and builds entries and events with the web core. A reader is a workgroup in which the admin granted itself
    `read` (a visible `grant`).
18. **Coverage source rule.** As in WEB-03 decision 18c: a single-statement `if` body that renders a state both suites
    reach is written without braces (the bundler reprints it that way; otherwise the branch ranges do not line up).
19. **Foreign files touched (minimal).** `core/account/session.ts` (onFrame, `tasks`), `core/net/sse.ts` (`hello` to
    the frame handler), `web/tests/core/net/live.test.ts` and `web/tests/ui/registries.test.tsx` (the new `hello`
    frame and the first navigation entry), `web/playwright.config.ts` (project `devices`),
    `web/e2e/fixtures/{env,server,global-setup}.ts` and `scripts/hub_test_server.py` (device double). Sessions and
    their transitions live in the `tasks` slice (the countdown needs them with the task rows); WEB-05's planned
    `slices/sessions.ts` should build on these entries instead of a second source of session state.
20. **E2E-01/06/07/08/37** (`e2e/platform/specs/`) do not exist yet; SCN-01/SCN-03 write them on these screens and test
    IDs. The web specs `home`, `countdown` and `tasks` cover the browser side against the real hub now.
21. **Stale rows.** Rows carry no version: a task row older than the stored one (last activity, then state time) is
    ignored; a session row is kept when the incoming one ends its transitions below the newest known one, is not final
    after a final one, lacks `first_working_at` after it was set, or is an earlier start-up state. So a `GET` answer
    overtaken by frames never sets the countdown back.
22. **Independent review – handled.** No high findings. Medium, fixed: the counting announcement said elapsed time as
    remaining (8); a hub-named title event of another task was accepted (titles now require the event's own task,
    `directory.ts`); older rows overwrote newer ones (21); Retry was rendered for readers (now only with `wg:write`);
    the list of device specs in `playwright.config.ts` (now the discovered directory, 16); the current step was one too
    far for in-progress states (6). Low, fixed: times after a Retry counted from the task's creation (6); Retry was not
    idempotent and a non-retryable failure kept the prepared request (5); the device double had no health check, a fixed
    online window and silent thread failures (16). Low, kept: the owner name comes from the hub's `owner_id` (a
    metadata field the hub sees anyway; checking it against the author of the verified `task.meta` belongs to the chat
    of WEB-05 where the events are shown); ownership notes (19, 13).

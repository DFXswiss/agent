# WEB-09 – decisions

GitHub connect, workgroup settings, operation cards and the Operations tab, subagent views (06-web.md §4.1, §5.2–§5.5,
§8.1, §13; 03-api.md §4.7, §4.8, §4.10, §5, §9, §10; 04-crypto.md §4.4, §4.5; ADR-014, ADR-015, ADR-017).

1. **Device-request client for WEB-10.** `web/src/core/requests/` is the shared client of every device request:
   `bodies.ts` (the closed request and response bodies of the four kinds, failure codes), `client.ts`
   (`eligibleDevices`, `deviceKeys`, `prepareRequest`, `submitRequest`, `readRequest`, `latestResponse`) and
   `watch.ts` (`watchRequest`). WEB-10 builds `claude.login` (with follow-ups: `prepareRequest({…, follows})`; responses
   to a follow-up are read with `readRequest` of the follow-up), `credential.check` and `mailbox.check` on it, and
   names failure codes with `ui/settings/sections/github/reason.ts` (`useReason`). The slice `requests` keeps the
   announced `seq` per request (`device_response` frames, a hint only).
2. **Device keys only from verified logs.** A request is sealed to, and a response verified with, the keys of the
   device as the verified membership log holds them (platform devices: the keys of their platform certification, which
   the log state copied). The device must be an active device assigned to a workgroup the user can access (the user's
   `cred` readers, the hub's eligibility rule) and its certification must not have ended; anything else is
   `device_not_assigned`. The hub's device list (`GET /v1/me/devices`) only chooses among such devices.
3. **Responses.** Every response must carry the request's ID (`in_reply_to`), user, device and kind, the `seq` it is
   listed under, verify with the device key, open with the user's X25519 key and hold a body the contracts allow for
   the kind; otherwise it is kept as `unverifiable` with its code and never shown or acted on. A hub state `failed`
   (device lost its eligibility, or a newer request of the same kind replaced it, HUB-13) shows the client code
   `request_failed`, `expired` shows `timeout`. Failed reads of the request are shown while waiting (and retried).
4. **Code expiry.** The countdown runs to the response's signed device time (`header.ts`) + `expires_in`; the browser
   clock is not corrected (a skew of seconds against a 15-minute code is irrelevant, and the device time is signed).
   Review L12 (use the receipt time) is declined for this reason; GitHub itself enforces the expiry.
5. **GitHub state.** `GET /v1/me/credentials`; with a `github` item the record is verified (owner = user, kind, author
   = the user or a device entitled to the user's `CK`, signature) and opened with the user's own `CK` self-wrap of the
   record's epoch; the body must match `contracts/credentials/github.json` exactly. Only the login and the creation time leave `core/github/connection.ts`; the token is never stored in
   the store or rendered. An unverifiable record is shown with its code and offers Connect and Disconnect. The state
   lives in the slice `github` (settings section and composer chip).
6. **Connect.** If `GET /v1/me/devices` is empty, `ensureDevice` of the first-task flow (WEB-04, `core/tasks/send.ts`)
   runs for the default workgroup of the active org (admin and platform access as there; its errors are shown with
   "Try again"). Online devices of the default workgroup come first and the first online device is preselected;
   offline devices are listed disabled. After `done` the section reloads the record; RUN-09 stores the record before it
   answers `done` (05-runner.md §11.4), so no retry loop is needed.
7. **Disconnect.** Confirmation dialog, `DELETE /v1/me/credentials/github` (`404` counts as done, HUB-13), then the
   note with the link to `https://github.com/settings/applications` (opens in a new tab, `noopener noreferrer`).
8. **Composer chip (foreign file `ui/composer/Composer.tsx`, WEB-04).** The hint "Connect GitHub" is shown while the
   connection is not known to be connected; choosing the chip loads the state once. Decision WEB-04 14 deferred
   exactly this to WEB-09.
9. **Workgroup settings.** Route `/o/:org/w/:wg/settings` (`ui/screens/wg-settings/`), linked from the task list
   (foreign file `ui/screens/tasks/TaskListScreen.tsx`, one link). Repositories one per line, matched with the regex of
   03-api.md §9 and stored lower case (the event contract requires lower case); patterns with the contract's character
   set; duplicates dropped; line-precise errors. Saved as a `settings.workgroup` event (`task`, `session`, `ref`
   empty). The newest verified event wins (`scope_seq`); a body that does not fit the contract is reported and the
   defaults are shown. An unedited form follows newer events; an edited one shows the concurrent-edit warning ("show
   the newer settings" / "save mine anyway"); a save checks again right before sealing (`settings_conflict`, client
   only). Readers see the values without any control (06-web.md §1).
10. **Operation cards.** `ui/chat-items/operation.params.tsx` renders the card (`ui/chat-items/operation/Card.tsx`,
    also used by the tab): type, live state from the `operation` frames (slice `operations`, rows never go back),
    subject from the decrypted parameters, result facts (PR number + URL, commit SHA, pushed branch + URL, comment URL,
    created branch, checkout commit, subagent session, new title, number of repositories), error code, "Duplicate of …"
    linking the original in the Operations tab, unverifiable parameters/results with their code. Events count only if
    the reader verified them, their body names the operation of their `ref`, and – with a known row – their author is
    the row's device; the newest verified event wins over any unverifiable one (an injected event cannot hide it). A
    verified `operation.*` body that names no operation (or a result without status) is a neutral "unverifiable item"
    row (`params_invalid`). `operation.result` renders a compact "… finished: …" row. Cards load the task's first page of operations
    once per live round (`ensureOperations`; the slice counts `hello` frames, so every reconnect pulls the lists again –
    lost `operation` frames are repaired) and an operation beyond that page by ID. A `resync` frame without reconnect
    is not forwarded to slices by the live channel; the next reconnect or the tab's reload repairs it (residual).
    Loads that answer after a sign-out write nothing. The chat item finds its workgroup and task
    from the verified event at its `scope_seq` (the timeline item carries no scope).
11. **Operations tab.** `ui/task-tabs/operations/` (order 30, after Sessions): newest first by `created_at` then ID,
    older pages on demand, live rows from frames; the detail drawer (`?tab=operations&op=<id>`, also the target of the
    duplicate link) loads `GET /v1/operations/{id}` and the operation's events (`?ref=<id>&kinds=operation.params,
    operation.result`), and shows transitions (state, time, device or hub, error), the decrypted parameters and result
    as formatted JSON. An opened operation that is not in the loaded pages is loaded by ID; the original of a duplicate may belong to
    another task of the workgroup (dedupe slots are per workgroup) and opens in the same drawer.
12. **Renamed by session.** The card of `task.set_title` says "Renamed by session"; the task page (foreign file
    `ui/screens/task/TaskScreen.tsx`) shows the same marker when the newest verified `task.meta` is device-authored
    (`core/operations/renamed.ts`), and reloads the task row when a verified title event newer than the row's
    `title_event_id` arrives – the row would otherwise keep naming the old title (`selectTitle` of WEB-04 prefers the
    named event) until the next load.
13. **Subagent views.** The Sessions tree with depth ≤ 2 and the child's own detail with terminal and chat exist since
    WEB-05; WEB-09 adds `ui/screens/session/children/ChildrenTab.tsx` (subtree of the session with label, state,
    device, model and the purpose of the child's `subagent.request`, decrypted from the origin operation's parameters; the purpose counts only if the operation's verified result names
    this child, since the hub supplies `origin_operation_id`)
    and replaces WEB-05's inline list in `SessionScreen.tsx` by it (the hook point named in the package text did not
    exist as a separate file). Test IDs `session.child`, `session.children`, `session.children.empty` stay.
14. **Test double.** `scripts/hub_test_requests.py` (answers the newest open request of a device through the device
    API: fetch and open the browser's envelope, store the `github` credential CK-encrypted as the device, sealed and
    signed responses) and `scripts/hub_test_operations.py` (registration through `POST /v1/device/operations` with the
    params event and the dedupe tag, completion through `POST /v1/device/outbox`: `executing`, `operation.result`,
    final transition; `task.meta` for `task.set_title`; the hub creates subagent children itself). The director only
    routes `POST /sessions/<id>/operations` and `POST /requests/answer` to them and signs device requests
    (`Director.signed`).
15. **Specs.** `web/e2e/devices/{github,operations}.spec.ts` (device project) and `web/e2e/wg-settings.spec.ts`
    (desktop). E2E-20/21/22/24 (`e2e/platform/specs/`) do not exist yet (SCN-04); they will use these screens and test
    IDs.
16. **Foreign files touched (minimal).** `ui/composer/Composer.tsx` (8), `ui/screens/tasks/TaskListScreen.tsx` (9),
    `ui/screens/task/TaskScreen.tsx` (12), `ui/screens/session/SessionScreen.tsx` (13), `tests/ui/chat.test.tsx`
    (an `operation.params` item now has a renderer; the body without an operation ID renders no card),
    `scripts/hub_test_director.py` (14).
17. **Independent review – handled.** No high findings. Medium, fixed: duplicate originals in other tasks (11), lost
    `operation` frames are pulled again per live round (10), loads and disconnect write nothing after a sign-out.
    Medium, kept with reason: the edit of `SessionScreen.tsx` (13; WEB-05 left no separate hook point). Low, fixed:
    operations beyond the first page loaded by ID, `request_failed` instead of `device_not_assigned` (3), failed reads
    shown, strict GitHub body (5), verified events preferred (10), malformed bodies as neutral rows (10), the detail's
    label for unverifiable parameters, the purpose check (13). Low, kept: device-code countdown on the device clock
    (4); the device picker shows ID prefixes – device names (`device.profile`) belong to the devices screen of WEB-07
    and can be reused there later; foreign edits are listed in 16. The WEB-05 test `chat.test.tsx` gained a
    `platform.read` item, the only kind without a renderer left, so the "no renderer" path stays covered; the WEB-08
    test `invite-org.test.tsx` now unmounts at the first tick that shows the verification (its 50 ms poll often came
    after the verification ended, which left a branch of `OrgInvite.tsx` uncovered at random) and holds a failing lookup
    until the screen is gone (the same race in `InviteScreen.tsx`).

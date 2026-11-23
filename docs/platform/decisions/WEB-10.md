# WEB-10 – decisions

Model access, credentials, quotas, usage strip and CK rotation in the web client (06-web.md sections 5.2, 8.2, 11
job 5; 03-api.md sections 4.1, 4.8, 4.10; 04-crypto.md sections 4.4, 4.5, 5.10, 6.3; ADR-010, ADR-011).

1. **Records in the core.** `web/src/core/credentials/records.ts` builds, opens, lists, stores and deletes the four
   credential kinds. Bodies are checked exactly against `contracts/credentials/*.json`; only a summary (GitHub login,
   subscription e-mail, mailbox address and host) leaves the module, so tokens, API keys and passwords never reach the
   store or the DOM (asserted in Vitest and Playwright). Authors are the user or a device the verified logs entitle to
   the user's `CK` (the rule of WEB-09). `sealRecord` is tested byte for byte against `contracts/vectors/credential.json`.
2. **Put.** `PUT /v1/me/credentials/{kind}` with `expected_version` from the list and the stored record's ID (HUB-13
   keeps the ID on update); one retry after `version_conflict` or `epoch_stale` (another browser or a rotation wrote in
   between). The current `CK` is the newest own self-wrap that verifies and opens.
3. **Model choices.** Only from `GET /v1/config` (`models`). An explicit access kind offers its list; `auto` offers the
   IDs all three kinds allow (the device decides the kind per session, so a model must be valid for whichever it
   picks); "default" (`model_id` null) is always offered and names `defaults.platform` for the platform. A model the new
   preference does not allow falls back to the default in the form. The locale is sent unchanged.
4. **Claude login.** `claude.login {mode}`; the pasted code is a follow-up request (`follows`), responses of the
   follow-up are read from the follow-up (decision WEB-09 1). The newest verified response of original and follow-up
   decides; `authorize` with `automation:"failed"` is shown as `login_automation_failed` with the relay continuing. The
   automatic mode can only be chosen with a verified `mailbox` record. The device stores the token (05-runner.md
   §11.2 step 5); the browser shows only the e-mail of `done`.
5. **Checks.** `credential.check {kind}` for the API key (and the subscription, core only – the UI offers "Check key"
   and "Check mailbox"), `mailbox.check {}` for the mailbox; the answer is only valid / not valid.
6. **Quotas.** Newest `fetched_at` per window wins (several devices may have polled), order 5 h, week, per model; no
   rows = "not available" (no subscription or the usage endpoint did not answer, ADR-010 feature detection). Bars use
   `<meter>` (no inline style under the CSP).
7. **Usage strip.** Totals over all sessions of the task (main and subagents): tokens in/out/cache, cost with every
   basis that occurs, context share of the main session. The counters come from the `usage` of
   `GET /v1/tasks/{task}` and from `session` frames (slice `usage`, newer `updated_at` wins).
8. **Foreign files (minimal).** `web/src/core/tasks/send.ts` (WEB-04): `loadTask` hands the `sessions` of its answer
   to `putTaskUsage` (one import, one line), so the task is not fetched twice for the strip (the WEB-05 chat test
   asserts one `GET /v1/tasks/{task}`). `web/src/ui/task-tabs/chat/UsageSlot.tsx` (WEB-05): the discovered module path
   is now `./usage-strip.tsx` (the path the package text names; the slot looked for `ui/usage/strip.tsx`).
   `scripts/hub_test_requests.py` (WEB-09): the `store` step also writes a `claude_subscription` record and keeps the
   stored record ID on updates. `scripts/hub_test_metrics.py` (WEB-12): route `quotas` (a `quota` outbox item of the
   polling device). New registration files: `web/src/ui/shell/header/credentials.tsx` (the toast of job 5, a discovered
   header item named after the area) and `web/e2e/helpers/credentials.ts`.
9. **Spec location.** The Playwright specs are `web/e2e/devices/settings-model.spec.ts` and
   `web/e2e/devices/usage.spec.ts`: specs that use the device double must live in `e2e/devices/` (Playwright project
   `devices`, one worker). The phone variants run there with the Pixel 7 profile (without its browser type, which a
   describe group cannot switch); the `mobile` project of the config is not changed.
10. **When a CK rotation is owed (job 5).** The hub does not tell a browser which devices hold wraps of its `CK`, and
    the spec gives no state for it. The job replays the verified logs of all the user's orgs: a device that was a `cred`
    reader before an entry and is not after it, and is not a reader now, lost eligibility at the entry's signed `ts`; a
    decertified platform device at the signed `ts` of its newest `device_decertify`; and the devices of an org the
    browser holds a log of but `GET /v1/me` no longer lists (the user was removed and cannot read the `remove` entry)
    are lost "now". A rotation is owed for a loss not yet handled in this sign-in that is not clearly older than the
    start of the current epoch (the hub's `created_at` of the user's own self-wrap): "clearly" allows 5 min between the
    entry signer's clock and the hub's (signed requests are bounded to 120 s), so a signer behind the hub cannot make a
    loss look older. An unknown or future epoch start fails closed (rotate). Losses a rotation handled are kept per
    sign-in, so neither skew nor a hub hiding the start causes repeated rotations; after a new sign-in a loss within
    the margin can cause one more rotation (cost: one re-encryption). Residual: an org the user was removed from while
    no browser of the user held its log is not noticed. Residual closed by KOR-04 (decisions 1–4) against an honest hub:
    job 5 also treats devices in `cred_holders` that no verified log entitles as a loss; a hub that withholds holders
    can still hide such a loss (KOR-04 decision 4).
11. **Rotation content.** `CK_{e+1}` random, wraps for the user and every reader now (decertified devices and orgs the
    user left excluded), every record re-sealed by the user (same ID and kind, `version + 1`, new `ts`), in one
    `POST /v1/me/credentials/rotate`. Records written by a device that has lost eligibility are **deleted**, not
    re-signed: their signed `ts` is the device's own claim, and re-signing would turn whatever that device (or a
    colluding hub) wrote into a user-authored record; the user connects again, and the toast counts them. Any other
    record the browser cannot open (signature, body, epoch, unknown author) **blocks** the rotation – nothing is deleted
    on the browser's own doubt – and a toast names the kinds once per sign-in (the list shows them as unverifiable,
    where the user deletes or replaces them). A `409 version_conflict` is retried once; only the queue of the first
    verified org acts (`CK` is per user, every org queue gets the trigger), runs are serialized per store, and nothing
    is written after a sign-out.
12. **New eligible devices.** Wrapping `CK` for a device that becomes eligible is already done by the catch-up job
    (job 3, `credWraps`, WEB-06) on the same triggers; job 5 does not wrap again (no duplicate wraps).
13. **Smaller points.** A stored model that the catalog no longer allows for the preference reads as the default. The
    mailbox password length counts code points as the contract does. Test fixtures use token texts the repository
    hygiene scan does not match. After a refused code (`code_invalid`) the login is started again ("Try again"): the
    device ended that login with a final answer, so a new code needs a new `claude.login`.
14. **Independent review – handled.** High: skew could hide a loss (margin, 10), the hub's `created_at` could suppress
    a rotation (missing or future start fails closed, 10), the user's own removal from an org (`removalLosses`, 10;
    residual named). Medium: repeated rotations (handled per sign-in, 10), deletion of unreadable records (now blocks,
    11), re-signing records of a lost device (now deleted, 11), missing tests (skew, null/future start, repetition,
    removal, blocked, conflict on retry – added). Low: header item renamed to the area, decision note committed, model
    form and password length (13), one acting org queue (11). Kept: spec locations and the phone profile (9), the
    `auto` intersection (3), the code field after a refused code (13), the stored record ID read before the put (the
    hub keeps the ID anyway; a wrong one is refused with `request_invalid`).

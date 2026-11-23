# KOR-05 – Live state and names in the web client

Corrections of audit findings A-10, A-22, A-23, A-24, A-25, B-18 and B-23. Each correction comes with a test that is
red without it and green with it.

## A-10: the notification named the workgroup "…"

**Cause.** The event reader checks the author of an event against the platform log when the org has platform devices
(`excludedFor`: a decertified platform device is no writer). A member who has just accepted an invitation holds the
org's log and keys (`installOrg`) before the first log job of that org verified the platform log: the profile events
pulled at that moment (`org.profile`, `wg.profile`, `member.profile`) were refused with `log_rights` and stayed
`unverifiable` for good (the reader's retry re-reads `pending` and `locked` entries only, and the pull is shared once
per store generation). Every name of the org then showed the neutral fallback, the notification text included. Whether
the pull or the log job came first depended on timing, hence the intermittent failure (about one run in eight). The hub
fix of HOT-02 (events and head from one snapshot) did not change this: the runs of HOT-03 after that merge still noted
"…".

**Correction.** The reader treats an unverified platform log like an `lh` beyond the verified head: it runs the log
job first (which verifies the platform log) and reads the event afterwards; if the job fails, the entry stays
`pending`; the log job's retry (A-22) verifies the platform log later, and a run that changed only the platform log
(the org log unchanged) now also calls `changed`, so the reader reads the pending entries again. A platform log that
the hub does not have at all still leads to `log_rights` after a successful run, as before; the reader remembers that
per org and does not start a log run for every further event. No check is relaxed.

**Ownership.** The root cause lies in `core/store/reader.ts` and `core/jobs/log-verify.ts`, outside the listed paths
except the latter; the fix belongs there (the notification text only shows the reader's verdict). No running package
owns the reader.

**Tests.** `reader.test.ts`: "reads an event that arrives before the platform log was verified …" and "keeps such an
event pending while the platform log cannot be read …" (red before: `unverifiable`/`log_rights`). E2E-26 now checks
the workgroup name of the toast and of the notification row strictly (no fallback, no note).

## A-22: an org stayed "being checked" after a network failure at sign-in

**Correction.** A job may opt in to retries (`JobRegistration.retry`); a run of such a job that fails with a transient
hub error (`HubError` `unavailable` – also a network failure – or `rate_limited`; a bug or a refusal is not retried)
makes the runner fire the same trigger for the org again
after a backoff of 1 s → 30 s with jitter (the backoff of the live channel), until a run succeeds, the org is blocked,
the user signs out or the runner stops. Re-firing the trigger (not only the failed job) also runs the jobs that depend
on the verified log (for `login`: wrap verification, catch-up). Log verification opts in. Refusals (`CryptoError`)
still block the org; other hub errors (`forbidden`, `not_found`, …) are not retried.

**Ownership.** The retry lives in the job runner (`core/jobs/index.ts`), not only in `log-verify.ts`: a job run cannot
schedule its own re-run through the per-org queue, and a retry loop inside the run would hold up the unlock, which awaits
the `login` runs. The change is additive (an optional field of the registration); no package owns the runner at the
moment.

## A-23: `resync` did not reach the slices

**Correction.** After the pulls of a `resync` frame the live channel hands the frame on (`handlers.frame('resync', …)`),
like it hands on `hello` after a reconnect. The operations slice counts `resync` like `hello` (its lists are pulled
again; `core/store/slices/operations.ts`, one handler added). The task, session and notification slices reload nothing
on `hello` either; giving them a reload after a reconnect is a separate change of those slices and screens (reported as
open in the package report).

## A-24: WebGL context loss

**Correction.** The WebGL addon's `onContextLoss` (fired when the context was not restored within its 3 s) loads the
canvas addon on the same terminal, which keeps its buffer. The WebGL addon is not disposed: its disposal installs
xterm's DOM renderer, whose `<style>` elements the CSP forbids. Tests: a Vitest with the addon fakes and a Playwright
spec that loses the real context (`WEBGL_lose_context`) and checks the canvas layers, the following output, no style
element and no CSP report.

## A-25: device list polling in hidden tabs

**Correction.** The poll only reads the list while `document.visibilityState === 'visible'`; when the tab becomes
visible again the list is read at once.

## B-23: device names and usage per session

**Correction.** Outside the device screen a device is named by its label from the newest verified `device.profile`
event of its org (`ui/screens/devices/labels.ts`: the profiles of each org with a verified log are pulled once; the
settings' device choices look in every org of the user, a session or operation in its own org). Until a label was read
the neutral name of the device screen is shown ("Device …" and the end of the ID), never the bare ID prefix. The messages
that wrapped the prefix (`task.sessions.device`, `github.devices.option`, `credentials.devices.option`,
`operations.detail.by-device`) now take the name. The sessions tab shows each session's usage (input and output tokens,
cost with its basis) from the `usage` slice, or "No usage yet". The hook re-renders only when a label map of its orgs
changes, not on every stored event. In the settings' device choices a device in several orgs takes the first label
found among the user's orgs (each label is a verified `device.profile` of an org the user belongs to).

## B-18: "quota not available" reaches the hub and the web

**Correction.** `body_quota` gains a second form `{owner_id, unavailable: true, fetched_at}` (contract note
`contracts/changes/KOR-05.md`). The runner sends it on the first `401`/`403` of the usage endpoint (05-runner.md
section 10.6); the hub accepts it only from the polling device (as windows) and deletes the owner's `quota_snapshot`
rows fetched at or before it. `GET /v1/me/quotas` then answers no rows, which the web already shows as "not
available" (06-web.md section 8.2) – no new table, no API change. A later successful fetch inserts windows again.

**Limit.** The rows are deleted, so nothing is left to compare a late report with: if the polling device fetched
windows, went offline before pushing them, another device reported `unavailable` meanwhile and the first device became
the polling device again, its queued older windows would show until its next poll (at most 900 s later) reports
`unavailable` again. A tombstone per owner would close this; it needs a schema change and was not added for this
low-severity finding. Documentation need for DOC-01: 02-data-model.md section 2.8 (`quota_snapshot` rows are dropped
on `unavailable`), 03-api.md sections 4.1 (no rows = "not available") and 6.1 (`quota` body forms), 05-runner.md
section 10.6 (the runner reports `unavailable` to the hub).

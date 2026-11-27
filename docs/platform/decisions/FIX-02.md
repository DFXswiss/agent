# FIX-02 – decisions: a finished review comment reaches GitHub in seconds

A session that had finished an A38 review text needed minutes to post it: its work VM calls timed out again and
again, before posting it rebuilt the A38 format check by hand, its posting step was then refused because the turn had
no acting user, and a model rate limit stopped it until the person wrote again. All four causes are closed as classes
([KONZEPT.md E-19](../KONZEPT.md#e-19)); the check is a platform operation that needs no VM
([KONZEPT.md E-37](../KONZEPT.md#e-37)).

## 1. Cause of the work VM timeouts

Every `workvm_exec` (default `sync: to_vm`) and every `workvm_sync to_vm` mirrored the workspace before it did
anything else, and that mirror cost grew with the size of the whole workspace, not with what changed:

1. The runner asked the guest for `files/export` of `.` - the guest read, hashed, spooled and tarred **every** file and
   streamed the whole archive through the forwarded control socket of the session's VM.
2. The runner then checked that archive twice (`read_export`, then `write_push_tar` scans it again) by hashing every
   member, synchronously on its event loop, and only then compared it with the mirror manifest to find the changes.
3. The guest waits for a work VM answer at most `GUEST_MAX` = 33 s (`agent_guest.mcp_tools._forward`). A mirror that
   takes longer ends the tool call with `timeout` although the work goes on; the repetition of the session started a
   second full export of the same workspace next to the first one (nothing serialised the mirrors of a session).

A task that checks out several repositories passes this limit quickly. Measured on the work VM of the task that
changed this (one checkout, 10 731 files, 68 MB), from the call to the start of the job: first mirror 49 s, a call
with **no** changed file 37.9 s, then 22.6 s and 62.9 s (a few changed files), every one of them answered `timeout`;
the same tool with `sync: none` answered at once. The CPU part of the same mirror on that VM is small (guest export
0.34 s, runner check 0.27 s, push tar 0.52 s), so the time is the transfer of the full archive over the guest link,
on every call.

## 2. What changed

- `files/export` takes `known` (keys of the files of the last mirror) and leaves out the content of every file whose
  key is known; the manifest still lists every file with its SHA-256 and names the left-out ones in `omitted`
  (contracts: [changes/FIX-02](../../../contracts/changes/FIX-02.md)). After the first mirror only changed files cross
  the link; an unchanged workspace is a manifest.
- The runner sends `known` only to guests from 0.8.0 (`guest_features.KNOWN_EXPORT`, the HOT-23 rule), asks again
  without it when a guest refuses the field all the same, and exports again in full when an export left out a file
  the plan needs (`PushPlan.incomplete`).
- The mirrors of one work VM and session run one after the other (`WorkVms.mirror_locks`), so a repeated call waits
  for the running mirror and then sends only what is left.
- The archive checks (`read_export`, `write_push_tar`, `merge_from_vm`) run in a worker thread, so a large mirror no
  longer stalls the runner's other calls (status and output calls of the same device timed out during a mirror).
- Every mirror logs `runner.workvm push ... transferred=<bytes> export_ms= apply_ms= total_ms=`.

## 3. The A38 check as one operation

`github.comment` takes `a38_review: true`. The runner reads the posting account (`GET /user`) and asks the guard's own
parser whether the guard would count the comment if it were posted now by that account:
`agent_cli.a38_review.check_comment` runs the guard's selection (`select_review_comment` for a first run,
`second_run_status` for a second run) on that one comment with the facts the guard reads (pull request, `REVIEW.md`
at the base, head commit time, changed files, a declared ancestor's history). Only then is the comment posted;
otherwise the operation fails `params_invalid` with the guard's reasons, and nothing is posted.

- The parser is the one shipped with the runner, the guard code of this repository; it is not copied, so the check and
  the guard cannot drift apart (00-overview section 5 lists the import). A consumer repository that pins an older
  guard revision may judge a new schema field differently; its guard comment stays the final word.
- The check does not judge the rest of the pull request (a second review run, the local CI report, open threads).
- A GitHub failure where a retry may succeed (network, 5xx, rate limits) stays a retryable failure of the operation
  instead of becoming a wrong "unavailable" reason; an account the token cannot read (`GET /user` refused) fails closed.
- The harness rules tell sessions to use it and to put the finished text into `body` directly instead of a file of
  the work VM.

## 4. The posting step lost the person (`github_not_connected`)

Same task, 17:01:21 UTC: `github.comment` of the main session was rejected `github_not_connected` with
`acting.NO_ACTOR` - the operation was stamped without an acting user. What the platform recorded: the person's prompt
"done" at 16:53:52 opened the turn (`session.working` 16:53:54); the session did not become `idle` before 17:04:31, it
journaled two `session.error` events at 16:56:13 and 16:56:17, and the rejected request came in that same stretch. The
event bodies are sealed, so which record left `runner.session.turn_actor` empty cannot be read back; the pipeline only
logged `no_acting_user operation_id=...`.

The acting user belongs to a turn and is taken only from the verified author of an accepted prompt. Every turn the
platform starts by itself has no such author, and its operations are refused although they continue a person's
instruction: the continuation after credit (`billing.pause`) and after a model pause (`model.fallback`) - and, with
this change, after a model rate limit. Changed:

- `credentials.acting.continuation` records a platform prompt before it is registered, with the session's
  `turn_actor` at that moment (the person of the interrupted turn; it stays after `idle`). When the guest accepts the
  prompt, that person acts in the new turn (`prompt_accepted`), once and only in that session; another person's open
  turn still makes it unknown, as before. Migration `0040`, table `runner.platform_prompt`. All three continuations use
  it.
- A refusal for a missing acting user now logs why: `no_acting_user operation_id= session_id= turn_open=<bool>
  last_prompt=<author|no_author|none>` (`acting.unknown_cause`), so the next case names its cause.

## 5. The model rate limit stopped the session (`429`)

17:02:29 UTC: `API Error: Request rejected (429)`. Claude Code ends such a turn with the hook `StopFailure`, whose body
names the error class (`error`: `rate_limit`, `overloaded`, `server_error`, ...). The guest made the session `idle` and
dropped the class; the model fallback acts only on used-up quota readings, so nothing continued the turn and the
posting waited for a new prompt (the person wrote twice, 17:04:39 and 17:06:04). Changed:

- The guest journals the class with the `idle` state (`stop_error`, guest 0.8.0; `hook_addons.idle`,
  `control_routes.control.turn_ended`).
- `model.retry`: an `idle` with `rate_limit`, `overloaded` or `server_error` of a main session is continued by the
  platform after 1, 3 and 10 minutes (at most three continuations until a turn ends normally), with the platform's
  sender line and the person of the interrupted turn (section 4). It does nothing when a person's prompt waits, the
  session ended, or a credit or model pause holds the session (that pause continues it). Migration `0040`, table
  `runner.turn_retry`; run by the daemon task `model_fallback`.

## 6. Measurement

The result's `a38.timings_ms` and the log line `runner.github a38_review` record every step from the operation
reaching the runner ("comment text ready") to the posted comment: `queued`, `account`, `check`, `post`, `total`; a
refused check logs the same steps with `posted=false`. Before: several minutes from the finished text to the first
comment (several timed-out work VM calls of about a minute each, the hand-written format check, then `github.comment`).
After: one operation whose duration is the sum of a handful of GitHub requests; the values of a real run are in the
result and the log.

# Script-owned issue coordination

The assignment-to-PR workflow is specified in
[DESIGN.md §19.7](../DESIGN.md#197-issue-assignment-to-human-merge). The
coordinator connects the existing session store, task spine, GitHub executor,
and bounded model lanes on the execution device. It never merges.

## Implemented versus deployed

| Piece | Status |
|---|---|
| Configuration schema (`coordinator_config.py`, `$AGENT_HOME/coordinator.json`) | Implemented. Absent/empty config enables **no** worker. |
| Runtime `coordinator.tick` / `coordinator_runtime` (+ `coordinator_*.py` helpers) | Implemented in this repository. |
| CLI / daemon | `agent coordinate --session ID` advances one worker; `--follow` is the script loop. The daemon starts explicitly configured workers on startup. Changes to existing workers are read each tick; changes to the daemon worker set require restart. Legacy assignment dispatch and `supervise` refuse these sessions. |
| Operator accounts, roles, `check_argv`, `readiness_argv`, workspace roots | **Never installed automatically.** Operators add them explicitly. |
| End-to-end deployment on a named host | Not claimed. Deployment hostnames stay out of this public repository. |
| Universal sandbox / forced model isolation | **Not claimed.** The implementer argv depends on the first-slot provider: Grok denies Bash/subagents/web-search; Claude and OpenRouter use Claude Code restricted mode with only worktree file tools, disabled slash commands and strict MCP configuration; Codex is refused as slot `first`. That is process argv hardening only. |

Distinguish a requirement (DESIGN §19.7), an implemented module, and a verified
deployment. This document does not invent evidence that a device is running the
coordinator.

## Explicit configuration

`$AGENT_HOME/coordinator.json` starts absent. A missing file, `{}`, or
null/empty `workers` enables no worker. Operator-supplied worker keys are
existing session IDs, bound explicitly in `github-accounts.json` and
`ai-accounts.json`; the coordinator does not select accounts or roles for them.

Each configured worker explicitly names `workspace_root`, `reply_logins`,
`poll_seconds`, `lane_timeout`, and `check_timeout`, plus at least one of
`repositories` or `repository_owners`.
Non-A38 workers also name `review_session`; A38 workers use the external-review
configuration below. Each repository entry names its `base`, `publication_repo`,
`check_argv`, and `readiness_argv`. The two argv arrays belong to trusted
device configuration, never issue text or model output. They run the target
repository's required tests and additional readiness validation;
repository-specific policy stays outside the core. The publication repository
can equal the target repository; a different repository requires operator
authorization for that publication route.

For an A38 worker, `check_timeout` must be long enough for the complete A38
suite. The same existing worker timeout bounds the optional trusted precheck
and each bounded A38 command; there is no separate A38 timeout setting.

`repository_owners` is a distinct, case-insensitive list of GitHub organization
or user logins. One paginated GitHub issue search per tick discovers assigned
open issues under those owners. An explicit `repositories` entry wins for the
same repository. At acceptance, an owner-scoped task pins the repository's
current default branch and its publication route: direct push when the worker
has push permission, otherwise `<worker-login>/<repository-name>`. A missing
fork is requested once per persisted request (a crash before the request is
recorded repeats it). If it is not yet visible, acceptance stays in the same
phase and the worker's `poll_seconds` loop retries; after 300 seconds the
coordinator publishes a blocker. An existing fork with another parent is
refused. Outside A38 mode,
`owner_check_argv` is required and nonempty for owner-scoped repositories; A38
mode alone permits `[]`. `owner_readiness_argv` is required outside A38 mode.
Both stay in the local worker config: the task stores only repository, base and
publication target, and the execution binding pins the commands' hash.
In A38 mode, an absent owner readiness argv may set `contributing_ok` only when
the latest recorded first-slot quality gate (`grok-pr` / `quality`) is approved
on the exact current head; it then records `conformity-a approved at <head>` and
closes the deviation keys only from the accepted hub `assignment.created` row.
That row must contain the exact inner statement
`deviation: {"declared": false, "stated_by": "<same actor>"}`. Both deviation
keys use source `human` and evidence `hub mandate <row id>: <actor> stated
deviation declared=false`. A missing, true, differently attributed, or
otherwise malformed statement closes neither deviation key and publishes
`mandate has no explicit deviation statement; issue the assignment through the
hub with "no deviation authorized"`. No grant is ever inferred; a declared
deviation still needs an explicit grant through `owner_readiness_argv`.

`require_hub_assignment` defaults to `false`. When true, acceptance also makes
one device-authenticated `/sync/query` for `assignment.created`. `agent assign`
sends the repository spelling exactly as supplied and requires
`--no-deviation`; the request carries `deviation: {"declared": false}`. The hub
records the actor as `stated_by` in the mandate while canonicalizing the
repository spelling. The coordinator therefore queries `payload.issue_key`
with the configured
`<repository>#<number>` spelling, its case-folded form, and GitHub's canonical
`<owner>/<name>#<number>` spelling from the issue or assignment-event response
(deduplicated when equal), then compares the returned repository
case-insensitively. The compatible hub adds the canonical issue key to every
`assignment.created` payload. The coordinator accepts only a row for the issue
and agent with all of these properties: the top-level query-row `github_login`
equals the GitHub assignment-event actor and is listed in `reply_logins`;
`origin_device_id` is
exactly `web:<that envelope github_login>`; and the inner assignment payload's
`actor` is case-insensitively equal to that envelope login and its `issue_key`
matches one of the queried issue spellings. Thus a device-origin row is rejected
even if the rest of its payload matches. This property requires
the hub to refuse device ids and pushed origins beginning with `web:`; only the
hub may create that named website origin. The hub's
`replica_out` query shape exposes `github_login` and `origin_device_id` at the
row top level; it does not use the `_github_login` copy produced by other hub
state endpoints.

The owner-scoped A38 default without `owner_readiness_argv` always performs
that hub-mandate verification even when `require_hub_assignment` is false.
Without a matching hub row it cannot reach implementation; after the normal
visibility grace it publishes the explicit-deviation blocker above. A matching
row without the exact no-deviation statement blocks acceptance at once with the
same blocker, drops the pinned mandate and resumes at `accept`, so the next
query picks up a corrected hub assignment; readiness checks the statement again.

A definite mismatch stays a same-phase retry while the GitHub `assignment_at`
is younger than 120 seconds. After that grace period it publishes `assignment
was not issued through the hub`, or the explicit-deviation blocker above for
the owner A38 default, blocks acceptance, and pins the issue reply checkpoint
without failing the task or starting a model. An unpaired device,
hub HTTP 400/401/403, or a query response with at least 500 rows is likewise a
configuration blocker published once with a reply checkpoint. Other hub
transport/protocol failures keep phase `accept`, retry,
and publish a blocker at most once per hour according to
`hub_assignment_retry_published_at`. The cap remains a defensive truncation
check rather than a definite mismatch. Both blockers post fixed texts; the hub
error detail, which can name the hub host, stays in the task as
`hub_assignment_retry`.

Example device configuration (illustrative values, **not installation defaults**):

```json
{
  "workers": {
    "worker-session": {
      "review_session": "review-session",
      "workspace_root": "/absolute/operator/worktrees",
      "repositories": {
        "example/project": {
          "base": "develop",
          "publication_repo": "example/project",
          "check_argv": ["/absolute/operator/full-checks"],
          "readiness_argv": ["/absolute/operator/readiness"]
        }
      },
      "repository_owners": ["example"],
      "owner_check_argv": ["/absolute/operator/owner-checks"],
      "owner_readiness_argv": ["/absolute/operator/owner-readiness"],
      "require_hub_assignment": true,
      "reply_logins": ["AuthorizedHuman"],
      "poll_seconds": 30,
      "lane_timeout": 1800,
      "check_timeout": 600
    }
  }
}
```

Both sessions must already exist with the required skills and explicit account
bindings. The accepting script verifies the actual human assignment event
before using it as the spine's human specification evidence. Missing evidence
blocks implementation. No model makes that acceptance decision or posts its
confirmation.

Selected execution profiles, repository route and check commands are pinned to
the task. Changing them blocks that task instead of silently changing its
execution identity. Adding unrelated profiles does not invalidate the binding.

## Public API

```python
from agent_cli.coordinator import tick
from agent_cli.coordinator_config import load_coordinator_config

workers = load_coordinator_config(store.home)
for worker in workers.values():
    observations = tick(store, worker, runner=run_argv, lane_runner=None)
```

### Assumptions

- **`tick(store, worker, *, runner=run_argv, lane_runner=None) -> list[str]`**
  performs **one** bounded, resumable advancement. It is not a monitoring loop.
  The outer CLI owns polling (`poll_seconds`) and invokes workers.
- A Postgres session advisory lock (`coordinator-worker:<session_id>`) is held
  for the whole tick and released on success and on error, so concurrent
  same-session ticks across processes are excluded. Device-wide source
  admission for `repo#issue` uses `coordinator-source:<repo>:<number>` so two
  workers cannot open duplicate tasks/PRs for the same issue.
- Before accepting work, `tick` preflights: worker session locally
  owned/active with skills `spine`, `review-loop`, `pr-review`; formal
  `review_session` owned/active with `pr-review` for non-A38 workers; all required AI lane slots
  present for the worker session. Non-A38 workers require configured,
  authenticated worker and review GitHub accounts bound to **different** logins.
  A38 workers require the worker account plus `second_reviewers` that exclude
  its login. The worker account has git
  identity for signed commits. Missing or mismatched profiles start **no**
  provider. A missing `models` block reuses the shared error and is reported as
  `preflight blocked: models is not configured: the device's ai-accounts.json
  must name models.first and models.second (set by the local harness)`; the
  outer `coordinate --follow` loop remains active and retries later ticks after
  configuration is repaired.
- Required AI slots, with `<first>` / `<second>` the providers of the device's
  model slots (`ai-accounts.json` → `models`): `<first>:implementer`,
  `<first>:reviewer`, `<first>:pr-reviewer-quality`, `<first>:pr-reviewer-logic`,
  plus `<second>:pr-reviewer-quality`, `<second>:pr-reviewer-logic` when the
  second slot is not external. The coordinator
  pins `models` on each task it accepts and refuses `models.first = codex`,
  because a codex implementer can run commands. It requires `implementer` to
  be absent or equal to `models.first`; a separate implementer is refused at
  preflight, task creation and launch with `SEPARATE_IMPLEMENTER_REFUSED`. A
  required `models` block and `lane_execution` are part of the execution
  binding, so changing either stops accepted tasks with "task execution
  configuration changed" until it is restored. A task accepted under the
  preceding hash formula accepts that legacy hash once and is immediately
  repinned when its only formula differences are the newly added
  `lane_execution` field and the old omission of the historical `grok`/`codex`
  `models` value. Other configuration changes still stop the task.
  `lane_execution` accepts only `this-device-is-lane-host`: the local harness's
  lane starter starts the coordinator on the external lane host. The Agent
  coordinator does not SSH, mirror, or copy a lane from a work device.
- Checkpoints live in `task.payload['coordinator']` and activity `result`
  fields. An accepted A38 task records `coordinator.a38.enabled=true`; external
  second-review evidence is refused without that marker. There is **no** second
  hub state machine and **no** new store table.
- `runner` executes `gh`/`git` trusted calls and returns
  `Completed(returncode, stdout, stderr)`. GitHub-scoped calls go through
  `Account.runner` (explicit `GH_CONFIG_DIR`), never an ambient login.
- `lane_runner(argv, stdin)` is optional. When omitted, lanes and trusted
  argv lists run via a Python bounded subprocess (process-group kill on
  timeout), preserving stdin and cwd. External `timeout(1)` is **not** used
  (absent on stock macOS). Tests inject fakes. The implementer argv
  is hardened per first-slot provider: Grok with `--deny Bash`,
  `--no-subagents`, and `--disable-web-search`; Claude and OpenRouter through
  Claude Code `--restricted`, `--strict-mcp-config`, disabled slash commands,
  and only `Read,Grep,Glob,Edit,Write` for the worktree. Read-only review lanes
  get only `Read,Grep,Glob` plus the coordinator control directory as an
  explicit read directory. OpenRouter account configuration must not overlap
  either readable root. Codex is refused as slot `first`. This is process argv
  hardening, **not** universal sandbox enforcement.
- Environment context for trusted `check_argv` / `readiness_argv` (set in the
  child environment, with cwd = worktree):
  `AGENT_COORDINATOR_HEAD`, `AGENT_COORDINATOR_BASE`, `AGENT_COORDINATOR_REPO`,
  `AGENT_COORDINATOR_PR`, `AGENT_COORDINATOR_SESSION`,
  `AGENT_COORDINATOR_WORKTREE`. Those argv arrays never come from model or repo
  content.
- The default process runner removes ambient GitHub tokens and uses an empty
  temporary `GH_CONFIG_DIR`. Script operations that need GitHub select their
  configured account through `Account.runner`; trusted check/readiness scripts
  must do the same (see [github-accounts.md](github-accounts.md)).
- Models only edit/review/read. They never Git, GitHub, test, monitor, or merge.
  Reviewer approval is only `STATUS: complete` plus `RESULT: approved`.
- Coordinator control/spec/log files live under
  `workspace_root/.coordinator-control/<task-id>/`, **outside** the model
  worktree, so internal prompts are never staged as a patch.
- Signed commits use explicit `git commit -S` with the configured Git identity.
  `git verify-commit` must succeed cryptographically. SSH verification requires
  trusted allowed-signers in the Git account executor environment/config;
  signature text alone is never treated as proof.
- Target repository is `source.repo` for all PR API/gate calls. `publication_repo`
  is the branch push location only. Base is fetched/pinned from `origin`
  (target), never from a stale fork develop. When publication differs, PR head
  is `publicationOwner:branch`.

## Workflow (script-owned)

1. **Discover** explicit repositories and approved owners for open issues assigned to the
   configured GitHub login (`gh api --paginate --slurp`). Owner discovery uses
   `search/issues?q=is:issue+is:open+assignee:<login>+archived:false&per_page=100`
   once per tick and filters `repository_url` by owner. Initial scan includes
   current assignments (no silent first-run ignore). Idempotent source key:
   `repo + issue number` device-wide — first session owns; one task/PR until
   terminal. Failed tasks are **not** auto-reopened merely because the issue is
   still assigned, and this coordinator does **not** implement automatic
   recovery of `state=failed` tasks on reply alone. Assignment evidence is
   verified on GitHub; forged model activity payloads are not trusted. Issue
   bodies are redacted/bounded before persistence. `updated_at` is not treated
   as `assigned_at`.
2. **Accept** only after verifying the GitHub assignment event and, when
   `require_hub_assignment=true`, the matching hub-created mandate. Resolve and
   pin an owner-scoped repository's default branch, checks, readiness command,
   and direct-push/fork route at this point. Then publish a deterministic issue comment (fixed wording + idempotency
   marker) via `comment.post` / `scan_github` **before** any model start. A
   failed comment never starts a model. Effects are discovered on retry.
3. **Checkout** under `workspace_root/<task-id>` with named remotes `origin` /
   `publication`, ownership marker, pinned base revision from origin, and a
   deterministic feature branch. Clone, fetch, push, and signed commits use the
   explicit GitHub account runner. Never push a protected branch, never
   force-push, never reuse an arbitrary dirty/wrong directory as a fresh
   checkout. Interrupted clones are refused without deleting unrelated content.
4. **Implement / inner review** via `lane.launch` builders with explicit session
   and config home. No round cap. Rejection routes findings to a fresh
   implementer. Outcome distinction (review-loop preserved, not waived):
   - `RESULT: ask` → publish a question checkpoint, keep the task non-failed,
     wait for an authorized reply, then resume `implement`.
   - `RESULT: blocked` → publish a blocker, set `task.state=failed`, clear
     `resume_phase`, and stop. Later ticks do not advance that task; discovery
     only notes that the failed task remains. Automatic reply-recovery of
     failed tasks is **not** implemented.
   - Incomplete implementer status / invalid RESULT, missing required
     `SUMMARY_EN`/`SUMMARY_DE` after `done`/`no-change`, accept-time
     unassignment, and external blockers (CI `action_required`, inaccessible
     CI logs, incomplete inner/PR reviews) keep a non-failed task, set
     `resume_phase` to the exact safe phase (`implement`, `accept`, `ci`, …),
     and pin `question_activity_id` on the published checkpoint so an
     authorized reply can resume that phase — never a blind implementer start
     for CI authorization or acceptance. `publish_blocker` also derives
     checkpoint eligibility from `resume_phase` + non-failed / non-done /
     non-uncertain state so a missed boolean cannot wedge another recoverable
     path; status re-publishes keep the existing checkpoint. Uncertain lane
     outcomes refuse a second model start (a human reply must not silently
     duplicate an uncertain process) and publish a GitHub-visible blocker.
5. **Draft** as soon as the first signed task commit exists (`pr.open` on the
   **target** repo), before full tests/reviews. Each new signed head is pushed
   to the existing PR before later stages. No empty fake PR when there is no
   patch and no existing PR. Crash after commit/push before draft reconciles
   without starting another implementer. `task.ref` holds the **PR** number
   only (never the issue number).
6. **Tests** run only via script `check_argv` on the exact clean signed head
   (cwd = worktree). Failure routes bounded output to the implementer. Stale
   passes from another head are not reused.
7. **PR gates**: model slot `first` quality+logic in **parallel** (fresh
   independent invocations; agent rows prepared on the main thread, subprocesses
   in threads, results persisted on the main thread), then model slot `second`
   quality+logic the same way only after both first-slot dimensions are approved
   on that head. The persisted phase names `pr_gates_grok` and
   `pr_gates_codex` select stages `review-1` and `review-2`, respectively. Author
   session does not sit those reviews. Incomplete/unavailable vendor output is a
   GitHub-visible blocker with `resume_phase` of `pr_gates_grok` /
   `pr_gates_codex` and a pinned `question_activity_id` — not a rejected
   complete gate and not an implementer fix loop. Rejections publish
   `review.post` **COMMENT** (not `REQUEST_CHANGES`) and invalidate
   head-specific evidence. A successful second-slot lane still starts when the
   checklist keys are `n_a`. It replaces `n_a` with `ja` for
   `review_2_quality` and `review_2_logic`, including on a later tick whose
   gate is already approved. After `ja`, both second-slot gates are required again.
   `n_a` alone does not grant Ready or done. One review is enough only when
   the pull request opener is listed in one_review_authors in the trusted
   .github/pr-guard.json on the default branch. A missing file, a missing
   key, or an empty list keeps two reviews required. An entry that exists
   only on the pull request head does not count.
8. **CI**: exact-head PR check rollup **and** paginated head workflow inventory
   (path+event+attempt), classified by one shared observer used by both
   ordinary `phase_ci` and Ready-side fresh rechecks. Only `success` counts.
   An absent `statusCheckRollup` is normalized to `[]` and inventory is still
   inspected — `action_required` (and actual failures) may be present only in
   inventory; absent rollup prevents green but must not hide those facts.
   `action_required` is an external authorization blocker (not routed to the
   implementer; `resume_phase` stays `ci`; `question_activity_id` is pinned so
   an authorized reply resumes CI observation). Hard protocol faults
   (malformed rollup, unexpected inventory shape, missing `workflow_runs`,
   pagination truncation — typed `CiObservationProtocolError` /
   `CiInventoryProtocolError`) enter recoverable blocked + `resume_phase=ci` +
   checkpoint; an authorized reply resumes CI once valid evidence is restored.
   Transient observation transport (PR view **or** inventory `gh_json` /
   typed `CiObservationTransportError`) and pending / absent-yet evidence stay
   on static `phase=ci` and retry without an idle model, blind implement, or
   reply gate. Missing / pending / failure / cancelled / skipped / neutral are
   not green. This core observes **cumulative GitHub CI** only; target-
   repository policy / A38 live join belongs to configured `readiness_argv`.
   Failures fetch plain-text logs via
   `gh run view <id> --repo <target> --log-failed --attempt <n>` (never ZIP
   `/logs` archive bytes). Inaccessible logs, including successful fetches with
   empty or whitespace-only output, are a reply-recoverable blocker.
   Transient pending returns without an idle model.
9. **Ready**: run `readiness_argv` (cwd = worktree, ambient GitHub tokens
   cleared). Stdout must be the fixed JSON readiness contract below (trusted
   operator script output — not model/repo input). Re-verify clean signed head
   **after** the command, re-observe CI fresh (no stale `ci_green`), unchanged PR
   head, author/base/mergeability, tests, and all four same-head gates. The two
   `review-2` gates are omitted on readiness, formal approve, and leave-draft
   only when both `review_2_*` checklist keys are already `n_a`, the evidence is
   exactly `second review not posted`, and the opener is listed in
   one_review_authors in the trusted .github/pr-guard.json on the default
   branch. A missing file, a missing key, or an empty list keeps all four
   gates required. An entry that exists only on the pull request head does not
   count. Fresh
   CI rechecks on readiness / formal_approve / leave-draft (including after the
   Ready evidence comment) treat pending / absent-yet evidence and transient
   observation or PR-metadata transport as same-phase retry: no new authorized
   reply, no idle model, no premature APPROVE/Ready. Hard protocol faults stay
   fail-closed blockers. Observed actual failed CI raises a typed
   `CiObservedFailureError` handled by `advance_one` (not a Ready-phase reply
   gate): phase returns to script `ci` so the next tick reuses existing
   `phase_ci` log fetch → implement routing; inaccessible logs keep the
   existing external blocker. No premature APPROVE/Ready and no model until
   actual logs exist. A known mismatched head/author/base is distinct from an
   unavailable fetch. Close `contributing_ok` / deviation checklist keys from
   that JSON via
   `chain.close_allowed` before Ready in the normal route. If A38's guard and a
   human merge move the task from `await_second_review`, `ci`, `readiness`, or
   `await_ready` to `await_merge` first, `await_merge` runs the same trusted
   readiness closing on the merged head before evaluating `task-done`. Formal
   `review.post` **APPROVE** from the separate review account with an
   explicitly validated full-SHA `commit_id` in the activity payload
   (executor discover-before-POST and POST both bind that head; verify
   state/head/login/id/url). Leave-draft re-runs readiness, then performs a
   fresh GET that must still show APPROVED on the exact head **immediately
   before** the Ready mutation (stored `formal_head` is not current proof; a
   dismissal during readiness or the evidence comment must block leave-draft).
   Only an **observed** same-marker revoked / non-APPROVED / misbound approval
   (or the matching typed `review.post` executor error) clears stale formal
   evidence, pins `resume_phase=formal_approve`, and requires an authorized
   **new** reply before another approval attempt — human dismissal is not
   silent override permission, and absent/unverified discovery alone is not
   treated as dismissal. Transient POST/transport or not-yet-visible discovery
   preserves the durable attempt/activity id and retries/reconciles through the
   existing executor without new human authorization; unknown delivery is never
   counted as approval. The resumed post-dismissal attempt uses a new durable
   activity occurrence (same attempt stays crash-idempotent; a dismissed
   same-marker APPROVE fails closed in `review.post` and cannot be marked
   done). One evidence comment (must complete with `execution_status=done`).
   When that comment relied on the one-review exception, leave-draft reads the
   trusted list again before Ready. A transport failure retries and does not
   set Ready. If the opener is no longer listed, it posts a new comment that
   Ready was not set and does not call `gh pr ready`. The same correction is
   posted on a later leave-draft when that one-review evidence comment is
   already done, both `review_2_*` checklist keys are still `n_a` with evidence exactly
   `second review not posted`, and the live list no longer grants the
   exception, before the gate check can hide it. A transport failure on that
   later read retries and does not set Ready. A later leave-draft that
   has both review gates posts its own evidence comment. One review is enough
   only when the pull request opener is listed in one_review_authors in the
   trusted .github/pr-guard.json on the default branch. A missing file, a
   missing key, or an empty list keeps two reviews required. An entry that
   exists only on the pull request head does not count.
   `allow pr-ready`, then leave draft and verify `isDraft=false`. **Never merge.**
10. **Complete** only after a verified **human** merge: GitHub merge actor type
    must be exactly `User` (missing type is not human; Bot is refused). Also
    require merge SHA, timestamp, and base/target. Then existing `task-done`
    checklist / summary guard (summaries must already describe the actual
    result — no boilerplate invented at merge), then `issue.assigned.ack`. A
    Ready PR closed unmerged, or a non-human merge, is an intentionally
    non-recoverable user-facing blocker: stale `question_activity_id` /
    `resume_phase` checkpoints are cleared, and later authorized comments must
    not consume replies or start an implementer. Reply resume requires
    `reply_checkpoint_eligible` (safe `resume_phase` + non-failed /
    non-done / non-uncertain); missing `resume_phase` never defaults to
    `implement`.
    Reassignment must not open a duplicate PR for a completed source.
    Revoked assignment stops new effects including formal approve / leave-draft;
    `await_merge` may continue observation only and, while the pull request is
    open, posts no further second-review comments once the assignment is revoked. An A38 task in `await_second_review`,
    `ci`, `readiness` or `await_ready` whose pull request is merged or closed (a
    merge closes the issue) moves to `await_merge` (an unreadable pull request state
retries the tick instead of blocking), which verifies the human merge
    or blocks a pull request closed without one. Unless readiness already ran on
    that exact merged head (`readiness_head`), even when its keys were closed on an
    earlier head, it first runs the owner hub-statement default or the configured
    trusted readiness command and publishes its blocker with a reply checkpoint
    when those keys cannot be closed.

### Trusted readiness JSON contract

Configured `readiness_argv` must print a single JSON object on stdout and exit
0. Installation defaults remain unconfigured (`NULL`); operators add the argv
explicitly. Required shape (exact HEAD + base binding):

```json
{
  "head": "<40-hex current clean signed HEAD>",
  "base": "<40-hex pinned base_sha>",
  "contributing_ok": true,
  "deviation": { "declared": false }
}
```

When a human-authorized exception exists (never inferred):

```json
{
  "head": "<40-hex>",
  "base": "<40-hex pinned base_sha>",
  "contributing_ok": true,
  "deviation": {
    "declared": true,
    "granted": true,
    "granted_by": "<login in worker.reply_logins>",
    "evidence": "<explicit human grant provenance>"
  }
}
```

No automatic grants. `deviation.declared=false` closes deviation keys as `n_a`
with human source tied to the verified assignment mandate plus this trusted
script attestation. A declared exception without `granted_by` in `reply_logins`
fails closed.

### GitHub executor scoping

`execute_github(store, runner, *, activity_ids=(...))` requires the exact
intended activity id batch. The worker must not scan the whole device store or
publish unrelated pending intents from other sessions.

### Lane outcomes and replies

Implementer `RESULT` must be `done|ask|blocked|no-change` (empty / approved /
rejected fail closed). Reviewer `RESULT` must be `approved|rejected`; `ask` /
`blocked` are not code rejections. Completed lane outcomes are persisted before
signing/publishing so crash recovery applies the recorded result instead of
starting another model. Authorized replies (`reply_logins` only), listed with
`gh api --paginate --slurp`, resume the exact `resume_phase` after the pinned
`question_activity_id` checkpoint only when `reply_checkpoint_eligible` holds
(not blindly `implement` for CI authorization / incomplete review / checkout
blockers, and not at all for terminal failed / done / uncertain /
closed-unmerged / non-human-merge outcomes). Uncertain prior agents refuse a
second model start even when a human replies. Inner and PR reviewers receive a
script-generated base→head diff artifact outside the worktree.

## Model output protocol

Every lane prompt includes strict prohibitions and requires:

```text
STATUS: complete|partial|timeout|unavailable
RESULT: done|blocked|ask|approved|rejected|no-change
```

A completed implementation additionally returns exactly one English and one
German change-summary sentence, each ending with a period, directly after
`RESULT` and before its body:

```text
SUMMARY_EN: Describe the actual change here.
SUMMARY_DE: Die tatsächliche Änderung hier beschreiben.
```

The script records these semantic summaries; it does not invent them at merge.
Missing summaries block progression. The remaining body is bounded. Empty,
partial, timeout, or unavailable output is
never zero findings and never approval. Nonzero process exits cannot approve,
even when stdout claims completion. Model text cannot certify checks, CI,
commits, or Ready. Recorded real assignment or an authorized human reply may
evidence human spec input; the script never invents human grants.

## Evidence hygiene

Outputs stored or published are redacted and bounded. Profile credentials,
config directory paths, signing-key paths, and raw auth errors must not appear
in replicated rows or GitHub text. User-facing blockers and questions are
published on the source issue only when `execution_status=done` is verified;
otherwise they remain locally visible (`CoordinatorError` / `StoreError`
subclass `SystemExit` and must not be mistaken for success). Preflight
account/config failures can only report locally when GitHub is unavailable.
No silent failure.

Every completed lane process also writes script-owned `lane.run` evidence with
the configured and provider-reported model, token usage when supplied, a bounded
redacted file-tool transcript, and bounded final text. Failure to write this
audit row is an observation and does not rewrite the lane verdict.

## Module layout

| Module | Role |
|---|---|
| `coordinator.py` | Public `tick` + worker advisory lock |
| `coordinator_runtime.py` | Preflight, discovery, implement/inner/tests, `advance_one` |
| `coordinator_git.py` | Checkout, signed commits, push, draft |
| `coordinator_lanes.py` | Lane launch + parallel PR gate stages |
| `coordinator_github.py` | Comments, CI, readiness, formal approve, Ready, merge, replies |
| `coordinator_exec.py` | Bounded subprocess helper |
| `coordinator_common.py` | Shared helpers / constants |
| `coordinator_config.py` | Parent-owned explicit-repository and owner-scoped configuration loaders |
| `hub_assign.py` | Paired-device client for operator assignment creation |
| `coordinator_a38.py` | A38 suite, author review comments, external review mail, guard Ready observation |

## A38 mode

A worker opts in explicitly with `"a38": true`. This is never an installation
default. The worker then requires a nonempty, distinct, lowercased
`second_reviewers` list that excludes its own configured GitHub login. Optional
`second_review_timeout_seconds` (default `86400`) must be at least 600.
`review_session` is unused and optional;
`check_argv` may be empty, while `readiness_argv` remains required for
configured repositories; owner-scoped repositories may omit
`owner_readiness_argv` only with the explicit hub deviation statement described
above. The device's
models must pin a non-Codex first provider and `"second": null`. A null second
slot is refused for every non-A38 worker.

Illustrative worker fragment:

```json
{
  "a38": true,
  "second_reviewers": ["reviewer-one", "reviewer-two"],
  "second_review_timeout_seconds": 86400
}
```

On each new clean signed head, the script optionally runs `check_argv`, then
validates the trusted-base A38 policy, runs the complete A38 suite in
`workspace_root/.coordinator-control/<task-id>/a38/<head>/`, verifies its
report, and posts that report unchanged with the worker GitHub account. A
failure returns bounded findings to the implementer. The first-slot quality and
logic gates then run as usual. Their approved result is rendered and validated
as the `conformity-a` / `logic-a` author comment.

The coordinator does not launch model slot `second`. It sends one
`review-request` person ping to every configured reviewer. A reviewer returns a
strict result through session mail without calling GitHub:

```json
{
  "to_session": "worker-session",
  "payload": {
    "kind": "a38.second-review/v1",
    "task_id": "11111111-1111-4111-8111-111111111111",
    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "defects": 0,
    "blocks_merge": false,
    "findings": "",
    "lanes": [
      {
        "id": "conformity-b",
        "provider": "OpenAI",
        "model": "Reviewer Model 2",
        "model_number": "reviewer-model-2",
        "prompt": "Read CONTRIBUTING.md and REVIEW.md at the base revision. Review this pull request against those files and against the linked issue. Do not change any files.",
        "status": "complete",
        "result": "pass"
      },
      {
        "id": "logic-b",
        "provider": "OpenAI",
        "model": "Reviewer Model 2",
        "model_number": "reviewer-model-2",
        "prompt": "Read CONTRIBUTING.md and REVIEW.md at the base revision. Review this pull request against those files and against the linked issue. Do not change any files.",
        "status": "complete",
        "result": "pass"
      }
    ]
  }
}
```

Save that shape with the task, head, model, prompts, and result from the actual
review, then send it through the existing activity interface. The lane
`provider`, `model`, `model_number` and `prompt` are published verbatim, so text
that redaction would change (a token, an authorization header, a private home
path) rejects the mail. Both prompts are published twice (review record and
machine block) and the findings once; together they may use at most 56,000
characters, which leaves room for the rest of the comment; the renderer still
checks the whole comment against GitHub's limit and refuses it loudly. The first
review comment carries both slot-`first` prompts twice as well, so an A38 worker
refuses a PR review prompt over 14,000 JSON characters before any lane runs:

```text
agent activity add --session OWN_SID --type message --payload-file result-mail.json
```

The relay-authenticated sender login must be allowlisted. The worker trusts that login, not the
process that wrote the mail: an allowlisted second reviewer vouches for every
second-review mail sent from a device paired to that login, as the A38 standard
expects of the person named in the second review comment. The worker validates
the mail and guard declaration, publishes the `conformity-b` / `logic-b`
comment as the pull request author, and marks both historical `codex_pr_*`
checklist keys with the evidence of every passing external review, so
the newest passing review is recorded and a pass after a block on the same head
(also after the merge) closes them. It continues accepting
unread reviews for that head through CI, readiness, Ready observation, and merge
observation, and publishes one idempotent review comment for every valid mail.
Each tick takes in all queued unread mail for the head, ordered by the mail
row's replica change time (`updated_at`, equal times by row id), and the newest
review decides; reviews with the same newest time block if any of them blocks: a blocking result returns the task to the implementer only when no newer
passing review of the same head follows it in that tick; stale, malformed, or
unauthorized mail is acknowledged and recorded as an
observation. Pending requests are re-pinged once per configured timeout and are
never skipped. A pull request closed while the task awaits the second review
stops the wait with a blocker; no further second-review comments or pings are
sent for it.

The A38 phase route is `publish_draft` → `tests` → `pr_gates_grok` →
`a38_first_comment` → `await_second_review` → `ci` → `readiness` →
`await_ready` → `await_merge` → `done`.
The worker never posts a formal approval and never calls `gh pr ready`. Lane prompts quoted in the review comments have this host's control directory, worktree, workspace root and home directory replaced by `<control-dir>`, `<worktree>`, `<workspace>` and `~`. While
the pull request remains draft it only records the newest exact enforce context
`A38 / report (<pinned base>)` selected by `a38_guard.status_context_enforce`;
observe contexts never count. It advances after the guard has made the
unchanged head Ready, then observes the human merge exactly like an ordinary
worker. The guard may make the pull request Ready while the worker is still in
`await_second_review`, `ci`, `readiness` or `await_ready`; a pull request merged
in any of these phases goes straight to `await_merge`, which verifies the merge;
a pull request closed there also goes to `await_merge`, which publishes the
`closed-unmerged` blocker. Before task completion, `await_merge` runs readiness
on that merged head with the same readiness source as the ordinary phase unless
it already ran on exactly that head (`readiness_head`); keys closed on an earlier
head do not count. An unreadable pull request state is retried on the
next tick without taking in mail.
Second-review intake runs while the pull request is open and, in `await_merge`,
also after the merge, so a review of the merged head is still published; it never
reopens work. After the merge every waiting review is taken in before
`await_merge` decides by the newest one. A merged pull request completes only with a passing second review:
`await_merge` waits up to `second_review_timeout_seconds` for it, publishes the
non-recoverable blocker `merged-without-second-review` after that, and publishes
`merged-with-blocking-second-review` at once when the newest posted review blocks the merge.
Both merge blockers are final like `closed-unmerged`: the task stays blocked, and a
review that arrives after that blocker is published is not taken in. A new head invalidates its report, both comment checkpoints, and
external-review state. A blocking review that arrives after the guard set Ready
is fixed on the same pull request: an A38 worker pushes the new head to the open
pull request whether or not it is still a draft, and the guard decides Ready again.

These A38 settings enter the task execution binding only for A38 workers, so
the binding hashes of existing non-A38 workers remain stable.

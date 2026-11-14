# Script-owned issue coordination

The assignment-to-PR workflow is specified in
[DESIGN.md §19.7](../DESIGN.md#197-issue-assignment-to-human-merge). The
coordinator connects the existing session store, task spine, GitHub executor,
and bounded model lanes on the execution device. It never merges.
The same coordinator runs local orders under the rules of
[DESIGN.md §19.10](../DESIGN.md#1910-orders-and-the-precondition-registry); see
[Orders](#orders).

## Implemented versus deployed

| Piece | Status |
|---|---|
| Configuration schema (`coordinator_config.py`, `$AGENT_HOME/coordinator.json`) | Implemented. Absent/empty config enables **no** worker. |
| Runtime `coordinator.tick` / `coordinator_runtime` (+ `coordinator_*.py` helpers) | Implemented in this repository. |
| CLI / daemon | `agent coordinate --session ID` advances one worker; `--follow` is the script loop. The daemon starts explicitly configured workers on startup. Changes to existing workers are read each tick; changes to the daemon worker set require restart. Legacy assignment dispatch and `supervise` refuse these sessions. |
| Orders, precondition registry, check lanes, dashboard order routes | Implemented in this repository. A worker accepts orders only with `"orders": true`. |
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
Non-A38 workers also name `review_session`; A38 workers use the second-review
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

`repository_owners` is a distinct, case-insensitive list of valid 1–39 character
GitHub organization or user logins (no leading, trailing, or consecutive
hyphens). Each owner is first resolved through `users/<owner>` to determine its
GitHub account type, then gets its own owner-qualified, paginated issue search
with `org:<owner>` or `user:<owner>` per tick. An empty page set, an
`incomplete_results` value other than the boolean `false`, a missing or negative
result count, changing `total_count` values across pages, a malformed or duplicate
issue identity, or a common `total_count` that differs from the unique fetched
issue count fails that owner's discovery loudly instead of silently accepting
an incomplete result set. An explicit `repositories` entry wins for the same
repository. At acceptance, an owner-scoped task pins the
repository's current default branch and its publication route: direct push when
the worker has push permission, otherwise `<worker-login>/<repository-name>`.
A missing fork is requested once per persisted request (a crash before the
request is recorded repeats it). If it is not yet visible, acceptance stays in
the same phase and the worker's `poll_seconds` loop retries; after 300 seconds the
coordinator publishes a blocker. An existing fork with another parent is
refused. Outside A38 mode,
`owner_check_argv` is required and nonempty for owner-scoped repositories; A38
mode alone permits `[]`. `owner_readiness_argv` is required outside A38 mode.
Both stay in the local worker config: the task stores only repository, base and
publication target, and the execution binding pins the commands' hash.
For an issue task in A38 mode, an absent owner readiness argv may set `contributing_ok` only when
the latest recorded first-slot quality gate (`review-1` / `quality`) is approved
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
For an order, no hub assignment row is used. On this default readiness closes
`deviation_declared` and `deviation_granted` as `n_a` from the local
submission, with evidence
`order <id> submitted by <who>: orders declare no deviation`.

`require_hub_assignment` defaults to `false`. When true, acceptance also makes
one device-authenticated `/sync/query` for `assignment.created`. `agent assign`
sends the repository spelling exactly as supplied and requires
`--no-deviation`; the request carries `deviation: {"declared": false}`. The
client accepts a success only with a positive, non-boolean issue number and a
credential-free HTTPS GitHub issue URL that names the same repository and
number, and it never prints the device bearer. The hub
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

For an issue task, the owner-scoped A38 default without `owner_readiness_argv`
always performs that hub-mandate verification even when
`require_hub_assignment` is false.
Without a matching hub row it cannot reach implementation; after the normal
visibility grace it publishes the explicit-deviation blocker above. A matching
row without the exact no-deviation statement blocks acceptance at once with the
same blocker, drops the pinned mandate and resumes at `accept`, so the next
query picks up a corrected hub assignment; readiness checks the statement again.
An order on that default performs no hub-mandate verification. Readiness
closes both deviation keys as `n_a` from its local submission, with the
evidence above.

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
check rather than a definite mismatch. Both blockers post fixed texts, and the
task records only a fixed `hub_assignment_retry` class; the hub error detail,
which can name the hub host, appears only in the worker's local output line.
An A38 worker with the external route takes its second review from people; do
not point one at a repository whose own rules require model slot `second`. An
A38 worker with `a38_second_review: "model"` runs model slot `second` itself
and can serve such a repository.

Eight optional worker keys control orders and scheduling. `orders` enables
orders and defaults to `false`. `draft_on_accept` opens the immediate draft for
issue tasks and defaults to `false`. `park_poll_seconds` is an integer at least
zero and defaults to 120. `lane_retries` is an integer at least zero and
defaults to 2. `max_rounds` is an optional positive worker-wide limit.
`max_rounds_by_model` maps model identifiers to positive limits and defaults to
`{}`. `a38_second_review` is `"external"` or `"model"`, only for an A38 worker,
and defaults to `"external"`. `conflict_remedy` is available only with orders;
it contains nonempty `check_argv`, `max_rebase_steps` from 1 to 200, and optional
positive `max_attempts`, which defaults to 2.

Only `a38_second_review: "model"` enters the execution binding. The other seven
keys are operating parameters; changing them does not stop an accepted task.
For an existing issue worker that sets none of them, waiting tasks are observed
every 120 seconds, incomplete lanes are retried twice, and the round limit is
10. `"park_poll_seconds": 0` and `"lane_retries": 0` restore the earlier
timing. The round limit has no off value; `max_rounds` and
`max_rounds_by_model` can only raise or lower it.

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
      "check_timeout": 600,
      "orders": true,
      "park_poll_seconds": 120,
      "lane_retries": 2,
      "max_rounds_by_model": {"<model identifier>": 10}
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
  A tick observes eligible waiting tasks, then advances the oldest runnable
  task by one phase. It admits at most one discovered issue, and only when the
  worker has no runnable task and no queued order. Several workers form the
  pool.
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
  A38 workers with the external second review require the worker account plus
  `second_reviewers` that exclude its login. A worker with `orders` also needs
  a read-only `<first>:check` binding and one model across the implementer,
  inner reviewer, and first quality and logic review roles. The worker account has git
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
  second slot is not external. This includes an A38 worker with
  `a38_second_review: "model"`. A worker with `orders` also requires
  `<first>:check`. The coordinator
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
- Successful GitHub login verification is cached within one tick, for at most
  `poll_seconds` per account.
- Waiting phases are `queued`, `ask`, `blocked`, `await_merge`, `await_ready`,
  and `await_second_review`; `ci` waits while its last observation is pending.
  A task also waits while its execution binding is blocked, an earlier lane
  process is alive, or retry backoff applies. A waiting task does not block the
  worker. It is observed at most once per `park_poll_seconds`, except that zero
  observes it every tick. A queued order and a task in backoff are not observed.
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
  content. In A38 mode, the trusted precheck receives the base revision from
  the stable live pull-request snapshot, including a newly observed base that
  has not yet been promoted to evidence.
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

One head follows this order:

| # | Step | Phase | Lanes |
|---|---|---|---|
| 1 | implementation; signed commit; push | `implement` | one implementer lane, slot `first` |
| 2 | inner review of the worktree | `inner_review` | one reviewer lane, slot `first`; for an order the same model as the implementer |
| 3 | local checks; A38 report comment | `tests` | none |
| 4 | first review pair on the checked head | `pr_gates_grok` | quality and logic, slot `first` |
| 5 | first review comment (A38) | `a38_first_comment` | none |
| 6 | second review pair | `pr_gates_codex` | quality and logic, slot `second` |
| 7 | second review comment (A38 model route) | `a38_second_comment` | none |
| 8 | CI, readiness, Ready | `ci` … | none |

Steps 1 and 2 repeat with fresh implementer and reviewer lanes until approval.
A rejection at step 3, 4, 6 or 8 returns to step 1 with the findings. Every
round counts against the round limit. Every lane is a fresh process
without memory of earlier lanes.

The first recorded pull-request review runs after local checks. The checklist
chain is `reviewer_approved` → `local_check_pass` → `pushed` →
`review_1_quality`, `review_1_logic`; a key that is not next cannot close. Inner
implement/review rounds are not pull-request reviews. Review gates are bound to
a head, and a failed check produces a new commit that invalidates an earlier
pair.

1. **Discover** explicit repositories and approved owners for open issues assigned to the
   configured GitHub login (`gh api --paginate --slurp`). For every approved
   owner, discovery first reads `users/<owner>` to distinguish an organization
   from a user, then performs that owner's own
   `search/issues?q=is:issue+is:open+assignee:<login>+archived:false+org:<owner>&per_page=100`
   query or the corresponding query with `user:<owner>`. There is no global
   issue result that is fetched once and then filtered by owner. Initial scan
   includes current assignments (no silent first-run ignore). Idempotent source key:
   `repo + issue number` device-wide — first session owns; one task/PR until
   terminal. The deterministic `issue.assigned` identity uses that source and
   the assignee, not the observation clock. An existing row at that identity is
   reused only when its origin device, session, type, execution status, and
   stable assignment payload match and both observation timestamps parse as
   aware ISO-8601 values.
   Otherwise the coordinator uses and validates a
   deterministic fallback identity that also includes this device id; a
   mismatched row at that fallback fails closed. Assignment activity and task
   admission are checked and written under one advisory transaction, so a
   crash or a losing worker leaves neither an orphan activity nor a duplicate.
   Failed tasks are **not** auto-reopened merely because the issue is
   still assigned, and this coordinator does **not** implement automatic
   recovery of `state=failed` tasks on reply alone. Assignment evidence is
   verified on GitHub; forged model activity payloads are not trusted. Issue
   bodies are redacted/bounded before persistence. `updated_at` is not treated
   as `assigned_at`.
   Discovery admits at most one issue per tick, and none while the worker has a
   runnable task or a queued order. The tick observes waiting tasks first and
   then advances the oldest runnable task by one phase.
2. **Accept** only after verifying the GitHub assignment event and, when
   `require_hub_assignment=true`, the matching hub-created mandate. Resolve and
   pin an owner-scoped repository's default branch, checks, readiness command,
   and direct-push/fork route at this point. Then publish a deterministic issue comment (fixed wording + idempotency
   marker) via `comment.post` / `scan_github` **before** any model start. A
   failed comment never starts a model. Effects are discovered on retry.
   An order is accepted without reading an issue and without posting an
   acceptance comment.
3. **Checkout** under `workspace_root/<task-id>` with named remotes `origin` /
   `publication`, ownership marker, pinned base revision from origin, and a
   deterministic feature branch. Clone, fetch, push, and signed commits use the
   explicit GitHub account runner. Never push a protected branch, never
   force-push outside the conflict remedy of step 9, never reuse an arbitrary dirty/wrong directory as a fresh
   checkout. Interrupted clones are refused without deleting unrelated content.
   The checkout marker and `coordinator.base_sha` remain the immutable checkout
   identity. In A38 mode, the current pull-request evidence base is stored
   separately as `coordinator.a38.base_sha`. A newly observed moved base stays
   in `coordinator.a38.pending_base_sha` until a report or inventory waiver and
   its local gate are recorded against that base.
4. **Implement / inner review** via `lane.launch` builders with explicit session
   and config home. A round counts once: an implementer start reuses the
   current round while its implementer has no verdict, so a lane retry and a
   restart after a blocker that left the verdict open (for example missing
   summaries) stay in that round. The limit is pinned when
   the task is created: submitted `--max-rounds`, then the implementer model's
   `max_rounds_by_model` entry, then worker `max_rounds`, then 10. Its origin is
   `submit`, `model`, `worker`, or `default`; no value disables it. Before the
   next round would exceed it, the script starts one assessment check lane over
   the task text and bounded findings from every round. Its fixed result is
   `continue`, `change-approach`, or `abort`, with a reason and an approach only
   for `change-approach`. The script stores the opinion and asks the human; it
   never acts on it. A reply grants the same number of rounds and becomes
   untrusted implementer guidance. It does not pass the proposed approach.
   Cancellation ends the order. An issue-only worker without `<first>:check`
   records the opinion as `unavailable`. Rejection routes findings to a fresh
   implementer. The transaction that first marks an implementer agent finished
   also stores its bounded output prefix and the validated English/German
   summaries plus reused/added lines extracted from the full output. Recovery
   consumes those structured fields without relaunching the implementer, even
   when they occurred after the persisted prefix. Outcome distinction
   (review-loop preserved, not waived):
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
     path; status re-publishes keep the existing checkpoint.
   Incomplete implementer output and incomplete inner or pull-request reviews
   reach that checkpoint only after automatic retries under `lane_retries`.
   The default is two retries; zero disables them. One budget applies per phase,
   with `poll_seconds` doubled for each backoff. A human reply starts a fresh
   retry budget. A retry consumes the saved incomplete result, so the next visit
   starts a lane. A saved complete sibling result is replayed. A completed
   implementer result that lacks summaries or its
   reused and added list is not retried. Pull-request review-pair preparation
   failure is not retried.
   A `no-change` result that adds no commit, on a task that already holds both
   summaries from an earlier round (and, when `REVIEW.md` exists at the base, a
   reused and added list), keeps them and closes its round like `done`; it
   does not block.
   A lane exception closes its agent row and retries under the same bound.
   After the retries it becomes a resumable `lane-failed` blocker with fixed
   text `Model lane interrupted in <phase>; not a code rejection.`. Exception
   text is never published. Before a real lane start, `no_live_lane` checks the
   saved process id and start time. A live process makes the task wait; one past
   `lane_timeout` is killed. A working row with no live process and no result is
   closed with `recovered: no live process` and retried under the same bound.
A record of a check lane is closed the same way, but closing it does not retry the phase: the check counts its own attempts.
   A finished reviewer result awaiting its transaction is replayed. A legacy
   `uncertain_lane` flag is removed on the next visit unless a lane is alive,
   and the phase retries under the same bound.
   Each real implementer or reviewer lane start records its process id and
   start time in a small file in the task control directory; a check lane
   records none, so its working row always reads as having no live process.
   No code sets `uncertain_lane` any more.
   Incomplete, invalid-result, and blocked implementer outcomes stage the task
   and task-round on copies, commit both together, and consume the checkpoint
   only after that commit succeeds.
5. **Draft** as soon as the first signed task commit exists (`pr.open` on the
   **target** repo), before full tests/reviews. Each new signed head is pushed
   to the existing PR before later stages. An empty start commit is allowed only
   for `draft_on_accept` and orders. Without them there is no pull request
   before the first patch, and a `no-change` result on the start commit closes
   the draft and ends the task as failed with kind `no-change`. In phase
   `start_draft`, between checkout and implement, the script
   creates one signed empty commit with message `Open the draft pull request.`,
   pushes it, and opens the draft through `pr.open`. Crash after commit/push before draft reconciles
   without starting another implementer. `task.ref` holds the **PR** number
   only (never the issue number).
6. **Tests** run only via script `check_argv` on the exact clean signed head
   (cwd = worktree). Failure routes bounded output to the implementer. Stale
   passes from another head are not reused. A check command that times out with
   124, is missing with 127, or ends by signal is infrastructure failure. It is
   retried under the same bound and then blocks with kind `check-unavailable`,
   a reply checkpoint, and fixed text `Check command could not run on <head7>
   (exit <rc>); not a code failure.`. It creates no `local_check` row and is not
   sent to the implementer as findings. This applies to the repository check,
   trusted precheck and A38 suite. Test, check and CI output is shortened from
   the start so its end is retained.
7. **PR gates**: model slot `first` quality+logic in **parallel** (fresh
   independent invocations; agent rows prepared on the main thread, subprocesses
   in threads, results persisted on the main thread), then model slot `second`
   quality+logic the same way only after both first-slot dimensions are approved
   on that head. The persisted phase names `pr_gates_grok` and
   `pr_gates_codex` select stages `review-1` and `review-2`, respectively. Author
   session does not sit those reviews. Incomplete/unavailable vendor output
   records an `unavailable` gate and attempts to close the matching checklist
   key as `unavailable` with bounded evidence for every affected dimension when
   `close_allowed` permits it, then publishes a GitHub-visible blocker with
   `resume_phase` of `pr_gates_grok` / `pr_gates_codex` and a pinned
   `question_activity_id` — not a rejected complete gate and not an implementer
   fix loop. Every complete dimension is persisted before incomplete dimensions
   are handled; a complete rejection always routes back to implementation even
   when its parallel peer is unavailable. The next implementer receives every
   rejecting dimension under `Quality review findings:` or `Logic review
   findings:`. An incomplete pair is retried only when no dimension rejected;
   only incomplete dimensions start again, while a finished sibling is
   replayed. The unavailable gates and checklist values are written for each
   retried attempt. Rejections publish
   `review.post` **COMMENT** (not `REQUEST_CHANGES`) and invalidate
   head-specific evidence. A successful second-slot lane still starts when the
   checklist keys are `n_a`. Every cached approved gate attempts to reconcile
   its matching non-`ja` checklist key to `ja`, including legacy partially
   recorded state. Reconciliation closes a key only when `close_allowed`
   permits that key; a denial keeps the coordinator in the gate phase and
   returns a recoverable blocker naming the still-open key. After `ja`, both
   second-slot gates are required again.
   On restart, the coordinator inspects durable current-head gate verdicts
   before preparing either dimension. Cached approvals reconcile their
   checklist keys, while an unhandled cached rejection routes its recorded
   evidence to implementation and is never superseded by a new lane. The
   rejection row is marked handled atomically with that route. After the
   resulting implement/inner-review cycle, a fresh gate attempt may run on an
   unchanged head; the old handled rejection remains history rather than
   routing the task again. Newly completed gate
   writes and their checklist reconciliation commit atomically; a crash rolls
   both back. Durable ledger insert order resolves gate rows that share a
   timestamp through one shared ledger-insert ordering rule used by coordinator,
   CLI, allow, and chain readers. Updates do not replace an insert sequence, and
   snapshot-only rows without a ledger insert remain unordered. The legacy fallback
   parses aware ISO-8601 timestamps, normalizes them to UTC, and treats equivalent
   representations as the same instant. Conflicting rows at one instant, and
   histories with a missing or invalid timestamp whose order cannot be
   established, yield an unavailable verdict before a checklist close or review
   publication. The coordinator checks this condition before reconciling saved
   rejection intents and returns without publishing an issue blocker or any
   review comment. The gate phase stops before preparing either reviewer when a
   needed dimension has that synthetic empty-head verdict. During `review-2`,
   this includes both `review-1` prerequisite dimensions as well as the active
   `review-2` dimensions. The phase rechecks the shared ordering after each new verdict before closing a checklist key or
   queuing a rejection comment. Legacy stage aliases are canonicalized before grouping or
   comparison. All completed and unavailable parallel gates, their checklist
   reconciliation, every rejection intent, the combined findings, and the task
   transition share one local transaction. For an approved A38 first review,
   the pass count, a recovery marker, and each completed reviewer outcome are
   checkpointed before that transaction. The marker and applied outcomes are
   cleared with the atomic gate/checklist commit, so recovery replays a saved
   outcome after a rollback without relaunching its reviewer or counting the
   same pass twice.
   GitHub publication runs only after that commit, using the saved intent batch,
   and a restart reuses every pending intent. A transient publication error
   reopens that same
   deterministic intent and retries it at the start of every later coordinator
   tick, before phase dispatch and before an early merged/closed transition,
   until the owned intent is verified done. Implementation and later phases may
   continue; the retry does not wait for a human reply or relaunch a reviewer.
   Each saved id is a unique non-empty string. Before any retry or GitHub
   execution, the complete batch is validated as device-owned `review.post`
   rows for this worker, task, rejected gate, repository, pull-request number,
   and `COMMENT` event, with a pending, error, or done status. New rejection
   payloads carry the task and gate ids explicitly.
   An invalid or foreign saved rejection checkpoint becomes a durable
   `phase-error` blocker with the exact resume phase and a reply checkpoint.
   The invalid active checkpoint is retained only as inert audit data, so an
   authorized reply can resume dispatch instead of retrying the same structural
   error. Transient GitHub execution failures retain the active checkpoint for
   retry. This recovery also applies in `await_merge`, where merge verification
   resumes after the reply. A pending post-suite checkpoint still publishes or
   retires after the pull request is merged or closed before merge verification
   continues; finalization to `await_merge` clears stale reply recovery fields.
   `n_a` alone does not grant Ready or done. One review is enough only when
   the pull request opener is listed in one_review_authors in the trusted
   .github/pr-guard.json on the default branch. A missing file, a missing
   key, or an empty list keeps two reviews required. An entry that exists
   only on the pull request head does not count.
8. **CI**: exact-head PR check rollup **and** paginated head workflow inventory
   (path+event, newest run id, then attempt within that run), classified by one
   shared observer used by both ordinary `phase_ci` and Ready-side fresh
   rechecks. `success` counts without an exception.
   An absent `statusCheckRollup` is normalized to `[]` and inventory is still
   inspected — `action_required` (and actual failures) may be present only in
   inventory; absent rollup prevents green but must not hide those facts.
   For an ordinary worker, `action_required` is an external authorization
   blocker (not routed to the implementer; `resume_phase` stays `ci`;
   `question_activity_id` is pinned so an authorized reply resumes CI
   observation). For an A38 worker it is a retryable guard-owned authorization
   state: the coordinator stays in `ci`, creates no reply gate, and routes
   nothing to the implementer. For A38 workers, a verified author A38 report
   on this head may treat a matching required check that concluded `skipped`
   or `neutral` as green. Report-covered rollup checks extend to workflow
   inventory only through a unique Actions run identity, never a workflow path
   or a non-unique workflow display name. An unmapped rollup leaves the
   inventory run uncovered. The report does not waive a missing check. Failed,
   `cancelled`, and pending checks still block. Hard
   protocol faults
   (malformed rollup, unexpected inventory shape, missing `workflow_runs`, an
   invalid or inconsistent nonnegative integer `total_count`, a short page,
   excess or duplicate run identities, pagination truncation, or any workflow-run
   entry that is not an object or lacks a valid exact head, workflow path, event, positive id, status,
   conclusion, or positive attempt — typed `CiObservationProtocolError` /
   `CiInventoryProtocolError`) enter recoverable blocked + `resume_phase=ci` +
   checkpoint; an authorized reply resumes CI once valid evidence is restored.
   For a non-completed workflow run, both `null` and `""` mean that no
   conclusion exists yet and are classified as pending; completed runs still
   require a known non-empty conclusion.
   Transient observation transport (PR view **or** inventory `gh_json` /
   typed `CiObservationTransportError`) and pending / absent-yet evidence stay
   on static `phase=ci` and retry without an idle model, blind implement, or
   reply gate. Missing workflow inventory, pending, failure, cancelled, and
   uncovered skipped/neutral are not green. This core observes **cumulative GitHub CI** only; target-
   repository policy / A38 live join belongs to configured `readiness_argv`.
   Failures fetch plain-text logs via
   `gh run view <id> --repo <target> --log-failed --attempt <n>` (never ZIP
   `/logs` archive bytes). Inaccessible logs, including successful fetches with
   empty or whitespace-only output, are a reply-recoverable blocker.
   Transient pending returns without an idle model.
   While CI is pending the task waits and does not block the worker. A failed
   run's bounded log keeps its end.
9. **Ready**: first read the live pull-request base. In A38 mode the script
   reconciles a pending base and invalidates evidence for a moved base before
   it runs `readiness_argv`; the command runs only when the observed base still
   matches the promoted evidence base. Then run `readiness_argv` (cwd =
   worktree, ambient GitHub tokens cleared). Stdout must be the fixed JSON
   readiness contract below (trusted
   operator script output — not model/repo input). Re-verify clean signed head
   **after** the command, re-observe CI fresh (no stale `ci_green`), unchanged PR
   head, author/base/mergeability, tests, and all four same-head gates.
   Mergeability is the check `pr_mergeable`, required before `readiness` and
   `leave_draft` for orders and issues. GitHub `UNKNOWN` is a same-phase retry;
   `CONFLICTING` is no. For an issue task, no creates a resumable blocker of kind
   `conflict`; the script starts nothing. The comment mandate of DESIGN §19.9 is
   a separate worker and is not connected to the task.

   For an order, `pr_mergeable` declares remedy `conflict_resolution`. The
   coordinator starts the script-owned resolution without a human comment:
   rebase in an isolated worktree, a confined lane for conflicted files,
   deterministic verification, the configured check, and a
   `--force-with-lease` push. The action `resolve_conflicts` requires
   `order_authorized`, `draft_open`, `head_published`, and `conflict_mandate`.
   The mandate fact binds the order, submitter, pull request and head. Worker
   `conflict_remedy` supplies `check_argv`, `max_rebase_steps`, and optional
   `max_attempts`; `conflicts.json` is not read. The session needs the
   `conflicts` skill. Control state is below
   `<workspace_root>/.coordinator-control/<task id>/conflict-remedy/`.

   After a remedy push the coordinator adopts the published head in its own
   checkout, removes old-head evidence and resumes at `tests`. Tests, report,
   both review pairs and CI return to pending and run on the new head. Recorded
   overrides and skips remain. An open remedy outcome retries under
   `lane_retries` with its backoff. Only a final remedy blocker blocks the
   order. It posts a pull-request status comment of kind `conflict` and a
   `question` event for `pr_mergeable`, offering `reply` and `cancel`.

   A reply measures mergeability again and starts resolution again while the
   conflict remains. At most `max_attempts` resolutions start between two human
   decisions. Starting or succeeding writes no order event. Without
   `conflict_remedy`, no resolution starts and the same question is published.

   For an order the second review is the check `review_2`. It means that review
   2 is free of findings on the current head or that a human used the declared
   one-review waiver. It is required by CI and readiness. For an issue task, the two
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
    require merge SHA, timestamp, and base/target. An A38 merge must also expose
    a valid base SHA equal to the promoted `coordinator.a38.base_sha` evidence
    base (the checkout pin is not a completion fallback), with a report or
    waiver and local gate bound to that exact base. If a pending base exists or
    the base moved after evidence and the pull request is already merged, the
    coordinator
    keeps the old base as the last trusted base, records both old and observed
    bases, reopens `local_check_pass` and the readiness checklist keys, and
    stops with a non-recoverable `task-done` blocker. A missing promoted base or
    a merged head different from the reviewed head is the same final boundary;
    stale reply recovery is cleared. It cannot remeasure a
    merged pull request; a human decides the outcome. Then existing `task-done`
    checklist / summary guard (summaries must already describe the actual
    result — no boilerplate invented at merge), then `issue.assigned.ack`. An
    order has no assignment row, so it writes no `issue.assigned.ack`; the
    script writes the `merged` order event. A
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
retries the tick instead of blocking); once its pull request exists, every tick
of an A38 task, also while blocked or asking, first checks for a merge the same way, which verifies the human merge
    or blocks a pull request closed without one. Unless readiness already ran on
    that exact merged head (`readiness_head`), even when its keys were closed on an
    earlier head, it first runs the owner hub-statement default or the configured
    trusted readiness command and publishes its blocker with a reply checkpoint
    when those keys cannot be closed. The exception is a persisted
    `provider-incomplete` checkpoint: even if the pull request becomes merged or
    closed after a crash, it stays in `blocked` until its queued blocker intent
    is verified published and an authorized reply consumes that checkpoint.

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
rejected fail closed). In `agent run` an implementer lane counts only when its
output has exactly one `STATUS`, exactly one role-valid `RESULT`, and that
single status is `complete`:
`done` and `no-change` close the round, `blocked` fails the task, and a
missing or duplicate record, or an `ask`, `approved` or `rejected` result,
aborts the row and leaves the same round retryable. Reviewer `RESULT` must be
`approved|rejected`; `ask` /
`blocked` are not code rejections. Completed lane outcomes are persisted before
signing/publishing so crash recovery applies the recorded result instead of
starting another model. The inner reviewer and each dimension of a parallel PR
review atomically persist a bounded, redacted, structured outcome, its
deterministically identified `lane.run` audit, and the finished agent, bound to
the task, round, head, stage, and dimension. A finished reviewer result remains
in a local checkpoint outside the worktree and every reviewer read directory
until that transaction commits. The checkpoint omits the account configuration
directory; recovery reloads it from the agent's configured role. Task id, agent
id, head, dimension, and stage are exact unredacted identity values used only
for recovery matching. The owned provider-output identity is stored as its file
name, never as an absolute path, and recovery derives the path from the task and
agent ids. Result strings, models, reported models, usage values, tool fields,
and collections are bounded and redacted through the same serializer as
`lane.run`, and the complete JSON file has a hard size limit. After tool entries
are removed to meet that limit,
stdout and stderr are shortened against their JSON-escaped size while the
already parsed protocol result remains intact. Each parallel worker writes its
checkpoint atomically as soon as its lane returns, before publishing the result to shared memory or
waiting for its sibling. If serialization raises, including a coordinator
validation error, that worker removes its owned raw output plus any temporary or
final spool before recording the failed outcome; the main thread marks the
agent finished only after that cleanup. Structurally valid incomplete, unavailable, and
malformed-protocol results are recovered and then classified by the normal
verdict path; they are
not converted into failed lane runs. An audit-only rollback retries only this
checkpoint on the next tick, visits every saved sibling before a working row
without a checkpoint is closed as a failed lane run, and never discards an already
committed parallel sibling. A saved sibling or failed checkpoint defers that
classification until a later tick. A reviewer execution failure closes its
agent row and is then retried under `lane_retries`. The failure is raised only
after every successful sibling checkpoint is committed, so the retry replays
the sibling's result instead of starting that reviewer again. When the retries
are used up, the task gets the resumable `lane-failed` blocker. Before
starting a reviewer, the coordinator replays a matching saved outcome. It also
sweeps and verifies absence of the deterministic raw output and spool paths for
already finished reviewers, covering a crash after the outcome transaction
committed but before ordinary cleanup; the sweep is idempotent. The coordinator
removes the saved outcome only in the transaction that records the round verdict or
the gates, rejection intents, and phase transition. Parsed implementer
summaries and reused/added surface lines are redacted structurally: summary
sentence terminators and surface-reference backticks plus line numbers are
retained. Before persistence, the control directory, worktree, workspace root,
Agent home, and user home are replaced with fixed placeholders. Every
backticked surface path must be repository-relative and traversal-free; an
absolute, drive, home, backslash, `.` or `..` form is malformed in both normal
flow and replay. The sanitized
checkpoint is parsed by the same summary and surface validators before it is
committed. If that cannot produce a safe valid checkpoint, the lane is not
checkpointed as finished and the implementation step remains retryable. Gate
transactions, lane checkpoints, and outcome replay transactions stage
task, agent, and task-round mutations on copies and adopt them only after
commit. A successful implementer replay writes its round verdict, the
`implementer_done` checklist item, and the task without its consumed lane
checkpoint in one transaction, so a database rollback cannot leave live state
pointing at rolled-back agents, verdicts, gates, or publication intents.
Replay blockers consume their checkpoint only in that committed task copy.
Likewise, a completed parallel gate stage commits its next phase, or its
retryable checklist-denial state, with the gates and checklist reconciliation
before the live task adopts the transition.
Reviewer replay routes by the separately checkpointed validated result; bounded
stdout and stderr remain diagnostic evidence and are not reparsed as the
protocol record. `STATUS` and `RESULT` are counted and validated exactly once
on the raw final lane text before multiline or terminal redaction; only the
redacted body and that parsed verdict are persisted. Authorized
replies (`reply_logins` only), listed with
`gh api --paginate --slurp`, resume the exact `resume_phase` after the pinned
`question_activity_id` checkpoint only when `reply_checkpoint_eligible` holds
(not blindly `implement` for CI authorization / incomplete review / checkout
blockers, and not at all for terminal failed / done / uncertain /
closed-unmerged / non-human-merge outcomes). An `order.reply` row is consumed
like an authorized comment reply. Before any model start, `no_live_lane` makes
the task wait for a live process, kills one past `lane_timeout`, and recovers a
working row with no process or result under the same bound. Inner and PR reviewers receive a
script-generated merge-base diff artifact (`base...head`) outside the worktree,
so commits added only to the base branch are not presented as pull-request
deletions.

## Orders

A reply, override or cancel acknowledges the question or blocker it answers.

`agent order submit --repo OWNER/NAME --title TEXT --body-file PATH` submits a
local order. Optional arguments are `--base BRANCH`, `--worker SESSION`,
`--notify-session ID`, repeated `--skip-check KEY --reason TEXT`,
`--max-rounds N`, and `--json`. Every option takes its value as the next
argument. The printed order id is the task UUID.

The task uses `payload.coordinator.source.kind=order`. An issue task has no kind
and is read as `github-issue`. The same task state machine and tables serve
both. A worker accepts orders only with `"orders": true`, and only for a
repository it already serves. An explicit repository accepts only its
configured base. An owner-scoped repository pins the submitted base.

Without `--worker`, the order-enabled worker serving the repository with the
fewest runnable tasks receives the order. A new order stays `queued` while that
worker has runnable work. The oldest queued order moves to `accept` when a tick
finds none. Acceptance reads no issue and posts no GitHub comment.

The order text is at most 60,000 characters and is stored once at
`<workspace_root>/.coordinator-control/<task id>/task-text.md`. The full text is
in the implementer prompt for orders and issue tasks. Reviewers use the
review-context file. The task row retains a 2,000-character preview. The text
is untrusted specification data.

The mandate is local submission by the device's paired GitHub login. Acting
operations refuse an unpaired device with `device is not paired`. An order does
not verify a hub assignment, even under `require_hub_assignment`. On the
owner-scoped A38 default without `owner_readiness_argv`, readiness closes the
two deviation keys as `n_a` from the submission, with evidence
`order <id> submitted by <who>: orders declare no deviation`. Submission
cannot declare a deviation.

The other commands are:

```text
agent order list [--open] [--json]
agent order show --id UUID [--json]
agent order events [--after CURSOR] [--notify-session ID] [--wake-only] [--unacked] [--json]
agent order wait [--after CURSOR] [--notify-session ID] [--timeout SECONDS]
agent order ack --event ID
agent order reply --id UUID --body-file PATH
agent order override --id UUID --check KEY --reason TEXT
agent order cancel --id UUID --reason TEXT
agent order report --id UUID [--json]
```

`show` prints state, phase, round, pull request, head, last event, pending
checkpoint, last answer and its fate, the current action's checks, overridable
checks, round limit and origin, and submit-time skips. It never prints raw lane
output. `wait` polls the local store at `poll_seconds`; it exits 0 with events
and 3 on timeout.

Order states are derived: `queued`, `running`, `waiting`, `ready`, `merged`,
`failed`, and `cancelled`. Order `merged` derives from task `done`. It is not
the spine task state `merged`, which only `agent task close-from-pr` writes;
that command refuses coordinator tasks.

The coordinator writes one `order.event` for `accepted`, `draft-opened`,
`question`, `blocked`, `resumed`, `ready`, `merged`, `failed`, and `cancelled`.
The event includes order id, kind, wake, per-device `seq`, a redacted digest of
at most 1,500 characters, pull-request URL, phase, round, failed check,
`answers`, `opinion`, notification session, time and acknowledgement.

Wake is true for `question`, `blocked`, `ready`, `failed`, and `cancelled`.
`merged` never wakes. A merge observed before Ready writes `ready` together
with `merged`. Events persist until acknowledged. Notification session is only
a filter. Any later concierge or the dashboard can list unacknowledged wake
events and acknowledge one.

Reply, override and cancel insert `order.reply`; the next tick consumes it. A
reply resumes the exact `resume_phase`, like an authorized GitHub reply. A
repeated question needs a new reply. Cancel closes the pull request with the fixed
comment and ends the order as failed with kind `cancelled`. These writes and
`order.event` are script-only; `agent activity add` refuses them with `<type>
is written by a script, not agent activity add`. Models never write them.

Wherever this document says a blocker or question is published on the issue,
an order publishes it on its pull request. Before the draft exists it is only
an order event. Once the draft exists, authorized comments from
`reply_logins` also resume the order.

An event's `answers` are `ack` for `ready`, `failed`, and `cancelled`. A
`question` or `blocked` event offers `reply` unless the failed check allows only
override, `override` when the check is overridable, and always `cancel`. A
configuration-only blocked event offers only `cancel`. A refused pending answer
creates a new unacknowledged question whose digest starts `Previous answer
refused: <text>`. A refused override that answers no question creates no event.
`show` still names its fate in `last-answer` and always prints the pending
checkpoint and overridable check statuses.

`--skip-check` must be followed by its key, literal `--reason`, and a one-line
reason of 1 to 500 characters. Only `duplicates_checked` is skippable. The
paired login is the actor. Acceptance records one fact per skip with status
`ja`, source `human`, and evidence `skipped at submit by <who>: <reason>`. Its
resolver never starts. `show` and the acceptance report list every skip.

`agent order report` reads only the store. It makes no GitHub call, runs no
lane and starts no Git process. In every state it prints a bounded dossier:
order id, title, state, phase, repository, base, pull-request URL and head;
English and German summaries; at most 50 changed files and counts; rounds and
limit origin; every skip or override with actor and reason; the last 40
review runs, each with stage, round, head, provider, model, lanes, verdict and
defect count; the last 12 review comment URLs with at most 600 visible
characters each;
A38 report outcome and local checks; and CI state.

A review run is one inner lane or one review-pair start. Its verdict is
`rejected` when a lane rejected, `incomplete` when a verdict is absent, and
otherwise `approved`. Its defects count rejecting lanes; external second review
uses the published defects value. A38 report outcome is `pass`, `fail`,
`unreadable`, `missing`, or `waived`; a waiver is printed with its reason. CI is `green`, `failed`, `pending`, or
`unknown`. Text is at most 12,000 characters, with single-line facts first and
list details last. A cut ends with `report truncated`. A concierge may use the
report for a plausibility recommendation; it does not perform a full review.

## Precondition registry

The script is the orchestrator. Every side-effecting coordinator action has an
ordered list of checks in `preconditions.py`. Adding a check to an action is one
registry entry and one resolver. Each check has exactly one resolver: a static
script function or one check lane. Script first.

Each fact has status `ja`, `nein`, `n_a`, `pending`, or `unavailable`, always
with evidence, source and time. `n_a` is valid only where declared.
`unavailable` means the resolver could not run. There is no implicit yes. An
action runs only when every check is `ja` or allowed `n_a`. Pending starts its
resolver or routes to the producing phase. No follows its declaration: remedy,
retry, question, reply checkpoint, or task end.

Existing checklist keys, round verdicts, gate rows and checkpoint values are
referenced. The registry-only `checklist_item` keys are `order_authorized`,
`spec_available`, `checkout_owned`, `duplicates_checked`, `no_live_lane`,
`round_budget`, `head_signed_clean`, `pr_mergeable`, `conflict_mandate`,
`review_2`, and `one_review_permitted`. Only the coordinator writes them via
`chain.close_allowed`. They are not workflow keys and do not affect `done`.
`agent checklist set` and `agent close-step` refuse them with `<key> is a
coordinator precondition check; its resolver or agent order override closes
it`.

Except for `close_draft`, every row below also starts with `order_active`:

| Action | Phases | Checks |
|---|---|---|
| `accept` | `accept` | `session_registered`, `order_authorized`, `spec_available`, `spec_written` |
| `checkout` | `checkout` | `spec_written` |
| `open_draft` | `start_draft` | `checkout_owned`, `duplicates_checked` |
| `implement` | `implement` | `spec_available`, `draft_open`, `checkout_owned`, `no_live_lane`, `round_budget` |
| `commit_push` | inside `implement`; `publish_draft` | `implementer_outcome` in implement; `checkout_owned` |
| `inner_review` | `inner_review` | `draft_open`, `implementer_done`, `checkout_owned`, `no_live_lane` |
| `tests` | `tests` | `reviewer_approved`, `head_signed_clean`, `checkout_owned` |
| `first_review` | `pr_gates_grok` | `tests_green`, `head_published`, `report_posted` for A38, `head_signed_clean`, `checkout_owned`, `no_live_lane` |
| `first_review_comment` | `a38_first_comment` | `review_1_quality`, `review_1_logic`, `report_posted` |
| `second_review` | `pr_gates_codex`, `await_second_review` | `review_1_quality`, `review_1_logic`, `first_comment_posted` for A38; in `pr_gates_codex`, `head_signed_clean`, `checkout_owned`, `no_live_lane`; waived by `review_2` |
| `second_review_comment` | `a38_second_comment` | `first_comment_posted`, `review_2_recorded`; waived by `review_2` |
| `ci` | `ci` | `draft_open`, `review_2` |
| `readiness` | `readiness` | `tests_green`, `ci_green`, `review_1_quality`, `review_1_logic`, `review_2`, `head_signed_clean`, `pr_mergeable` |
| `ready` | `formal_approve`, `leave_draft`, `await_ready` | `contributing_ok`, `deviation_declared`, `deviation_granted`, `readiness_on_head`, `ci_green`, `ready_allowed`; in `leave_draft`, `formal_approved`, `pr_mergeable` |
| `merge_observation` | `await_merge` | `draft_open` |
| `waive_second_review` | — | `draft_open`, `one_review_permitted` |
| `close_draft` | — | `draft_open` |
| `resolve_conflicts` | — | `order_authorized`, `draft_open`, `head_published`, `conflict_mandate` |

`duplicates_checked` and `review_2` are overridable; only
`duplicates_checked` is skippable. An override is available only through
`agent order override --id UUID --check KEY --reason TEXT`. Each human decision
records actor and reason on the fact, is final, is not head-bound, and prevents
another resolver run.

For order tasks the table covers the full flow. For issue tasks the registry
checks `no_live_lane` before `implement`, `inner_review`, `pr_gates_grok`, and
`pr_gates_codex`; `round_budget` before `implement`; and `pr_mergeable` before
`readiness` and `leave_draft`. Other issue preconditions remain in phase
handlers.

Every order pull request gets review 1 and review 2. The roles bound to
`<first>:implementer`, `<first>:reviewer`, `<first>:pr-reviewer-quality`, and
`<first>:pr-reviewer-logic` must name one model. Review 2 uses model slot
`second`. `review_2` means that the current-head second review has no findings,
or a human waived it for this order. CI and readiness require it.

Only `agent order override --id UUID --check review_2 --reason TEXT` waives it,
also while pending. The override is refused with `check review_2 cannot be
waived: <reason>` unless the task has a nonexternal second review, review 1 is
complete, no second review is approved or has rejected an A38 pull request, and
the pull-request opener is in `one_review_authors` in the trusted default-branch
configuration. It cannot be skipped at submit. Manual `n_a` review keys do not
count. The script never drops review 2 itself.

Step 9 describes the `pr_mergeable` check and its declared conflict remedy for
orders. Issue conflicts remain a resumable blocker.

## Check lane

A check lane makes one judgment. Its whole input is a script-compiled prompt
with untrusted data between marker lines carrying a nonce. It has no tools or
file access, runs in an empty directory, and answers in a fixed grammar that the
script validates. The script never interprets free text. This is process argv
hardening, not a sandbox.

The lane uses model slot `first` through `<first>:check`, bound to a read-only
role. `codex` cannot run it. It has no `STATUS` line; each check defines its
grammar.

`duplicates_checked` is required before opening an order draft. The script
lists open pull requests: number, title up to 200 characters, and description
up to 500 characters. With none it records yes without a lane. With at most 200
it passes that list, title and order text to the lane. The only results are
`RESULT: duplicate <number>` or `RESULT: no-duplicate`, followed by one
`REASON:` sentence. A duplicate number must be in the supplied list.

A duplicate records no, creates no start commit or pull request, and raises a
question naming the suspected pull request and check. Only an override or
cancel resolves it, unless skipped at submit. Invalid answers retry with
backoff under `lane_retries`. More than 200 open pull requests, or exhausted
lane retries, records `unavailable`; a plain reply runs it again.

The round assessment is another check lane. Before the next round would exceed
the pinned limit, its input is only the task text and bounded findings from each
round. It returns `RESULT: continue`, `RESULT: change-approach`, or `RESULT:
abort`, then `REASON:` and, only for change, `APPROACH:`. The script validates
and stores the opinion on `round_budget`, publishes it with the question, and
never acts on it. An authorized reply grants the same number of rounds and its
text becomes untrusted implementer guidance. The proposed approach is not
passed to the implementer. An issue-only worker without the check binding
stores `unavailable` and asks without an opinion.

## Front ends

The model between the human and the script is the concierge. It submits the
request, reads status and the report, relays questions, returns the human's
answer, and recommends whether closer inspection is useful. It is not an
orchestrator; the script is. The command line, dashboard orders view and
concierge call the operation table in `order_ops.py`. Every readable field and
operation available to a concierge is available in the dashboard.
The device daemon runs `agent coordinate --session ID --follow` for each
configured worker. The coordinator does not depend on a living front end.

The dashboard binds `127.0.0.1`. Read operations use
`GET /api/orders/<name>`, writes use `POST /api/orders/<name>` with a JSON object,
and `GET /api/orders/schema` returns `enabled`, `skippable`, and `operations`.
Every route under `/api/orders/` accepts only `Host` equal to
`127.0.0.1:<port>` or `localhost:<port>`, with any port. A present, nonempty
`Origin` must equal `http://` plus that Host. Otherwise it returns 403 and
`cross-origin request refused`. `wait` has no dashboard route; the dashboard
polls `events`.

## Model output protocol

Every lane prompt includes strict prohibitions and requires:

```text
STATUS: complete|partial|timeout|unavailable
RESULT: done|blocked|ask|approved|rejected|no-change
```

A check lane is the exception: it answers in the fixed grammar of its check and
has no `STATUS` line.

The parser first counts every `STATUS:` and `RESULT:` line, including lines
whose values are malformed, and requires exactly one of each. It then validates
both values and the result allowed for that lane role; an additional malformed
record cannot be hidden by a later valid one.

A completed implementation additionally returns exactly one English and one
German change-summary sentence, each ending with a period, directly after
`RESULT` and before its body:

```text
SUMMARY_EN: Describe the actual change here.
SUMMARY_DE: Die tatsächliche Änderung hier beschreiben.
```

The script records these semantic summaries; it does not invent them at merge.
Missing summaries block progression, except after a `no-change` result that
adds no commit while the task holds summaries from an earlier round. The
remaining body is bounded. Empty,
partial, timeout, or unavailable output is
never zero findings and never approval. Nonzero process exits cannot approve,
even when stdout claims completion. Model text cannot certify checks, CI,
commits, or Ready. Recorded real assignment or an authorized human reply may
evidence human spec input; the script never invents human grants.

## Evidence hygiene

Persisted outputs and diagnostics are redacted and bounded. The shared pipeline
first searches once for ESC or C1 bytes. If none exist, the existing precise
scanner runs unchanged. For a raw logical line that does contain ESC or C1, a
compiled case-insensitive candidate check runs over a control-byte-translated
view. Only candidate lines build the three views, one at a time: a
control-stripped view that removes complete or
unterminated controls with their payloads, a payload-kept view that removes
only control introducers, terminators, and CSI parameter bytes, and a key-join
view. The key-join view uses the assignment scanner's own classifier to retain
only control-sequence bytes that the scanner may join into a key, including a
key-character CSI/ESC final and a `-` intermediate, while dropping string-control
payloads and all other control bytes. If any view contains a shared secret
keyword, case-insensitively, the raw logical line is redacted from the start of the
whitespace token containing the first mapped keyword
through the end of that logical line. All three views remove standalone
removable C0 bytes such as backspace, NUL, DEL, and carriage return; those bytes
do not activate the rule without ESC/C1. Removable control bytes inside retained
string-control payloads are omitted from every structural view, so they cannot
split a keyword in the payload-kept view. Complete and unterminated
string-control spans are indexed once on the full raw input before line
splitting, so newlines inside OSC, DCS, PM, APC, or SOS payloads are not
boundaries. If a mapped keyword starts inside such a span, redaction starts at
its introducer. A following normal line outside the span remains independent. An unterminated string control
extends through the end of the text and swallows every following physical line
into its payload. A controlled line without a secret keyword continues through
the existing control handling. A whitespace token exactly `<config_dir>/`
followed only by `[A-Za-z0-9._-]+` is the one structural exception: keywords
inside that placeholder do not trigger the line rule. Any other byte in that token, or a
keyword elsewhere on the line, still triggers it. Lane sinks replace raw and
control-interleaved config-directory paths on the complete source before this
structural rule, apply their length bound next, and run precise redaction last.
The pipeline then classifies secret assignments on the full
raw text. During that scan,
every complete or unterminated OSC, DCS, PM, APC, or SOS span is one opaque
skipped span between the key, operator, and value and within quoted or unquoted
values. Only after assignment classification does the pipeline replace those
spans with `[redacted]` on the classified text, before whitespace tokenization. A
complete span includes its seven- or eight-bit introducer and terminator; an
unterminated span consumes the remainder of the text and is replaced rather
than merely removed. While a key has not yet matched a complete secret keyword,
an assignment-looking `=` or `:` inside such a span remains opaque, so a key
split as `pass` + span + `word` still joins to `password`. Once the key already
contains a complete secret keyword, a terminal control ends an unquoted key
only when an operator follows the skipped whitespace and controls before any
further key character. A visible `=` or `:` wins, even when an earlier
string-control payload contains one of those bytes; only when no visible
operator follows may a skipped, validly terminated string control supply its
payload operator. Otherwise the control is an opaque key interleave, so
`password` + control + `_file`, `password` + control + `suffix`, and
`aws_secret` + control + `_access_key` remain sticky secret keys. As a
fail-closed defence, a raw whitespace token that contained a string-control
span and contains a secret keyword is redacted whole before span replacement.
The pipeline then replaces any
whitespace-delimited raw token containing another ESC or C1 sequence, including
a lone ESC byte and a sequence at either token boundary, while preserving the
separating whitespace.
The only possible
exemption is a token whose sequences are exclusively one leading and/or one
trailing `ESC [` SGR wrapper containing only digits and semicolons before `m`,
around otherwise escape-free text. Before granting that exemption, the secret
matcher checks the SGR parameter bytes joined to the visible text; a match
redacts the entire token. The pipeline then applies control-tolerant secret
matching to raw text, removes terminal escapes and controls, redacts again, and
finally applies its length bound. One lexical assignment scanner walks complete,
unbounded alphanumeric, underscore, and hyphen key tokens (including quoted JSON
keys) and redacts values when the key contains a secret keyword. A plain compact
unquoted `key=value` assignment retains the established whole-token replacement;
structured or quoted assignments keep their key and delimiters while their value
is replaced. The secret keywords include `passwd` and the `access_key`,
`access-key`, and `accesskey` forms; removable terminal controls are consumed
alongside whitespace between the key, assignment operator, and value. Before a
complete secret keyword is present, a string-control payload stays opaque even
when it contains `=` or `:`. After that keyword is present, whitespace and
terminal controls are skipped together. A visible operator after them wins;
only without one does either byte inside a skipped complete string control
supply the operator, with the value starting after all skipped separators.
When another key character comes first, the control remains inside the key
instead of ending it.
Payload whitespace does not end an unquoted value.
Quoted values run to their closing quote or the bounded input end; unquoted
values stop at whitespace, comma, or semicolon. Control-tolerant URL schemes, escape parameters, and control
interleaves use fixed matching windows; value runs that do not need delimiter
backtracking are possessive. This keeps every redaction pass linear on long
separatorless provider text. The initial ESC/C1 search and any controlled-line
structural pass are linear over the raw input. After that structural rule, a
sink with an existing length limit takes only that limit plus a small inspection
margin before the remaining precise redaction expressions run. If that cut
splits a token, the cut token is redacted as a whole, and the usual truncation
marker and hard output limit still apply.
Tool targets pass their unsliced source into that bounded pipeline so
credentials crossing the visible cutoff remain inside the inspection margin.
Outputs that keep their end, such as test and check output and failed-workflow
logs, are redacted whole without a length limit and only then cut to their
end, which follows the usual truncation marker; the cut therefore never
exposes part of a credential.
Configured paths
are matched in both raw and resolved form despite interleaved terminal controls,
then hidden again after sanitization and before the diagnostic length bound is
applied. An unreadable
lane output reports only a fixed error category rather than the raw `OSError`.
Live terminal frames resolve the attached session's configured provider profile,
join tmux soft-wrapped lines, and use `redact_lane_text` with that configuration
directory and no diagnostic length bound. This also hides profiles outside the
usual user-home roots before publication, and frame deduplication compares the
complete sanitized capture. The sanitizer preserves
tabs and newlines, and terminal publication otherwise preserves captured
whitespace. Tmux-backed lane
diagnostics likewise join soft-wrapped lines before redaction and before their
bounded text may be printed or stored as `lane.run` evidence. Hub assignment
failures first remove the raw or header device bearer with an escape- and
control-tolerant matcher that accepts every bearer character literally or as a
case-insensitive percent-encoded byte, including mixed representations. They
then use that same generic pipeline; exact bearer removal is repeated after
sanitization.
Profile credentials, config directory paths, signing-key paths, and raw auth
errors must not appear
in replicated rows or GitHub text. GitHub activity failures replace the exact
configured profile and signing-key paths in raw text before terminal
sanitization, the second redaction pass, and the replicated-row length bound.
User-facing blockers and questions are
published on the source issue, or on the pull request of an order, only when
`execution_status=done` is verified;
otherwise they remain locally visible (`CoordinatorError` / `StoreError`
subclass `SystemExit` and must not be mistaken for success). Preflight
account/config failures can only report locally when GitHub is unavailable.
No silent failure.

Every lane process writes script-owned `lane.run` evidence with the configured
and provider-reported model (both bounded and redacted), token usage when
supplied, a bounded redacted file-tool transcript, and bounded final text.
All agent rows and reviewer replay checkpoints store that same bounded,
config-path-hidden redacted model value; only provider argv retains the raw
configured model.
One shared redaction pipeline handles final text, models, diagnostics, agent
notes, terminal output, hub/GitHub errors, and evidence. Its raw matcher treats
a CSI/ESC final byte as a possible token character, so the terminal sanitizer
cannot assemble an unredacted token by deleting that byte.
Seven-bit DCS, PM, APC, and SOS strings accept either the 8-bit ST byte or
`ESC \`; their eight-bit forms accept those same ST spellings. BEL terminates
only OSC, so BEL inside DCS, PM, APC, or SOS remains payload until ST.
The assignment scan treats complete and unterminated string-control
spans as opaque; only after that classification are they replaced with
`[redacted]` before tokenization, subject to the complete-secret-key operator
rule above for a validly terminated span.
Failure to read or decode the lane output file (`OSError`,
`UnicodeDecodeError`) is not an interruption: the lane ends as an `unavailable`
result with a bounded, redacted diagnostic, its `lane.run` row is still written,
and the normal incomplete-output path applies: automatic retries under
`lane_retries`, then the provider-incomplete reply checkpoint (`resume_phase`,
never `uncertain_lane`) in both the sequential and the parallel PR-gate path.
Parallel PR-review preparation exceptions remain reply-gated and publish only
`PR review lane preparation failed`. A parallel execution exception is a failed
lane run. It is retried under `lane_retries` and then becomes the resumable
`lane-failed` blocker. The agent note alone says `PR review lane execution failed`,
while the task blocker and the published comment carry only this fixed text:
`Model lane interrupted in <phase>; not a code rejection.` Raw exception text,
worktree paths, and account configuration paths do not
enter task blockers, issue comments, or agent notes.
A reviewer spool write failure, in either the sequential inner-review path or
the parallel PR-review path, removes the provider's raw output and any partial
`.tmp` or uncommitted final spool before the execution failure is recorded.
For output-file providers, each reviewer uses an agent-bound deterministic raw
path in the managed reviewer-spool directory. A path-free ownership marker is
committed on the working agent before the provider starts. On restart, raw
output without a valid committed recovery spool is removed before the lane
record is closed as failed; raw output is retained only alongside such a spool. Spool-write
failures expose only fixed categories (the sequential checkpoint-failure
category or the fixed lane-interrupted text), never filesystem exception text
or its local path.
For a sequential lane execution exception, the finished agent and the task's
failed-lane state commit in one transaction; the run is then retried under
`lane_retries`. A rollback leaves the
agent working and the task in its prior runnable phase.
Failure to write an implementer audit row itself is an observation and does not
rewrite the lane verdict. A reviewer audit failure rolls back its finished agent
and replicated outcome checkpoint because all three records share one
transaction; the local finished-result checkpoint and provider output remain
for the next tick's transaction retry, without a model relaunch and without
counting as a failed lane run. An unavailable sequential inner reviewer uses the same
published-once `provider-incomplete` reply checkpoint as unavailable
implementers and parallel gates after the automatic retries, so an early merge
cannot bypass the blocker.
File-tool targets use one fail-closed rule after terminal,
separator, and regex-escape normalization: `..` anywhere, `~`, a drive fragment,
or an absolute/UNC fragment at the start or after any character outside
`[A-Za-z0-9._-]` redacts the whole target. A drive fragment is one ASCII letter
followed by `:` at that same boundary, whether the following path is absolute or
drive-relative. Any numeric or named escape (`\x`, `\u`, `\U`, `\N{`, octal
`\0` through `\7`, or `\c`) also redacts the complete target without decoding.
Any percent escape of the form `%HH` is likewise fully redacted before
normalization, for both path and pattern targets; it is never decoded.
Any raw ESC or C0/C1 control byte redacts the complete target before
normalization or outside-path classification.
The only exception is a single plain whole absolute path under
the worktree whose relative remainder passes the same checks; it is stored
worktree-relative and may contain glob `*`, `?`, `[` and `]` characters.

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
| `gate_order.py` | Durable canonical gate ordering shared by every gate reader |
| `timestamps.py` | Shared aware ISO-8601 parsing and UTC normalization |
| `text_sanitize.py` | Shared terminal escape/control matching and sanitization between redaction passes |
| `coordinator_config.py` | Parent-owned explicit-repository and owner-scoped configuration loaders |
| `preconditions.py` | Declarative precondition registry data |
| `coordinator_checks.py` | Precondition evaluation and static resolvers |
| `coordinator_check_lane.py` | Check lane, duplicate check, round assessment |
| `coordinator_orders.py` | Order creation, events and answers |
| `order_ops.py` | Shared table of order operations |
| `orders.py` | `agent order` command line generated from the operation table |
| `order_report.py` | Bounded order acceptance report |
| `dashboard_orders.py` | Local dashboard order routes |
| `coordinator_conflicts.py` | Conflict remedy of `pr_mergeable` for orders |
| `hub_assign.py` | Paired-device client for operator assignment creation |
| `coordinator_a38.py` | A38 suite, author review comments, external review mail, guard Ready observation |

## A38 mode

A worker opts in explicitly with `"a38": true`. This is never an installation
default. With the external second review, the worker requires a nonempty,
distinct, lowercased `second_reviewers` list that excludes its own configured
GitHub login. Optional
`second_review_timeout_seconds` (default `86400`) must be at least 600.
`review_session` is unused and optional;
`check_argv` may be empty, while `readiness_argv` remains required for
configured repositories; owner-scoped repositories may omit
`owner_readiness_argv` only with the explicit hub deviation statement described
above. The device's
models must pin a non-Codex first provider. The external route pins
`"second": null`. With `a38_second_review: "model"`, model slot `second` must be
set and `second_reviewers` is not required. A null second slot is refused for
every non-A38 worker. The task pins the A38 second-review mode at creation. The
`model` value enters its execution binding; the default external value is
omitted for compatibility with existing bindings.

Illustrative worker fragment:

```json
{
  "a38": true,
  "second_reviewers": ["reviewer-one", "reviewer-two"],
  "second_review_timeout_seconds": 86400
}
```

Model-route fragment:

```json
{"a38": true, "a38_second_review": "model"}
```

On each new clean signed head, the script optionally runs `check_argv`, then
validates the trusted-base A38 policy, runs the complete A38 suite in
`workspace_root/.coordinator-control/<task-id>/a38/<head>/`, verifies its
report, and posts that report unchanged with the worker GitHub account. A
failure returns bounded findings to the implementer. The first-slot quality and
logic gates then run as usual. Their approved result is rendered and validated
as the `conformity-a` / `logic-a` author comment.

On the external route, the coordinator does not launch model slot `second`. It sends one
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
comment as the pull request author, and marks both `review_2_*` checklist keys
with the evidence of every passing external review, so
the newest passing review is recorded and a pass after a block on the same head
(also after the merge) closes them. It continues accepting
unread reviews for that head through CI, readiness, Ready observation, and merge
observation, and publishes one idempotent review comment for every valid mail.
The old stages `grok-pr` and `codex-pr` and old keys `grok_pr_*` and
`codex_pr_*` are legacy aliases: CLI input accepts and warns about them, and
stored rows remain readable without a database rewrite.
Each tick takes in all queued unread mail for the head, ordered by the mail
row's replica change time (`updated_at`, equal times by row id), and the newest
review decides. A review older than one already posted for the head is
acknowledged as superseded and not posted, so the last posted review is also the
newest, as the guard reads the comments. A blocking result returns the task to the
implementer only when no newer passing review of the same head follows it in that
tick; when the implementer leaves the head unchanged, the reviewers are asked again; stale, malformed, or
unauthorized mail is acknowledged and recorded as an
observation. Pending requests are re-pinged once per configured timeout and are
never skipped. A pull request closed while the task awaits the second review
stops the wait with a blocker; no further second-review comments or pings are
sent for it.
The originally verified first-review comment payload is immutable for its
head. A same-head no-change implementation pass reuses that exact comment even
when task summaries changed, and writes only the next deterministic reviewer
ping occurrence. Reconciliation validates the full saved comment and ping
payloads before treating either outside effect as complete. Legacy saved
first-comment intents that contain only `head` and `activity_id` are upgraded
from the exact owned activity payload; when that activity does not exist yet,
the coordinator renders and checkpoints `body` and `passes` before publication.
Every saved first-comment body is parsed and checked against the declaration,
visible record, and machine block before checkpointing, publication, or result
reconciliation; its declared head and pass count must equal the checkpoint.

The A38 phase route is `publish_draft` → `tests` → `pr_gates_grok` →
`a38_first_comment` → `await_second_review` → `ci` → `readiness` →
`await_ready` → `await_merge` → `done`.
With `a38_second_review: "model"`, the route after the first comment is
`pr_gates_codex` → `a38_second_comment` → `ci` → `readiness` → `await_ready`.
The script runs the second-slot quality and logic lanes, renders their comment
through the same guard renderer without a reviewer line, and posts it as the
pull-request author. A rejected pair posts a blocking second comment with
`blocks_merge: true` and defects equal to the number of rejecting dimensions;
all rejecting findings return to the implementer. The `review_2_*` checklist
keys close from the gate rows. Readiness requires the report, the first comment,
a non-blocking second comment on the head, and all four approved gates; after a
`review_2` waiver of an order it requires the report, the first comment, and the
two approved review-1 gates. The
guard still owns Ready; the coordinator never marks an A38 pull request Ready.
The tests phase reads pull-request metadata, the paginated file inventory, and
the metadata again. Head SHA, base SHA, base name, and open state must match
across both metadata reads; a race is retried a bounded number of times and
then blocks recoverably. A merged or closed state in either metadata read
rejects the measurement context and routes directly to merge verification,
without checkpointing, running or waiving the suite, publishing a report, or
promoting base evidence. Before publication is queued, the task checkpoints
the pull-request repository and number, exact verified report-body digest,
visibility, head, base, and policy revision. Recovery first reads pull-request
state. An open pull request still requires the current execution binding and
source assignment; only a confirmed merged or closed pull request may finish
the checkpoint without a live assignment. An unreadable state retries without
changing the task or publishing. Recovery requires full payload equality and revalidates the
v2 schema, repository, head, live inventory facts, and trusted policy jobs; a row
that merely occupies the deterministic id is measured again and is not test
evidence. A validated legacy report intent that predates the post-suite
checkpoint is first converted into that checkpoint, then resumes through the
same stable identity, head, base, base-name, and state checks; it cannot use the
older state-only finalization path. Its report publication identity includes
the head, base, and active policy revision, so a base move requires new evidence
and a new comment. Immediately before creating or reusing that intent, recovery reads
two equal snapshots containing the pull-request number, head SHA, base SHA,
base name, and state. An open identity, head, base, or base-name mismatch retires
the checkpoint and pending report and routes to measurement; a closed mismatch
retires them before merge verification. A merged mismatch retires them before a
non-recoverable stop and is never remeasured or published. The verified report intent and its pending comment row become
visible in one transaction. Under the task-wide report lock, that checkpoint
reloads the task and rejects it when another writer changed the head, pending
or promoted base, policy revision, or report intent from the snapshot read at
the start of the tick. Report publication and inventory-waiver finalisation
reload that checkpoint under the task lock; publication also holds the
activity lock and requires the current intent to match the completed activity.
Only then do the report or waiver, local gate, base promotion, and phase
transition commit together. Every such conflict reloads the task and retries
without saving the stale caller row or publishing a blocker. The checkpoint
then records the newly verified head on the
fresh row and, when it differs from the promoted evidence base, records the
observed base as pending. A head or active-base checkpoint change retires a
stale report intent and its pending activity in that same transaction while
holding the report activity lock. While a base remeasurement is pending,
`pending_base_sha` is the active checkpoint binding and the previously promoted
`base_sha` remains historical evidence until replacement evidence and the local
gate promote the pending base. A checkpoint conflict reloads the task and
retries the coordinator tick without saving the stale caller snapshot or
publishing a blocker. The same lock covers report invalidation. The
transaction changes every other still-pending
coordinator A38 report for the task, including legacy and superseded
deterministic ids, to `error`. Publication and invalidation additionally hold
the affected per-activity cross-process advisory lock; the executor re-reads
both the pending activity and the task's current matching intent immediately before a GitHub
POST. It also requires the binding head and base to equal the task's current
`head_sha` and active pending-or-promoted base. A stale row and its matching
intent are retired together while the activity lock is held. It never moves a
non-pending row to `done`. A row bound to a coordinator task may make that one
verified report-intent removal through the otherwise activity-only scoped
GitHub executor; no other task write is permitted. Rows bound to a coordinator
task, including
rows with either the current or legacy deterministic coordinator report id,
fail closed without a current matching intent. Unbound report-marked comments
from other executor paths remain eligible for publication. A moved live value
first updates
`coordinator.a38.pending_base_sha`; only bound report-or-waiver evidence plus
the recorded local gate promotes it to `coordinator.a38.base_sha`, while the
checkout's `coordinator.base_sha` remains unchanged; A38 policy, readiness,
report validation, and review diffs use the live evidence value. The script
reconciles an existing pending SHA on every stable open-PR context read. A
missing or non-40-hex pull-request base revision during `tests` is a transient
read: the phase returns `PR state temporarily unavailable; retrying` without
changing the saved task phase, base evidence, or pending base. Other invalid
metadata remains a hard error. A merged or closed pull request moves to merge
verification before pending-base reconciliation.
If the branch returns to an earlier trusted SHA before remeasurement, the stale
pending SHA is replaced and evidence for the currently observed SHA is
reconstructed before promotion. The script
fetches the configured base ref and verifies that this exact observed base
commit exists locally before either running the suite or taking an inventory
waiver. Independently confirmed markdown-only and guard-docs inventories
record an exact-head author-report waiver, skip local report execution,
verification, and publication, and continue to both required review comments.
Report publication and inventory waivers close `local_check_pass` before the
validated `pushed` bookkeeping step, preserving the spine checklist order.
After a successful suite, a pull request that merged on the measured head skips
draft reconciliation but still receives the verified report and records the
same report, local-test, and promoted-base bindings before merge verification.
Before that follow-up read, the coordinator persists the verified report body,
digest, head, base, policy revision, and visibility as a post-suite checkpoint.
An unreadable follow-up head keeps the task in `tests`; the next tick bypasses
the generic merged/closed redirect and resumes only live-head validation,
report publication, and evidence promotion without rerunning the suite. A
pending rejection-review comment is still retried before that checkpoint read
and before any merge, close, or blocker decision. If an assignment failure first
blocks checkpoint recovery, a later merged or closed state resumes `tests`
without a reply so the checkpoint can publish or retire under its live-context
rules before merge verification. A saved matching report intent is reported as
`already posted` after its
publication result is verified, rather than being described as a new report. A
confirmed different merged head is a non-recoverable completion boundary
rather than an `await_merge` retry. A confirmed different head on a closed,
unmerged pull request remains unbound and moves to merge verification, which
publishes the `closed-unmerged` blocker. Failed suites and results whose live
head no longer matches remain unbound.
When a trusted coordinator precheck is configured, the waiver records a
current-head `coordinator-check` skip so an older failure cannot remain the
selected result.
Immediately before draft reconciliation, the waiver path rechecks pull-request
identity, head, base revision, base name, and state in one stable snapshot. It
repeats that snapshot immediately before finalization. An open mismatch retires
the waiver measurement and returns to `tests`; a merged or closed mismatch moves
to merge verification without promoting the stale waiver. A matching merged or
closed snapshot may still bind the waiver before merge verification.
The 500-file waiver ceiling counts the expanded GitHub path inventory, so each
rename or copy contributes both its old and new path, matching the guard.
Coordinator A38 mode does not implement policy migrations; a changed
`.github/a38.json`, `.github/pr-guard.json`, or any path below
`.github/workflows/` fails loudly before a suite or inventory waiver can publish
evidence against old base policy. The sole exception is
`.github/workflows/a38-guard.yml` in an independently confirmed guard-docs change
set. This blocker is non-recoverable and has no reply checkpoint. A
legacy same-head report without `base_sha` or `policy_revision` is invalidated
at readiness and routes the task back to `tests` for current base-bound
measurement and publication.
The movement immediately reopens `local_check_pass` and the readiness checklist
keys. An open pull request can therefore be remeasured after a base move. A base
move
first observed after merge is different: the new base is never recorded as
verified, the old/new pair is retained in `base_moved_after_evidence`, and the
task stops at the non-recoverable completion boundary described above.
When the retries of `lane_retries` are used up, an incomplete parallel PR
review commits its `unavailable` gates, checklist
values, blocked phase, exact resume phase, reply checkpoint, and deterministic
issue-comment intent in one transaction. GitHub execution happens only after
that commit, so a process restart remains blocked until the queued checkpoint
is published and an authorized reply is observed; it cannot relaunch a reviewer
from the persisted unavailable gate alone.
Each rejected gate's comment identity includes that durable gate row id. A
crash retry therefore reuses the same publication intent, while a later gate
attempt with identical findings creates and publishes a distinct comment.
The worker never posts a formal approval and never calls `gh pr ready`. Lane prompts quoted in the review comments have this host's control directory, worktree, workspace root and home directory replaced by `<control-dir>`, `<worktree>`, `<workspace>` and `~`. While
the pull request remains draft it only records the newest exact enforce context
`A38 / report (<evidence base>)` selected by `a38_guard.status_context_enforce`;
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
Before `task-done` is evaluated for a merged A38 head, `await_merge` evaluates
the merged-head second-review outcome and starts or checks its timeout on every
tick. A blocking review or expired timeout publishes its merge blocker even
while cumulative CI is pending or temporarily unavailable. A non-expired wait
may still return the cumulative-CI pending result, but its timer has already
been persisted and checked. `await_merge` re-observes the exact-head cumulative
GitHub check rollup and Actions workflow inventory with the same classifier as
`ci`. Pending or transport-unavailable
evidence retries without effects. Failed, cancelled, `action_required`, or
uncovered `skipped`/`neutral` evidence leaves the merged task at a
non-recoverable human-intervention blocker. A hard protocol fault of the check
rollup or the workflow inventory is a recoverable blocker with
`resume_phase=await_merge`, as in `ci`, and a later tick observes the merge
again. Only a verified exact-head author
report may cover a matching `skipped` or `neutral` check. This cumulative CI
gate remains a precondition for `task-done` and its `evaluate_allow` decision;
it does not defer second-review timeout or blocker handling.
Second-review intake runs while the pull request is open and, in `await_merge`,
also after the merge, so a review of the merged head is still published; it never
reopens work. After the merge every waiting review is taken in before
`await_merge` decides by the newest one. A merged pull request completes only with a passing second review:
`await_merge` waits up to `second_review_timeout_seconds` for it, publishes the
non-recoverable blocker `merged-without-second-review` after that, and publishes
`merged-with-blocking-second-review` at once when the newest posted review blocks the merge.
Both merge blockers are final like `closed-unmerged`: the task stays blocked, and a
review that arrives after that blocker is published is not taken in. A model-route
task merged before its second comment was posted is a final blocker at once. A new head invalidates its report, both comment checkpoints, and
external-review state. A blocking review that arrives after the guard set Ready
is fixed on the same pull request: an A38 worker pushes the new head to the open
pull request whether or not it is still a draft, and the guard decides Ready again.
When the script had already composed the second comment for the merged head, it finishes that comment at
the merge, for an issue task only while its issue is still open and assigned, and the merge check reads it.

`a38_second_review: "model"` enters the task execution binding. The default
external mode does not, so existing A38 hashes remain stable. Other A38
settings enter the task execution binding only for A38 workers, so the binding
hashes of existing non-A38 workers remain stable.

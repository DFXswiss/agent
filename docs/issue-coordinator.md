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
| Universal sandbox / forced model isolation | **Not claimed.** Grok implementer argv denies Bash/subagents/web-search; that is process argv hardening only. |

Distinguish a requirement (DESIGN §19.7), an implemented module, and a verified
deployment. This document does not invent evidence that a device is running the
coordinator.

## Explicit configuration

`$AGENT_HOME/coordinator.json` starts absent. A missing file, `{}`, or
null/empty `workers` enables no worker. Operator-supplied worker keys are
existing session IDs, bound explicitly in `github-accounts.json` and
`ai-accounts.json`; the coordinator does not select accounts or roles for them.

Each configured worker explicitly names `review_session`, `workspace_root`,
`repositories`, `reply_logins`, `poll_seconds`, `lane_timeout`, and
`check_timeout`. Each repository entry names its `base`, `publication_repo`,
`check_argv`, and `readiness_argv`. The two argv arrays belong to trusted
device configuration, never issue text or model output. They run the target
repository's required tests and additional readiness validation;
repository-specific policy stays outside the core. The publication repository
can equal the target repository; a different repository requires operator
authorization for that publication route.

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
  `review_session` owned/active with `pr-review`; all required AI lane slots
  present for the worker session; worker and review GitHub accounts configured,
  authenticated, and bound to **different** logins; worker account has git
  identity for signed commits. Missing or mismatched profiles start **no**
  provider.
- Required AI slots: `grok:implementer`, `grok:reviewer`,
  `grok:pr-reviewer-quality`, `grok:pr-reviewer-logic`,
  `codex:pr-reviewer-quality`, `codex:pr-reviewer-logic`.
- Checkpoints live in `task.payload['coordinator']` and activity `result`
  fields. There is **no** second hub state machine and **no** new store table.
- `runner` executes `gh`/`git` trusted calls and returns
  `Completed(returncode, stdout, stderr)`. GitHub-scoped calls go through
  `Account.runner` (explicit `GH_CONFIG_DIR`), never an ambient login.
- `lane_runner(argv, stdin)` is optional. When omitted, lanes and trusted
  argv lists run via a Python bounded subprocess (process-group kill on
  timeout), preserving stdin and cwd. External `timeout(1)` is **not** used
  (absent on stock macOS). Tests inject fakes. Grok implementer argv is
  hardened with `--deny Bash`, `--no-subagents`, and `--disable-web-search`.
  This is process argv hardening, **not** universal sandbox enforcement.
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

1. **Discover** configured repositories for open issues assigned to the
   configured GitHub login (paginated API). Initial scan includes current
   assignments (no silent first-run ignore). Idempotent source key:
   `repo + issue number` device-wide — first session owns; one task/PR until
   terminal. Failed tasks are **not** auto-reopened merely because the issue is
   still assigned; recovery needs an authorized reply or a verified new event.
   Assignment evidence is verified on GitHub; forged model activity payloads
   are not trusted. Issue bodies are redacted/bounded before persistence.
   `updated_at` is not treated as `assigned_at`.
2. **Accept** with a deterministic issue comment (fixed wording + idempotency
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
   implementer. Ask/blocked results are published on the source issue; the tick
   returns with no model active. Authorized replies (`reply_logins` only),
   strictly after the verified own question comment id/login, resume as
   untrusted spec with exactly-once consumption checkpoints. Uncertain lane
   outcomes refuse a second model start and publish a GitHub-visible blocker.
5. **Draft** as soon as the first signed task commit exists (`pr.open` on the
   **target** repo), before full tests/reviews. Each new signed head is pushed
   to the existing PR before later stages. No empty fake PR when there is no
   patch and no existing PR. Crash after commit/push before draft reconciles
   without starting another implementer. `task.ref` holds the **PR** number
   only (never the issue number).
6. **Tests** run only via script `check_argv` on the exact clean signed head
   (cwd = worktree). Failure routes bounded output to the implementer. Stale
   passes from another head are not reused.
7. **PR gates**: Grok quality+logic in **parallel** (fresh independent
   invocations; agent rows prepared on the main thread, subprocesses in
   threads, results persisted on the main thread), then Codex quality+logic the
   same way only after both Grok dimensions are approved on that head. Author
   session does not sit those reviews. Incomplete/unavailable vendor output is a
   GitHub-visible blocker — not a rejected complete gate and not an implementer
   fix loop. Rejections publish `review.post` **COMMENT** (not
   `REQUEST_CHANGES`) and invalidate head-specific evidence.
8. **CI**: exact-head PR check rollup **and** paginated head workflow inventory
   (path+event+attempt). Only `success` counts. `action_required` is an
   external authorization blocker (not routed to the implementer; `resume_phase`
   stays `ci`). Missing / pending / failure / cancelled / skipped / neutral are
   not green. This core observes **cumulative GitHub CI** only; target-repository
   policy / A38 live join belongs to configured `readiness_argv`. Failures fetch
   plain-text logs via `gh run view <id> --repo <target> --log-failed
   --attempt <n>` (never ZIP `/logs` archive bytes). Inaccessible logs are a
   blocker. Transient pending returns without an idle model.
9. **Ready**: run `readiness_argv` (cwd = worktree, ambient GitHub tokens
   cleared). Stdout must be the fixed JSON readiness contract below (trusted
   operator script output — not model/repo input). Re-verify clean signed head
   **after** the command, re-observe CI fresh (no stale `ci_green`), unchanged PR
   head, author/base/mergeability, tests, and all four same-head gates. Close
   `contributing_ok` / deviation checklist keys from that JSON via
   `chain.close_allowed` **before** Ready — never after human merge. Formal
   `review.post` **APPROVE** from the separate review account pinned with
   `commit_id` (discover-before-POST; verify state/head/login/id/url). Before
   leave-draft, a fresh GET must still show APPROVED on the exact head (stored
   `formal_head` is not current proof). One evidence comment (must complete with
   `execution_status=done`), `allow pr-ready`, then leave draft and verify
   `isDraft=false`. **Never merge.**
10. **Complete** only after a verified **human** merge: GitHub merge actor type
    must be exactly `User` (missing type is not human; Bot is refused). Also
    require merge SHA, timestamp, and base/target. Then existing `task-done`
    checklist / summary guard (summaries must already describe the actual
    result — no boilerplate invented at merge), then `issue.assigned.ack`. A
    Ready PR closed unmerged is a user-facing blocker, not completion.
    Reassignment must not open a duplicate PR for a completed source.
    Revoked assignment stops new effects including formal approve / leave-draft;
    `await_merge` may continue observation only.

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
starting another model. Authorized replies resume the exact `resume_phase`
checkpoint (not blindly `implement` for CI authorization / checkout blockers).
Uncertain prior agents refuse a second model start. Inner and PR reviewers
receive a script-generated base→head diff artifact outside the worktree.

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
| `coordinator_config.py` | Parent-owned configuration loaders |

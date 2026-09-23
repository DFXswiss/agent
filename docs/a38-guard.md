# dfx pr guard

dfx pr guard explains a repository's centrally defined [A38 rules](a38.md), checks the latest author report and maintains one friendly English/German comment. Adopters keep only their manifest, integration, and contributing pointer; this guide is not copied into consumer repositories. The guard never checks out or executes pull-request code. A valid report is a consistent author declaration, not cryptographic proof of execution.

## Installation

The adopting repository's file checklist (manifest, `pr-guard.json`, contributing pointer, and what not to copy) is in [Adopting A38 in a repository](a38.md#adopting-a38-in-a-repository). This section is only the guard workflow.

Install the [example workflow](../examples/a38-guard.yml) on the target repository's default branch. Replace `USES_REF_PIN_ME` with a reviewed, published **full commit SHA** of this repository. The example is not deployable until that placeholder is replaced. Keep the guard's executable action pinned even when approving policy migrations.

The [composite action](../.github/actions/a38-guard/action.yml) uses pinned setup-python and PyYAML 6.0.2, and imports only the trusted action's sources through `github.action_path/../../../src`. Both Python steps run from the trusted action directory with safe-path mode (`python -P`), and replace inherited `PYTHONPATH` with the trusted source path, preventing consumer modules from shadowing the guard or its installer. It does not install dependencies or run scripts from the consumer checkout. Install the package's declared dependencies for standalone use; there is no fallback YAML parser.

The guard's comment and JSON expose three different immutable links:

- `standard_url` is `DFXswiss/agent` `docs/a38.md` at the exact trusted guard runtime SHA.
- `guard_docs_url` is this guide at that same runtime SHA.
- `policy_url` is the consumer `.github/a38.json` at the active base SHA or exact approved head SHA, including the head repository for an approved fork migration.

`policy_url` is derived assessment output only. It is not an `a38/v1` manifest input or schema field. The fixed `documentation: docs/a38.md` token identifies the central standard and does not assert that the consumer has that file. Central documentation links never use the consumer head, consumer base, active policy revision, or a moving branch.

For the composite action, `A38_RUNTIME_REVISION` is overwritten from `${{ github.action_ref }}` on the guard step. Composite context values must be passed through `env`, as documented by [GitHub's contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts). The value must be a lowercase 40-hex commit SHA; a branch or moving tag fails clearly. `github.sha` is not suitable because it identifies consumer workflow context.

Standalone execution accepts the same explicit trusted `A38_RUNTIME_REVISION`. Without it, source-checkout fallback is allowed only when the loaded module is exactly `<root>/src/agent_cli/a38_guard.py`, `<root>/.git` belongs to that root, Git reports the same top-level using explicit `--git-dir` and `--work-tree`, and `HEAD` is lowercase 40-hex. The lookup is anchored to the module source root, removes inherited `GIT_*` variables, and never discovers from the current directory or an enclosing consumer checkout. A non-Git packaged install requires the explicit trusted revision; it never guesses `develop` or another moving ref. Closed PRs, ignored events, and empty all-open scans remain successful no-ops and do not need provenance resolution.

The token requires contents write, pull requests write, issues write and statuses write. Listing workflow runs for the informational `PR-GUARD:CI-MANUAL:v1` comment needs Actions read (`actions: read`). Approving held fork workflow runs and pending environment deployments needs Actions write (`actions: write`). `markPullRequestReadyForReview` needs `contents: write` on `GITHUB_TOKEN` or it returns HTTP 200 with `isDraft` unchanged (`Resource not accessible by integration`). Publishing the guard comment on a pull request needs `pull-requests: write` for `GITHUB_TOKEN`; `issues: write` alone is not enough and yields 403. Policy migrations also require permission to read collaborators' effective repository permissions. If that API is unavailable, the migration fails closed. Use a dedicated GitHub App or service account with the necessary repository access for external operation. Tokens are taken from `GH_TOKEN` or `GITHUB_TOKEN` and never printed.

Actions must actually be available for event-driven operation. When Actions are blocked or unavailable, run the same reconciler on a trusted external host:

```sh
agent pr-guard --repo OWNER/NAME --all-open --dry-run
agent pr-guard --repo OWNER/NAME --all-open
```

Schedule that command externally when Actions are unavailable; no daemon is installed. The example workflow also reconciles all open PRs at minutes 17 and 47 of every hour and uses two concurrency groups (`event` versus `sweep`) so PR events cannot starve all-open reconciliation. Its manual dispatch accepts either a PR number or `all_open=true`. GitHub Actions does not guarantee delivery of every pending concurrency event, so scheduled reconciliation recovers missed events, base changes and permission changes. Immutable SHA-addressed contents and trees are cached within the API client, up to 128 entries; comments, reviews, permissions and PR snapshots are never cached.

## Trust and policy

The authoritative PR API supplies the target repository, exact head SHA, exact base SHA, target branch, actual boolean visibility and numeric author ID. Immutable PR-head workflow trees and files, approved head policy, and proposed `.github/pr-guard.json` bytes are read through the base/target repository at the exact validated head SHA (Git object network). Repository-scoped tokens cannot list private fork trees directly; `head_repo` remains provenance only and is never replaced by a merge ref or other mutable tip. Base policy at the base SHA still comes from the target repository. Missing repository identity or visibility fails closed.

### Target-branch scope (`.github/pr-guard.json`)

A38 applicability is **repository configuration**, except for the built-in exact-`main` skip when `main` is **not** the default branch. The repository default branch is not itself a built-in enforce/exclude decision; it locates the file and decides that skip. Optional [`.github/pr-guard.json`](../examples/pr-guard.json) on the trusted default-branch revision selects which PR **target** branches enforce A38:

```json
{
  "schema": "pr-guard/v1",
  "a38": {
    "enforce": ["integration"],
    "exclude": ["release"],
    "default": "enforce"
  }
}
```

| Field | Meaning |
| --- | --- |
| `schema` | Exactly `pr-guard/v1`. |
| `a38.enforce` | Bounded array of exact target branch names that require A38. |
| `a38.exclude` | Bounded array of exact target branch names that skip A38. |
| `a38.default` | `enforce` or `exclude` for every target branch not listed above. |

Rules enforced centrally by the Agent:

- Branch entries are exact, case-sensitive names (same 1–75 character limits as status contexts). There is no glob DSL.
- No duplicates within a list, and no overlap between `enforce` and `exclude`.
- Unknown JSON keys and duplicate JSON keys fail closed. The schema string must match exactly.
- The only built-in branch-name rule is exact `main` **when it is not the repository default branch**: that target is out of scope (nothing for A38 to check). When the default branch is `main`, `a38.enforce` / `a38.default` apply. Other names have no built-in meaning. The repository default branch is used to **locate** this file and to decide that `main` skip; it is never an implicit enforce by itself.
- Evaluation order: skip exact `main` only when it is not the default branch, else exact `enforce` match, else exact `exclude` match, else `a38.default`.
- When the entire file is missing on the trusted revision, legacy **enforce-all** applies for every in-scope target. Malformed configuration, HTTP 403, or any non-404 configuration API error fails closed and cannot exempt a PR.
- The live PR's `base.repo.default_branch` metadata (never the head repository) is validated, resolved to an immutable commit via `GET /repos/{repo}/commits/{urlencoded_default_branch}` (lowercase 40-hex SHA), then the file is read from that revision in the **base** repository. Configuration from the PR head can never self-exempt.
- Assessment JSON records `trusted_default_branch` and `config_revision` for audit. Closed PRs remain successful no-ops **before** any configuration lookup.

Excluded targets return `ok: true`, `status: not_applicable`, `closed: false`, with a reason from the configuration or the built-in exact-`main` rule. The guard does not load A38 policy, author reports, migration approvals or provenance for those PRs, and does not publish comments. It publishes only the stable `A38 / report (<target>)` success status with an explicit not-applicable description (to clear prior erroneous red statuses), deduplicated; that success is not a test-pass claim and does not touch another target context. `--dry-run` writes nothing. In-scope targets keep the full original report, policy and migration behaviour.

Edits to `.github/pr-guard.json` on an enforced PR (add, remove or change bytes versus the immutable base) are policy migrations: they require the same exact head/base maintainer `A38-POLICY-APPROVAL:v1` declaration as workflow or manifest changes. Even after approval, the proposed head configuration is **not** activated for the current PR; scope continues to come from the trusted default-branch revision until merge. Base A38 mode, security rules and the pinned executable remain unchanged.

Before any publication and again immediately before a success status, the guard re-fetches the PR snapshot and the trusted configuration revision/content. Changed target branch, default branch, config revision, config bytes, head, base, state, title or body reject the stale verdict and retry assessment. Event, explicit PR and all-open routes share this same central resolver.

### A38 manifest and workflows

By default, policy is `.github/a38.json` from the immutable **base SHA**. Every workflow job at the head must be classified exactly once as required or explicitly excluded; classifications for absent jobs fail as well. Matrix profiles must run every required variant. The guard does not infer or execute matrix expressions. Added, removed or changed workflow bytes require the explicit policy migration described below. Independently confirmed guard-docs change sets (every path is a markdown file and/or exactly `.github/workflows/a38-guard.yml`) do not treat a bytes change of that guard workflow file as a policy migration; any other workflow path still does. Unclassified jobs and invalid workflow YAML still fail.

Policy JSON uses the strict `a38/v1` schema. Workflow YAML is safely loaded with duplicate-key detection, no aliases and bounded nesting. Files are limited to 1 MiB and API responses to 16 MiB. Unsupported YAML fails closed; use explicit mappings instead of aliases in adopted workflow files. Missing or invalid policy is an error when the guard is installed or invoked, never a successful empty check.

## Approving a policy migration

A policy update must not authorize itself. A maintainer can explicitly authorize using the proposed **head manifest as data** by submitting an APPROVED GitHub review with this declaration, replacing both SHAs:

```text
A38-POLICY-APPROVAL:v1 head=<HEAD_SHA> base=<BASE_SHA>
```

The canonical form stays one line. For usability, the three exact tokens may instead be separated by spaces or tabs, or wrapped at token boundaries onto immediately adjacent CRLF/LF lines with horizontal indentation; leading and trailing horizontal whitespace on the declaration lines is ignored. The declaration remains bounded to its own line or adjacent lines: surrounding prose is allowed only on separate lines, while prefix prose, suffix garbage, substring matches, changed key names or SHAs, missing or duplicate keys, split SHAs and fragments from separate declarations are rejected.

The review's GitHub `commit_id` must equal the current head. The reviewer must differ from the PR author by numeric account ID and currently have write, maintain or admin permission on the target repository. The permission response must confirm the same numeric identity. A normal approval without the line does not authorize migration.

Bot/app identities and non-collaborators are ineligible: their reviews neither authorize migrations nor invalidate ordinary reports. A missing collaborator permission record (404) is ignored; authentication/authorization failures (401/403), other API errors and mismatched numeric identities remain errors.

For each reviewer, the latest substantive submitted state controls authorization. Dismissed or superseded approvals do not count; ordinary comments and pending drafts do not change an approval. A current-head changes request from an eligible maintainer blocks the migration exception. New head or base SHAs require renewed explicit approval.

An authorized migration may introduce, remove or change workflows, and may also add, remove or change `.github/pr-guard.json`, but the complete current head inventory and author report still must satisfy the approved head policy. Proposed pr-guard configuration is never used for scope until it is merged to the trusted default-branch revision. The executable guard remains pinned and never runs head commands. The **base policy's enforcement mode stays active** for this PR, even if the proposed mode is `observe`. If no valid base policy exists, explicit approval permits bootstrap under `enforce`; initial adoption cannot silently bypass reporting.

Publish the migration proposal draft first per the [pull request lifecycle](pull-request-lifecycle.md). Measurement and verification against the still-active base policy may follow on that same signed commit; that run is proposal evidence, not Ready evidence. Once the exact current head/base approval exists, apply the canonical [evidence reuse after approval](a38.md#evidence-reuse-after-approval) rules. An unchanged complete successful report can be revalidated and published with its original timestamps and results; approval alone does not require executing the tests again. A complete new run is required when the reuse conditions are not met. Always perform fresh local verification and the live join. Bootstrap with a missing or invalid base policy likewise publishes the draft first under the lifecycle (full pre-push test gates are not a draft blocker), then requires explicit approval and `enforce`; it must not invent a report or waive approval.

The bot identifies the active policy revision in its comment. Download `.github/a38.json` from that exact revision before generating the report. For ordinary PRs, this is the base; for explicitly approved migrations, it is the head.

## How fork GitHub Actions are meant to work

This is the intended sequence. Ready for review is **not** a CI switch. Labels such as `ci` / `ci:full` are repository-specific scope, not the A38 gate.

1. A pull request is opened (often from a fork, often as a draft).
2. GitHub may create `pull_request` workflow runs and **hold** them (`action_required`). That hold is not a test result and is not a red A38 report.
3. The author measures A38 on the current head and posts the report.
4. The **trusted** guard (pinned action, no PR checkout) validates that report.
5. Only a fresh **enforce pass** on this head may **approve** those waiting **initial** runs, and only for workflow paths listed in the trusted `.github/pr-guard.json`.
6. Superseded or non-allowlisted `action_required` runs on this head are **cancelled** so GitHub does not keep the yellow “workflows awaiting approval” banner. That is not stopping a running test.
7. Approval means GitHub may start those runs. It is not a green check. The jobs still have to finish.
8. A human merges. The merger does **not** click **Approve and run workflows**.

The guard does **not** approve on open, push, label, or Ready alone. Missing, failed, observe-mode, excluded, closed, or same-repository PRs get no approval. Changing the repository's fork-protection setting is not a fallback.

## Informational comment when a human starts workflows

When the latest run of a product workflow on the current head was started by a human User (re-run, `workflow_dispatch`, `repository_dispatch`, or a different triggering actor), the guard POSTs one informational EN/DE comment (`PR-GUARD:CI-MANUAL:v1`). Bot or App retries do not count. Visible sentences use `@login` when GitHub reports a User login, otherwise “an administrator” / “einem Administrator”. That comment is one per current head and base; a later head or base POSTs a new comment and must not PATCH an older one. It does not authorize auto-ready. It is always-on for open in-scope PRs (no `pr-guard.json` flag; it does not require `workflow_approval.enabled`). Assessment JSON includes `manual_workflows`. `--dry-run` computes the comment without writing. The guard's own workflow file `a38-guard.yml` is ignored.

## Optional fork workflow approval

A repository may opt in to bot-owned CI authorization in its trusted default-branch `.github/pr-guard.json`:

```json
"workflow_approval": {
  "enabled": true,
  "workflows": [".github/workflows/ci.yml", ".github/workflows/security.yml"]
}
```

This is an optional top-level object alongside `schema` and `a38`. Omission disables the feature. Both fields are required when present; `enabled` is a boolean, and `workflows` is a duplicate-free list of at most 64 exact workflow YAML paths (nonempty when enabled). Globs and unknown fields fail closed. The proposed PR-head config cannot activate approval. Workflow selection and the token's `actions: write` permission belong to the adopting repository, not to a global runner policy.

The bot approves only an **initial** `pull_request` run waiting in `completed` / `action_required`, with `run_attempt: 1`, for an allowlisted workflow on the exact current head, fork repository and branch. A fresh A38 `pass` under `enforce` is required. Failed or incomplete evidence, observe mode, excluded targets, closed PRs and same-repository PRs cannot trigger approval. Existing migration authorization remains required for policy/workflow/config changes.

For each workflow, the newest matching run across **all** states wins. A queued, successful, failed or rerun attempt supersedes an older blocked run. The fork workflow-approval path never calls a rerun, dispatch, merge, review-approval or environment-approval endpoint. It **cancels** superseded or non-allowlisted `action_required` runs on the current head so GitHub does not keep the pull request banner “workflows awaiting approval”. It never cancels an in-progress or queued test. Approval authorizes execution; it is not a test result or a Ready verdict.

The run's PR association must match the current PR/head/base. For private forks whose API association array is empty, the fork branch must identify exactly one open PR, its head must include the current base, and the run must not predate the PR or a later recorded target/lifecycle change. Ambiguous association, incomplete pagination, API errors or denied permissions fail closed. Head/base, trusted config, latest author report and maintainer authorization are refreshed before every POST. GitHub provides no atomic compare-and-approve operation; these checks minimize, but cannot eliminate, a change racing the final API call.

Enable `actions: write` in the trusted guard workflow (or equivalent Actions write access on a dedicated App token). The guard uses GitHub's [approve-workflow-run endpoint](https://docs.github.com/en/rest/actions/workflow-runs#approve-a-workflow-run-for-a-fork-pull-request), accepts only its documented `201` success, and never retries that POST. Insufficient permissions remain an explicit failure; changing the repository's fork protection setting is not a fallback. `--dry-run` previews candidates without any writes, including audit comments. Assessment JSON includes `workflow_approvals`; completed authorization also records `workflow:approve:<run-id>` and `workflow:cancel:<run-id>` in `writes`.

The first successful approve or cancel in a guard invocation POSTs a new visible EN/DE comment (`PR-GUARD:CI-AUTH:v1` / `PR-GUARD:CI-CANCEL:v1`). Further successful same-kind mutations in that same invocation PATCH that comment. A later invocation POSTs a new comment; it must not PATCH an older one (GitHub PATCH does not move the comment in the timeline). Ready/Draft still POST a new `PR-GUARD:LIFECYCLE:v1` comment per transition. HTTP 409 on cancel does not comment. Approve and cancel comments are posted even when lifecycle is disabled. Auto-ready uses the latest bot-owned AUTH record (highest comment id) on the current head/base when the guard authorized held runs. When no bot-owned AUTH row exists and required CI is already green (nothing was held), auto-ready still performs leave-draft (`isDraft=false`). A present but mismatched bot-owned AUTH row still blocks.

The trusted default-branch workflow and config must be installed before optional fork workflow approval is active. A head-only proposal does not grant itself permissions or authorize its own runs. Scheduled reconciliation catches runs created after the author report event. After authorization, inspect the actual independent GitHub checks through completion, including blocked `action_required` workflow runs that may be absent from the PR check rollup.

## Optional environment deployment approval

A repository may separately opt in to approval of deployments waiting on one
GitHub environment in its trusted default-branch `.github/pr-guard.json`:

```json
"environment_approval": {
  "enabled": true,
  "environment": "pr-ci",
  "workflows": [".github/workflows/pr.yml"]
}
```

This optional top-level object is a sibling of `workflow_approval`, not a
replacement. Omission disables it. All three fields are required when present;
`enabled` is a boolean, `environment` is a nonempty string of at most 255
characters, and `workflows` follows the same bounded, duplicate-free exact-path
rules as fork workflow approval (nonempty when enabled). Unknown fields fail
closed. Configuration proposed only on the pull-request head cannot activate
the feature.

After a fresh A38 `pass` under `enforce`, the guard selects the latest matching
`pull_request` run for each allowlisted workflow on the current head, reads its
`pending_deployments`, and approves only pending items whose environment name
exactly equals the configured name. It sends `A38 enforce pass` as the approval
comment. Runs without a matching pending deployment are unchanged. This applies
to fork, organization-member, and same-repository pull requests; unlike fork
workflow approval, it does not skip a same-repository head. It does not require
`action_required`, `run_attempt: 1`, or a particular run status because a later
job may wait on an environment while the run is `in_progress`.

The guard rechecks the pull, trusted configuration, A38 assessment, author
report, migration approval, latest workflow run, run identity, and pending
deployment list immediately before each write. It calls GitHub's GET and POST
`/repos/{repo}/actions/runs/{run_id}/pending_deployments` endpoints and accepts
only HTTP 200 from the POST. It still never dispatches or reruns workflows,
merges, or submits pull-request review approvals, and this feature never calls
the fork workflow-run `/approve` or `/cancel` endpoints.

The token must belong to a required reviewer of the configured environment
and have Actions write. `GITHUB_TOKEN` acts as `github-actions[bot]` and
cannot approve unless that bot is explicitly listed as an environment
reviewer. A successful approval uses the
existing visible `PR-GUARD:CI-AUTH:v1` EN/DE audit comment; the first approval in
one guard invocation posts a new comment and later approvals in that invocation
update it. Assessment JSON includes `environment_approvals`, and each successful
write adds `environment:approve:<run-id>` to `writes`. `--dry-run` reports
`planned` approvals without POSTs or audit comments.


## Optional continuous readiness

The same trusted repository configuration can enable CI and conflict monitoring:

```json
"lifecycle": {
  "enabled": true,
  "auto_ready": true,
  "required_workflows": [".github/workflows/ci.yml"],
  "ignored_workflows": [".github/workflows/pr-guard.yml"],
  "required_checks": {".github/workflows/ci.yml": ["Test"]},
  "conditional_workflows": [
    {
      "workflow": ".github/workflows/security.yml",
      "base_branches": ["release"],
      "labels_any": ["full-ci"]
    }
  ]
}
```

All fields except `conditional_workflows` and `required_checks` are required when `lifecycle` is
present. Omission disables lifecycle writes. `auto_ready` requires enabled
workflow approval and a nonempty required-workflow list. Authorization rows
for ignored or otherwise absent workflows are skipped; they must not block
Ready after the live inventory dropped them. Workflow paths are
exact, bounded and unique across these lists. A conditional workflow is required
when its target branch **or** any listed PR label matches; branches and labels
are exact, case-sensitive strings. Its condition must not be empty. This is
repository configuration, including which CI is expected for release PRs.
`required_checks` maps required or conditional workflow paths to exact job check
names (including expanded matrix names). A reusable-workflow check
`{caller} / {called}` satisfies a listed caller name when it starts with that
name plus ` / `. When several check runs match one listed name, each distinct
matched name in that suite must finish successfully (latest run per name), so a
successful sibling cannot hide a skipped nested job. When that workflow is
required, each listed check must actually finish successfully in its latest
check suite.
Use this for workflows whose setup job can succeed while the test jobs skip;
an overall workflow success must not hide a missing or skipped required test.
The only exceptions are: the PR file inventory is independently README-only
(exactly `README.md` or a path that ends with `/README.md`, case-sensitive,
fail-closed) **or** independently markdown-only (every path ends with `.md`,
case-sensitive, same fail-closed inventory rules) **or** independently
confirmed guard-docs (every path is a markdown file and/or exactly
`.github/workflows/a38-guard.yml`, same fail-closed inventory rules);
**or** a verified author A38 report on this head has a passing job whose
name matches that required check (so a draft that skips GitHub E2E by design
stays Ready-eligible when local E2E already passed). Then a completed
required check may conclude `success`, `skipped`, or `neutral`. `cancelled`
and failed GitHub required checks still block. Missing required checks still
block. The guard does not trust a report's `readme_only` /
`markdown_only` flags or `not_applicable` results without independently listing
the pull request files. There is no guard-docs report flag; confirmation is
inventory-only.

For **every open Ready PR targeting an A38-enforced branch**, confirmed merge
conflicts or CI that is missing, queued, waiting, running, blocked, cancelled
or failed cause a Draft transition. A write collaborator holds Ready through
missing or red CI only: the PR author currently has `write`/`maintain`/`admin`
on the target, or the latest human `ready_for_review` timeline actor does.
Confirmed merge conflicts always return Ready to Draft, including while that
write hold would otherwise apply. That hold skips auto-draft for CI only; it
does not waive policy, workflow inventory, or migration failures, and it does
not skip auto-ready when A38 is already a fresh enforce `pass`. Restore after
an auto-draft requires GitHub `mergeable` true and no conflicts (CI may still
be red). Lifecycle Draft/Ready writes run only on A38-enforced targets; excluded
bases (for example a develop→main release PR) are left untouched. Missing
required workflows are not an empty green result.
Only completed, successful required workflows satisfy CI. Optional workflows
that intentionally skip are not counted as successful required tests. Pending
or failed independent check runs and commit statuses also block Ready. A
skipped check whose GitHub name still contains an unevaluated `${{`
expression is a matrix placeholder (the job-level `if:` never ran), not a
test result, and does not block — including when that placeholder is nested
under a reusable-workflow prefix that `required_checks` matches. Optional
skipped or neutral jobs that are not listed in `required_checks` do not
block. The newest workflow run
supersedes historical results; both workflow inventories and checks are
inspected, including approval-blocked runs absent from GitHub's rollup.

Ignore only repository control workflows that are not product CI, particularly
the guard itself: otherwise its in-progress check would always prevent Ready.
Required, conditional and ignored workflow paths cannot overlap. Conditional
jobs skipped inside a successful workflow do not make that workflow fail.

Automatic Ready requires all required CI green, GitHub `mergeable: true`, and a
fresh enforced A38 pass. When this bot authorized held runs, the latest
bot-owned AUTH record on the current head/base must still name those latest
runs. When no bot-owned AUTH row exists and required CI is already green
(nothing was held), auto-ready still performs leave-draft (`isDraft=false`).
A present but mismatched bot-owned AUTH row still blocks. An unknown merge status neither invents a
conflict nor permits Ready. This feature changes readiness only; it creates no
review approvals and never bypasses review requirements, branch protection or
human merge.

The first successful approve or cancel in a guard invocation POSTs a new visible
EN/DE comment (`PR-GUARD:CI-AUTH:v1` / `PR-GUARD:CI-CANCEL:v1`). Further
successful same-kind mutations in that same invocation PATCH that comment. A
later invocation POSTs a new comment; it must not PATCH an older one (GitHub
PATCH does not move the comment in the timeline). Ready/Draft still POST a new
`PR-GUARD:LIFECYCLE:v1` comment per transition. Each Ready/Draft transition names
the blockers that actually apply (never an "or" between CI and merge conflicts);
the full reason list remains in collapsed details. A durable intent is written
before the mutation and updated after success; the next scan repairs the comment
if that update was interrupted. Unchanged readiness creates no duplicate comment.
Convert-to-draft that GitHub accepts without GraphQL errors but leaves `isDraft`
false is not API denial: the planned record is closed as applied Ready, readiness
stays unchanged, and the EN/DE comment says the Draft conversion did not take
effect. HTTP 409 on cancel does not comment. Approve and cancel comments are
posted even when lifecycle is disabled. Auto-ready uses the latest bot-owned
AUTH record (highest comment id) on the current head/base when that row exists.
Only the authenticated bot's numeric user ID can supply these records. When no
such row exists and required CI is green, auto-ready still performs
leave-draft (`isDraft=false`). A present but mismatched bot-owned AUTH
row still blocks.
`--dry-run` still writes no audit comments.

The adopting workflow owns runner routing, `contents: write` for
`markPullRequestReadyForReview`, `actions` and `checks` read access,
`pull-requests`/`issues`/`statuses` write access, and `actions: write` for initial
workflow approval. The guard authorizes waiting allowlisted fork runs **before**
it mutates Ready or Draft, so a failed convert-to-draft cannot skip approval.
Serialize event-driven single-PR runs and all-open sweeps in two
repository-wide concurrency groups (`event` versus `sweep`) with
`cancel-in-progress: false`, so PR events cannot starve all-open reconciliation.
The `sweep` group is `schedule` or `workflow_dispatch` with `all_open`.
Sweep jobs may use a 45-minute timeout; event-driven single-PR jobs may use 10 minutes. Run trusted
`--all-open` reconciliation on a repository-configured schedule (for example every
five minutes). GitHub may delay scheduled execution; this is not a real-time SLA.
Privileged runs must never check out PR code. Bot readiness must not be wired to
automatic test retries; with `GITHUB_TOKEN`, the bot's own readiness/comment
events do not trigger new workflow runs. Other token integrations must ensure
their Ready handlers do not repeat already-requested CI.

Head/base, configuration, evidence and CI are refreshed before promotion.
GitHub does not offer an atomic CI-and-readiness transaction; subsequent changes
are corrected by the next reconciliation. A head/base change returned by the
Ready mutation immediately restores Draft. If that restore does not change
`isDraft`, the scan fails explicitly. After GraphQL reports success, the guard
re-reads the REST `draft` field and fails closed if it is missing, non-boolean,
or still the old value; GraphQL HTTP 200 with matching `isDraft` is not enough
to record the transition as applied. API denial fails explicitly and is
not a successful transition. Neither this code nor its configuration is active
until the trusted installation is deployed.

## Author report

The author processes the full local job list from a clean checkout of the exact head. When every changed path ends with `.md` (case-sensitive) and the inventory is complete and trustworthy, the local suite is skipped entirely and no author report is required: the guard independently confirms the GitHub file list and waives the report gate with reason `markdown-only change set` (status pass, same author-report waiver path as write-ready). When every changed path is a markdown file and/or exactly `.github/workflows/a38-guard.yml` (guard-docs) under the same fail-closed inventory rules, the local suite is likewise skipped without `markdown_only: true` and no author report is required: the guard waives the report gate with reason `guard-docs change set` and does not fail that guard workflow file's bytes-changed policy line. Other workflow paths still need maintainer policy approval. That full skip is distinct from optional policy `readme_only.omit_jobs`, which remains a subset omission for README.md-only change sets. Authorized README-only subset omissions and markdown-only full omissions are recorded as `not_applicable` without executing those jobs. Guard-docs full omissions use log text `omitted: guard-docs change set`. Empty inventories, truncated lists, unknown statuses, renames involving a non-`.md` path other than `.github/workflows/a38-guard.yml`, more than 500 files, or API/git errors are not markdown-only or guard-docs. Keep the policy copy, report and logs outside the checkout:

```sh
agent a38 run --repo . --repository OWNER/NAME \
  --policy /tmp/a38-policy.json --base-sha BASE_COMMIT_SHA \
  --output /tmp/a38-report.md --logs-dir /tmp/a38-logs
```

`--repository` identifies the target repository, especially when the checkout origin is a fork. Post the complete generated report as a PR comment using the **PR author's account**. Preserve its JSON and markers. The existing local-CI wire schema remains unchanged for compatibility.

For the private opt-in process, the checkout must be clean at the final repository-required signed commit before Ready measurement. The draft may already exist under the [pull request lifecycle](pull-request-lifecycle.md). When an author report is produced, run the active policy (executing non-omitted jobs; recording authorized README-only subset omissions and markdown-only full omissions as `not_applicable` with the `readme_only` / `markdown_only` flags) and locally verify it before recording `local_check_pass` evidence; that verified SHA must be on the open draft with no intervening commit after measurement. Independently confirmed markdown-only or guard-docs change sets need no author report for the report gate. Guard-docs Ready uses independent file inventory only; authors must not record or verify a guard-docs local report. There is no guard-docs report flag; confirmation is inventory-only. Any fix, amend, or rebase creates a new SHA and requires the complete run and verification again. Execution roles follow the repository's orchestration rules; reviewers remain read-only. Job adapter commands are catalogued in [A38 job adapters](a38-job-adapters.md), without duplicating their schemas here.

The latest author report-like comment, ordered by `updated_at` and numeric comment ID, is authoritative. A newer malformed or failed report never falls back to an older success. Other authors' reports cannot satisfy the requirement. Matching repository, head, visibility, full job set, names, commands, timeouts and successful measured results are mandatory, including for public repositories.

**Author-report Ready waivers:** When the PR author is a GitHub `User` who currently has `write`, `maintain`, or `admin` on the target repository, or when a GitHub `User` with one of those roles is the latest `ready_for_review` timeline actor **or** the `ready_for_review` webhook sender, the author-report gate is waived and enforce status may succeed with an explicit waiver description. Independently confirmed markdown-only change sets (every changed path ends with `.md`, fail-closed) also waive the author-report gate with reason `markdown-only change set`, reusing the same status-pass path so fork workflow approval and auto-ready can proceed without a local suite. Independently confirmed guard-docs change sets (markdown files and/or `.github/workflows/a38-guard.yml`, fail-closed) waive the same report gate with reason `guard-docs change set` and do not fail the `.github/workflows/a38-guard.yml` bytes-changed policy line; other workflow paths still need maintainer policy approval. Draft does not skip the Ready-actor path. A valid passing author report still takes the normal “report accepted” path when present. Only `User` actors can grant the write waiver (allowlist); bots and apps cannot, even with write/admin. `MEMBER` / association is not write; 404 or denied permission lookups do not grant it; timeline pagination 401/403/404 yields no waiver and does not crash assessment. Invalid policy, unclassified workflows, and pr-guard migration failures are not waived by write access or by markdown-only or guard-docs detection, except that guard-docs skips the bytes-changed line for `.github/workflows/a38-guard.yml` only. No-write authors who mark Ready without a valid report still fail and are still auto-drafted by lifecycle unless the markdown-only or guard-docs waiver applies; lifecycle does not override Ready back to Draft while the write hold applies for missing or red CI, confirmed merge conflicts still draft, and it restores Ready after an auto-draft if the Ready actor still has write, GitHub `mergeable` is true, and there are no conflicts.

## Statuses and events

| Mode | Stable status context | Meaning |
| --- | --- | --- |
| `enforce` | `A38 / report (develop)` for target branch `develop` | On a draft PR without `hard_fail` that is independently confirmed markdown-only (every changed path ends with `.md`, fail-closed): post enforce **success** (`pass: markdown-only change set; A38 report not required`) and the markdown-only waiver comment. Independently confirmed guard-docs drafts (markdown files and/or `.github/workflows/a38-guard.yml`, fail-closed, no `hard_fail`) post enforce **success** (`pass: guard-docs change set; A38 report not required`) and the guard-docs waiver comment. Other drafts omit the **blocking** commit status (not failure, not pending, not a fabricated pass); see the `not_applicable` success-clear carve-out below, and the leftover success-clear when a prior `hard_fail:` failure remains on the same head (that leftover clear is for drafts that are not markdown-only or guard-docs). The guard process exits 0 so `dfx pr guard` is not red merely for a missing draft report. An author report is still required before Ready **unless** a report waiver applies: write collaborator (author is a GitHub `User` with `write`/`maintain`/`admin` on the target), independently confirmed markdown-only, or independently confirmed guard-docs. Markdown-only and guard-docs do not hold Ready through red CI. Once Ready (`draft=false`): success for a valid author report, or for a report waiver; otherwise failure. Non-`User` actors, association strings, denied/404 permission lookups, and timeline 401/403/404 do not grant the write waiver. Policy/workflow/migration failures are never waived, except that guard-docs skips the bytes-changed line for `.github/workflows/a38-guard.yml` only. Separately, tool-attribution in the PR title, PR body, or a commit (`generated-with` banner, AI `Co-Authored-By` trailer, AI session header, AI author identity) sets `hard_fail` and exits 1 even on draft; an unscannable or missing commit message is fail-closed the same way. On `hard_fail` the blocking `A38 / report (<target>)` status is posted as `failure` on the PR head even on draft. |
| `observe` | `A38 / report (observe: develop)` | Advisory status only; do not require this context for merging. Unchanged on drafts. Tool-attribution and unscannable/missing commit-message `hard_fail` still exit 1. |

Configured `not_applicable` exclusions still publish success on the target enforce context to clear a wrong prior status, including on drafts; that success is not a test-pass claim.

Contexts use the **target branch name**, not the moving base SHA. Thus branch protection can require a stable name while a head targeting different branches gets distinct contexts. Supported branch names are bounded to 75 ASCII letters/digits, dots, underscores, hyphens and slashes; unsupported names fail closed. The exact base SHA remains in the comment and approval binding.

After exercising missing, valid, failed, edited, deleted and stale reports, configure branch protection to require the enforced context for each target branch. The JSON verdict remains false for an invalid report even in advisory mode. This gate does not remove other required checks or human merge rules.

The local-code-gate equivalence is a private-only opt-in: the target must actually be private and its trusted base must contain a valid A38 manifest. Public adopters and repositories without that private opt-in keep their existing cumulative GitHub CI expectations. Independently required GitHub-only checks always remain gates, and the guard cannot bypass technical GitHub merge restrictions.

Immediately before Ready in the private opt-in path, run a separate live `agent pr-guard --repo OWNER/NAME --pr N --dry-run --json` with the token in `GH_TOKEN` or `GITHUB_TOKEN`. Validate `ok: true`, `status: "pass"`, `closed: false`, `private: true`, `dry_run: true`, the exact target `repo` and `pr`, refreshed current `head` and `base`, and the expected active `policy_revision`. Exit zero or `state: "success"` alone is insufficient. API/configuration failure, malformed or stale output, or a missing field blocks. Repeat after changes to head, base, title, body, the latest author comment, or approval.

In base `enforce` mode, the actual stable `A38 / report (<target-branch>)` context must be successful on the current head. The observe context is advisory, but the live-valid author report still supplies private code-gate equivalence. Neither mode removes independent review, human merge, required GitHub-only checks, or platform merge controls.

Supported events:

- `pull_request_target`: opened, reopened, synchronize, edited, ready_for_review.
- `issue_comment`: created, edited, deleted, for PRs only.
- Scheduled all-open reconciliation on the trusted default branch; cadence is repository configuration.
- `workflow_dispatch`: an explicit repository and PR number, or `all_open=true` reconciliation.

Issue-only events and the bot's own comments are ignored. The installed workflow deliberately has no `pull_request_review` trigger because that event loads workflow code from PR context. After approving or dismissing a policy review, post a normal PR comment such as `A38 recheck` for immediate reassessment, or dispatch the default-branch workflow. Scheduled reconciliation catches other review/base changes. The CLI can consume submitted/edited/dismissed review events supplied by an external trusted event handler, but never grant elevated credentials to PR-context workflow code. Never check out the PR head in a privileged bot job.

## Publication and failures

Closed PRs return `status: closed` and process exit zero without reading policy, pr-guard configuration or publishing comments/statuses, including when a PR closes during an all-open scan. Ignored events and empty all-open scans are also successful no-ops.

On an open **draft** in `enforce` mode the guard still publishes or updates its educational comment. Independently confirmed markdown-only drafts (every changed path ends with `.md`, fail-closed, no `hard_fail`) skip the short greeting and use the Ready markdown-only waiver comment (author local-CI report not required because every changed path is a markdown file; the details block records that the report is optional for this markdown-only waiver). Independently confirmed guard-docs drafts (markdown files and/or `.github/workflows/a38-guard.yml`, fail-closed, no `hard_fail`) skip the short greeting and use the Ready guard-docs waiver comment (author local-CI report not required because the change set is markdown files and/or `.github/workflows/a38-guard.yml`; the details block records that the report is optional for this guard-docs waiver). They also post enforce **success** (`pass: markdown-only change set; A38 report not required` or `pass: guard-docs change set; A38 report not required`) on `A38 / report (<target>)` so fork workflow approval and auto-ready are not deadlocked on Ready. Other drafts keep a friendly greeting only that uses a Markdown link whose text is the rules name and whose target is `standard_url`, not a raw URL in the visible sentence; no draft-status lecture, no missing-report or tool-attribution lecture, no write-waiver lecture, and no details block (jobs, problems, run command). Current problems and run instructions appear in the comment once the pull request is Ready (`draft=false`), except for those markdown-only and guard-docs draft waiver comments. The guard does **not** create or update a blocking `A38 / report (<target>)` commit status on a draft that is neither markdown-only nor guard-docs unless `hard_fail` (and does not post an invalidating `error` status on draft). Configured `not_applicable` exclusions may still write success on that context only to clear a wrong prior status; that is not a test-pass claim. Process exit is 0 so `dfx pr guard` is not red merely because a draft lacks an author report. Ready (`draft=false`) publishes success for valid author-report evidence or for a report waiver (write collaborator, independently confirmed markdown-only, or independently confirmed guard-docs); missing or invalid evidence without a waiver is failure. Markdown-only and guard-docs do not hold Ready through red CI. Independently, the guard scans the live PR title, PR body, and every PR commit message plus author/committer identity (not the diff, not comments). A truncated GitHub pull-commits list (the endpoint caps at 250) is fail-closed. A `generated-with` banner, AI `Co-Authored-By` trailer, AI session header, or AI author identity fails the assessment with `hard_fail`; an unscannable or missing commit message is fail-closed the same way (`hard_fail`, exit 1 even on draft/observe). `dfx pr guard` then exits 1 even on a draft and even in observe mode. On Ready the comment tells authors to remove attribution markers, supply a non-empty commit message, or both. On `hard_fail` the guard posts `failure` on that context against the PR head even on a draft, so the pull request Checks box is red. The draft exemption (omit the blocking status, exit 0) applies only when there is no `hard_fail` and the draft is not independently confirmed markdown-only or guard-docs. If a prior `hard_fail` left `failure` whose description starts with `hard_fail:` and the next draft reconcile is no longer `hard_fail`, the guard writes success on that context only to clear the stale red for drafts that are not markdown-only or guard-docs; that is not a test-pass claim. Other enforce failures on the same head stay.

The bot marker is `<!-- PR-GUARD:A38:v1 -->`. Only comments owned by the numeric acting user may be updated. `/user` resolves normal tokens; fallback to the verified official Actions bot is allowed only when `GITHUB_ACTIONS=true`. Failed authentication outside Actions does not impersonate that bot. Existing identical comments/statuses are not reposted.

Before publication, the guard re-fetches head/base/branch/state/title/body, the trusted pr-guard configuration revision and bytes, the latest author report and any active migration approval. It checks again immediately before a success status and reassesses if evidence changed. GitHub offers no atomic transaction across comments, reviews and statuses: an edit after the final read is corrected by the next event or scheduled reconciliation.

API or assessment errors terminate with failure. If the head is known and status writes remain available, the guard posts an `error` status to invalidate prior success. If GitHub denies or cannot perform that write, the CLI explicitly reports that invalidation failed; an old remote status may remain until a successful reconcile. Treat the failed guard run as an operational failure and rerun before merging. No implementation can invalidate remote state during a complete API outage.

An all-open scan isolates errors per PR, continues reconciling later PRs, and returns aggregate failure after the full scan. Isolation covers any per-PR exception, including workflow-approval GuardError (fork head does not contain current base), not only inaccessible PRs. Its JSON includes an error entry for each failed PR, so one inaccessible PR cannot prevent other statuses from being refreshed.

The issue timeline for the Ready actor is walked newest-first and stops at the newest GitHub `User` `ready_for_review` event, so old PRs do not download thousands of older timeline events. The composite action runs the guard unbuffered; `--all-open` prints per-PR progress on stderr so GitHub Actions logs update during a long sweep.

HTTP is restricted to `https://api.github.com`, redirects are refused, and safe GET retries are bounded. Comment/review pagination is complete up to its explicit 2000-item limit, with cycle/page limits; exceeding a bound fails instead of accepting partial evidence.

## CLI

```sh
agent pr-guard --repo OWNER/NAME --pr N --dry-run
agent pr-guard --repo OWNER/NAME --pr N
python -m agent_cli.a38_guard reconcile --event-file PATH --event-name NAME
python -m agent_cli.a38_guard reconcile --repo OWNER/NAME --all-open
python -m agent_cli.a38_guard publish --repo OWNER/NAME --pr N --assessment-file FILE
```

Event flags default to `GITHUB_EVENT_PATH` and `GITHUB_EVENT_NAME`. `--dry-run` performs reads and prints prospective JSON without mutations. `publish` re-assesses live evidence rather than trusting a previously saved verdict. Ordinary event runs publish using the configured token.

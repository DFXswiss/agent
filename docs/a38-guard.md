# dfx pr guard

dfx pr guard explains a repository's centrally defined [A38 rules](a38.md), checks the latest author report and maintains one friendly English/German comment. Adopters keep only their manifest, integration, and contributing pointer; this guide is not copied into consumer repositories. The guard never checks out or executes pull-request code. A valid report is a consistent author declaration, not cryptographic proof of execution.

## Installation

Install the [example workflow](../examples/a38-guard.yml) on the target repository's default branch. Replace `USES_REF_PIN_ME` with a reviewed, published **full commit SHA** of this repository. The example is not deployable until that placeholder is replaced. Keep the guard's executable action pinned even when approving policy migrations.

The [composite action](../.github/actions/a38-guard/action.yml) uses pinned setup-python and PyYAML 6.0.2, and imports only the trusted action's sources through `github.action_path/../../../src`. Both Python steps run from the trusted action directory with safe-path mode (`python -P`), and replace inherited `PYTHONPATH` with the trusted source path, preventing consumer modules from shadowing the guard or its installer. It does not install dependencies or run scripts from the consumer checkout. Install the package's declared dependencies for standalone use; there is no fallback YAML parser.

The guard's comment and JSON expose three different immutable links:

- `standard_url` is `DFXswiss/agent` `docs/a38.md` at the exact trusted guard runtime SHA.
- `guard_docs_url` is this guide at that same runtime SHA.
- `policy_url` is the consumer `.github/a38.json` at the active base SHA or exact approved head SHA, including the head repository for an approved fork migration.

`policy_url` is derived assessment output only. It is not an `a38/v1` manifest input or schema field. The fixed `documentation: docs/a38.md` token identifies the central standard and does not assert that the consumer has that file. Central documentation links never use the consumer head, consumer base, active policy revision, or a moving branch.

For the composite action, `A38_RUNTIME_REVISION` is overwritten from `${{ github.action_ref }}` on the guard step. Composite context values must be passed through `env`, as documented by [GitHub's contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts). The value must be a lowercase 40-hex commit SHA; a branch or moving tag fails clearly. `github.sha` is not suitable because it identifies consumer workflow context.

Standalone execution accepts the same explicit trusted `A38_RUNTIME_REVISION`. Without it, source-checkout fallback is allowed only when the loaded module is exactly `<root>/src/agent_cli/a38_guard.py`, `<root>/.git` belongs to that root, Git reports the same top-level using explicit `--git-dir` and `--work-tree`, and `HEAD` is lowercase 40-hex. The lookup is anchored to the module source root, removes inherited `GIT_*` variables, and never discovers from the current directory or an enclosing consumer checkout. A non-Git packaged install requires the explicit trusted revision; it never guesses `develop` or another moving ref. Closed PRs, ignored events, and empty all-open scans remain successful no-ops and do not need provenance resolution.

The token requires contents read, pull requests read, issues write and statuses write. Policy migrations also require permission to read collaborators' effective repository permissions. If that API is unavailable, the migration fails closed. Use a dedicated GitHub App or service account with the necessary repository access for external operation. Tokens are taken from `GH_TOKEN` or `GITHUB_TOKEN` and never printed.

Actions must actually be available for event-driven operation. When Actions are blocked or unavailable, run the same reconciler on a trusted external host:

```sh
agent pr-guard --repo OWNER/NAME --all-open --dry-run
agent pr-guard --repo OWNER/NAME --all-open
```

Schedule that command externally when Actions are unavailable; no daemon is installed. The example workflow also reconciles all open PRs at minutes 17 and 47 of every hour and serializes all bot runs for the repository. Its manual dispatch accepts either a PR number or `all_open=true`. GitHub Actions does not guarantee delivery of every pending concurrency event, so scheduled reconciliation recovers missed events, base changes and permission changes. Immutable SHA-addressed contents and trees are cached within the API client, up to 128 entries; comments, reviews, permissions and PR snapshots are never cached.

## Trust and policy

The authoritative PR API supplies the target repository, exact head SHA, exact base SHA, target branch, actual boolean visibility and numeric author ID. For forks, head workflow files come from the head repository; base policy comes from the target repository. Missing repository identity or visibility fails closed.

By default, policy is `.github/a38.json` from the immutable **base SHA**. Every workflow job at the head must be classified exactly once as required or explicitly excluded; classifications for absent jobs fail as well. Matrix profiles must run every required variant. The guard does not infer or execute matrix expressions. Added, removed or changed workflow bytes require the explicit policy migration described below.

Policy JSON uses the strict `a38/v1` schema. Workflow YAML is safely loaded with duplicate-key detection, no aliases and bounded nesting. Files are limited to 1 MiB and API responses to 16 MiB. Unsupported YAML fails closed; use explicit mappings instead of aliases in adopted workflow files. Missing or invalid policy is an error when the guard is installed or invoked, never a successful empty check.

## Approving a policy migration

A policy update must not authorize itself. A maintainer can explicitly authorize using the proposed **head manifest as data** by submitting an APPROVED GitHub review with this exact line, replacing both SHAs:

```text
A38-POLICY-APPROVAL:v1 head=<HEAD_SHA> base=<BASE_SHA>
```

The review's GitHub `commit_id` must equal the current head. The reviewer must differ from the PR author by numeric account ID and currently have write, maintain or admin permission on the target repository. The permission response must confirm the same numeric identity. A normal approval without the line does not authorize migration.

Bot/app identities and non-collaborators are ineligible: their reviews neither authorize migrations nor invalidate ordinary reports. A missing collaborator permission record (404) is ignored; authentication/authorization failures (401/403), other API errors and mismatched numeric identities remain errors.

For each reviewer, the latest substantive submitted state controls authorization. Dismissed or superseded approvals do not count; ordinary comments and pending drafts do not change an approval. A current-head changes request from an eligible maintainer blocks the migration exception. New head or base SHAs require renewed explicit approval.

An authorized migration may introduce, remove or change workflows, but the complete current head inventory and author report still must satisfy the approved head policy. The executable guard remains pinned and never runs head commands. The **base policy's enforcement mode stays active** for this PR, even if the proposed mode is `observe`. If no valid base policy exists, explicit approval permits bootstrap under `enforce`; initial adoption cannot silently bypass reporting.

An unpublished migration proposal is first measured and verified against the still-active base policy and pushed at that same commit; that run is proposal publication evidence, not Ready evidence. Once the exact current head/base approval exists, rerun the full approved head policy and local verification, publish that newly generated report, and perform the live join. Bootstrap with a missing or invalid base policy instead follows the repository's existing pre-push checks to publish the proposal, then requires explicit approval and `enforce`; it must not invent a report or waive approval.

The bot identifies the active policy revision in its comment. Download `.github/a38.json` from that exact revision before generating the report. For ordinary PRs, this is the base; for explicitly approved migrations, it is the head.

## Author report

The author runs the full local job list from a clean checkout of the exact head. Keep the policy copy, report and logs outside the checkout:

```sh
agent a38 run --repo . --repository OWNER/NAME \
  --policy /tmp/a38-policy.json --base-sha BASE_COMMIT_SHA \
  --output /tmp/a38-report.md --logs-dir /tmp/a38-logs
```

`--repository` identifies the target repository, especially when the checkout origin is a fork. Post the complete generated report as a PR comment using the **PR author's account**. Preserve its JSON and markers. The existing local-CI wire schema remains unchanged for compatibility.

For the private opt-in process, the checkout must be clean at the final repository-required signed commit before measurement. Run the full active policy and locally verify it before push, record that verified report as `local_check_pass` evidence, then push the same SHA without an intervening commit. Any fix, amend, or rebase creates a new SHA and requires the complete run and verification again. Execution roles follow the repository's orchestration rules; reviewers remain read-only. Job adapter commands are catalogued in [A38 job adapters](a38-job-adapters.md), without duplicating their schemas here.

The latest author report-like comment, ordered by `updated_at` and numeric comment ID, is authoritative. A newer malformed or failed report never falls back to an older success. Other authors' reports cannot satisfy the requirement. Matching repository, head, visibility, full job set, names, commands, timeouts and successful measured results are mandatory, including for public repositories.

## Statuses and events

| Mode | Stable status context | Meaning |
| --- | --- | --- |
| `enforce` | `A38 / report (develop)` for target branch `develop` | Success only for valid evidence; otherwise failure. |
| `observe` | `A38 / report (observe: develop)` | Advisory status only; do not require this context for merging. |

Contexts use the **target branch name**, not the moving base SHA. Thus branch protection can require a stable name while a head targeting different branches gets distinct contexts. Supported branch names are bounded to 75 ASCII letters/digits, dots, underscores, hyphens and slashes; unsupported names fail closed. The exact base SHA remains in the comment and approval binding.

After exercising missing, valid, failed, edited, deleted and stale reports, configure branch protection to require the enforced context for each target branch. The JSON verdict remains false for an invalid report even in advisory mode. This gate does not remove other required checks or human merge rules.

The local-code-gate equivalence is a private-only opt-in: the target must actually be private and its trusted base must contain a valid A38 manifest. Public adopters and repositories without that private opt-in keep their existing cumulative GitHub CI expectations. Independently required GitHub-only checks always remain gates, and the guard cannot bypass technical GitHub merge restrictions.

Immediately before Ready in the private opt-in path, run a separate live `agent pr-guard --repo OWNER/NAME --pr N --dry-run --json` with the token in `GH_TOKEN` or `GITHUB_TOKEN`. Validate `ok: true`, `status: "pass"`, `closed: false`, `private: true`, `dry_run: true`, the exact target `repo` and `pr`, refreshed current `head` and `base`, and the expected active `policy_revision`. Exit zero or `state: "success"` alone is insufficient. API/configuration failure, malformed or stale output, or a missing field blocks. Repeat after changes to head, base, the latest author comment, or approval.

In base `enforce` mode, the actual stable `A38 / report (<target-branch>)` context must be successful on the current head. The observe context is advisory, but the live-valid author report still supplies private code-gate equivalence. Neither mode removes independent review, human merge, required GitHub-only checks, or platform merge controls.

Supported events:

- `pull_request_target`: opened, reopened, synchronize, edited, ready_for_review.
- `issue_comment`: created, edited, deleted, for PRs only.
- Scheduled all-open reconciliation every 30 minutes on the trusted default branch.
- `workflow_dispatch`: an explicit repository and PR number.

Issue-only events and the bot's own comments are ignored. The installed workflow deliberately has no `pull_request_review` trigger because that event loads workflow code from PR context. After approving or dismissing a policy review, post a normal PR comment such as `A38 recheck` for immediate reassessment, or dispatch the default-branch workflow. Scheduled reconciliation catches other review/base changes. The CLI can consume submitted/edited/dismissed review events supplied by an external trusted event handler, but never grant elevated credentials to PR-context workflow code. Never check out the PR head in a privileged bot job.

## Publication and failures

Closed PRs return `status: closed` and process exit zero without reading policy or publishing comments/statuses, including when a PR closes during an all-open scan. Ignored events and empty all-open scans are also successful no-ops.

The bot marker is `<!-- PR-GUARD:A38:v1 -->`. Only comments owned by the numeric acting user may be updated. `/user` resolves normal tokens; fallback to the verified official Actions bot is allowed only when `GITHUB_ACTIONS=true`. Failed authentication outside Actions does not impersonate that bot. Existing identical comments/statuses are not reposted.

Before publication, the guard re-fetches head/base/branch/state, the latest author report and any active migration approval. It checks again immediately before a success status and reassesses if evidence changed. GitHub offers no atomic transaction across comments, reviews and statuses: an edit after the final read is corrected by the next event or scheduled reconciliation.

API or assessment errors terminate with failure. If the head is known and status writes remain available, the guard posts an `error` status to invalidate prior success. If GitHub denies or cannot perform that write, the CLI explicitly reports that invalidation failed; an old remote status may remain until a successful reconcile. Treat the failed guard run as an operational failure and rerun before merging. No implementation can invalidate remote state during a complete API outage.

An all-open scan isolates errors per PR, continues reconciling later PRs, and returns aggregate failure after the full scan. Its JSON includes an error entry for each failed PR, so one inaccessible PR cannot prevent other statuses from being refreshed.

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

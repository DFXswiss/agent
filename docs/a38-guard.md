# dfx pr guard

dfx pr guard explains a repository's [A38 rules](a38.md), checks the latest author report and maintains one friendly English/German comment. It never checks out or executes pull-request code. A valid report is a consistent author declaration, not cryptographic proof of execution.

## Installation

Install the [example workflow](../examples/a38-guard.yml) on the target repository's default branch. Replace `USES_REF_PIN_ME` with a reviewed, published **full commit SHA** of this repository. The example is not deployable until that placeholder is replaced. Keep the guard's executable action pinned even when approving policy migrations.

The [composite action](../.github/actions/a38-guard/action.yml) uses pinned setup-python and PyYAML 6.0.2, and imports only the trusted action's sources through `github.action_path/../../../src`. Both Python steps run from the trusted action directory with safe-path mode (`python -P`), and replace inherited `PYTHONPATH` with the trusted source path, preventing consumer modules from shadowing the guard or its installer. It does not install dependencies or run scripts from the consumer checkout. Install the package's declared dependencies for standalone use; there is no fallback YAML parser.

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

The bot identifies the active policy revision in its comment. Download `.github/a38.json` from that exact revision before generating the report. For ordinary PRs, this is the base; for explicitly approved migrations, it is the head.

## Author report

The author runs the full local job list from a clean checkout of the exact head. Keep the policy copy, report and logs outside the checkout:

```sh
agent a38 run --repo . --repository OWNER/NAME \
  --policy /tmp/a38-policy.json --base-sha BASE_COMMIT_SHA \
  --output /tmp/a38-report.md --logs-dir /tmp/a38-logs
```

`--repository` identifies the target repository, especially when the checkout origin is a fork. Post the complete generated report as a PR comment using the **PR author's account**. Preserve its JSON and markers. The existing local-CI wire schema remains unchanged for compatibility.

The latest author report-like comment, ordered by `updated_at` and numeric comment ID, is authoritative. A newer malformed or failed report never falls back to an older success. Other authors' reports cannot satisfy the requirement. Matching repository, head, visibility, full job set, names, commands, timeouts and successful measured results are mandatory, including for public repositories.

## Statuses and events

| Mode | Stable status context | Meaning |
| --- | --- | --- |
| `enforce` | `A38 / report (develop)` for target branch `develop` | Success only for valid evidence; otherwise failure. |
| `observe` | `A38 / report (observe: develop)` | Advisory status only; do not require this context for merging. |

Contexts use the **target branch name**, not the moving base SHA. Thus branch protection can require a stable name while a head targeting different branches gets distinct contexts. Supported branch names are bounded to 75 ASCII letters/digits, dots, underscores, hyphens and slashes; unsupported names fail closed. The exact base SHA remains in the comment and approval binding.

After exercising missing, valid, failed, edited, deleted and stale reports, configure branch protection to require the enforced context for each target branch. The JSON verdict remains false for an invalid report even in advisory mode. This gate does not remove other required checks or human merge rules.

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

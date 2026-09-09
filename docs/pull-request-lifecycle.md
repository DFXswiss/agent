# Pull request lifecycle

This document is the **canonical** central rule for when a draft pull request is published, what may wait until after publication, and what Ready for review / completion still requires. Entrypoints in this repository link here. Tool plugins add only a short pointer; they do not redefine the standard.

Session-specific review waivers are not a global default.

## Status terms (canonical)

Use these terms only. Do not call an earlier stage finished, done, or completed.

| Term | Meaning |
|---|---|
| **Draft** | Open GitHub pull request with `isDraft=true`. Never finished, done, or completed — even when tests pass. |
| **Ready for review** | Leave-draft transition (`gh pr ready` / `isDraft=false`) after all required checks and reviews on the exact clean signed final head. Still not merged and still not completed. |
| **Merged / completed** | Only after a **verified human merge**. Ledger `task-done`, checklist closes, spine states, and Ready for review are **not** proof of pull-request completion. |

A draft plus local tests is still **not** Ready for review and **not** completed.

## Draft publication

As soon as the **first signed task commit** exists on the feature branch, **push** that commit and **open a draft pull request immediately** through the existing activity and scoped executor flow (`pr.open`, then `agent github pending` or the equivalent knock-driven scan).

- Reuse a matching open pull request for the same session, repository, and base; do not open a second one.
- Do **not** wait for full local test suites, A38 author reports, live joins, or independent reviews before that first draft publication.
- Before publication, perform only the **basic** secret, scope, and signature checks needed to publish safely. Those are not full test gates.
- An explicit stop, or missing permission to publish, **blocks** draft publication and must be reported promptly. Do not invent a substitute path.

Early draft publication does **not** mark spine checklist keys such as `local_check_pass` or `pushed` as passed, and it does not claim tests, A38 evidence, or reviews are complete. `agent allow --action pr-create` already permits draft creation; no separate runtime switch is required for this rule.

The spine checklist key `pushed` remains **final validated push bookkeeping** after the applicable measured checks for that workflow step. Early draft publication via `pr.open` is allowed independently of that checklist close.

## After the draft exists

Work continues on the same draft. Proposal measurement for A38 migrations or bootstrap may follow publication; it is not a precondition for opening the draft.

Applicable full tests, A38 author evidence, current-base policy checks, and the live join remain required for **Ready for review** on the exact clean signed final head. Independently required GitHub checks and repository review gates also remain required unless a separately granted deviation says otherwise. Do not encode a one-off session waiver as the standing rule. Completion still requires human merge.

## CI while the draft is open

Hosted CI and other applicable checks may fail. There is no promise that CI never fails.

**Red CI is a blocker owned by the author:**

- Inspect the **actual** failing logs for the current head.
- Fix the root cause.
- Rerun the **actual** affected checks on the current head.
- Do not hide, skip, or override failures.
- Do not claim green from local results alone when the required hosted check is red or missing.

Pending checks must be labeled **pending**. Do not fabricate a pass.

The blocking `A38 / report (<target>)` commit status is **omitted** on drafts (not pending, not failure, and not a fabricated pass). `observe` stays advisory and unchanged. Configured `not_applicable` exclusions may still write success on that context only to clear a wrong prior status; that is not a test-pass claim. Real red hosted CI remains a blocker. Once the pull request is Ready for review, A38 publishes success for valid author-report evidence, or for the write-collaborator author-report waiver (author is a GitHub `User` who currently has `write`/`maintain`/`admin` on the target, or the latest `User` `ready_for_review` actor does); otherwise failure. That waiver covers only the author-report gate—not policy, workflow inventory, or migration failures. Only `User` actors can grant it; bots/apps and association strings cannot; timeline 401/403/404 yields no waiver without crashing assessment.

Ready for review does **not** start GitHub Actions. Where the repository opts in to [bot-owned fork workflow approval](a38-guard.md#how-fork-github-actions-are-meant-to-work), the trusted guard approves held fork runs only after a fresh A38 **enforce pass** on this head. That approval starts execution; it is not itself a green check. The merger does not click **Approve and run workflows**.

Repositories can enable the [guard's continuous readiness reconciliation](a38-guard.md#optional-continuous-readiness).
Lifecycle Draft/Ready writes run only on A38-enforced targets; excluded bases (for example a develop→main release PR) are left untouched.
An open Ready PR on an enforced target returns to Draft with an explanatory comment when required CI
is missing, queued, running, blocked or failed, or GitHub confirms merge conflicts —
**except** while a write collaborator holds Ready (author is a GitHub `User` with write on a Ready PR, or the latest `User` `ready_for_review` actor has write). In that hold, lifecycle leaves the PR Ready (`action: unchanged`) and still records the CI reasons for audit; it does not post a draft-intent comment or call the draft transition. No-write authors who mark Ready without a valid author report still fail A38 and are still auto-drafted.
After the CI authorized by the bot succeeds, it can restore Ready only with
current A38 evidence and confirmed mergeability (including a write-author enforce `pass` without a report). Required workflows, conditional
CI scope, control-workflow exclusions and the polling schedule belong to the
adopting repository. This does not rerun tests, submit review approvals or merge.

If the repository's guard integration is known to be defective, require a **verified** rollout of the fixed integration before Ready for review. Do not instruct merging through red statuses.

## Ready for review

Stay draft until Ready for review is earned on the **exact clean signed final head**:

1. Full applicable tests for that head (repository rules and, when adopted, the complete A38 policy run and local verification).
2. For A38 adopters: author report publication (unless waived because the author or the latest human Ready actor currently has write/maintain/admin on the target), current-base (or exact approved head) policy checks, and the live join required by [a38.md](a38.md) and [a38-guard.md](a38-guard.md).
3. Independently required GitHub checks on this head (`skipped` and `cancelled` are not green unless the workflow documents that skip). Inspect both the PR check rollup and the current-head workflow-run inventory: `action_required` runs may be absent from the check rollup. A38 equivalence covers only the jobs in its active policy; it does not replace independently required security or other GitHub-only checks. Bot authorization to start a run is not a successful run.
4. Independent required reviews and approvals per the attached skills and the target repository's written rules.
5. Then the Ready comment / leave-draft steps those rules define (`isDraft=false`).

`agent allow --action pr-ready` only checks task state when spine is attached; it is not itself the leave-draft verdict. Leaving draft is **Ready for review**, not pull-request completion.

## Merge / completion

A human merges. The client never merges. Report the pull request as completed only after that merge is verified.

## Related documents

- [CONTRIBUTING.md](../CONTRIBUTING.md) — repository contributing contract; defers lifecycle timing to this file
- [AGENTS.md](../AGENTS.md) — short agent entrypoint
- [DESIGN.md](../DESIGN.md) — product rules; error-fix opens drafts under this lifecycle
- Spine and pr-review skills — checklist bookkeeping and review gates
- [a38.md](a38.md) / [a38-guard.md](a38-guard.md) — A38 measurement, report, and Ready join (draft timing follows this lifecycle)

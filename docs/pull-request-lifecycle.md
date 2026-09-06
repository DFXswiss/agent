# Pull request lifecycle

This document is the **canonical** central rule for when a draft pull request is published, what may wait until after publication, and what Ready / completion still requires. Entrypoints in this repository link here. Tool plugins add only a short pointer; they do not redefine the standard.

A draft plus local tests is still **not** done. Human merge stays required. Session-specific review waivers are not a global default.

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

Applicable full tests, A38 author evidence, current-base policy checks, and the live join remain required for **Ready** and completion on the exact clean signed final head. Independently required GitHub checks and repository review gates also remain required unless a separately granted deviation says otherwise. Do not encode a one-off session waiver as the standing rule.

## CI while the draft is open

Hosted CI and other applicable checks may fail. There is no promise that CI never fails.

**Red CI is a blocker owned by the author:**

- Inspect the **actual** failing logs for the current head.
- Fix the root cause.
- Rerun the **actual** affected checks on the current head.
- Do not hide, skip, or override failures.
- Do not claim green from local results alone when the required hosted check is red or missing.

Pending checks must be labeled **pending**. Do not fabricate a pass.

If the repository's guard integration is known to be defective, require a **verified** rollout of the fixed integration before Ready. Do not instruct merging through red statuses.

## Ready and completion

Stay draft until Ready is earned on the **exact clean signed final head**:

1. Full applicable tests for that head (repository rules and, when adopted, the complete A38 policy run and local verification).
2. For A38 adopters: author report publication, current-base (or exact approved head) policy checks, and the live join required by [a38.md](a38.md) and [a38-guard.md](a38-guard.md).
3. Independently required GitHub checks on this head (`skipped` and `cancelled` are not green unless the workflow documents that skip).
4. Independent required reviews and approvals per the attached skills and the target repository's written rules.
5. Then the Ready comment / leave-draft steps those rules define.

`agent allow --action pr-ready` only checks task state when spine is attached; it is not itself the leave-draft verdict.

## Merge

A human merges. The client never merges.

## Related documents

- [CONTRIBUTING.md](../CONTRIBUTING.md) — repository contributing contract; defers lifecycle timing to this file
- [AGENTS.md](../AGENTS.md) — short agent entrypoint
- [DESIGN.md](../DESIGN.md) — product rules; error-fix opens drafts under this lifecycle
- Spine and pr-review skills — checklist bookkeeping and review gates
- [a38.md](a38.md) / [a38-guard.md](a38-guard.md) — A38 measurement, report, and Ready join (draft timing follows this lifecycle)

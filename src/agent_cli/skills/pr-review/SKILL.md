---
name: pr-review
description: >-
  Public review contract: quality and logic gates on a head SHA, grok then
  codex. Requires spine. Review lanes execute no software. A human merges.
---

# Pull-request review contract

Requires **spine**. Roles: `pr-reviewer-quality`, `pr-reviewer-logic`.
Vendors: `grok`, then `codex`.

Without this skill, `agent gate` and pr-reviewer `agent agent` commands refuse.

This file is the review contract. Operators attach the skill; they do not
replace it with a second store or a side process.

## Gates

Two dimensions (quality, logic) and two vendor stages (`grok-pr`, then
`codex-pr`). Codex stages run only if both grok dimensions are `approved`.

```bash
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-quality --vendor grok
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-logic --vendor grok
agent agent finish --id <uuid> --verdict approved|rejected
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict approved|rejected --head <sha> --agent <reviewer-uuid>
```

Then the same two dimensions with `--vendor codex` and `--stage codex-pr`.

Review lanes execute no software (no tests, builds, or servers).

## Verdicts

- `approved` → close the matching checklist key with evidence.
- `rejected` → do not treat the stage as passed. `--evidence` is required. On a
  task that carries a pull request the evidence is queued as a comment there,
  so the findings reach the author instead of stopping the task silently;
  `agent github pending` performs the HTTP. A task without a pull request
  records the rejection and reports that nothing was queued. On implement /
  resolve-conflicts, `agent gate record` returns the task to `implementing`.
  On workflow `review`, the task stays in pr-review and is not `done`.
- If a vendor cannot run, abort loudly. Do not record `approved`. Do not
  substitute another vendor.

Zero findings only after an explicit complete pass. Empty, partial,
timeout, or unavailable output is not zero findings.

A reported point that contradicts a verified repo rule or fact may be
dismissed with that evidence; it is not a defect.

## Coverage, handbook, contributing

`coverage_ok` and `handbook_ok`: `ja` / `nein` / `n_a` only from the **target
repository’s** written rules, with evidence. `n_a` only when that repository
does not have the requirement.

`contributing_ok` after the gates. `deviation_declared` and
`deviation_granted` are separate. An undeclared or ungranted break stays
`nein` on `contributing_ok`.

## Pull requests

The agent does not merge. Open pull requests as drafts; a human merges.

A draft plus local tests is not done. Quality and logic of one vendor stage
run in parallel on **this** head. The session that authored the diff does not
sit those reviews. Inner `review-loop` rounds are not these gates. Stay draft
until four lane verdicts on this head are approved (grok quality and grok logic, then Codex quality and Codex logic) and CI on this head is green
(`skipped` and `cancelled` are not green unless the workflow documents
that skip). `agent allow --action pr-ready` only checks task state; do
not mark ready if it denies. Then one comment whose review-pass count
is those four `approved` verdicts on this head, then mark the GitHub
pull request ready.

Locate these files with `agent skills path`.

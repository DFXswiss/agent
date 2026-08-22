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
agent agent finish --id <uuid> --verdict approved|rejected|unavailable
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict approved|rejected|unavailable --head <sha> --agent <reviewer-uuid>
```

Then the same two dimensions with `--vendor codex` and `--stage codex-pr`.

Review lanes execute no software (no tests, builds, or servers).

## Verdicts

- `approved` → close the matching checklist key with evidence.
- `rejected` → checklist `nein`. On implement / resolve-conflicts, return to
  `implementing` and open a new inner round. On workflow `review`, the task
  stays in pr-review and is not `done`.
- `unavailable` → checklist `unavailable` with evidence; task `gate-blocked`;
  no silent vendor substitute; no new round.

Zero findings only after an explicit complete pass. Empty, partial, timeout,
or unavailable output is not zero findings.

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

Locate these files with `agent skills path`.

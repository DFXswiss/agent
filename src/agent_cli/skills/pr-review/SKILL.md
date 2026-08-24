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
- `rejected` → do not treat the stage as passed. On implement /
  resolve-conflicts, `agent gate record` returns the task to `implementing`.
  On workflow `review`, the task stays in pr-review and is not `done`.
- If a vendor cannot run, abort loudly. Do not record `approved`. Do not
  silently substitute another vendor.

Zero findings only after an explicit complete pass. Empty, partial, or
timeout output is not zero findings.

A reported point that contradicts a verified repo rule or fact may be
dismissed with that evidence; it is not a defect.

## Coverage, handbook, contributing

`coverage_ok` and `handbook_ok`: `ja` / `nein` / `n_a` only from the **target
repository’s** written rules, with evidence. `n_a` only when that repository
does not have the requirement.

`contributing_ok` after the gates. `deviation_declared` and
`deviation_granted` are separate. An undeclared or ungranted break stays
`nein` on `contributing_ok`.

Tests exist to find and document product defects. A quality finding if the
change adds tests but leaves a discovered defect without a tracker row and an
expected-fail case that asserts the correct behaviour. Encoding broken
behaviour as a passing assertion is a logic finding. The contract is
DESIGN.md and CONTRIBUTING.md.

## Pull requests

The agent does not merge. Open pull requests as drafts; a human merges.

Locate these files with `agent skills path`.

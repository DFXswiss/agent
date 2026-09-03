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
  --verdict approved --head <sha> --agent <reviewer-uuid>
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict rejected --head <sha> --agent <reviewer-uuid> --evidence "<findings>"
```

Then the same two dimensions with `--vendor codex` and `--stage codex-pr`.

Review lanes execute no software (no tests, builds, or servers).

## Verdicts

- `approved` → close the matching checklist key with evidence.
- `rejected` → do not treat the stage as passed. `--evidence` is required. On a
  task that carries a pull request the evidence is queued as a review there, so
  the findings reach the author instead of stopping the task silently; the review
  is a `COMMENT`, never `REQUEST_CHANGES`, so it cannot hold a merge closed.
  `agent github pending` performs the HTTP. A task without a pull request
  records the rejection and reports that nothing was queued.

  On implement / resolve-conflicts, `agent gate record` returns the task to
  `implementing`. `agent gate record --verdict rejected` also leaves
  `state=failed` unchanged (rather than its usual auto-transition to
  `implementing`) when the sibling already failed the task in the same
  batch — the rejected gate row is still recorded either way, for audit,
  even though the task stays permanently stopped. On workflow `review`,
  the task stays in pr-review and is not `done`.

  The evidence becomes the body of that review unaltered, under a generated
  heading naming vendor, dimension and head. Write it for the
  author and not for the lane: one finding per line, `file:line` first, then what
  is wrong in a sentence. Leave out `STATUS=`, session ids and anything else that
  only means something inside the runner — it reaches a human who has none of that
  context, and it buries the finding it is printed next to.
- If a vendor cannot run, abort loudly. Do not record `approved`. Do not
  substitute another vendor.
- `unavailable` → neutral release for that case: clears the `working` agent
  row, does not record `approved`, does not affect gate/checklist/round state;
  a later scan retries.

Auto-pass only when the lane output is a single terminal report with
`STATUS: complete` and an explicit, present `FINDINGS:` header that parses to
zero -- a missing or duplicated `FINDINGS:` header, or multiple STATUS:/FINDINGS:
blocks in the output, resolves to retry instead. A zero-`FINDINGS:` report still
resolves to retry, not an automatic pass, when its `GAPS:` section discloses
genuine, non-trivial content (anything other than a zero token like `none`/`0`),
or when the output contains more than one `GAPS:` header. Empty, partial,
timeout, or unavailable output is not zero findings.

A reported point that contradicts a verified repo rule or fact may be
dismissed with that evidence; it is not a defect.

A finding gates the stage only when this change introduced it. A rule the
surrounding code already broke is reported, not gated: put it in the `--evidence`
of the verdict you do record, say that it is inherited, and leave it to the
author. Judge against the diff to the merge base, not the file as it now stands —
touching a line does not put everything about that line in scope. The check is
cheap: if the same defect sits in code this change did not touch, it is
inherited. Blocking a pull request on debt it did not create is how a review
stops being read.

Exposing a defect counts as introducing it. If the change makes a pre-existing
fault reachable where it was not, more likely to be hit, or worse when it is,
gate on that: the fault is older than the change, the reachability is not. Say
which of the two you are gating on, because they are fixed differently — the
author can undo the exposure without owning the fault.

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

## Approving

Once all four lane verdicts on **this** head are `approved` and CI on this head is
green, insert a `review.post` with `event: APPROVE` alongside the pass-count comment.
That is a review this account submits on the pull request, not a merge: the agent
still does not merge, and a human still does.

`APPROVE` is only for that state. A rejected gate publishes `COMMENT`, never
`APPROVE` and never `REQUEST_CHANGES` — the executor refuses the last one, because an
account that can request changes can hold a merge closed through branch protection.

Locate these files with `agent skills path`.

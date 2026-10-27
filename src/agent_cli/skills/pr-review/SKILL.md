---
name: pr-review
description: >-
  Public review contract: quality and logic gates on a head SHA, model slot
  first then model slot second (providers per ai-accounts.json). Requires spine. Review lanes execute no software. A human merges.
---

# Pull-request review contract

Requires **spine**. Roles: `pr-reviewer-quality`, `pr-reviewer-logic`.
Order: model slot `first`, then model slot `second`. The provider of each slot (`grok`, `codex` or `claude`) is the `models` value pinned on the task when it was created (from the device's `ai-accounts.json`; read it with `agent next --task <uuid> --json`); the examples below assume `first` = grok and `second` = codex.

Without this skill, `agent gate` and pr-reviewer `agent agent` commands refuse.

This file is the review contract. Operators attach the skill; they do not
replace it with a second store or a side process.

## Gates

The static script starts every review lane and any implementation pass needed
to address findings. Reviewers return findings to that script; they never start
subagents or interact with GitHub. Gate recording and GitHub publication below
are script operations. See
[DESIGN.md §19.1](../../../../DESIGN.md#191-responsibility-split).

Do not start a monitor, poll GitHub, or wait for CI or another review. Return
findings or blockers to the script when the review work is exhausted. The
script monitors events and informs a lane when further work is useful.

Two dimensions (quality, logic) and two slot stages (`grok-pr` = slot first,
then `codex-pr` = slot second; historical names). The second stage runs only if
both first-slot dimensions are `approved`.

```bash
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-quality --vendor grok
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-logic --vendor grok
agent agent finish --id <uuid> --verdict approved|rejected|unavailable
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict approved --head <sha> --agent <reviewer-uuid>
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict rejected --head <sha> --agent <reviewer-uuid> --evidence "<findings>"
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict unavailable --head <sha> --agent <reviewer-uuid> --evidence "<why>"
```

`gate record --verdict approved|rejected|unavailable` (`unavailable` needs `--evidence`).

Then the same two dimensions with `--vendor <second-slot provider>` and `--stage codex-pr`.

Review lanes execute no software (no tests, builds, or servers).

The first review run reads `CONTRIBUTING.md` and `REVIEW.md` at the base
revision of the pull request, not at its head.
A pull request that changes either file does not replace that base text for
the rest of its diff. Both files are binding. Quality judges the change
against those files and against the skills attached to the review. Logic
judges whether the change is sound and complete, and
whether it adds a second mechanism for a job those files say to reuse. A
missing issue is not a defect. The lane does not change files. Its prompt contains this reminder on one line:
`Read CONTRIBUTING.md and REVIEW.md at the base revision. Review this pull request against those files and against the linked issue. Do not change any files.`
Further sentences may follow. They do not remove the task. The sentence is a
reminder, not proof that the lane obeyed it.

A new endpoint, user-interface control, visible error text, clock or time
window, or permission check is a finding when an existing element in that
repository does the same job and the pull request does not name that element
and state the different job it has. The same job is the purpose the caller
already has. Different behavior is not a different job. A helper the caller
does not see is outside this rule. "Cannot" alone, a missing or empty reason,
or calling the finding not relevant does not clear it. Setting the finding
aside does not clear it. A hard requirement in those files cannot be waived
by a sentence in the pull request. An existing element whose job is different
is not a substitute, and the review names the element and its job.

## Verdicts

- `approved` → close the matching checklist key with evidence.
- `rejected` → do not treat the stage as passed. `--evidence` is required. On a
  task that carries a pull request the evidence is queued as a review there, so
  the findings reach the author instead of stopping the task silently; the review
  is a `COMMENT`, never `REQUEST_CHANGES`, so it cannot hold a merge closed.
  `agent github pending` performs the HTTP. A task without a pull request
  records the rejection and reports that nothing was queued.

  On implement / resolve-conflicts, `agent gate record` returns the task to
  `implementing`. On workflow `review`, the task stays in pr-review and is not
  `done`.

  The evidence becomes the body of that review unaltered, under a generated
  heading naming vendor, dimension and head. Write it for the
  author and not for the lane: one finding per line, `file:line` first, then what
  is wrong in a sentence. Leave out `STATUS=`, session ids and anything else that
  only means something inside the runner — it reaches a human who has none of that
  context, and it buries the finding it is printed next to.
- If a vendor cannot run, record `unavailable` with `agent gate record`. That
  sets the task to `gate-blocked` only on workflows `implement`,
  `resolve-conflicts`, and `review`, and only from state `pr-review` or
  `pushing`. It does not write the checklist. Then close the matching gate
  checklist key with `close-step --status unavailable` and evidence. Do not
  record `approved`. Do not substitute another vendor.

Zero findings only after an explicit complete pass.
Empty, partial, timeout, or unavailable output is not zero findings.

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

The agent does not merge. Open pull requests as drafts as soon as the first
signed task commit exists
([pull request lifecycle](../../../../docs/pull-request-lifecycle.md)); a human
merges. Full tests and these review gates are Ready requirements, not draft
publication gates.

A draft plus local tests is not done. Quality and logic of one model-slot stage
run in parallel on **this** head. The session that authored the diff does not
sit those reviews. Inner `review-loop` rounds are not these gates. Stay draft
until both review runs on this head are approved. The first review run is
`conformity-a` with `logic-a`. The second review run is `conformity-b` with
`logic-b`. That approval still
covers a later commit when the current head contains the reviewed commit and
that commit is after the pull request base. The target
repository's written CI rules hold on this head.
CI stays bound to the current head. Two review runs are required. The checklist
keys for the second run are `n_a` with evidence exactly
`second review not posted` only when the pull request opener is listed in
`one_review_authors` in the trusted `.github/pr-guard.json` on the default branch. That list is the
exception. A missing file, a missing key, and an empty list keep the standard,
and `done` waits for both gates. An entry that exists only on the pull request head does not count. The frozen `dfx-local-ci/v1` format and legacy verifier do not
themselves determine applicability. Private visibility alone is not A38 opt-in or
permission to skip GitHub CI. Under the central [A38 standard](../../../../docs/a38.md)
and [guard guide](../../../../docs/a38-guard.md), private local code-gate equivalence
requires trusted-base opt-in through a valid A38 manifest, assessment against the
canonical active policy, and a separate live join against the actual latest
report-like GitHub comment by the PR author; public A38 adopters require the
author report in addition to cumulative GitHub CI.
Private repositories without that opt-in and non-A38 repositories retain their
existing written CI rules. For applicable GitHub CI checks, `skipped` and `cancelled` are not green unless the workflow documents that skip. A verified author A38 report on this head may treat a matching required check that concluded `skipped` or `neutral` as green; `cancelled` and failed still block. Independently
required GitHub-only checks, technical merge restrictions, review gates, and
human merge remain required.
`agent allow --action pr-ready` only checks task state; do
not mark ready if it denies. Then one comment whose lanes are exactly
`conformity-a` with `logic-a`, and a later comment whose lanes are exactly
`conformity-b` with `logic-b`. Two reviews are required before Ready. One
review is enough only when the pull request opener is listed in
`one_review_authors` in the trusted `.github/pr-guard.json` on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count. The second
comment does not replace the first-run comment, does not start CI, and is ignored
when the guard chooses the review comment. Ready still requires it under the standard. A newer one does not hide an older valid first-run
comment. Choosing that comment ignores the second run. The newest
well-formed second-run comment that covers this head still returns a ready
pull request to draft when its JSON boolean `blocks_merge` is true and
`defects` is an integer >= 1. The reason is `Review defects block the merge`.
A write hold does not keep Ready. A newer well-formed second-run comment
with `blocks_merge` false clears that. A comment that does not parse, whose
declaration or human record is invalid (including a missing prompt or a
missing record), whose visible sentence does not match `blocks_merge`, whose
pass-run titles do not name one shared non-empty model, or whose
`blocks_merge` is not that boolean paired with a counted `defects`, does not
clear an older blocking comment and does not itself return the pull request
to draft. Under the standard that
comment leaves the second review missing (`second review comment missing`).
For an opener on the exception list, that absence does not fail Ready. The
guard does not judge that model name. Posted before the first run, the second
comment still does not replace the first-run comment. If the only review comment is the second run, the result is
`review completion comment missing`. The comment Ready reads is the first
run. A pass run there that does not name one shared non-empty model in both
titles is `review title model missing`. An `n_a` run names no model. A comment that contains both full runs is neither comment
(reason `review runs must be separate comments`). Then mark the GitHub
pull request Ready for review (`isDraft=false`). That leave-draft step is
not merge and not pull-request completion.

## Approving

Once both review runs on **this** head are `approved` and CI on this head is
green, insert a `review.post` with `event: APPROVE` alongside the first review run comment.
One review is enough for that step only when the pull request opener is listed in
`one_review_authors` in the trusted `.github/pr-guard.json` on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count.
That is a review this account submits on the pull request, not a merge and not
completion: the agent still does not merge, and a human still does. Claim
completion only after that human merge is verified.

`APPROVE` is only for that state. A rejected gate publishes `COMMENT`, never
`APPROVE` and never `REQUEST_CHANGES` — the executor refuses the last one, because an
account that can request changes can hold a merge closed through branch protection.

Locate these files with `agent skills path`.

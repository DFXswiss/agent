---
name: spine
description: >-
  Task spine: checklist, rounds, local checks, open work, and the one-step
  chain (allow / next / close-step / run). Attach before those commands.
---

# Spine

A session must exist. Attach this skill before creating tasks:

```bash
agent session register --id <session-id> --kind human|runner|other --skill spine
# or later:
agent session skill attach --id <session-id> --skill spine
```

Without spine, `task`, `checklist`, `round`, `check`, `work`, `allow`, `next`,
`close-step`, and `run` refuse.

## One open step

The static script owns execution and progression. A model lane must not invoke
`agent run` to launch another lane, run tests, or perform GitHub operations.
Tests and lane starts in the workflow below are script responsibilities; models
return their implementation or review results. See
[DESIGN.md §19.1](../../../../DESIGN.md#191-responsibility-split).

`agent next`, `agent close-step`, and `agent run` are the spine. Do not skip
keys. Quality and logic of the same vendor stage may be open together.
`close-step` applies chain guards, then writes via `checklist set`.

```bash
agent task create --session <session-id> --workflow implement|review|resolve-conflicts --title "…"
agent next --task <uuid>
agent close-step --task <uuid> --key session_registered --source script --evidence "session register"
agent run --task <uuid> [--dry-run] [--head SHA] [--cwd PATH] [--spec-file PATH] [--no-tmux]
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--task <uuid>] [--draft true|false] [--json]
```

`run` git-pushes (no force) when `pushed` is open and measures mergeability when `mergeable` is open. Protected branches stay refused.

`allow` exits 0 when permitted, 2 when denied, 1 on usage errors.

## Checklist values

Keys are `pending`, `ja`, `nein`, or `n_a`. `ja` and `n_a` need `--evidence`.

## Workflow keys

Do not invent keys. Chains:

**implement:** `session_registered`, `spec_written`, `implementer_done`,
`reviewer_approved`, `local_check_pass`, `pushed`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `contributing_ok`,
`deviation_declared`, `deviation_granted`

**review:** `session_registered`, `contributing_read`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `coverage_ok`,
`handbook_ok`, `contributing_ok`, `deviation_declared`, `deviation_granted`

**resolve-conflicts:** `session_registered`, `conflicts_resolved`,
`reviewer_approved`, `local_check_pass`, `pushed`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `mergeable`

`done` still requires the workflow checklist and both summary sentences
(`agent task summary`). That ledger `task-done` / checklist close is not
Ready for review and not pull-request completion. `local_check_pass` and
inner `reviewer_approved` are not Ready for review. A draft plus local
tests is not done. See pr-review and CONTRIBUTING.md.

Draft publication timing is the central
[pull request lifecycle](../../../../docs/pull-request-lifecycle.md): as soon
as the first signed task commit exists, open the draft via `pr.open` (executor
flow; `allow pr-create` already permits draft). That early publication does
**not** close `local_check_pass`, `pushed`, or any test/review checklist key.
The checklist key `pushed` is final validated push bookkeeping after the
applicable measured checks for that step.

For A38 work, follow the central [A38 standard](../../../../docs/a38.md) and [guard guide](../../../../docs/a38-guard.md); this skill is only a pointer.

Locate these files with `agent skills path`.

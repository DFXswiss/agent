# ADR-041: Runner operation pipeline and its extension points

Status: Accepted (2026-11-22)

## Context
05-runner.md §12 and 03-api.md §8.3/§9 define "register-then-execute" for every `request_operation`: formal checks on
plaintext, a dedupe tag, registration at the hub, execution only after `accepted`, the encrypted result, the
transition and the answer to the pending MCP call. The specification leaves open:
- how the GitHub executors (RUN-09, RUN-10) contribute the checks that need GitHub or the workspace
  (`repo_not_accessible`, the commit snapshot with parent and tree SHA, secret scan, sizes) and the dedupe fields only
  known at check time (`github.checkout` resolved `ref`, `github.commit` `parent_sha`/`tree_sha`), without editing
  the pipeline;
- how the `operation.finished` ping (RUN-14) is reached;
- where the pipeline gets the workgroup settings and an original's result from another device (the hub stores
  neither in plaintext);
- what survives a daemon restart and how a registration is retried without `409 id_conflict` (a re-sealed params
  event has new bytes).

## Decision
- **Pipeline** in `agent_runner/operations/` (task `operations`): the journal handler records `operation.request` as
  `received` inside the journal page's transaction and sends `NOTIFY runner_operation`; a listener wakes the pipeline
  after the commit (plus a scan every 5 s). Each operation runs in its own asyncio task through checks →
  registration → decision → execution → result → answer, every step persisted in `runner.operation` (migration
  0004), so a restarted daemon continues at the stored state.
- **Registration body written once** (`registration` column) before the first attempt: the params event is sealed
  once, the dedupe tag uses the same (current) epoch, every retry sends identical content and the hub answers
  `replayed`.
- **Extension points** (discovered like ADR-027 §4): executors at the existing `<area>/executors.py:EXECUTORS`;
  `<area>/op_prechecks.py:PRECHECKS` (type → pre-check returning dedupe fields and executor data, or `CheckFailed`);
  `<area>/op_finished.py:NOTIFY` (at most one, called for a final answer after the MCP call returned `pending`).
  A type is enabled only with an executor (and, for checkout and commit, a pre-check); anything else is rejected
  locally `unknown_type`.
- **Inbox consumers** (ADR-039) keep the latest verified `settings.workgroup` event per workgroup and every verified
  `operation.result` event, as received with the author's key; both are opened into memory only.
- **No double execution**: an operation found `executing` after a restart ends `failed` `timeout`
  (`retryable:true`); retries happen only inside one executor run (3 more attempts after 2 s, 8 s, 30 s).

## Consequences
- RUN-09/RUN-10/RUN-11 add `executors.py` and `op_prechecks.py` in their areas; RUN-14 adds `op_finished.py`;
  RUN-08 reads `Pipeline.answer`/`wait_answer` for `get_operation`.
- A restart during an executor run may leave a side effect at GitHub while the slot is released (documented; the
  alternative, re-running, could duplicate it).
- An operation the hub never decided within 10 min ends `expired` locally only; the hub has no record of it.

## Alternatives considered
Executing GitHub checks in the pipeline itself (would make RUN-07 own GitHub code); re-sealing the params event per
attempt (breaks the hub's replay rule); keeping settings and results in plaintext (against the store pattern of
ADR-022/033).

## Deviation from DESIGN.md
None beyond ADR-001.

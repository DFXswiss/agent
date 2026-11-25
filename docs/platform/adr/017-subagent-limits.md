# ADR-017: Limits for controlled subagents (KONZEPT §13)

Status: Superseded by ADR-088 (2026-11-25; accepted 2026-11-21)

## Context
KONZEPT §13: sessions may request subagents via the platform; only the platform starts them; each subagent is a full session with its own sandbox, harness, hooks, sync, audit and monitoring, assigned to the parent task and requesting session; subagents may request further subagents; a possible limit on parallel or nested subagents is left open. Spike B: models tried `request_subagent` as an execution side channel.

## Decision
- Subagents are started only after an accepted `subagent.request` operation; the hub creates the child session (`origin_operation_id`, `parent_session_id`, `depth` = parent + 1) on the parent's device and VM.
- Limits (checked by the hub, check H4; pre-checked by the runner): **depth ≤ 2** (main session 0, children 1, grandchildren 2), **≤ 3 running children per task**, **≤ 10 children per task in total**, **runtime ≤ 30 min** per child.
- Children are **one-shot**: they receive `purpose` + `prompt` as initial prompt, finish at their first `Stop`, the last assistant message becomes `final_text` (`get_subagent_result`), then the session stops. The parent receives a content-free `subagent.finished` ping.
- Exceeding a limit ⇒ `denied` with `limit_exceeded` (audited).

## Consequences
- Bounded cost and resource use per task; deterministic and testable.
- Interactive multi-turn subagents are not possible in v1; users can still watch every child session (chat and terminal).
- Limits are constants in v1; per-org configuration is a later ADR.

## Alternatives considered
No limits (runaway cost), depth 1 only (contradicts "subagents may request further subagents"), long-lived children (unclear completion semantics).

## Deviation from DESIGN.md
None.

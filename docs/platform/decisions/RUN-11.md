# RUN-11 decisions (controlled subagents: start, one-shot end, final text, results, ping)

Small decisions of the package (05-runner.md §13, §4.3, §6.1, §8; 03-api.md §8.1, §8.3, §9, §12; ADR-017, ADR-020).
The architecture-level points are ADR-038.

## Modules
1. `subagents/`: `rules.py` (limits, child limits, prompt and harness texts), `store.py` (queries over
   `runner.session`/`runner.operation`; no own table, no migration), `controller.py` (per-daemon `Subagents`),
   `executors.py` (`subagent.request`), `op_prechecks.py`, `session_request.py` (amender), `session_state.py`
   (observer), `journal.py` (`platform.query`), `query_tools.py` (`get_subagent_result`), `extensions.py`
   (`query_tools.py:TOOLS`, `subagent_finished.py:NOTIFY`), `results.py` (final text, turn end), `reconcile.py`
   (step 55), `tasks.py` (task `subagents`).

## Limits (ADR-017)
2. Pre-check before registration: depth of the requester + 1 > 2, running children of the task ≥ 3, children of the
   task ≥ 10 ⇒ `CheckFailed("limit_exceeded")`, i.e. registered and answered `rejected` (never executed); the hub's H4
   stays binding (`denied` `limit_exceeded`). Counted on this device: sessions with `depth` > 0 of the task (running =
   not `stopped`/`failed`) plus `accepted`/`executing` requests whose child is not a local session yet. Children are
   always started on the parent's device, so the local view equals the hub's except for requests in flight.
3. The executor refuses a hub answer that contradicts the rules: no or an invalid `child_session_id`, a child ID that
   is a session of another parent (`upstream_error`), a requester that ended (`not_found`), depth > 2
   (`limit_exceeded`). A child ID already started for this parent is returned as the result again (idempotent).

## Start (05 §13, §6.1)
4. Desired child: `depth = parent.depth + 1`, `parent_session_id`, the parent's task, org, workgroup and owner, model
   access preference = the parent's access (a parent without recorded access is refused `upstream_error`), model = the checked `model_id` or the parent's model. The start is RUN-04's
   (`schedule_start`), so steps, budget marks, failure reasons and the journal are the same as for main sessions;
   `ensure_vm` returns the workgroup's running VM, and the amender refuses a child whose VM is not the parent's.
5. Initial prompt ID = the operation ID (a UUID the guest registers for the prompt-origin check); text
   `"Purpose: <purpose>\n\n<prompt>"`; limits = main limits with `runtime_max_sec` 1800; the harness line is appended to
   the context block: "You are a subagent session of session `<parent>` for task `<task>`; finish by answering; you may
   request further subagents only via `subagent.request`."
6. The operation succeeds with `{session_id}` once the start ran (it waits for RUN-04's start up to `agent_starting`).
   A start that failed leaves the child `failed` with its reason; the parent learns it from the ping and from
   `get_subagent_result` (`failed`). The operation itself is not failed: the hub created the child.

## One shot, runtime (05 §13, §6.3)
7. The first `idle` of a child (its only turn ended) ends it after `GRACE_S` = 2 s: `stopping`, guest `stop`,
   `stopped`. If the child's newest `Stop`/`StopFailure` hook record is `StopFailure` (GST-06: no completion), it ends
   `failed` with `agent_exited` and its guest session is stopped. An idle without any hook record (interrupt) ends
   `stopped`.
8. Runtime: 30 min after the child's creation (device wall clock) it fails `runtime_exceeded` and its guest session
   is stopped; checked every second by the task `subagents`; a child already `stopping` (its turn ended, the guest
   stop is retried by reconcile) is not failed for its runtime. The unit's `RuntimeMaxSec=1800` starts later and is the
   second line.
9. Children are never suspended (RUN-05 suspends main sessions only) and never receive prompts.

## Final text and `get_subagent_result` (03 §8.1)
10. Final text = newest `session.output` draft of the child in the outbox plus the older records of the same message
    (`<id>-<n>`), joined by a blank line, cut to 64 KiB (UTF-8 bytes; ≤ 65536 characters). No output ⇒ `""`.
11. A `stopped` child is reported `stopping` until it settled: 2 s (`SETTLE_S`) after the runner first saw its end
    **and** the VM's stored journal cursor reached the guest's journal head fetched at that moment (a lagging reader
    never fixes an incomplete text), at the latest 30 s (`SETTLE_MAX_S`) after the end; a VM that does not run or
    cannot be reached has nothing left to wait for. Then `stopped` with `final_text` and `finished_at` (the local end
    time); a `failed` child at once with `finished_at`. Cached texts and other per-child state are dropped once the
    child ended more than an hour ago.
12. Only a child of the calling session in its task is readable; anything else is `not_found` "Only child sessions
    of this session can be read." An accepted child not yet started locally is `requested`. `wait_seconds` (0–30) is
    polled every 0.5 s until a final answer.

## Reads (`platform.query`, ADR-038)
13. Every answered read writes the device event `platform.read {tool, arguments, result_count}` (session of the
    caller) and the audit `read.session.<tool>` (result `ok`; `denied` for `not_found`/`forbidden`; `failed`
    otherwise; target = the child read, else the caller) before the answer; a read that cannot be recorded is not
    answered (the guest times out). A tool without a provider (RUN-08 not merged) is not answered and not recorded, so
    `get_operation` keeps its guest-side `pending` fallback. A pending call is taken once per daemon (a journal page
    read again after a rollback does not record or answer it twice; the last 4096 IDs are kept). At most 4 reads of one
    session run at a time; a fifth is answered `unavailable` without a record (bounded store polling). Reads are not
    persisted: a daemon restart between journal and answer leaves the call to the guest's timeout.

## Ping (ADR-020)
14. `subagent.finished` through `<area>/subagent_finished.py:NOTIFY` with ping ID = UUIDv5 of the child, once per child
    and daemon after the end settled, only for a parent that has not ended. Without RUN-14 a log line
    `runner.subagents finished ...` is written. After a restart every child that ended within the last hour is pinged
    again with the same ping ID (the guest's ping table is keyed by it), so an end during downtime or a failure by the
    startup reconcile still reaches the parent.

## Reconcile (step 55)
15. Desired children in assigned workgroups: `stopping` and known ⇒ stop. Local children without a start or end task:
    not listed and created more than 60 s ago ⇒ stop (the desired state of a reconcile run may be older than a child
    the runner just started); workgroup unassigned ⇒ stop `unassigned`; left in a start state before `agent_starting` ⇒
    `failed` `device_shutdown` and guest stop. A desired `requested` child unknown locally, with no pending
    `subagent.request` of its task (`received`/`accepted`/`executing`) and no start running ⇒ recorded and `failed`
    `agent_start_failed` (the request ended before the start, e.g. a daemon restart during execution).

## Outside the package's paths
16. Registry assertions of another package's test that listed exactly its own entries now check inclusion
    (`prompts/test_wiring.py`: its observer and amender come first; `operations/test_wiring.py` was made
    inclusion-style by RUN-09 already). ADR index line for ADR-038.

## Affected E2E/ATK tests
17. E2E-24 and E2E-17 (SCN-04, SCN-07) do not exist yet in agent-core; their runner side is covered by
    `tests/platform/runner/subagents/` (approval, denial, local and hub `limit_exceeded`, grandchild through the
    platform, one-shot end, runtime limit, parent-only results, ping). ATK-05 runs the guest/sandbox only; this package
    changes no guest, image or sandbox file, so its outcome cannot change.

## After the independent review (1 high, 6 medium, 9 low)
18. H1 (ownership of `platform.query`, RUN-08): kept the single dispatcher here with the discovered tool point; ADR-038
    tells RUN-08 to register its tools at `query_tools.py:TOOLS` (or move the dispatcher with the point). One handler
    per kind leaves no conflict-free alternative for whichever package lands first.
19. M2 unrouted reads are not answered (item 13); M3 fresh children (item 15); M4 settling by journal head (item 11);
    M5 pings after a restart (item 14); M7 once per pending call, M8 at most 4 reads per session (item 13).
    M6: the two registry assertions of other packages (item 16) had to change, because they listed the registry
    exhaustively; the change keeps their intent (their own entries, first).
20. L10 per-child memory is pruned; L11 `stopping` children are not runtime-failed; L12 idle times are reset when a
    child leaves `idle`; L13 item 4. Not changed: L9 (the outbox scan is bounded by the child's lifetime and runs once
    per child), L14 (orphan rows only fail a session; the hub's H4 stays binding), L15 (the runner's wait is the
    session's `wait_seconds`; the guest adds 3 s on top), L16 (the guest validates the schema first; the runner
    still refuses non-UUIDs).

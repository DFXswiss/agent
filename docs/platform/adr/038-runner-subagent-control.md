# ADR-038: Runner control of subagent sessions (start, one-shot end, final text, forwarded reads)

Status: Accepted (2026-11-22)

## Context
05-runner.md §13 and ADR-017 define controlled subagents: the platform starts a child session after the hub accepted
`subagent.request`, the first `Stop` ends the child, its last assistant message becomes `final_text`, the parent gets a
content-free `subagent.finished` ping and reads the result with `get_subagent_result`. The specification leaves open:
- how the runner starts a child through the session start of RUN-04 without editing it (RUN-04 starts main sessions
  only and leaves `depth` > 0 to RUN-11);
- where the runner gets the last assistant message: the guest journals transcript records (`session.output`) that the
  session area turns into events, the guest's `Stop` hook only moves the session to `idle`, and the transcript record
  of the last message may arrive after the end of the session;
- who answers the guest's forwarded reads (`platform.query`, GST-07): the journal registry allows one handler per kind,
  `get_subagent_result` belongs to RUN-11 and the other read tools to RUN-08;
- how the ping reaches RUN-14's `send_ping` before RUN-14 exists.

## Decision
- **Start through the existing session start**: the executor of `subagent.request` (`subagents/executors.py`) builds
  the desired child (parent's task, owner, model access or the checked `model_id`, `depth = parent + 1`) and runs
  `SessionService.schedule_start`. The child's request is completed by a request amender
  (`subagents/session_request.py`, ADR-032): initial prompt `"Purpose: <purpose>\n\n<prompt>"` with the operation ID as
  prompt ID, `RuntimeMaxSec` 1800 s, the harness line of 05 §13. The amender refuses every `depth` > 0 session the
  controller did not ask for, so no path but an accepted operation can start a child. The reconcile step 55 handles
  children like step 50 handles main sessions, but never starts one: a child the hub created that this device did not
  start is failed (`agent_start_failed`).
- **One shot from the runner**: a state observer (ADR-032) tells the controller when a child became `idle`; after a
  grace of 2 s (the guest flushes a transcript message after one quiet second) the runner stops it (`stopping`, guest
  `stop`, `stopped`); a turn that ended with `StopFailure` ends it `failed` `agent_exited` instead. The runtime limit
  is enforced by the runner at 30 min after the child's creation (`failed` `runtime_exceeded`, guest stop) before the
  unit's own `RuntimeMaxSec` (started later) ends the process. The guest's `SUBAGENT_STOP` hook (GST-06) stays unused,
  so the guest is unchanged.
- **Final text from the outbox**: the runner keeps no second copy of confidential text. The final text is read back
  from the event drafts the session area stored in the outbox (sealed with the storage key): the newest
  `session.output` of the child and the further blocks of the same message, cut to 64 KiB. The result counts as final
  2 s after the end was first seen and once the VM's journal was handled up to the guest's head of that moment (at
  most 30 s), then it is cached in memory.
- **Forwarded reads**: `subagents/journal.py` registers the one handler of `platform.query`. It hands each read to the
  tool registered at the discovered point `<area>/query_tools.py:TOOLS` (tool name → `QueryTool`), records the
  `platform.read` event and the `read.session.<tool>` audit, then answers the pending call (3 attempts); a read of a
  tool without provider is not answered (the guest's own timeout or fallback, as before). RUN-08 registers its read tools at that point and adds no second
  `platform.query` handler (it may move the dispatcher into its own area if it keeps the point).
- **Ping**: `<area>/subagent_finished.py:NOTIFY` (at most one provider, the RUN-07 pattern of `op_finished.py`) gets a
  `FinishedPing` with a stable ping ID (UUIDv5 of the child) once per child and daemon, only for a parent that has not
  ended; a restarted daemon pings children that ended within the last hour again with the same ID; without a provider
  the end is logged.

## Consequences
- Children are full sessions on the existing paths (events, terminal, hooks, audit, metrics, heartbeat).
- The final text depends on outbox items being kept until the child settled (the outbox is never pruned today; a
  later pruning must keep event items of the last hour). Update (KOR-03): acknowledged items are kept for 1 h, so a
  child read an hour or more after it ended answers an empty `final_text`.
- A ping may reach the guest twice (restart); the stable ping ID makes the second one a no-op at the guest.
- RUN-08 adds its read tools at `query_tools.py:TOOLS` instead of a second `platform.query` handler.
- A limit exceeded locally is `rejected` `limit_exceeded`; one only the hub sees is `denied` `limit_exceeded`.

## Alternatives considered
A guest `SUBAGENT_STOP` callback that stops the session and journals the final text (needs a new journal kind, a
contract change and guest code outside the package); an event observer point in the session area's journal handler
(editing RUN-04's module); a runner table with the sealed final text (a second copy and a migration for data the
outbox already holds); answering only `get_subagent_result` and leaving other reads to time out (RUN-08 would then have
to replace the handler).

## Deviation from DESIGN.md
None beyond ADR-001.

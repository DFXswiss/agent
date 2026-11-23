# ADR-033: Runner prompt delivery (store, routing, resume and restarts)

Status: Accepted (2026-11-22)

## Context
05-runner.md §2.2 step 6, §6.2 and §6.3 and 03-api.md §4.6.3 and §6.1 require that only signed, verified `prompt`
events of workgroup writers reach a session, exactly once and in event order, also after interruptions and a wipe of
the local store, and that follow-up prompts resume a suspended session (`--resume`) or go to a new main session of the
task. Four points are open in the specification:
- where a verified prompt waits until its session can take it (the inbox hands every event over once, ADR-039; the
  runner schema of RUN-01 has only `prompt_delivery(event_id, session_id, state)`, and plaintext must not be stored);
- how the device learns which session a prompt goes to (a prompt addresses its task: header `session=""`);
- how the hub learns what was delivered (`delivered_prompt_event_ids` is an output of `GET /v1/device/state`, but no
  device message carries a delivered prompt's ID);
- what "resume" means for a **new** main session with `resume_from` on the same device: the Claude transcript of the
  previous session belongs to that session's user and directories in the guest (05 §4.3), and the guest control
  protocol has no way to carry it over.

## Decision
- **Store** (migration `0003_prompt_event.sql`): `runner.prompt_event` keeps every prompt event the inbox verified
  (E1–E5, author `wg:write` at `lh`) exactly as received, with the author's verified signing key; the text is opened
  into memory only when it is delivered (signature and decryption again). `prompt_delivery` gains `task_id`,
  `scope_seq` (delivery order), `channel` (`start`: initial prompt on the agent's command line; `prompt`:
  `POST /v1/sessions/{id}/prompts`) and `attempts`.
- **Routing comes from the hub, verification from the device**: `pending_prompts` (reconcile step 60) and the
  `prompt` hint name the session; the runner accepts a routing only to a local main session of exactly the event's
  task, workgroup and org at the event's `scope_seq`, else `runner.inbound_rejected` (`prompt_misrouted`). The first
  prompt of a start is the head routed to that session (`FIRST_PROMPT`), so a store wipe never re-delivers an older
  prompt of the task by guesswork.
- **Exactly once**: write-ahead state `registered` before every guest call; repeats use the same `prompt_id`, which
  the guest accepts once (registry keyed by `prompt_id`, GST-03/GST-06); `accepted`, `refused`, `failed` are final;
  the hub's `delivered_prompt_event_ids` are adopted as `accepted`. Strict `scope_seq` order per session; a prompt
  older than one the task already handed to a guest is stale and refused (`prompt_stale`).
- **Delivered list at the hub**: the runner reports no prompt IDs (no contract field exists, and an unknown field
  would make the hub reject the whole `session_transition`). The hub derives "delivered" from its own state (HUB-09);
  an additive optional field (e.g. `prompt_event_id` on the `working` transition) is left to the hub package that
  implements `delivered_prompt_event_ids`.
- **Resume** of a `suspended` session: `resuming`, `ensure_vm`, the same session ID with `resume_claude_session_id`,
  credential, then the prompt through `POST …/prompts` (the guest waits for the resumed TUI). A VM other than the
  session's fails the session (`vm_start_failed`); the prompt stays routable to the next session.
- **Restarted sessions** (`resume_from`, or a first prompt that is not the task's first): a fresh start whose harness
  context gets one line pointing to `get_task_messages`. A new session never uses `--resume` with the previous
  session's Claude session ID. The new extension point `<area>/session_request.py:AMENDERS` (session area,
  `RequestAmender(name, amend)`) lets the prompt area add that line to the start request.

## Consequences
- Prompts survive daemon restarts and network failures without plaintext at rest; the delivery order and the
  exactly-once rule are enforced locally and survive a wipe through the hub's list.
- A same-device `resume_from` restart loses the Claude-side conversation state; the model reads the history through
  the platform tool instead. Carrying the transcript into a new session needs a guest protocol extension.
- A prompt handed to a guest (`typed`) whose VM died before the journal reported the acceptance moves to the next
  session and may be delivered once more (at-least-once in that crash case only).
- HUB-09 must list a task's prompts (including the initial one) in `pending_prompts` for the session it routes them
  to, and must produce `delivered_prompt_event_ids` without a device report.

## Alternatives considered
Delivering every pulled prompt to "the" main session of its task without hub routing (ambiguous after restarts and
wipes), keeping prompts only in memory (lost on restart), storing decrypted text (plaintext at rest), resuming a new
session from the previous session's transcript (not possible with the guest protocol), reporting delivered IDs in a
new outbox field (contract change in agent-core, out of this package).

## Deviation from DESIGN.md
None beyond ADR-001.

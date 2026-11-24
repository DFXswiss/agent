# RUN-05 decisions (prompt verification, delivery, follow-ups, resume and restart)

Small decisions of the package (05-runner.md §2.2 step 6, §2.4, §6.1 step 5, §6.2, §6.3; 03-api.md §4.6.3, §6.1
`pending_prompts`/`delivered_prompt_event_ids`, §6.2 `prompt`/`session.control`, §7.1; 02-data-model.md §4
`prompt_delivery`; 04-crypto.md §6.4; ADR-016, ADR-025). The architecture-relevant ones are ADR-033.

## Modules
1. `prompts/`: `queue.py` (store of prompt events and delivery rows), `verify.py` (routing check, opening the text),
   `deliver.py` (`Deliverer`: routing, order, hand-over, retries, idle → suspend, controls, loop), `resume.py` (resume
   of suspended sessions, restart hint), `first.py` + `session_prompt.py` (`FIRST_PROMPT`), `session_state.py`
   (`OBSERVERS`), `session_request.py` (`AMENDERS`), `consumers.py` (inbox consumer `prompts.events`), `reconcile.py`
   (step 60), `hints.py` (`prompt`, `session.control`), `tasks.py` (daemon task `prompts`).
2. The package text lists `reconcile.py:STEPS` step 6 and `hints.py:HANDLERS`; the first-prompt provider is
   registered through `session_prompt.py:FIRST_PROMPT` (ADR-032) with its implementation in `first.py`.

## Verification
3. E1–E5 with the author's right at `lh` are the inbox's checks (RUN-03); only events that passed reach the consumer
   and are stored. "E6" for a reader is "holds the scope key of the epoch" (04 §6.4): the text is opened with the
   stored key of the header's epoch; failing that is `prompt_unreadable`.
4. A prompt must address its task (`session` and `ref` empty); the consumer refuses others (`prompt_misrouted`). The
   inbox already refuses most of them (kind rule); the consumer check stays as a second line.
5. Routing check: local main session (depth 0), same task as the event and as the routing, same workgroup (= scope)
   and org, the routing's `scope_seq` equals the event's. A routing to a session that is not local yet is no
   refusal (the start asks for its first prompt); a routing of an event that is not stored triggers one inbox pull;
   when the scope's inbox cursor is at or beyond the event's `scope_seq` and it is still missing, the inbox refused
   or skipped it → `prompt_unverified`. Refusals are audited once per (event, session) and daemon.
6. "Stale" (`veraltet`): a head prompt first waits up to 60 s (`HOLD_S`) for an earlier stored prompt of its task that
   has no delivery row yet (its hint was lost; the next reconcile routes it). A prompt routed after a later prompt of the same task was handed to a guest or accepted is
   refused (`prompt_stale`) instead of breaking the event order; a prompt with a final row (accepted, refused,
   failed) is never delivered again, whoever routes it.

## Delivery
7. States: `pending` → `registered` (write-ahead, before the guest call) → `typed` (guest answered `registered`, or
   the guest started the agent with it: `agent_starting` for channel `start`) → `accepted` (hook) | `failed` (guest
   `prompt_refused delivery_failed`) | `refused` (runner checks or a guest refusal other than below).
8. Guest answers to `POST …/prompts`: `state_conflict` (accepted before, or the guest's session ended) → `typed`, the
   journal decides; `not_found` → `pending` again with backoff; any other refusal → `refused` (`prompt_guest_refused`).
   Transport errors keep `registered` and retry with the same `prompt_id` (backoff 1 s doubling to 30 s per session).
9. A `typed` row without result while the session is `idle`/`prompt_delivering` is sent again after 150 s (the guest's
   delivery queue is in memory, GST-06 decisions 17–20); the registry's `prompt_id` keying makes this safe.
10. Session states: `idle` and `prompt_delivering` take the head prompt; `suspended`/`resuming` are resumed first;
    `working` and the start states before `prompt_delivering` wait; `stopping`/`stopped`/`failed` get nothing. While a
    start runs, its `start`-channel prompt is never sent through the prompt endpoint.
11. `prompt_refused delivery_failed`: the row `failed` and a `session.status` event `{state, reason:
    "delivery_failed", details: {prompt_event_id}}` (the contract allows a non-failed state with an error code); a
    session that never accepted its first prompt (still in the start states) fails `delivery_failed` and its guest
    session is stopped by the next round of the delivery task.
12. A row of an open state whose session ended (or is unknown locally) moves to the session the hub routes it to next;
    final rows never move.
13. `delivered_prompt_event_ids` rows become `accepted` (inserted when unknown) before the routings of the same desired
    state are applied, so after a wipe nothing listed there is delivered again.
14. The runner does not report delivered prompt IDs to the hub (no contract field; ADR-033). HUB-09 has to derive them.

## Follow-ups, resume, restart
15. Resume reuses the start's building blocks of `sessions/start.py` (`Starter._check_guest`, `_model`, `_context`,
    `_provision`; private helpers of RUN-04, like GST-06 reused GST-02's) so a resumed session gets exactly the same
    request; `POST /v1/sessions` is the guest's resume (same ID, `resume_claude_session_id` when known).
16. A repeated resume after a lost answer may get `id_conflict` (the context's names changed): accepted for a session
    that was already `resuming`, the credential still starts it.
17. The prompt of a resume goes through `POST …/prompts`, not as initial prompt: the guest waits up to 120 s for the
    resumed TUI (GST-06), which keeps one delivery path for all follow-ups. Measured on the runner device: suspended →
    answered in < 1 s (acceptance/RUN-05.md).
18. Model access of a resume: the owner's preferences from the desired-state entry when listed, else the session's
    recorded access and model.
19. ~~A VM replaced meanwhile (golden replacement, deletion) has no transcript: the session fails `vm_start_failed`, the
    prompt goes back to `pending` for the session the hub routes it to next.~~ **Replaced by KOR-33** (ADR-072 §7,
    decisions/KOR-33.md): the session moves to the workgroup's VM and is restored there from its newest checkpoint.
20. ~~Restarted sessions (ADR-033): `resume_from` in the desired entry, or a first prompt with an earlier stored prompt
    of the task → one harness context line pointing to `get_task_messages`; no `--resume` across sessions.~~
    **Replaced by KOR-33** (ADR-072, decisions/KOR-33.md): such a session continues the original conversation from the
    task's context checkpoint (`contexts.restore`); the `get_task_messages` line remains only as the reported
    emergency path (`session.context_fallback`).

## Idle and controls
21. Idle times are kept in memory (no column in the schema): from the journal's `idle` record, or from the first time a
    session is seen idle after a daemon restart (a restart therefore extends the idle time at most once). Sessions
    with open prompts are not suspended. A guest refusal (`state_conflict`, a turn started) restarts the idle time.
22. `session.control`: `interrupt` → guest `interrupt` for a live session on a running VM; `stop` → the session area's
    `schedule_stop`. The hub authorises both (`task:write`); the runner only checks that the session is its own and
    not ended.

## Outside ownership (minimal, additive)
23. `sessions/extensions.py`, `sessions/env.py`, `sessions/service.py`, `sessions/start.py`: the extension point
    `session_request.py:AMENDERS` (ADR-033), called after the start request is built. Migration
    `0003_prompt_event.sql`; `tests/platform/runner/test_store.py` lists the new table and columns.

## Independent review
24. Fixed: every write of a delivery row names the states it may leave, so a row the journal ended meanwhile is never
    reopened (no second registration of an accepted or failed prompt); a late-routed earlier prompt is waited for
    (item 6) instead of being refused; rows adopted from the delivered list get the event's task and place once the
    event is stored again, so an older prompt left out of the list is refused as stale after a wipe; a resume checks
    that `resuming` was set (a session stopped meanwhile is left alone) and fails `guest_unreachable` after 10
    attempts; the restart hint ignores earlier prompts that were refused or failed; a resumed VM's journal is read at
    once.
25. Accepted, documented: the hub's delivered list has no producer yet (decision 14, ADR-033, HUB-09); an open `typed`
    row of a session whose VM died before its journal was read moves to the next session and may be delivered again
    (at-least-once for that case, ADR-033); the delivery wake-up from a journal record may come before the commit
    (the next round, at most 5 s later, delivers); an audit refused key is remembered before its commit; prompt events
    are kept without purge (bounded by the workgroups' prompts; retention with the session retention of later
    packages); inbox pulls inside routing are bounded to 2 s.

# HUB-09 – Tasks, sessions, transitions, scheduler, workgroup and device lists

Decisions taken where `03-api.md` sections 3, 4.5, 4.6 (4.6.1–4.6.3), 6.1 and 6.2, `02-data-model.md` sections 2.4
and 2.4.1, `06-web.md` section 4.4, ADR-002, ADR-009, ADR-013, ADR-021 and ADR-027 were silent or ambiguous.

## Scheduler (`scheduler/`)

1. **One lexicographic key.** The levels of §4.6.2 form one key `(level, −vm_slots_free, −free memory, device_id)`:
   level 1 = running VM of the workgroup, 2 = stopped VM, 3 = otherwise. So (3) and (4) also break ties between two
   devices that both have a running (or stopped) VM of the workgroup. Free memory = `vm_memory_budget_mib −
   vm_memory_used_mib` (never negative). Device IDs compare as canonical lower-case text (= UUID order).
2. **VM states.** `running` wins over `stopped`; any other VM state but `deleted` counts as "a VM of the workgroup"
   for the skip rule (such a device is not skipped for `vm_slots_free = 0`) but only reaches level 3 (estimate 3000/8000).
3. **Reasons.** No online candidate → `device_offline`; online candidates but all skipped → `capacity_exhausted`. A
   workgroup without any active assigned device refuses `POST /v1/tasks` with `409 no_device`; when that happens later
   (a follow-up prompt or a re-scheduling after an unassignment) the session waits with `device_offline`.
4. **Online** = `device_presence.last_heartbeat_at` newer than `AGENT_HUB_DEVICE_ONLINE_SECONDS` (HUB-07's rule),
   read directly from the table so the scheduler does not depend on HUB-07's module.
5. **Capacity** counters that are missing, negative, non-integer or boolean count as 0; `capacity_low` uses the exact
   integer form `free × 10 < total`; a device with `vm_slots_total = 0` is low.
6. **Re-scheduling.** `scheduler/signals.py` subscribes to `device.presence`, `device.capacity` (both HUB-07) and
   `log.appended` (orgs; a `device_assign` adds a candidate). Like HUB-08's reconcile hints, subscribers only queue work
   on one background thread and read through a bound hub context (bound by the tasks routes and by the job
   `scheduler.reschedule`). The job also sweeps all waiting sessions every 15 s (safety net for a hub process that
   never received the signal). One decision per workgroup per run; a waiting session whose reason did not change is not
   touched (no transition, no frame, no `last_activity_at` change).
7. **Candidates** (`device-candidates`): certified (`certified_seq` = `platform_seq`), `active`, not yet assigned to the
   workgroup, and none at all when the org has no platform access (ADR-026). Ordered **online first**, then by free
   capacity (slots, memory), then ID: §4.6.1 step 1 lets the browser pick the first candidate, which must not be an
   offline device. `detail.candidates_available` of `no_device` is "this list is not empty".

## Tasks and sessions (`tasks/`, `sessions/`)

8. **State machine.** Start-up states may be skipped forward (never backward); `stopping` from every live state,
   `stopped` and `failed` from every live state; `stopped`/`failed` are final (a later report is refused). The current
   state again is a no-op (`Applied.changed = False`), so an outbox retry with another `origin_seq` is harmless.
   Refused steps raise `TransitionRefused` (`400 request_invalid`), which HUB-10 acknowledges as `rejected`.
9. **Task derivation** beyond 02 §2.4.1: main `suspended`, `stopping`, `stopped` ⇒ `idle` (the task waits for the
   user; "closed only by user action"); `resuming` ⇒ `starting`. The main session is the **newest** session of depth 0.
   `main_session_id` in the browser task rows is therefore the current main session. HUB-08's device task rows
   (`device/tasks.py`, not owned here) still name the first one; HUB-10/HUB-12 should align them when they touch that
   file (recorded as a follow-up, no functional impact on the runner, which reads `sessions`). Aligned by KOR-06
   (decision 15).
10. **Times.** `ready_at` = hub receive time of the first `prompt_delivering`, `first_working_at` = hub receive time of
    the first `working`, `ended_at` = receive time of `stopped`/`failed`; the device time stays in
    `session_transition.at`. `last_activity_at` follows every prompt and transition.
11. **Prompt routing and delivery without a new table.** A prompt belongs to the main session that was current when the
    hub received it (newest depth-0 session with `created_at ≤ received_at`; a new main session and its prompt share
    the hub time of one transaction). A session's first *n* prompts are delivered when it has *n* transitions into
    `working` (UserPromptSubmit accepted, 05 §6.1/§6.2; one prompt per `working`). Undelivered prompts of an ended
    session are **not** carried over: "Retry" (06-web §4.4) sends the same text again and must not be delivered twice.
    Known limit: two concurrent follow-ups on an ended session can route the earlier one to the old session (it stays
    visible in the chat, but is not delivered); accepted because it needs two simultaneous sends by the same user.
    Superseded: the known limit by KOR-06 (decisions 1–3), the no-carry-over rule for sessions that ended
    `stopping`/`stopped` by KOR-11 (decisions 1–5).
12. **Follow-up prompts.** A live main session (start-up, `working`, `idle`, `suspended`, `resuming`) gets the hint
    `prompt` when it has a device (the runner queues while `working`, resumes when `suspended`); a waiting session gets
    no hint (it receives the prompt with `session.start`). After `stopping`, `stopped` or `failed` – and on a closed
    task, which a follow-up prompt **reopens** (state derived again, `closed_at` cleared) – the hub creates a new main
    session placed by the scheduler. `resume_from` is derived in the device state (previous main session on the same
    device), so no column is needed.
13. **Close and control.** Close: task `closed` first (so later transitions never change it), then every live session
    `stopping` with `session.control {action:"stop"}` to its device; a session that never got a device is `stopped` at
    once; closing a closed task answers it unchanged. Control: hint only (the runner reports the transitions); a session
    without device: `stop` ends it, `interrupt` does nothing; an ended session: nothing; every call is audited.
14. **Idempotency.** `POST /v1/tasks` replays need the same workgroup, owner and byte-identical events (E9); a create
    whose events were stored before without the task (or only partly) is `409 id_conflict`. The replay answers the
    current task row and main-session fields with `replayed: true` and every event result `replayed: true`. Prompt
    replays answer the original `scope_seq` and the session the prompt was routed to.
15. **Order of checks.** Rights (`403`/`404`) before the event form (`400` with JSON pointers), then the event checks of
    `append_events` (e.g. `401 signature_invalid` for a bad event signature, as HUB-06 maps E4).
16. **Lock order** (documented in `sessions/taskstate.py`): `append_events` (org rows, scope heads) → task row →
    session rows → audit. `apply` therefore locks the task before the session; HUB-10 must call `apply` after its
    `append_events` and before its audit rows, and send `Applied.effects.flush()` after the commit.
17. **Audit.** `task.create {origin:"browser"}`, `task.close {by:"user"}`, `session.<state>` with the contract details
    (`failed`: `{reason, model_access}`), `session.control {action}`; hub-made transitions are attributed to the user
    who caused them (close/control) or to `hub`.
18. **IDs of hub-made sessions** are UUIDv7 at the hub clock.

## Device state (`sessions/device_state.py`)

19. `sessions`: every non-ended session placed on the device in a workgroup the device's verified view still reads;
    `model_access_pref` from `user_settings` (default `auto`); `resume_from` only when present.
    `pending_prompts` only for main sessions that are not `stopping`; `delivered_prompt_event_ids` for every listed
    main session (last 100).

## Other areas

20. **Changed test of HUB-08.** `tests/platform/hub/device/test_signals.py` asserted that the device area is the only
    subscriber of `log.appended`; it now asserts that its subscriber is among them (the scheduler subscribes too).
21. **Lists.** Workgroup devices and org devices newest enrolment first (`enrolled_at DESC, id DESC`), workgroup
    members newest grant first; `last_seen_at` = last heartbeat while a presence row exists (HUB-07 deletes the row on
    disconnect), else null.

## Review

23. **Fixed after the independent review:** `resuming → suspended` is allowed (02 §2.4.1 `suspended ⇄ resuming`, a
    failed resume); `resume_from` only names an earlier main session that had **ended** before the new one was created
    (a follow-up during `stopping` starts fresh, never two processes on one conversation); refused prompts (outbox audit
    `session.prompt_refused`) count as consumed like accepted ones; re-scheduling books every placement on the loaded
    candidates (a device back with one free slot does not take every waiting session); a concurrent create of the same
    `task_id` with other events is `409 id_conflict` instead of a unique violation; the subscriber queue drops finished
    futures.
24. **Kept after review:** writes on an existing task answer `403 forbidden` also to non-members (03 §1 "403 for writes
    on known scopes"; reads stay `404`); candidates online first (decision 7); a VM of the workgroup in any live state
    exempts a full device from skipping (decision 2: one VM per workgroup and device, no new slot is needed);
    `device.capacity` is emitted by HUB-07 (`stream/presence.py` on its branch); task and session frames go to the org
    audience and carry `wg_id`, the stream of HUB-07 filters frames by the readable workgroups of each browser (like the
    `event` frames of HUB-06); the edit of HUB-08's test is decision 20.

## E2E

22. E2E-06, E2E-07, E2E-08, E2E-13 and E2E-38 do not exist yet (SCN packages); they are not run here.

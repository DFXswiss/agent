# KOR-14 – decisions

The hub re-schedules a session the device refuses with `capacity_exhausted` (05-runner.md §3.4, 03-api.md §4.6.2,
§4.6.3; 02-data-model.md §2.4.1; ADR-009, ADR-049; PFM-01 open point 3).

## Failure reasons with a promised re-scheduling

| Reason | Promise in the specification | Before KOR-14 | After KOR-14 |
|---|---|---|---|
| `capacity_exhausted` reported by the device (`failed`) | 05 §3.4 "fail the request with `capacity_exhausted` (the hub then re-schedules)" | session and task `failed`, nothing re-scheduled (`tests/platform/hub/scheduler/test_capacity_refusal.py`: 13 of 22 tests red on the integration head) | next attempt created and placed at once, or waiting with the reason; all 22 green |
| `capacity_exhausted` / `device_offline` from the scheduler (no candidate) | 03 §4.6.2 "task stays `scheduling` … the hub re-runs the scheduler when a device of the workgroup connects or reports capacity" | kept (`test_reschedule.py`) | unchanged, also places the attempts |
| `device_shutdown` (store wiped, 05 §2.3) | "so the next follow-up starts a new main session" – a re-start on the next prompt, not a re-scheduling | kept (`tests/platform/hub/tasks/test_prompts.py`, follow-up after `failed`) | unchanged |
| `device_offline` as a session failure reason | no promise; the hub never sets it and the runner reports it for no session | – | unchanged |

## Decisions

1. **New attempt instead of the same session** (ADR-049 §1): the runner never starts a known session ID twice and
   `failed` is final; an attempt is a new main session whose first transition `requested` has the reason
   `capacity_exhausted`. No new column and no contract change: the reason of a `requested` transition and of the audit
   detail `session.requested` is already an optional error code in the contracts.
2. **No intermediate `failed` task**: `transitions.record(..., derive=False)` for the refusal, then the attempt's
   placement derives the task once. Otherwise the `task.state` signal `failed` would notify the user (HUB-15) of a
   failure the hub is already handling.
3. **Prompt routing**: `prompts.Main.retry` (the reason of the first transition) decides whether the pending prompts of
   the previous main session are carried. Only an attempt takes over prompts of a `failed` session, so a "Retry" after
   the final refusal never delivers the refused prompt a second time.
4. **Refusal checks from `hub.session`** (migration `0009`, partial index on refused sessions and an index on
   `(device_id, wg_id, created_at, id)`): device-wide "refused after the last heartbeat" makes the stored capacity
   untrusted (`vm_slots_free = 0`); per workgroup "the newest session of the workgroup there was refused" removes the
   stopped-VM exemption of §4.6.2 until the device reports a free slot. Without the second rule the scheduler would put
   the attempt back on the same full device after every heartbeat (the E2E case below) until the limit fails the task.
5. **Limit 3** refusals in a row (constant `retry.MAX_REFUSALS`): the refused session and the attempts directly before
   it that were refused before they ever worked (`retry.refusals`); a session that worked (refused later on a resume)
   or a follow-up starts a new row (review finding). Reaching the limit is logged (`capacity_refusal_limit`); the audit
   shows it as the third `session.failed` without a following `session.requested` by the hub.
6. **`resume_from`** skips a predecessor that was refused before it ever worked (it ran no agent) and falls back to
   the session before it, so an attempt on the device of a session that ran resumes from that one (review finding).
7. **Lock order**: the refusal runs inside the outbox transaction like every transition (task row, then session rows,
   then audit); `planned` reads before `record` writes, `start` places after it. A concurrent close or user stop
   serialises on the task row: close first → no attempt; refusal first → the close stops the attempt; stop first → no
   attempt (audit `session.control`); refusal first → the stop finds the refused session ended and the attempt goes on
   (the user stops the attempt, the task's main session). Tested sequentially in both orders and with two threads for
   stop and close. An unassignment is a log entry that only changes the candidates; a refusal after it places the
   attempt on another device or lets it wait `device_offline` (tested).
8. **No web change**: the countdown follows `task.main_session_id` and shows `countdown.reason.capacity_exhausted`
   while the task is `scheduling` (06-web.md §4.4); the E2E case asserts it.
9. **E2E** as a second test in `e2e-38-device-scheduling.spec.ts` (the scheduler scenario), tags `@E2E-38 @R-0.3`;
   `@R-3.12` is not added because `traceability.json` lists E2E-38 for neither and is changed only by acceptance
   packages. Real trigger without a database change: a runner with one VM slot, workgroup A's VM stopped, workgroup
   B's VM running → the runner refuses A's restart (no idle victim). Runs on T1 and T2 (the original E2E-38 case stays
   T1-only).
10. **Stale capacity** (review finding): the device-wide rule compares against the last heartbeat, and after a
    (re)connect - which writes the heartbeat time without a capacity report - against the connect time minus the
    online window until the first real heartbeat. A heartbeat measured before the refusal but received after it still
    clears the rule (WebSocket and outbox travel separately); the hub has no device-side timestamp of the capacity, so
    this race is accepted and bounded by the limit. A heartbeat with unchanged capacity emits no `device.capacity`
    signal; a waiting attempt is then placed by the 15 s sweep (`scheduler.reschedule`).
11. **Running VM of the workgroup** keeps the device a candidate after a refusal (no slot needed, §4.6.2 (1)); if the
    hub's VM row lags an idle stop, the attempt can be refused once more, bounded by the limit (accepted).
12. **Query cost**: the per-workgroup check reads the newest session of the workgroup on the device (one probe of
    `session_device_wg`); the device-wide check uses the partial index of refused sessions.
13. **Chat timeline**: each refused session still shows as a failed start step in the session's status rows while the
    task continues `scheduling`/`starting`; the countdown follows the attempt and shows the waiting reason. Accepted
    as the real history (no web change, KOR-14 scope).
14. The workspace copy of the specification outside the repositories is not updated (it was not kept in sync by the
    earlier correction packages either); `03-api.md` and `05-runner.md` are changed identically in both repositories,
    and ADR-049 exists in both.

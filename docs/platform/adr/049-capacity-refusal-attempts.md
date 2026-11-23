# ADR-049: A session the device refuses for lack of capacity gets a new attempt

Status: Accepted (2026-11-23)

## Context
- `05-runner.md` §3.4: a device that needs a VM slot and has none (and no idle VM of another workgroup to stop) fails
  the request with `capacity_exhausted` "(the hub then re-schedules)". The hub only re-ran the scheduler for sessions
  still `requested` without a device (`03-api.md` §4.6.2), so a refused session stayed `failed` and the task failed
  (PFM-01, PERF-05 trial: one of 30 sessions).
- The hub's view of a device's capacity always lags: heartbeat every 15 s, pool refill, several starts in one
  heartbeat window. The scheduler also places a workgroup with a **stopped** VM on a device without free slots (§4.6.2
  level 2), although restarting the VM takes a slot – the runner can free one only by stopping an idle VM of another
  workgroup.
- A runner never starts a session ID twice (`agent_runner.sessions.start`: a session known locally is not started
  again), `failed` is final in the session state machine (`02-data-model.md` §2.4.1) and the transitions are
  append-only. A `failed` session keeps its undelivered prompts because "Retry" sends the prompt again (KOR-11).

## Decision
1. **New attempt, not the same session.** When the device fails the newest main session of an open task with
   `capacity_exhausted`, the user did not stop it and prompts are pending there, the hub creates the next main session
   of the task in the same transaction (`agent_hub.sessions.retry`). Its first transition `requested` carries the
   reason `capacity_exhausted` (audit `session.requested` by the hub with that reason): that marks it as the hub's
   attempt. The refused session keeps its full history; the task is derived once, after the attempt exists, so it never
   shows `failed` (no `task.state` signal, no notification) in between.
2. **Prompts exactly once.** The routing (`agent_hub.sessions.prompts.route`) hands the prompts a session did not
   deliver to the next main session when that one is an attempt, as it does after `stopped` (KOR-11). The refused
   session is `failed` and never listed for a device again, so each prompt is delivered by the attempt only. A
   follow-up ("Retry") after a final refusal gets its own prompt only, as after any other failure.
3. **Placement.** The attempt is placed by the scheduler. A device that refused counts with `vm_slots_free = 0` until
   its next heartbeat (its stored capacity is older than the refusal; after a reconnect, until the first heartbeat
   after it). For the refused workgroup (the newest session of
   the workgroup on the device was refused) a stopped or starting VM no longer exempts the device from needing a free
   slot until it reports one; a running VM still does. So the attempt goes to another device of the workgroup with a
   free slot or waits `scheduling` with the reason `capacity_exhausted`, and the existing re-scheduling (capacity
   signal, 15 s sweep) starts it when a device reports room. Both checks read `hub.session` (migration `0009`, two
   indexes); no new column, no contract change.
4. **Limit.** At most 3 refusals in a row for the same prompts (the refused session and the attempts directly before
   it that were refused before they ever worked; a session that worked or a follow-up starts a new row); the third
   refusal ends the task `failed` with `capacity_exhausted` and "Retry" (logged `capacity_refusal_limit`). Because the
   refusing device must report again first, the limit mostly ends a race; a heartbeat measured before the refusal but
   received after it can still send the attempt back once, which the limit bounds.
5. **Not re-scheduled:** a refused child session, a main session a follow-up already replaced, a session the user
   stopped, a closed task, a refusal without pending prompts. `resume_from` skips a refused session that never worked
   (it ran no agent) and names the session before it when that one ran on the same device.

## Consequences
- The browser shows the attempt as the task's main session: the countdown restarts, or the waiting reason "All
  execution slots are busy" appears (06-web.md §4.4); no web change.
- The task list and audit show one `failed` session per refusal plus the attempt; `session.requested` with a reason
  identifies attempts.
- The limit is a constant (3), not a setting.

## Alternatives considered
The same session back to `requested` without a device (refused by the runner, which never starts a known session ID
twice, and breaks the final `failed` state and the append-only transitions); an unlimited wait without a refusal limit
(a device whose reports keep lagging could bounce the attempt forever); letting the user press "Retry" (the
specification promises the re-scheduling).

## Deviation from DESIGN.md
None.

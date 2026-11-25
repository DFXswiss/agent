# ADR-087: Device failure – interrupted cloud sessions continue on another device, temporary VMs start again

Status: Accepted (2026-11-25)

Implements [Idee 44](../KONZEPT.md#k-idee-44) (cloud sessions continue after a device failure on another device,
with an interruption but without losing the last synchronised context; temporary test and browser VMs need no resume,
they start again and their logs and results stay), [§16 point 5](../KONZEPT.md#k-16-b5) (a device failure may
interrupt cloud sessions but must not prevent resuming the synchronised context), [§16 point 7](../KONZEPT.md#k-16-b7)
(real session restore across devices), [Idee 38](../KONZEPT.md#k-idee-38) (deterministic choice),
[E-02](../KONZEPT.md#e-02) (say clearly instead of failing), [E-04](../KONZEPT.md#e-04) (everything through the
interfaces and `agent-platform`), [E-05](../KONZEPT.md#e-05) (what is missing is pushed, not explained),
[E-11](../KONZEPT.md#e-11) and [E-19](../KONZEPT.md#e-19) (exclude the class of error: a lost device never holds a
task). Builds on ADR-072 (context checkpoints and restore on any VM and device), ADR-075 (resource choice),
ADR-080 (test runs), ADR-083 (ordered prompt queue) and ADR-049 (the hub's next attempt after a refusal).

## Context
- A device is **online** while its heartbeats arrive (`AGENT_HUB_DEVICE_ONLINE_SECONDS`, 30 s); the job
  `stream.presence_expire` deletes stale presence rows. Nothing acted on a device that stayed away: its sessions kept
  their state (`working`, `idle` …), the task showed them as running, and prompts routed to them waited for the device
  (KOR-46: `waiting_for_device` with a notification).
- ADR-072 checkpoints the conversation and the workspace of every main session (at `idle`, `suspended`, `stopped`,
  `failed` and every 10 min while `working`) encrypted at the hub, and every new main session of a task restores the
  newest one on whatever VM or device it starts. What was missing is the decision to end the lost session and to place
  the next one elsewhere, and the guard against the lost device continuing when it comes back.
- In operation there is one execution device (F3). A failure there cannot move anything; the design must still be
  correct there (wait, say so) and work at once when a second device exists.

## Decision

### 1. When a device counts as failed
- New setting `AGENT_HUB_DEVICE_FAILOVER_SECONDS` (default **300**, 1–86400, at least twice
  `AGENT_HUB_DEVICE_ONLINE_SECONDS`; a smaller value stops the hub at start, fail-closed like every setting). A short
  network wobble or a runner restart (seconds to a minute) never triggers anything.
- `hub.device.offline_since` (migration `0031_device_failure`): set to the hub time when the device goes offline (its
  presence row is deleted by a disconnect or by the expiry job; only when it is not set yet), cleared when it
  connects. A device without presence row at migration time gets the migration time. Only hub times are used, never a
  device clock; a hub outage does not age devices (the expiry after a hub restart writes the restart time, and devices
  reconnect within seconds).
- A device has **failed** while it is not online and `offline_since <= now - AGENT_HUB_DEVICE_FAILOVER_SECONDS`.

### 2. Interrupted sessions (state "interrupted - device failed")
- The job `failover.sweep` (every 15 s, in every hub process) works task by task, each in its own transaction: the
  global audit lock first (like `operations.expire`), then the task row, the failed devices' rows and the task's live
  sessions, each `FOR UPDATE SKIP LOCKED`. A task, device or session another writer holds - a device that is just
  connecting (its connect holds the device row), a user's `control` (it locks the session without the task) - is taken
  at the next sweep. So the sweep never waits in a lock cycle, two hub processes never handle the same task twice, and
  a device that connects after the sweep's first look is not treated as failed.
- **Active** sessions on a failed device (state in the start chain, `working`, `resuming` or `stopping`) get
  `hub.session.interrupted_at` (hub time) and the audit row `session.interrupted {offline_since}` by the hub. The
  session state stays what the device reported last (the hub does not know more). An `idle` or `suspended` session
  runs nothing and is not interrupted (§3 ends it quietly when the task can move).
- **Task.** A task whose main session is interrupted derives to `scheduling` with the reason **`device_failed`**
  (scheduling category **`interrupted`**, `contracts/enums/task-states.json`); an interrupted session never counts as
  `working`. Attention (ADR-076): `blocked/device_failed`, a workgroup-wide reason (one notification per owner,
  workgroup and reason - a failed device with many tasks sends one). The web offers the next steps that exist
  (assign a device, choose "Automatic"), E-05.
- **Back in time.** When the device is online again before the task moved, the sweep clears `interrupted_at` (audit
  `session.interruption_ended`) and the task derives as before: the session simply goes on there.

### 3. Moving to another device (deterministic)
For every task whose newest main session is live on a failed device (interrupted, or idle/suspended), at each sweep:

1. **Candidates** are the scheduler's for a new session of the task (ADR-075: devices assigned to the workgroup,
   online, keys, the owner's credential key, credit, the task's chosen device and VM - the pin is applied first)
   **without the failed device**. When the failed device belongs to an organization (`device.kind = 'org'`), only
   other devices of **that organization** qualify: a session on own hardware moves only to the organization's further
   own devices, never to DFX devices. A session on a DFX device moves to any candidate. A task pinned to the failed
   device has no candidate: it waits for that device (choosing "Automatic" moves it at the next sweep). The rule binds
   the hub's own move; a later follow-up is placed by the scheduler as any new session.
2. `choose` of ADR-075 decides. **No placement** (only one device, others full or offline): nothing changes; the task
   stays `scheduling/device_failed` (active session) or `idle` (idle session: a follow-up waits for the device as
   before, KOR-46), and the next sweep tries again.
3. **Placement:** in one transaction the hub ends the lost main session `stopped` with the reason **`device_failed`**
   (audit `session.stopped`, actor the hub) and every other live session of the task on the failed device (its
   subagents) the same way, ends their operations (§4), sends the device the hint `reconcile` (dropped while it is
   offline) and creates the **successor** (`agent_hub.sessions.failover`): a main session on the chosen device (first
   transition `requested` with the reason `device_failed`, `failover_of` = the lost session, `triggered_by` = the
   author of the first prompt it takes over, else the lost session's), hint `session.start`.
   - **No successor** when a user stopped the lost session (audit `session.control` `stop`) or it was `stopping` (a
     stop or a closing task the device never learned): its undelivered prompts wait for the next follow-up, as after
     any stop. A closed task's sessions there just end.
   - **No successor** when nothing is to be continued: an `idle` or `suspended` session, or a turn a user interrupted
     (audit `session.control` `interrupt` at or after its last `working`), with no undelivered prompt. The task
     becomes `idle`; the next follow-up creates a new main session that restores the context.
4. **Prompts.** The successor takes the lost session's undelivered prompts in event order (carry-over of KOR-11; its
   first-transition reason `device_failed` counts like the hub's other attempts, so this also holds for a lost session
   that `failed`, §3.7); withdrawals (ADR-083) keep working. **The interrupted instruction is delivered again:** when
   the lost session's turn was running (`working`, not interrupted by a user), its last accepted prompt goes to the
   successor first. It is chosen once at the move and stored (`session.redeliver_event_id` of the lost session), so the
   routing stays exact; a prompt delivered once can no longer be withdrawn (`409 conflict`). A prompt the lost device
   took over but whose acceptance the hub never learned counts as undelivered and is delivered as well. So every
   instruction is completed at least once; a repeated step with an external effect meets the duplicate protection of
   the operations (KOR-28) or the executor's own check (an existing pull request).
5. **Restore.** The hub pins the task's newest complete checkpoint at the move (`session.restore_object`); the device
   state names it as the successor's `context` while it is complete (then the newest again), so a checkpoint the lost
   device uploads late never changes what the successor restores. `failovers {session_id, from_session,
   cancelled_runs}` tells the runner not to wait for the final checkpoint of the lost session (it will not come;
   ADR-072 §7 would wait `CONTEXT_WAIT_SECONDS`). The session notice says in one sentence that the session continues
   on another device after the device it ran on failed, the time of the checkpoint, that work after it may be lost,
   which test runs were cancelled, and that a work VM must be requested again. An attempt after a refused successor
   (ADR-049) keeps `failover_of` and the pinned checkpoint.
6. Deterministic: the sweep takes tasks in ID order, the candidates and `choose` decide; each successor is a pending
   session of its device for the next decision of its workgroup. Successors of several workgroups can overbook a
   device; it refuses what does not fit and ADR-049 places the next attempt.
7. **A device back after a restart.** A restarted runner reports the sessions it lost `failed` (`device_shutdown`,
   `guest_unreachable`). For a main session that was interrupted (its device stayed away beyond the deadline) the hub
   creates the successor at once by the same rules on any device - the same one included (`load.decide`) - instead of
   a failed task that needs "Retry". This is what one execution device gains today.

### 4. No double run (lease and epoch)
- **The session ID is the epoch.** A main session is the lease of one device on one task: every placement elsewhere is
  a new session ID, and the hub ends the old one before it creates the new one, in one transaction. A stopped session
  is final (ADR-013): every later report of the lost device for it is refused (`TransitionRefused`; the outbox item is
  acknowledged `rejected` and dropped), a new operation of it is denied (rule H1, `rights_denied`), no `session.start`
  or prompt is sent for it again, and **its events stamped after the move are refused** (rule E7: the session has no
  device for them), so what a cut-off device produced beside the successor never reaches the task.
- **The device that comes back** reconciles: the lost session is no longer listed, so the runner stops it at once
  (guest control `stop`) and delivers nothing more to it, also after a restart (it keeps and reuses its VMs). A
  runner that restarts and finds a live session's VM gone reports the session `failed` (`guest_unreachable`), which
  starts the successor of §3.7 when the session was interrupted.
- **Operations.** Every operation needs the hub's acceptance before it runs (register-then-execute, ADR-012). At the
  move, every operation of the lost sessions that is `accepted` or `executing` ends `failed` with `device_failed` and
  frees its dedupe slot: its outcome on the lost device is unknown, the session learns it from the answer and checks
  the external state (a repeated `github.create_pull_request` finds the open pull request). The slots of the lost
  sessions' `subagent.request` and `human.*` operations are freed as well (their children and answers belong to the
  lost session); every other slot stays, so a repeated step that succeeded before is a `duplicate`. A device that comes
  back reports outcomes the hub then refuses; a restarted runner never executes an operation twice.
- **Runner lease for the work VM.** `workvm_exec` and `workvm_browse` (and an implicit work VM start) reach the
  internet without a hub registration. The runner refuses them while it has had no hub contact for longer than
  `failover_seconds` (from the device state): a cut-off device cannot act outside the hub while its successor works.
  Running turns are not stopped - a hub outage must not stop every device (E-25).

### 5. Temporary VMs start again
- **Test runs** (ADR-080): a run of the lost session ends at the hub as `failed` with `device_failed` ("cancelled by a
  device failure"). The successor repeats it on its device in a fresh VM when it repeats the interrupted instruction
  (§3.4); the notice names the cancelled runs. The platform does not re-run a test on its own: the successor's
  workspace is the checkpoint's, which may differ from the one the run tested. **The log stays where it can:** a
  runner that restarts captures the output of a run whose VM still exists before it deletes the VM (until now it ended
  such a run `error`/`restart` without log) and uploads it like any run log. After a loss of the whole host (power,
  hardware) the run VM is gone and so is its output. The web shows the operation's state when the encrypted run event
  still says `running`.
- **Browser sessions** (KOR-44, ADR-085): a `browser.*` operation of the lost session that is still running ends
  `failed`/`device_failed`, the browser VM goes with the failed device, the successor opens a new browser when it
  repeats the interrupted instruction; the action record of the earlier browser session stays.
- **Work VMs** (ADR-059) are not moved: the successor requests a new one when it needs one (the notice says so).

### 6. Interfaces (E-04)
- `session_row`: `interrupted_at`, `failover_of`; `task_row` unchanged (state `scheduling`, reason `device_failed`).
  Audit actions `session.interrupted`, `session.interruption_ended`. Enums: session end reason `device_failed`,
  scheduling reason `device_failed` with category `interrupted`, attention reason `device_failed`, operation error code
  `device_failed`. Device state: `failover_seconds` and `failovers`. Migration `0031_device_failure`.
- Web and `agent-platform task show` show the interruption and the continuation. No new action: the move needs none,
  and "Automatic" placement and device assignment (ADR-075) are the existing ways out.

## Consequences
- **One execution device (today):** a device away beyond the deadline shows "interrupted - device failed" on every
  running task, with one notification per owner and workgroup; nothing moves. When it is back without a restart the
  sessions go on; after a restart (or a host reboot) every interrupted task continues on it from its last synchronised
  checkpoint with the interrupted instruction repeated, instead of failing.
- **With a second device:** running tasks continue there after the deadline, from the last synchronised checkpoint.
- **Interruption time** (measured value: from the device's last heartbeat to the successor's first `working`) =
  online window (30 s) + expiry (≤ 5 s) + deadline (300 s) + sweep (≤ 15 s) + start and restore on the new device.
- Work since the last checkpoint (at most 10 min of a working session) is lost; the interrupted instruction is
  repeated; the session is told.
- **Known limits:** liveness is the device WebSocket (03-api.md §6.2) - a device whose socket stays broken for the
  deadline while its HTTPS works is moved (its runner then stops the session). VMs of a failed device still count in
  `AGENT_HUB_VMS_PER_WG` until it is back. Runtime and tokens a cut-off device consumed are billed (they were used).

## Alternatives
- **Move at the online window (30 s)**: a runner restart or a short outage would move every task; rejected.
- **A platform prompt "continue"** for the successor of a running turn: it would count as a delivered prompt in the
  routing (every transition into `working` consumes one) and shift the queue; repeating the interrupted instruction
  keeps the routing exact.
- **Re-running test runs automatically on the new device**: on another workspace state than the run tested; the
  session decides.
- **A new session state `interrupted`**: every client and the runner would need it; the interruption is a property of
  a live session the hub can no longer reach, not a step of the machine.
- **Ending the session `failed`**: the task would show "Retry" and the attempt chain of ADR-049 would count it.
- **A button "move now"**: not needed for the requirement; the deadline is a setting.

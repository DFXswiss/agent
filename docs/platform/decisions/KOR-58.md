# KOR-58 – Decisions (device failure, ADR-086)

Small decisions taken while building ADR-086; architecture-relevant ones are in the ADR itself.

1. **Deadline 300 s, at least twice the online window.** Short outages and runner restarts (seconds to a minute)
   never move anything; a smaller setting stops the hub at start (fail-closed like every setting). The deadline counts
   from the first moment the hub saw the device offline (`offline_since`, hub clock), never from a device clock.
2. **No new session state.** "Interrupted" is a mark on a live session (`interrupted_at`) plus the task reason
   `device_failed` (category `interrupted`); a new state would have touched every client and the runner for a
   condition only the hub can see.
3. **The lost session ends `stopped`, not `failed`.** `failed` would show "Retry" and count in the refusal chain of
   ADR-049; `stopped` with the reason `device_failed` hands its prompts on by the existing carry-over.
4. **The interrupted instruction is repeated instead of a platform prompt "continue".** Every transition into
   `working` consumes one routed prompt, so an injected prompt would shift the queue; the repeated instruction keeps
   the routing exact. It is chosen once at the move and stored (`redeliver_event_id`), and a prompt delivered once can
   no longer be withdrawn (review finding 7).
5. **The checkpoint is pinned at the move** (`restore_object`) so a late upload of the lost device never changes what
   the successor restores (review findings 8, 21). Matching the repeated prompts to the checkpoint's time (review
   finding 8, second half) is not done: the at-least-once rule and the duplicate protection cover it.
6. **Operations of the lost session** that are `accepted`/`executing` end `failed`/`device_failed` with their slot
   freed (review finding 4): an outcome nobody will ever report must not hold a slot for days. Slots of
   `subagent.request` and `human.*` are freed (finding 3); succeeded operations keep theirs, so a repeated step that
   succeeded is a duplicate.
7. **Late events of the lost session are refused** when the device stamped them after the move (rule E7, finding 5);
   earlier ones are still taken. The device clock decides only between "before" and "after the move" of its own
   session; a wrong clock can only hide its own late output.
8. **Runner lease only for the work VM** (finding 6): `workvm_exec`/`workvm_browse` are refused after
   `failover_seconds` without hub contact; running turns are not stopped (a hub outage must not stop every device).
9. **A restart of the only device continues** the interrupted sessions (finding 9): a `failed` report with
   `device_shutdown`/`guest_unreachable` of an interrupted main session creates the successor on any device.
10. **Locks without waiting** (findings 1, 15): task, failed devices and sessions are taken `FOR UPDATE SKIP LOCKED`
    after the audit lock; a device that is connecting or a session a `control` holds is handled at the next sweep.
    After a move the lost device gets `reconcile`.
11. **Not changed (documented in ADR-086, Consequences):** liveness stays the device WebSocket (finding 14; a socket
    broken for five minutes is a fault, and the moved session is stopped on the next reconcile); VMs of a failed device
    still count in `AGENT_HUB_VMS_PER_WG` until it is back (finding 11; default 100 per workgroup); successors of
    several workgroups can overbook a device, which then refuses and ADR-049 re-places (finding 16); consumption of a
    cut-off device is billed (finding 18).
12. **No "move now" action** (goal 5): the deadline is a setting, and the existing placement "Automatic" and device
    assignment are the user's ways out of a pinned or single device; `agent-parity.md` needs no row.
13. **Org devices:** the "only the organization's own devices" rule binds the hub's own move; a later follow-up is
    placed by the scheduler like any new session (finding 17).
14. **Test runs are not re-run by the platform:** the successor's workspace is the checkpoint's; the session repeats
    the run when it repeats the interrupted instruction. A run's log survives a runner restart when its VM does; after
    a loss of the whole host it is gone (finding 10). Incremental log upload during a run was not built (E-25).
15. **Browser sessions (KOR-44, merged while this package was built):** a `browser.*` operation of the lost session that
    is `accepted` or `executing` ends `failed`/`device_failed` like any other; the browser VM of the failed device goes
    with it, and the successor opens a new browser when it repeats the interrupted instruction (ADR-086 §5). The record
    of the earlier browser session stays.

# ADR-013: Device assignment, scheduling and offline sync (KONZEPT §3, §7, §14.6)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §3: devices are added to organizations and assigned to workgroups; DFX provides servers by default, users may attach own hardware; initial allocation: one DFX machine hosts `agent-core`, another hosts DFX sessions. §6: events stored locally at once and synced with retry after interruptions. §7 (optional): bottleneck warnings and capacity-based task distribution. §14.6: assignment, resource planning, offline/reconnect sync open.
- Legacy sync: gapless `origin_seq` per device, idempotent push, capped backoff (`agent_cli.store`, `cmd_sync`).

## Decision
- **Assignment**: devices are assigned per workgroup in the membership log (`device_assign`). Platform devices are the default: a private org's member, or an org admin, assigns a candidate returned by `GET /v1/workgroups/{wg}/device-candidates` automatically on the first task.
- **Scheduler** (deterministic, `03-api.md` §4.6.2): running VM of the workgroup → stopped VM → most free slots/memory → smallest ID; devices without free slots skipped; no candidate ⇒ task stays `scheduling` with the real reason, re-run on presence/capacity change.
- **Capacity**: devices report slots and memory budget in heartbeats; `capacity_low` when free slots < 10 % (UI badge, alert rule). Both §7 optional points are thereby included in v1.
- **Sync**: runner outbox with gapless `origin_seq`, strict in-order processing, replay-safe, `origin_seq_conflict` fatal; poison items acknowledged as `rejected`. WebSocket carries hints only; on every (re)connect: reconcile `GET /v1/device/state`, push outbox, pull cursors. Backoff 1 s → 30 s with jitter, reset after 30 s stable. Sessions keep running while the hub is unreachable; events queue locally.
- **Initial allocation by role**: the hub device runs the hub; the runner device runs DFX sessions (platform device). Production deployment is out of scope (M4 plan).

## Consequences
- Placement and reconnection are testable deterministically; reconnect needs no hub-side queue.
- The hub holds no per-device hint queue; latency after reconnect equals one reconcile round trip.

## Alternatives considered
Devices pick work (DESIGN §25.7), round-robin, hub-queued commands (lost on hub restart).

## Deviation from DESIGN.md
Yes, covered by ADR-002 (§25.7 "never picks a device", §25.18 capability scheduler refused).

# ADR-002: The hub coordinates work

Status: Accepted (2026-11-21)

## Context
- KONZEPT: `agent-core` "koordiniert und synchronisiert" (Grundprinzip); every request creates a task (§2); devices are assigned to workgroups and VMs must be ready in < 10 s (§3); the platform validates and records every operation (§11) and alone starts subagents (§13).
- DESIGN.md refuses exactly this: §20 "hub as a coding control plane", §3 hub-owned lifecycle and leases, §25.1 "the hub does not decide who works on what", §25.7 "never authors work, never picks a device, grants no lease", §25.18, §24.3 "the hub never authors an event".

## Decision
- The hub creates **metadata rows** for tasks, sessions and operations (`02-data-model.md` §2.4–2.5) when a browser submits a task or a runner registers an operation.
- The hub runs the deterministic scheduler of `03-api.md` §4.6.2 and chooses the device for every new session.
- The hub decides operations (rights, type, limits, duplicates; checks H1–H6 of `03-api.md` §8.3) before execution, and creates child sessions for accepted `subagent.request` operations.
- The hub never authors **content** events: every event is written and signed by a browser or a device. Execution (VMs, sessions, GitHub calls) stays on the execution device, as in DESIGN §19.1 / `AGENTS.md` ("scripts execute").

## Consequences
- A single place enforces rights and duplicate protection across devices.
- The hub becomes availability-critical for starting work and executing operations (register-then-execute, ADR-012). Running sessions continue offline; operations wait up to 10 min.
- Scheduling logic must be deterministic and fully tested (same inputs → same device).

## Alternatives considered
- Devices pull work and choose themselves (DESIGN §25.7): cannot meet the < 10 s first-task target without a placement decision and cannot deduplicate across devices.
- Browser chooses the device: the browser lacks live capacity data and would need to be online for every rescheduling.

## Deviation from DESIGN.md
Yes: §3 (hub-owned lifecycle/leases), §20, §24.3 (metadata rows only, no content events), §25.1, §25.7, §25.18. §19.1 is kept in spirit (execution on the device).

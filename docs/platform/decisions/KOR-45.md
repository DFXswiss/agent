# KOR-45 – decisions

Package: sessions may do what their human may – reading beyond the own workgroup, full subagents, no fixed limits.
Concept references: [§8 point 5](../KONZEPT.md#k-8-b5), [§8 point 6](../KONZEPT.md#k-8-b6),
[Idee 26](../KONZEPT.md#k-idee-26), [Idee 43](../KONZEPT.md#k-idee-43); rules [E-02](../KONZEPT.md#e-02),
[E-04](../KONZEPT.md#e-04), [E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19);
interpretations [A1](../KONZEPT.md#a-1), [A5](../KONZEPT.md#a-5), [A7](../KONZEPT.md#a-7), [A10](../KONZEPT.md#a-10),
[A11](../KONZEPT.md#a-11), [A14](../KONZEPT.md#a-14). Design: [ADR-087](../adr/087-sessions-read-like-their-human.md)
and [ADR-088](../adr/088-full-subagents-and-limits-as-settings.md), reviewed with fresh context before the build.

1. **Two decisions, not one.** Reading (rights, keys, audit, plaintext) and subagents/limits touch different parts and
   supersede different ADRs (ADR-024 resp. ADR-017/ADR-038), so each has its own ADR.
2. **Keys by assignment only (way A).** The key model (§8 point 2) forbids this package a new wrap rule; a session
   reads another workgroup only with the key its device holds because it is assigned to it (directly or through the
   organization, ADR-075). The per-task read release of the package text (way B) is a new entitlement in the
   membership log and therefore cryptography work; it is not built (decision D3 of the key model, recommendation
   "way A now"). A workgroup the people may read but the device does not serve answers `unavailable` with the
   sentence that an admin can assign the device to it or to the organization (E-05; the key is technically
   required, so this is the exception of E-11). No assignment request is raised automatically – the session tells
   the person, who tells an admin; a request card is possible later.
3. **Owner ∩ instructing human, effective access.** Both people must be able to read the workgroup as people, the
   admin's inherited read included (E-11: "was der Nutzer darf, darf auch seine KI"). This amends ADR-058 §1 ("platform
   sessions do not inherit") for reads; writes stay in the session's own workgroup and the credential scope stays out
   of reach.
4. **Unknown instructing human → own workgroup only.** A turn with prompts of several people (or none) has no single
   person; the reads stay within the session's workgroup and the organization level, where every author of the turn
   had write access. Other workgroups answer `forbidden` with the reason, the same for every workgroup, so nothing
   leaks. This is the "None never widens" rule of `agent_hub.sessions.attribution` (KOR-46).
5. **The person is captured when a read arrives** (design review, high): the `platform.query` journal handler takes
   `acting.stamp` in the journal transaction and hands it to the tool; the answer never re-reads it, so a prompt of
   another person accepted meanwhile cannot lend its rights to the earlier turn. At read time the person must still be
   an active member with access to the session's workgroup (design review: a removed member keeps nothing).
6. **A child's person is the one of the instruction its turn works on** (design review, high): set when the child
   accepts the instruction's prompt (prompt ID = operation ID), like the turn actor of a main session; queued
   instructions of other people change nothing until their turn. One function (`acting.stamp`) decides reads,
   `triggered_by` and personal connections.
7. **`my_access` instead of a new field.** `list_workgroups` keeps its shape; `my_access` now is the session's
   access (read for another readable workgroup). An older guest validates results against its own contract; a new
   field would make every `list_workgroups` answer invalid there.
8. **The intersection is a runner test, not a shared vector.** Only the runner evaluates session reads; the hub
   serves the device metadata of its assigned workgroups and never knows for whom a read is. The key model's
   "vectors in hub and runner" (§8 point 6) is met by the runner's table of cases plus the attack E2E; recorded as a
   deviation.
9. **The hub cannot widen a read.** The task's workgroup comes from the hub's row, but content comes only from
   events sealed in the checked workgroup's scope; a lying hub yields at most an empty answer.
10. **Limits are hub settings, not organization settings.** The hub enforces them (check H4), so an organization
    setting would be plaintext organization metadata with a new route, screen, audit and parity rows; A5 asks for
    settings with generous defaults and a clear message, which operator settings like those of ADR-075 give. 0 = no
    limit, as in ADR-075. Defaults: depth 5, 50 running per task, no total, no runtime limit (the package's
    proposal). The runner reads them from the device state section `limits` – no copy anywhere; an older hub sends
    none and the runner then does no local pre-check (the hub decides).
11. **Suspended children do not count as running**; resuming one for an instruction checks the running limit and the
    VM's room (`AGENT_RUNNER_SESSIONS_PER_VM`) first – 50 children in one 8 GiB VM would otherwise end in a crash
    instead of a message (design review, medium).
12. **Older guests keep the one-shot child.** A guest before ADR-088 validates `get_subagent_result` against the
    former contract (no text while `idle`) and does not know the new operation types; children on such a VM end
    after their first turn as before, and `get_context.limits` is answered within the former range. The runner
    knows the guest version from the VM's golden record (guest version 0.3.0 marks the new guest).
13. **Newest instruction only.** `get_subagent_result` answers the newest instruction; the parent is pinged per turn
    and reads it then. Keeping every turn's text (design review, medium) would add a second sealed copy of
    confidential text on the device (ADR-038 avoided one); the one-hour window of the outbox stays.
14. **One instruction per turn, in order.** The next instruction of a child is handed over only after the previous
    one was accepted and its turn ended, so each has its own result (design review, medium).
15. **Reporting never cuts.** Guest health and metrics list every session; the heartbeat sends several complete
    frames when one is not enough (the hub validates every frame against 1000 items and 64 KiB). The 10 children of
    `GET /v1/sessions/{id}` (the former total limit) became a bound of 1000 IDs in one answer.
16. **Completeness check (E-19).** `tests/platform/test_fixed_limits.py` with the inventory
    `tests/platform/fixed_limits.json`: every bound found by name (`MAX`/`LIMIT` anywhere), literal slice or SQL
    `LIMIT`, and every contract `maxItems`/`maximum`, has a reason from a closed list; "quantity" is no reason. Counts
    of things people create that are still fixed (pairings, GitHub records, live streams per browser, tabs, work-VM
    slots, repository and branch lists of a workgroup, task sessions in one answer, queue sizes) carry the reason
    `open` – they are listed in the report as the next step of A14, not silently allowed. The page sizes of KOR-35
    carry `page`.
17. **Situation view.** `limit_exceeded` is its own reason `blocked/limit_reached` (it was the generic
    `operation_refused`), so the view says that a limit of the platform was reached.
18. **E2E-24 sets the former limits** through a new orchestrator route (`POST /hub/subagent-limits`, a restart of the
    hub with other settings) instead of relying on defaults, so it keeps testing the refusals; E2E-67 sets the running
    limit to 1 for the clear message.
19. **Rollout order:** hub, runner, golden (ADR-088 §5).
20. **No new parity rows.** The web client gains no action (the limits are operator settings; follow-ups are a
    session's operations, visible in the operation lists that already have their commands).

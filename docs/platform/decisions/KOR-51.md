# KOR-51 – decisions

Package: agent roles and rights profiles – work, review and release kept apart, voluntary and never a precondition.
Concept references: [Idee 42](../KONZEPT.md#k-idee-42), [§10 Punkt 7](../KONZEPT.md#k-10-b7); rules
[E-04](../KONZEPT.md#e-04), [E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19);
interpretation [A1](../KONZEPT.md#a-1). Design: [ADR-090](../adr/090-rights-profiles-and-agent-roles.md), reviewed
with fresh context before the build (four high and five medium findings, all worked into the ADR).

1. **Profiles on the task, not on one main session.** A task gets a new main session after every end (03-api.md
   §4.6.3), so a profile on a main session would be lost at the next prompt. The task carries the profile of its main
   sessions; a child carries its own only when it was started with one.
2. **Effective rights are computed, assignments are snapshots.** The hub and the runner intersect the task's snapshot
   with every snapshot along the chain of parents at every check. A restriction a person sets later therefore reaches
   children started before it (review finding "narrow, then instruct an old child"); a changed or deleted definition
   never changes an assignment (no silent widening).
3. **Classes instead of lists.** Built-in roles are derived from one class per operation type
   (`contracts/ops/classes.json`), so a new type falls into the roles by its class; tests refuse a type without one.
4. **Work VM and browser tools are part of the profile.** They never become operations, so the runner checks them
   where they arrive (`workvm.call`) and the guest does not offer them (`--disallowedTools`); a `read_only` session
   could otherwise run commands in a work VM (review finding).
5. **The guest's check sits in the control process.** The API process cannot read a session's metadata; the control
   process marks an operation outside the profile `local_check: "rejected"` (`rights_denied`) when it records it, which
   the runner treats as binding. Hub and runner check it anyway.
6. **`child_profile` only for runners that send it.** The hub answers the child's effective profile only when the
   registration named one, because an older runner refuses unknown fields of the answer.
7. **Feature gate for assignments only.** Reads, work VM and browser tools are checked by the runner alone, so a
   profile cannot be assigned while an older runner serves the organization (`409 peer_outdated`). Clearing a profile
   is always possible.
8. **Audit details use `profile`, not `name`.** The contract tests forbid `name` in audit details (it could carry
   free text); the profile name is a closed pattern.

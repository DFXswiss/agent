# ADR-087: Sessions read like their human

Status: Accepted (2026-11-25) – supersedes [ADR-024](024-session-read-scope.md) (its default-workgroup addendum was
already superseded by interpretation [A13](../KONZEPT.md#a-13)); amends [ADR-058](058-admins-read-every-workgroup.md)
§1 ("platform sessions do not inherit") for reads.

Implements [§8 point 5](../KONZEPT.md#k-8-b5) ("alles lesen und entdecken, wofür sie im Kontext ihrer zugewiesenen
Rechte autorisiert sind … Keine pauschale Einsicht in fremde Workgroups"), [§8 point 6](../KONZEPT.md#k-8-b6) (reads
are logged), [Idee 43](../KONZEPT.md#k-idee-43) ("Berechtigte Plattformdaten können dennoch abgerufen werden"),
[E-11](../KONZEPT.md#e-11), [E-05](../KONZEPT.md#e-05) and interpretation [A11](../KONZEPT.md#a-11); follows the key
model's requirements for reading across workgroups ([key model §8](../key-model.md#8-reading-across-workgroups),
KM-34) and uses "triggered by" of [ADR-083](083-who-speaks-in-a-shared-session.md) §2.

## Context
- ADR-024 gave a session the rights of its task owner **intersected with the session's workgroup**: other workgroups
  were visible by name only, even when the owner could read them. A task that needed facts of another workgroup had
  to be created there. That is a restriction no rule asks for (E-11) and it contradicts Idee 43.
- A session works on instructions of several people (shared sessions, ADR-083). If it read with the owner's rights
  alone, a member B could read through A's session what only A may read ("Keine pauschale Einsicht in fremde
  Workgroups").
- Hub and devices are end-to-end encrypted: the device can only read a workgroup whose key it holds, and it holds
  keys only of the workgroups it is assigned to - directly or through the organization-wide assignment of ADR-075
  (KM-14). The hub's device routes already answer tasks of every assigned workgroup (`device/access.py`
  `reads_wg`); only the runner narrowed reads to the session's workgroup.

## Decision

### 1. Who reads: owner ∩ instructing human
- The **readers** of a read are the task owner and the **instructing human** of the turn the read belongs to: for a
  main session the acting user of its current turn (`runner.session.turn_actor`, ADR-070/ADR-083), for a child session
  the acting user of the instruction its current turn works on (the `subagent.request` or `subagent.instruct` whose
  prompt the child accepted last, ADR-088 §1). Both come from verified signed prompts, never from text.
- The instructing human is **captured when the read arrives**, in the journal transaction that handles the guest's
  `platform.query` record - the same order in which the guest journaled the turn's prompt acceptance - and handed to
  the tool with the call. The answer never re-reads it, so a prompt of another person accepted while the read is
  answered cannot lend its rights to the earlier turn.
- At the moment of the read the instructing human must still be an active member with access to the session's
  workgroup; otherwise every read is `forbidden` ("The person whose instruction this session works on is no longer a
  member of this workgroup.").
- A workgroup W other than the session's own is **readable** when, on the verified membership log at call time,
  - the owner and the instructing human both have **effective** access to W (`wg_access`: explicit `read`/`write`,
    or the inherited read of an admin, ADR-058) - what each of the two may read as a person (E-11). An archived
    workgroup is readable by its ID like for people; `list_workgroups` leaves it out as before (it takes no tasks).
  The credential scope is no workgroup and stays out of reach (ADR-058 §1 unchanged for credentials).
- **Instructing human unknown** (a turn with prompts of several people, or no verified author, `turn_actor` NULL):
  reads stay within the session's own workgroup and the organization level. Every author whose prompt reached the
  session has `write` there (the inbox verified `wg:write`), so this never widens anything (the "None never widens"
  rule of `agent_hub.sessions.attribution`). A read of another workgroup then answers `forbidden` with the sentence
  "This turn has instructions of several people (or none), so only this workgroup can be read. Ask one person to
  give the instruction." - for every other workgroup alike, so nothing is revealed.
- The **session's own workgroup** keeps its rule: the owner's explicit access (a session is never started for an
  owner who cannot write there); every instructing human has `write` there by the prompt check.
- Writes are unchanged: a session writes (operations, whiteboards, chat) only in its own workgroup and task.

### 2. Keys - way A only
- The device reads W with the workgroup key it already holds **because it is assigned to W** (directly or through
  the organization). This package adds no wrap rule, no scope and no wrap outside the log (key model §8 point 2;
  the log verifiers are used through the existing wraps only).
- Readable by the people but **not held by the device** (W not assigned): the read answers `unavailable` with the
  reason and what resolves it - "This execution device does not serve workgroup <name>; an admin of the
  organization can assign the device to that workgroup or to the whole organization." This is the exception of E-11
  (technically required), stated per E-05. `list_workgroups` shows such a workgroup as not readable.
- **The hub cannot widen a read.** The hub names the workgroup of a task (metadata), but content is only taken from
  events sealed in the checked workgroup's scope (`projection` filters by `scope_id`), so a hub that presents a task
  of an unreadable workgroup W2 as one of a readable W yields no content of W2 (at most an empty answer).
- Way B (a per-task read release signed by the owner and the instructing human, key model §8 point 3, decision D3) is
  not built; it is cryptography work outside this package and only needed when a session must read a workgroup whose
  device must not serve it.

### 3. The read tools (03-api.md §8.1)
- `list_workgroups`: names of every workgroup of the organization as before (names are organization metadata every
  member sees); `my_access` becomes the **session's** access: `read`/`write` of the owner for the own workgroup,
  `read` for another readable workgroup, null otherwise (no new field, so older guests keep validating the result).
- `list_tasks {workgroup_id?}`, `get_whiteboard {level, task_id?, workgroup_id?}`, `list_members {scope,
  workgroup_id?}`: an optional `workgroup_id` (default: the session's workgroup) selects another readable workgroup.
- `get_task`, `get_task_messages`, `get_whiteboard` level `task`: the task's workgroup comes from the hub's task row;
  a task of a readable workgroup is answered, any other is `not_found` ("There is no such task you can read.").
  `get_task` lists the task's sessions and operations (id, type, state); `list_operations {task_id?}` lists the
  operations of another readable task from the same hub metadata; `get_operation` (parameters and results, sealed
  events of the operation's own device) stays for the session's own task.
- An unreadable workgroup is `not_found` ("There is no such workgroup you can read.") - with the same answer whether
  it does not exist or the people may not read it. Its name stays visible in `list_workgroups` where every member of
  the organization sees it anyway.

### 4. Audit (§8 point 6, key model §8 point 5)
Every read keeps its `read.session.<tool>` audit row and `platform.read` event (03-api.md §8). The audit detail gains
`read_wg_id` (the workgroup read, null for organization-level reads) and `triggered_by` (the instructing human, null
when unknown); the row's own `wg_id` is the session's workgroup and the hub knows the task owner from the task. A
refused cross-workgroup read (`not_found`, `forbidden`, `unavailable`) is audited the same way with its result. These
are plaintext metadata at the hub like every audit row (IDs only, never content): the hub learns which session read
which workgroup and task when, on whose instruction - the read graph that §8 point 6 asks to log. The key model's
inventory of hub metadata (§6) names it.

### 5. What lies where (A7, key model §8 point 4)
Content of another workgroup that a session read is plaintext on the execution device and in the session's VM, as
everything the session works on (A7, §5 point 4), and it goes to the session's model provider as part of the
conversation (like everything the session reads); the session's logs (ADR-080) may contain it. A device assigned to the
whole organization holds the keys of every workgroup and can read all of them in plaintext - on a DFX device this is
the trust of §5 point 4 for the whole organization, so the assignment dialog says it (ADR-075 §5). What the session writes from it into its own workgroup - chat,
whiteboard, results, context checkpoints - is sealed under the **session's** workgroup key and readable by every
reader of that workgroup, the way a person copying a text makes it readable there (Idee 30). The harness tells the
session so; the security page says it. A stricter mode (a session reads only what every reader of its workgroup may
read) can come later as a voluntary workgroup setting (E-11), never as the default.

## Consequences
- A session can work with facts of the other workgroups its people may read, without a second task or a release.
- No one reads more through another person's session than they may read themselves; the attack "B instructs A's
  session to read a workgroup only A reads" is `not_found`.
- A device assigned to one workgroup only cannot read others; an organization that wants cross-workgroup sessions
  assigns the device to the other workgroups too or to the organization (ADR-075 §5) - with the plaintext consequence
  of §5.
- **Where the intersection is evaluated.** Only the runner evaluates session reads (the hub serves the device the
  metadata of its assigned workgroups and never sees which person a read is for), so the intersection function lives
  in `agent_runner.queries.rights` and its table of cases (owner, instructing human, admin inheritance, a removed or
  downgraded instructing human, unknown human, device not assigned) is a runner test, not a shared vector of the hub
  (a deviation from the key model's "hub and runner", recorded in `decisions/KOR-45.md`).
- Older guests (golden before this ADR) keep working: the new arguments are refused there by the guest's own schema
  check, results keep their former shape.

## Alternatives considered
- Owner's rights alone: lets a co-instructing member read through the owner's session (rejected, §8 point 5).
- Explicit access only, without the admin's inherited read: a session would read less than its admin owner and
  admin instructor may read as people (E-11).
- Fetching keys of non-assigned workgroups at task creation (way B): a new entitlement in the membership log for all
  three verifiers; left to a cryptography package if wanted (decision D3).

## Deviation from DESIGN.md
None beyond ADR-001.

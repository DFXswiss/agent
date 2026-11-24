# KOR-46 – decisions

Shared sessions: the session knows who speaks, consequences belong to the person who instructed, the queue is
visible. Architecture in [ADR-083](../adr/083-who-speaks-in-a-shared-session.md); rules
[Idee 36](../KONZEPT.md#k-idee-36), [A15](../KONZEPT.md#a-15), [E-04](../KONZEPT.md#e-04),
[E-05](../KONZEPT.md#e-05), [E-19](../KONZEPT.md#e-19).

1. **Draft, review, build.** The draft of ADR-083 was reviewed by a reviewer with fresh context (forgeability,
   zero knowledge, withdrawal against the count-based routing, version skew, understandability). Taken over: the
   first prompt of every start carries the line too (`initial_prompt.speaker`); the line is part of the
   `UserPromptSubmit` decision (fail-closed), not an addon; a child session never gets a person's line; the
   imitation sentence for look-alikes in the text; the fallback for old guests only after the runner checked its own
   line against the contract; withdrawal only to devices that announced `prompt.withdraw` (else `peer_outdated`);
   every withdrawal ends visibly (`withdrawn`, `withdrawing`, `too_late`); a withdrawal before the routing reached
   the device is recorded; `withdrawn` never holds back a later prompt; `get_task_messages` leaves out withdrawn
   prompts; the hub accepts `triggered_by` only from the task's prompt authors; "through their agent" is worded as
   the hub's report; duplicate names are marked. Not taken over: a notice after a compaction (the next prompt
   carries the line again; recorded as a consequence in the ADR).
2. **The sender line is composed by the runner, attached by the guest.** The runner holds the verified author and
   the stored, self-signed `member.profile`; the guest only stores and hands out what the runner registered with
   exactly that prompt. The model provider sees members' names and IDs with the prompts (like the owner's name in
   the harness before); the hub sees nothing new of it.
3. **"Previous person" from what the device knows.** The change sentence compares with the author of the previous
   accepted prompt of the same task on this device; after a move to another device the first prompt carries the
   line without it. Deterministic and local; no new hub data.
4. **Via agent.** The runner cannot tell a prompt signed by the user from one signed by the user's agent (ADR-056:
   the agent signs as the user). The hub knows it (audit `event.append` with `agent_id`) and reports it in the
   `prompt` hint and in `prompt_queue.via_agent`; the line words it as the hub's report.
5. **Triggered by is the ADR-070 stamp.** No second attribution logic: operations use `operation.actor_id`, usage of
   a main session the turn's acting user when the record is processed, a child its inherited stamp, a work VM the
   acting user of its request. Null (several authors in one turn, none, old runner) is shown as "not attributed".
6. **Consumption per person is a view, not a booking.** `hub.charge.triggered_by` and a daily per-person sum; the
   organization is charged as before ([A4](../KONZEPT.md#a-4), [A15](../KONZEPT.md#a-15)).
7. **Withdrawal decided by the device.** The hub alone cannot know whether the guest already has a prompt; with the
   count-based routing (delivered = transitions into `working` plus refusals) a withdrawal decided by the hub could
   shift every later prompt. The device decides and reports through the ordered outbox; the hub decides alone only
   for a session without device. Prompts of a failed session wait for "Retry" and cannot be withdrawn.
8. **Device offline.** A prompt for a session whose device is offline (or that has no device because none is
   online) notifies its author once (`prompt.waiting_for_device`, `ref` = the event), also when the device goes
   offline while it waits; the queue shows `waiting_for_device`, the category the situation view (KOR-39) uses.
   Moving the session to another device is KOR-58.
9. **GitHub for the instructing person** was already built by ADR-070 (HOT-14): an operation runs only with the
   acting user's own connection (or the operator's account for the operator) and is held for that person otherwise;
   E2E-64 proves it again in the attack case of this package (Wendy without account in Ada's task: Wendy's card,
   nothing reaches GitHub). The choice among several accounts is KOR-47.
10. **Interface for KOR-45.** `triggered_by` of `hub.operation`/`hub.session` (and the runner's `acting.stamp`) is
    the person whose rights KOR-45 intersects with the task owner's ([A11](../KONZEPT.md#a-11)); null means the
    task owner's rights alone.
11. **E2E-64 in T1.** Two people, one device; the offline case cuts the runner's link to the hub.
12. **Independent review of the diff** (fresh context): no high finding. Fixed: a withdrawal report counts only while
    the reporting device still holds the prompt undelivered; the KOR-45 interface never reads null as "the owner
    alone"; consumption counts for a person only when that person wrote a prompt of the task; look-alike lines are
    matched after removing format characters and collapsing white space and punctuation; the platform's own
    continuation after a credit carries a platform line; an older guest is reported once per VM (`runner.alert
    speaker_unsupported`); parentheses in names become brackets; RULES.md says where the real line appears.
    Recorded, not changed: `get_task_messages` hides only the prompts this device withdrew; a person's line after a
    compaction comes back with the next prompt.
13. **Work VMs.** A work VM keeps the person whose turn requested it (`triggered_by` on its record); work VMs produce
    no `runtime` items yet, so their runtime is not attributed until such metering exists.
14. **Queue in the browser under load.** The queue reloads from the prompt count of the chat's own timeline and the
    task's session entries (compared by reference), never by scanning events, so a burst of thousands of events
    costs nothing extra (chat-volume spec: 31-40 % busy against the 50 % limit).

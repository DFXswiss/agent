# KOR-75 – Captured tasks like Jira tickets (E-41)

Owner rule [E-41](../KONZEPT.md#e-41) with interpretation [A22](../KONZEPT.md#a-22); design in
[ADR-102](../adr/102-captured-tasks.md).

1. **Reused, not rebuilt.** The task row (number, title in `task.meta`, states and transitions, owner, workgroup), the
   routes `POST /v1/tasks`, `POST /v1/tasks/{task}/prompts` (it already creates the first main session) and
   `POST /v1/tasks/{task}/close`, the task lists with cursor and the overview of KOR-32, the member directory, the
   GitHub identities, the audit chain and the sealed `task.meta` (it already had `description`). New are four columns,
   one route, one event kind and the list filters.
2. **A captured task is `idle` without a session.** No new task state: `idle` already means "waits for someone".
   The situation view and the main agents of a person skip it until its first prompt.
3. **closed ⇔ completed is a trigger, not code in every writer.** Every path that closes or reopens (user, hub,
   follow-up prompt, the previous hub code) keeps the status right; a check states it.
4. **The hub filters IDs and codes; texts stay sealed.** GitHub organisation and repository are sealed in
   `task.meta` and filtered in the client with the bounded scan of the title search; topic names are sealed
   `topic.profile` events. An opaque tag per repository was rejected (E-25): it changes with every key rotation.
5. **Unknown person or topic is `400 request_invalid` with the pointer**, no new error code (E-25).
6. **"Assign to an agent" is the start.** The platform's agent works on a task once its first prompt is sent ("Start
   agent", `task start`); a person or an agent account as assignee is the responsible member (A22), who may start it.
7. **Sessions are unchanged.** The device routes and the session tools keep their fields; a session works on its task
   as before. The agent tool and its MCP server (`search`/`run`) carry the new commands (E-04, E-26).
8. **The rule text of E-41 and A22 is the concept's** (develop); this package sets only the line "Umsetzung" of E-41.
   Its ADRs are 102 and 103 (099-101 were taken on develop meanwhile), its migration is `0042`.
9. **The task dashboard is the task overview, widened** ([ADR-103](../adr/103-task-dashboard.md)): a direct order of
   the client, delivered with E-41, without a rule of its own in the concept. Every
   organization of the user and every readable workgroup, the same rows, filters and window; new are the organization
   filter, the order "most urgent first", the counters and the link on Home. The agent tool's `task overview` became
   the same dashboard (`task list --org` without an attention category too); no second command.
10. **Counters come from the hub, as facets.** The lists are paged, so the client cannot count; the hub counts by the
    indexed codes of ADR-102 over the readable workgroups. A status counter ignores the status filter and an urgency
    counter the urgency filter: the number on a counter is the number of rows a click on it gives. The sealed filters
    narrow only the list.
11. **"Prio" is the urgency of E-41**, no second field.
12. **The start page's three groups are a filter of the dashboard, from one rule.** In progress, waiting for a question
    and no agent come from `OVERVIEW` / `overview_of` in `agent_hub.attention.derive` over the attention records – the
    identical definition the start-page package carries; whoever is merged first lays it down, the other builds on it.
    A captured task that was never started therefore needs its attention record: it gets `done/idle` (no change to
    the derivation; before, the record was skipped for a task without a session) and counts under "No agent".

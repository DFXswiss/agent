# KOR-75 – Captured tasks like Jira tickets (E-41)

Owner rule [E-41](../KONZEPT.md#e-41) with interpretation [A22](../KONZEPT.md#a-22); design in
[ADR-100](../adr/100-captured-tasks.md).

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
8. **The links to E-41/A22 resolve once the concept package lands on develop**; this package does not change
   `KONZEPT.md` (the orchestrator keeps it).

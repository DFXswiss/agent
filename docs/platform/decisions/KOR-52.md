# KOR-52 – decisions

Main agents with names, their people and their actors. Architecture in
[ADR-095](../adr/095-main-agents-with-names.md); rules [Idee 31](../KONZEPT.md#k-idee-31),
[§6 point 4](../KONZEPT.md#k-6-b4), [E-02](../KONZEPT.md#e-02), [E-04](../KONZEPT.md#e-04),
[E-19](../KONZEPT.md#e-19), [A15](../KONZEPT.md#a-15).

1. **Draft, review, build.** The draft of ADR-095 was reviewed with fresh context (understandability, zero knowledge,
   audit model). Taken over: a plain summary with an example and a word list (a task's main agent is not a person's
   delegated agent of ADR-056/082; the endpoints are `people/agents` and `main-agents`, the audit side map `actors`);
   `on_behalf_of` of an operation row is the operation's own `triggered_by` with null kept (never the session's – a
   turn with several authors is not pinned on one person); a closed rule for the rows that name the agent, the kind
   taken from the session's depth; the old actor kind `session` stays allowed and unwritten; responsibility = task
   owner, the instructing people stay visible where they acted; how the runner learns names and handles at call
   time; the agent tool commands; `on_behalf_of` named as a device report. Not taken over: extending the attention
   endpoint with an owner filter (its order is per category; a person's agents are ordered by activity) and a flat
   per-task subagent number (the dotted path shows whom a helper reports to); a check that `on_behalf_of` wrote a
   prompt of the task (a further protection layer nobody ordered, E-25 – the device report is labelled as such).
2. **Handle instead of a unique name.** The hub cannot see names, and one browser does not know every name of a
   large organization, so uniqueness of a name cannot be checked. The handle `#<number>` (a per-organization counter
   at the hub, metadata it already has as the count and order of tasks) makes every shown name unique; two agents
   may carry the same name.
3. **Automatic name from the current title.** No event is written at creation: every reader derives the name from
   the title by the rule of `contracts/tasks/agent-name.json`, so old clients, the agent tool, a task created by an
   agent and tasks from before this package all get one. It follows a session's `task.set_title` until a person names
   the agent; the handle stays.
4. **Name per task, not per session.** All main sessions of a task continue one conversation (ADR-072); the name
   belongs to that agent. Subagents get no name of their own – "Subagent #42.1 of <name> #42" with their purpose is
   what identifies them, and it is what the session learns about its children.
5. **Numbers by trigger.** `task.number` and `session.agent_path` are set by BEFORE INSERT triggers, so every insert
   path – also test fixtures and a later code path – gets them; the counter table `task_counter` serialises only
   task creation within one organization.
6. **Grouping, not a new navigation entry.** "By person" is a switch of the situation view (`?by=person`): the
   counters and the composer stay; the concept asks for one dashboard that shows responsibilities, and a further menu
   entry would split it.
7. **People ordered by ID at the hub, by name in the browser.** Names are sealed; the hub orders the caller first and
   the rest by user ID; the browser sorts the loaded rows by the names it can read.
8. **Roles.** KOR-51 landed during this package: the agent row shows the task's rights profile as the main agent's
   role and the tree the role of every session (KOR-51's badge).
9. **Volume gate.** The KOR-35 volume gate is not on the integration branch yet; the hub and browser volume tests of
   this package follow the situation view's (KOR-39) form.
10. **Independent review of the diff.** No zero-knowledge or hygiene finding. Fixed: `on_behalf_of` only for an
    active member and only on agent actors; a partial index for the newest `task.agent_name` of a task (task rows
    stay cheap on long histories); the task counter never falls behind an existing number; trimmed one-line names in
    the agent tool; clearer help of `agents` (delegated agents vs. main agents of tasks). Accepted: the runner names a
    session from the newest *verified* name it holds (it never stores unverifiable events; ADR-095 §3 says so);
    `main-agents` pages by last activity, which may move while paging (the browser removes duplicates; the next
    refresh shows the current order); the browser sorts all loaded person rows by name (with more than one page a
    later page may sort in above – a person list of more than 50 rows is rare and the user's own row stays first);
    E2E-71 is booked under milestone 3 like the other scenarios of this series. Numbers that `develop` took in the
    meantime (migrations, ADR, E2E) are reassigned by the merge tool.
11. **No fixed depth of a handle.** Since KOR-45 the subagent depth is a setting without an upper bound, so a path
    has as many levels as the tree is deep (no fixed cap in the hub's check, the contracts, the runner or the web);
    a child takes its parent's path, and a child of a main session (a parent without a path) the next first-level
    ordinal of the task. The new bounds of this package are listed with their reasons in the inventory of fixed limits.
12. **A timing-dependent test of the agent tool (E-19).** The merge pre-check failed once on a missed line of the
    remote pairing's wait loop: its test confirmed the pairing after a fixed 0.3 s, which under load came before the
    tool's first look, so the loop never waited. The test now confirms right after the first look, so it always covers
    the wait.

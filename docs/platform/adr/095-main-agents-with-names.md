# ADR-095: Main agents with names, their people and their actors

Status: Accepted (2026-11-25)

Implements [Idee 31](../KONZEPT.md#k-idee-31) (people, main agents and subagents are clearly distinguishable; main
agents have names and GitHub username assignments, shared or individual; subagents report to their main agent; the
dashboard shows responsibilities and relations), [§6 point 4](../KONZEPT.md#k-6-b4) (actor of every action),
[E-02](../KONZEPT.md#e-02) (ten or a thousand agents), [E-04](../KONZEPT.md#e-04) (everything without a browser),
[E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19) (no view fails on volume) and [A15](../KONZEPT.md#a-15)
(consequences belong to the person who instructed). Builds on ADR-076 (situation view), ADR-078 (GitHub identities)
and ADR-083 ("triggered by").

## In short
Anna starts a task "Fix the login bug". Its agent is called **Fix the login bug #42** – the number is unique in the
organization, the name is readable and Anna (or anybody who may write in the workgroup) can change it, e.g. to
"Login doctor #42". The agent's helpers are **#42.1**, **#42.2**, and a helper's helper **#42.1.1**. Each one knows
its own name and its helpers'. The agent acts on GitHub as the account chosen for the task (shown next to it). The
audit says "Subagent #42.1 of Login doctor #42 on behalf of Wendy" when Wendy gave the instruction. The home page can
be grouped by person: Anna – 12 agents, 2 waiting for her; Wendy – 3 agents. With a thousand agents a person's row opens
a paged list. The server stores only the number; the name is encrypted like the title.

## Words
- **Main agent**: the agent of one task, across all its main sessions (depth 0; a follow-up after `stopped`/`failed`
  starts a new main session that continues the conversation, ADR-072).
- **Subagent**: a child session (depth ≥ 1) a session of the task requested (`subagent.request`); it reports to the
  session that requested it (its parent), which belongs to the same main agent.
- Not to be confused with a person's **delegated agent** (KOR-24, ADR-056/082: `agent_id` of audit rows,
  `/v1/me/agents`), which acts *as* its person from outside the platform. This ADR never uses "agent" alone for it.

## Context
- The hub told main and subagents apart only by `depth`/`parent_session_id`; there was no name and no short handle,
  and the session knew neither.
- The audit attributed everything a session did (operations, child starts, hook refusals, platform reads) to the
  **device** that reported it; whose instruction it followed was only in some details.
- The situation view (ADR-076) groups tasks by attention; it did not answer "which agents does each person run, as
  whom on GitHub, with which helpers".
- Names are content: the hub must not read them (04-crypto.md §7), so it can neither check uniqueness nor sort by name.

## Decision

### 1. Handle and name
- **Handle** (unique, plaintext metadata): the hub numbers the tasks of an organization when they are created
  (`task.number`: 1, 2, 3 … per organization, never reused; a per-organization counter in the creating
  transaction). The main agent's handle is `#<number>`. A subagent's handle is `#<number>.<agent_path>`: the hub sets
  `session.agent_path` when it creates the child, under the task lock it already holds – a child of a main session
  gets the next ordinal among the task's depth-1 sessions (children of a later main session never repeat an earlier
  handle), a deeper child its parent's path, a dot and the next ordinal among the parent's children; `(task_id,
  agent_path)` is unique. Existing tasks and children are numbered by the migration in creation order.
- **Name** (readable, sealed): the newest verified `task.agent_name` event of the task (§2); without one, or when it
  names null, the name is **automatic**: derived from the task's current title by the fixed rule of
  `contracts/tasks/agent-name.json` (white space collapsed, the first five words, at most 40 code points, cut at a
  space; empty → `Agent`), the same in browser, agent tool and runner. An automatic name follows the title (also a
  session's `task.set_title`) until a person sets one – accepted: it stays recognizable by its handle.
- **Shown** as `<name> #<number>`; a subagent as `Subagent #<number>.<path>` "of `<name> #<number>`", with its purpose
  where the reader can open it (sealed `operation.params` of its `subagent.request`). Subagents have no name of their
  own. The handle makes the shown text unique; two main agents may carry the same name.

### 2. Renaming is a sealed event
- New event kind `task.agent_name` (scope `wg`, header `task` set, `session`/`ref` empty; author kind `user` – a
  person's delegated agent writes as its person, ADR-082; right `wg:write`, like the title), body `{name}`: 1–80
  characters on one line, or `null` (back to automatic). It is appended like every event (`POST /v1/events`
  of 03-api.md §4.4, sealed under the workgroup key). The hub stores the header only and names the newest one in the
  task row (`name_event_id`).
- Readers take the newest **verified** event of the task; one whose body breaks the schema (the hub cannot check
  the sealed body) or that does not verify is shown as unverifiable, never replaced by an older one (as titles,
  06-web.md §1).

### 3. The session knows who it is
`get_context` (03-api.md §8.1) adds `agent {kind:"main"|"subagent", handle, name, name_source:"given"|"automatic",
main:{handle,name}, parent:{session_id,handle}|null}` (a subagent's `name` is its main agent's) and
`children:[{session_id,handle,state}]` (its own children, oldest first, at most 100). The runner reads at call time:
`number` and `name_event_id` from the task row and the sessions with `agent_path` from `GET /v1/device/tasks/{task}`,
the name from the newest verified `task.agent_name` / `task.meta` events it holds (the runner keeps only verified
events, so where a browser would show an unverifiable newest name the session sees the newest verified one or the
automatic name) (child sessions run on their parent's
device, which pulls the workgroup's events). RULES.md tells the agent that `get_context` names it and its helpers.
Only a guest from 0.5.0 gets `agent` and `children` (`guest_features.NAMES`): a guest validates answers against the
contracts bundled with it, and an older one refuses an answer with fields they do not allow, so its call would time out
(HOT-23; `tests/platform/guest/test_contract_version.py` ties the guest's contracts to its version).

### 4. The actor of what a session does is the agent, on behalf of a person
- New `actor_kind` values `main_agent` and `subagent`, chosen by the hub from the session's `depth` (never from what
  a device says); `actor_id` = the session, `device_id` = the device that reported it. The old value `session` stays
  allowed for compatibility and is not written.
- **Closed rule.** The hub writes the agent as actor for exactly these rows: (a) `operation.<state>` of an operation
  registered for a session (the registration and every outcome the device reports); (b) `session.requested` of a
  child: actor = the requesting session, `session_id` = the child; (c) every outbox `audit` item that names a session
  (`03-api.md` §6.1, closed list `outbox-audit-actions.json`). Everything else a device reports (session state
  transitions, context uploads, outbox rejections, event batches, enrollment, connections, rows without a session)
  keeps `actor_kind: device`.
- **On behalf of.** New column `on_behalf_of` (a user, or null): for (a) the operation's `triggered_by` as stored
  (ADR-083 §2; null stays null – a turn with several authors or none is not pinned on one person); for (b) the child's
  `triggered_by`; for (c) the session's `triggered_by`. Like `triggered_by` it is the **report of the device** in
  `device_id`; the hub checks only that it names an active member of the organization (ADR-078 H2a). The value of an
  operation row is the same as its `detail.triggered_by` (one source: the operation row).
- **Hash chain.** `on_behalf_of` is part of the hashed row only when set (as `agent_id`, KOR-24): earlier rows keep
  their hashes and are not rewritten. Updated readers: the hub's `row_json`/`verify_chain`, the response contracts
  (`on_behalf_of` optional), the web audit view and the agent tool; a reader that rebuilds rows from a fixed field
  list must add the field.
- **Audit API.** `GET /v1/orgs/{org}/audit` adds, outside the hashed rows, `actors`: for every `main_agent`/`subagent`
  actor of the page `{task_id, number, agent_path, parent_session_id}`. Filters: `actor` may name a session; new
  `on_behalf_of=<user>`. Visibility is unchanged (a member sees rows of readable workgroups and their own actions).
- **Display.** "Main agent `<name> #42` on behalf of `<person>`", "Subagent `#42.1` of `<name> #42` on behalf of
  `<person>`"; without a person "… (no single person)"; the agent tool prints the same text.

### 5. Responsibilities: a grouping of the situation view
- **Responsible** for a main agent is the owner of its task (the person who created it, the signed author of the
  first `task.meta`). Others who instruct it (shared sessions, ADR-083) are shown where they acted: the operation and
  session rows ("triggered by") and the audit ("on behalf of").
- The situation view gets a switch "By situation | By person" (kept in the URL, `?by=person`), not a new navigation
  entry. By person: one row per person with open main agents in a readable workgroup – number of main agents,
  waiting for a decision, stuck, running, active subagents; the user's own row first, the rest of a page sorted by
  name in the browser. A row expands into that person's open main agents (newest activity first): name and handle,
  workgroup, state and reason, the GitHub identity (§6), active/total subagents, "Rename" for writers, and the agent
  tree on demand (handles, purposes, states, identities). Closed tasks are not listed (they stay in "Recently
  finished").
- Endpoints (org:member, readable workgroups only, metadata only; the hub sees nothing new):
  `GET /v1/orgs/{org}/people/agents?after=&limit=` → `{people:[{owner_id,agents,needs_decision,blocked,working,
  subagents_active}],next_after}` (the caller first, then by `owner_id`; `limit` default 50, max 100) and
  `GET /v1/orgs/{org}/main-agents?owner=&before=&limit=` → `{items:[{task,attention}],next_before}` (open tasks of
  `owner`, `last_activity_at DESC, task_id DESC`, `limit` default 50, max 100; `attention` null while the hub has no
  record).
- **Volume** (E-02, E-19): both are paged by the hub and windowed in the browser (at most 50 rows in the DOM per list,
  the window of the situation view); the counts are one aggregate over the open tasks of the readable workgroups
  (partial index `task (org_id, owner_id, last_activity_at DESC, id DESC) WHERE state <> 'closed'`). With four people
  the view is four rows; with a thousand agents a person's row opens a paged list, never a full tree. The volume gate
  of KOR-35 is not on the integration branch yet; this view carries its own volume tests (hub and browser) in the form
  of the situation view's (ADR-076) and is registered with that gate when it lands.

### 6. The GitHub identity is shown where the agent is
The task's chosen identity (ADR-078 §3; "automatic" = the instructing person's own account) is shown in the agent row
of the person grouping, in the agent tree (a child that acts with another identity shows its own) and in the task
header (since KOR-47). Shared (a company account of the organization) and individual (a person's own account) are
told apart by the record's owner kind; another person's own account that the browser cannot name is "another
person's account".

### 7. Agent tool (E-04)
`agent-platform agents people --org ORG` (the person rows), `agents list --org ORG [--owner me|USER]` (a person's
main agents with name, handle, state, identity, subagents), `agents show --task TASK` (the tree with handles),
`agents rename --task TASK (--name TEXT | --automatic)` (seals and appends `task.agent_name`), `task list
--by-person`, and `audit --org ORG --on-behalf-of USER`; audit rows print the actor as in §4. Rows in
`docs/platform/agent-parity.md` and examples in `docs/platform/agents.md`.

### 8. Roles
The agent row and the agent tree show the role of an agent: the rights profile of KOR-51 (ADR-094) – a main
agent's is its task's profile, a subagent's its own or else its parent's.

## Consequences
- The hub learns no content: `task.number`, `agent_path`, `name_event_id` and `on_behalf_of` are counters, IDs and a
  person ID it already knows in other columns (04-crypto.md §7). Two agents with the same name are possible and told
  apart by the handle.
- Old clients ignore the new fields; an old runner's `get_context` lacks `agent`, and so does the answer to a guest
  before 0.5.0 (its VM gets the names with the next golden); an older web client shows the new
  actor kinds by their kind name. A device filter (`actor=<device>`) no longer finds the operations of its sessions;
  the device stays in `device_id`.
- `on_behalf_of` is as trustworthy as the device that reported it, as `triggered_by` is (ADR-083).

# ADR-100: Captured tasks – a task is captured like a ticket, assigned and prioritised, and an agent starts only when someone starts it

Status: Accepted (2026-11-26)

Implements [E-41](../KONZEPT.md#e-41) with its interpretation [A22](../KONZEPT.md#a-22) ("ich will tasks erfassen
können, analog jira …"), under [E-04](../KONZEPT.md#e-04)/[E-26](../KONZEPT.md#e-26) (the agent tool and its MCP
server can do the same), [E-21](../KONZEPT.md#e-21) (the task overview) and [E-25](../KONZEPT.md#e-25) (exactly what
was ordered). Builds on the task row and its states (02-data-model.md §2.4), the task overview of KOR-32, the
membership log (members and agent accounts, [ADR-071](071-agent-own-account.md)), the GitHub connections
([ADR-077](077-github-every-repository.md), [ADR-078](078-github-identities.md)), the audit chain and the sealed
`task.meta` event (04-crypto.md §4.3).

## Context
- Every task begins with a prompt and gets its main agent at once (§2 point 5, idea 43). There is no way to write down
  "topic xy must be looked at" without starting an agent, nor to say who is responsible, how urgent it is, or which
  GitHub organisation and repository it concerns.
- The hub is zero-knowledge ([ADR-003](003-zero-knowledge-hub.md)): titles, workgroup and member names, pull-request
  addresses ([ADR-084](084-fix-conflicts-by-merge-commit.md)) are sealed; hub columns carry IDs, codes, numbers and
  times only ([ADR-046](046-hub-columns-against-the-visibility-list.md)).
- What exists and is reused: the task row with number, title (`task.meta`), state machine with transitions, owner and
  workgroup; `POST /v1/tasks`; `POST /v1/tasks/{task}/prompts`, which already creates a main session when the task
  has none or its last one ended; `POST /v1/tasks/{task}/close`; the task lists with cursor and the overview's merge of
  several workgroups; the member directory (`member.profile`, `member.name`); the GitHub identities and the
  workgroup's repository list; the audit chain; `task.meta` already has an optional sealed `description`.

## Decision

### 1. Data model
`hub.task` gets four columns (migration `0040_captured_tasks`):

| Column | Values | Default |
|---|---|---|
| `status` | `todo`, `inProgress`, `completed` | `inProgress` (existing and agent tasks) |
| `urgency` | `low`, `normal`, `high`, `urgent` | `normal` |
| `assignee_id` | a user of the organization (a person or an agent account), or null | null |
| `topic_id` | a topic of the organization (§3), or null | null |

The migration sets `status = 'completed'` for every closed task and `inProgress` for every other (A22: a running agent
task is inProgress, a closed one completed). A trigger keeps the one invariant **closed ⇔ completed**: a row written
with `state = 'closed'` gets `status = 'completed'`, a row that leaves `closed` with `status = 'completed'` gets
`inProgress` (so every existing path that closes or reopens a task – the user, the hub, a follow-up prompt – keeps the
status right without knowing it, also the previous hub code on this schema); a check constraint states it. Partial
indexes `(wg_id, <column>, last_activity_at DESC, id DESC)` bound the filtered lists.

A **captured task** is a task row without any session: state `idle` ("waits for the user", 02-data-model.md §2.4.1),
`main_session_id` null, status `todo`. It needs no new task state: every reader that knows task states shows it, and
an older web client shows it as an idle task.

### 2. What the hub sees (the question of this package)
The hub filters by **IDs and codes only**; every text stays sealed as before:

| Field | Where | Filter |
|---|---|---|
| status, urgency | `hub.task` (codes) | at the hub |
| person (`assignee_id`) | `hub.task` (user ID, like `owner_id`) | at the hub |
| topic (`topic_id`) | `hub.task` (ID); the name sealed in `topic.profile` (§3) | at the hub |
| title, description | `task.meta` (sealed, as the title today) | title search in the client (KOR-32) |
| GitHub organisation, repository | `task.meta` (sealed: `github_org`, `github_repo`) | in the client, like the title search |

GitHub organisation and repository stay sealed because the hub never held a repository name in clear (the PR index is
an opaque tag, ADR-084); a plaintext column would be the first. The client already opens every listed row's
`task.meta` for the title, so it filters by organisation and repository at no extra request – with the bounded scan of
the title search ("searched n tasks", "search further", ADR-086). An opaque tag per repository was considered and
rejected (E-25): it would change with every key rotation and need its own derivation in three implementations.

### 3. Topics
A topic is an org-scope event of the new kind **`topic.profile`** `{name}` (1–100 characters, one line), header `ref`
= the topic ID (UUIDv7), `wg` and `task` empty, sealed under the organization key like `wg.profile`. Right
**`org_member`**: every member of the organization writes it (agents with their user's right, E3/E5 as for any user
kind); so a topic is created while capturing, by whoever captures. Renaming is a newer `topic.profile` with the same
`ref` (the newest wins, as for profiles). `device_pull: false` – sessions do not need topic names. The list of an
organization is the newest `topic.profile` per `ref` of its org scope (`GET /v1/scopes/{org}/events?kinds=topic.profile`,
existing route). The hub accepts a `topic_id` only when a `topic.profile` with that `ref` exists in the task's
organization (`400 request_invalid` with the pointer otherwise). Topics are not deleted (a task keeps its topic).

### 4. `task.meta` carries the sealed ticket fields
`body_task_meta` gains two optional fields beside `title` and `description`: `github_org` (a GitHub login, `""`
clears) and `github_repo` (`owner/name`, `""` clears; its owner is `github_org` when both are set). Readers take each
field from the newest `task.meta` that carries it (a device's `task.set_title` writes only `title` and so changes
nothing else). Clients write the full current set of fields on every change. Older readers read only `title`.

### 5. Contracts
- **`POST /v1/tasks`** takes `events` with one or two items. One item (`task.meta` only) **captures** a task: no
  session, no scheduler decision, no device, state `idle`, status `todo`, answer `{task, session: null, events}`. Two
  items (`task.meta`, `prompt`) create a task with its agent exactly as today, status `inProgress`. Both take the
  optional `urgency`, `assignee_id`, `topic_id` (`400 request_invalid` with the pointer for a value outside its set,
  a user that is no active member of the organization or a topic the organization does not have); `analysis` needs
  the prompt (`400` with one item). A repetition must name the same fields (`409 id_conflict`). Audit `task.create` gains
  `captured: true` for a captured task.
- **`PUT /v1/tasks/{task}/fields`** (`task:write`) with at least one of `status`, `urgency`, `assignee_id`,
  `topic_id` (null clears the last two) answers `{task}`; audit **`task.fields`** with the changed values (codes and
  IDs). `status: completed` closes the task (the close of `POST /v1/tasks/{task}/close`: live sessions stop, audit
  `task.close`); `todo` or `inProgress` on a closed task reopens it without an agent (state `idle`, a transition by the
  user). A status never starts an agent.
- **Starting** a captured task, or assigning it to an agent, is its first prompt: `POST /v1/tasks/{task}/prompts`
  (unchanged route) creates the main session; the hub then sets `todo` → `inProgress`. The clients' "Start agent"
  sends the title and description as that prompt (editable). The task is the same task – number, fields and history
  stay.
- **`GET /v1/workgroups/{wg}/tasks`** gains the filters `status`, `urgency`, `assignee` (a user ID or `none`) and
  `topic` (a topic ID or `none`), combinable with `state`, `order` and the cursor.
- **`task_row`** carries `status`, `urgency`, `assignee_id` and `topic_id` (absent from an older hub; clients read a
  missing status as derived from the state).
- The device routes and the session tools are unchanged: a session works on its task as before.

### 6. Readers that assumed an agent
The situation view derives no attention record for a task without a session (`attention.refresh` and the backfill skip
it), and the main agents of a person (ADR-095 §5) count only tasks with a main session – a captured task is no agent.

### 7. Clients
- **Web.** The task list of a workgroup and the overview get "Capture task" beside the composer: title, description,
  person (members of the organization, agent accounts included), topic (choose or create), GitHub organisation and
  repository (suggestions from the GitHub identities' accounts and the workgroup's repository list, free input
  allowed), urgency (preset normal). The rows show status, urgency, person and topic. Both lists filter by person,
  topic, GitHub organisation, repository, urgency and status. The task page shows the fields with "Edit", the status
  as a choice, and for a task without an agent "Start agent".
- **Agent tool and MCP (E-04, E-26).** `agent-platform task capture`, `task edit`, `task start`, `topic list`, `topic
  create` and the filters of `task list` (`--status --urgency --assignee --topic --github-org --repo`); `task show`
  and `task list` print the fields. The MCP server reaches them through `run`/`search` as every command.

## Consequences
- A ticket costs nothing until someone starts it: no session, VM or tokens; it waits as `todo` in the lists.
- Every agent task carries the same fields; existing tasks are `inProgress` or `completed` after the migration.
- The hub learns who is responsible for a task, its topic ID, urgency and status – IDs and codes like owner and state
  today; it learns no topic name, title, description, organisation or repository.
- Filtering by GitHub organisation or repository reads pages in the client (bounded, with the count of searched
  tasks), as the title search does.
- Rollout: the hub first (migration, routes, `topic.profile` in the hub's kind table), then the web client and the
  agent tool. Runners need nothing: they never receive `topic.profile` and read only `title` from `task.meta`.

## Specification and evidence
- 02-data-model.md §2.4 (columns, invariant), 03-api.md §4.6 (routes, filters, errors), 04-crypto.md §4.3 (kind,
  right), §7 (hub columns); contracts: `api/tasks-create.request.json`, `api/task-fields-put.*`,
  `api/workgroup-tasks.request.json`, `api/objects.json#task_row`, `common.json` (`task_status`, `task_urgency`),
  `events/topic.profile.json`, `events/objects.json#body_task_meta`, `enums/event-kinds.json`,
  `audit/task.fields.json`, `audit/task.create.json`; `agent-parity.md`.
- Evidence: hub tests (capture, fields, filters, status on start/close/reopen, invariant on old paths, topic and
  assignee checks, attention and main agents skip captured tasks, migration of existing rows); contract tests;
  web unit tests and end-to-end tests on desktop and phone (capture, edit, filter, start); agent tool tests including
  the MCP `run`.

## Alternatives considered
- A separate ticket object beside the task: two lists, two numbers and a hand-over when the agent starts; the task is
  already the unit of §2 point 5.
- A new task state `todo`: every state machine, filter and older client would have to learn it; `idle` without a
  session already means "waits for someone".
- Plaintext topic names, organisations and repositories at the hub: simpler filters, but the first plaintext content
  at the hub.

## Deviation from DESIGN.md
None beyond ADR-001.

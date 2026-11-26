# ADR-101: Task dashboard – every task the user reads, across organizations and workgroups, filtered, counted and sorted

Status: Accepted (2026-11-26)

A direct order of the client, delivered together with the captured tasks of [E-41](../KONZEPT.md#e-41)
([ADR-100](100-captured-tasks.md)) and without a rule of its own in the concept: "ganz wichtig, wir brauchen ein
dashboard für alle tasks! wo man filtern kann nach Zuständig, prio, status und co". Under
[E-04](../KONZEPT.md#e-04)/[E-26](../KONZEPT.md#e-26) (the agent tool can do the same) and [E-25](../KONZEPT.md#e-25)
(exactly what was ordered). Built from the task overview of an organization ([E-21](../KONZEPT.md#e-21), KOR-32), the
agent tool's `task overview` and the situation view per organization (KOR-39), and the ticket fields of
[ADR-100](100-captured-tasks.md) – not beside them.

## Context
- The task overview (`/o/:org/tasks`) lists the tasks of the readable workgroups of **one** organization; the agent
  tool's `task overview` lists working tasks of one organization; the situation view groups one organization's tasks
  by attention. A person in several organizations has no single list.
- ADR-100 gave every task status, urgency, person and topic (hub columns, codes and IDs) and the sealed GitHub
  organisation and repository; the lists filter by them. "Prio" is that urgency – no second field.
- There are no numbers per status or urgency: the lists are paged (ADR-086), so the client cannot count what it has
  not loaded.
- The hub lists tasks per workgroup only (`GET /v1/workgroups/{wg}/tasks`, right `wg:read`); the readable workgroups
  come from the verified membership log, and a workgroup the user cannot read is never requested.

## Decision

### 1. Counters: `GET /v1/orgs/{org}/tasks/counts`
Right `org:member`, audit read `read.org.tasks.counts`. The answer is `{by_status: {todo, inProgress, completed},
by_urgency: {low, normal, high, urgent}, total}` over the tasks of the workgroups of the organization the user can
read (the same rule as the situation view, `attention.read.readable_workgroups`; `wg` limits it to one readable
workgroup, an unreadable one is `404`). The hub filters of the lists apply: `state`, `status`, `urgency`, `assignee`,
`topic` (ADR-100 §5). **Facets:** the counters per status ignore the `status` filter and the counters per urgency the
`urgency` filter, `total` applies both – so every counter says how many rows a click on it gives, also while another
status or urgency is chosen. The sealed filters (title search, "only mine", GitHub organisation and repository) never
reach the hub and narrow only the list. One `GROUP BY status, urgency` over the indexed columns of ADR-100; the
answer carries codes and numbers only.

### 1a. The start page's three groups as a filter
The overview of the open tasks that the start page shows as three numbers – **In progress**, **Waiting for a
question**, **No agent** – is offered by the dashboard as a filter and as a group of counters, from the same rule: the
mapping `OVERVIEW` / `overview_of` in `agent_hub.attention.derive` over the tasks' attention records (`working` →
in progress; `needs_decision` → waiting; `blocked` and `done` except `done/closed` → no agent; a closed task is in
none). There is no second state logic. Both the start-page package and this one carry the identical definition; the
first to be merged lays it down and the other builds on it.

- `GET /v1/workgroups/{wg}/tasks` and `GET /v1/orgs/{org}/tasks/counts` take `overview=working|waiting|no_agent`
  (the attention record of the task belongs to the overview); the counts answer `by_overview {working, waiting,
  no_agent}`, a facet like the others (it ignores the `overview` filter).
- A captured task that was never started has the record `done/idle` (ADR-100 §6) and therefore counts under **No
  agent**; so does a task reopened by its status until its next prompt.
- A row frame does not carry the attention record: a list filtered by overview keeps the rows it holds and takes no new
  one from a frame, and loads again when a row of its workgroups appears or changes its state; the counters do the
  same.
- Agent tool: `--overview working|waiting|no-agent` on `task overview` and `task list`; `counts.by_overview`.

### 2. Order "most urgent first"
`GET /v1/workgroups/{wg}/tasks` takes `order=urgency` beside `activity` and `created`: urgency rank descending
(`urgent`, `high`, `normal`, `low`), then latest activity, then task ID, with a cursor of its own
(`{at, id, rank}`, opaque to the client). Clients merge several workgroups' lists in the same order (the cut-off of
KOR-32 applies to every order).

### 3. Web: the task overview becomes the dashboard
The route stays `/o/:org/tasks` ("Tasks" in the menu, the bottom bar on a phone) and gains a link on Home ("Dashboard of
all tasks"), one click each. It lists every task of every organization of `GET /v1/me` whose log is verified and every
workgroup there the user can read – captured tasks and agent tasks in one list – with "organization · workgroup" per
row when there are several organizations. On top the counters of the three groups of the open tasks (§1a), per
status and per urgency (summed over the organizations shown); a click on one sets that filter, a second click clears
it. Filters: organization, workgroup, state, the three groups, status, urgency (Prio), person (Zuständig), topic,
GitHub organisation and repository, "only mine", title search; they combine. Sort: latest activity (default), newest,
most urgent. The counters load again when a filter changes, when a row of the shown workgroups appears or changes its
state, status or urgency (a live row frame), and after a reconnect. "Capture task" stays (in the route's organization). Filters stay in the screen's state, not in the URL (a
search text in the address would reach the hub).

### 4. Agent tool: `task overview` and `task list`
`agent-platform task overview` is the same dashboard: every readable workgroup of every organization of the user
(`--org` one organization, `--wg` one workgroup), the filters `--state` (a task state or `open`), `--mine`,
`--search`, `--overview`, `--status`, `--urgency`, `--assignee`, `--topic`, `--github-org`, `--repo`, the order `--order
activity|created|urgency`; the answer has the rows with organization and workgroup, the organizations and workgroups
read, and the summed `counts`. `task list --org` without an attention category is the dashboard of that organization;
`task list --wg` takes the same filters, `--order` and `--search`. "Only mine" means the owner or the person whose
agent account wrote the task, as in the web. The MCP server reaches both through `run`/`search`.

## Consequences
- One list for every task a person may read; the numbers on top come from the hub and are right without loading
  every page.
- The hub answers counts per readable workgroup set – codes and numbers it already holds; it learns nothing new.
- A dashboard over many organizations requests one list per readable workgroup and one counter request per
  organization; the lists stay paged and bounded as before (ADR-086).
- Rollout: the hub first (route, order), then web and agent tool; an older hub answers the counter request with `404`
  and the dashboard says that the counts could not be loaded, the list works.

## Specification and evidence
- 03-api.md §4.6 (counts route, order `urgency`, `overview`), 06-web.md §5.1a (dashboard); contracts:
  `api/orgs-tasks-counts.request.json`, `api/orgs-tasks-counts.response.json`, `api/workgroup-tasks.request.json`
  (`order`, `overview`), `audit/read.org.tasks.counts.json`; `agent-parity.md` (`tasks-dashboard`).
- Evidence: hub tests (facets, readable workgroups only, one workgroup, `order=urgency` with paging, the overview
  filter and counters, a captured task's record `done/idle`, every record in exactly one overview); contract tests;
  web unit tests (counters, order, merge, two organizations, click on a counter) and end-to-end tests on desktop and
  phones (two organizations, counters, click to filter, person, organization, order, search); agent tool tests against
  the hub double and the real hub.

## Alternatives considered
- A new screen beside the overview: two lists of the same tasks (the order asks to build it from the existing overview).
- Counting in the client: wrong as soon as a list has a further page.
- One hub route over all organizations: the rights, keys and logs are per organization; the client already verifies
  each organization's log and merges.

## Deviation from DESIGN.md
None beyond ADR-001.

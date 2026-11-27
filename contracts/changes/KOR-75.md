# KOR-75 – captured tasks (E-41, ADR-102)

Additive changes; an older client never sends the new fields, an older hub never sends them in task rows.

- `api/tasks-create.request.json`: `events` holds one item (`task.meta` alone: a captured task without an agent) or
  two as before; optional `urgency`, `assignee_id`, `topic_id` (ADR-102 sections 1 and 5).
- `api/tasks-create.response.json`: `session` is null for a captured task; `events` one or two results.
- `api/task-fields-put.{request,response}.json`, `api/INDEX.json`: `PUT /v1/tasks/{task}/fields` (`task:write`) with
  at least one of `status`, `urgency`, `assignee_id`, `topic_id`; answer `{task}`.
- `api/workgroup-tasks.request.json`: filters `status`, `urgency`, `assignee` (ID or `none`), `topic` (ID or `none`).
- `api/objects.json` `task_row`: optional `status`, `urgency`, `assignee_id`, `topic_id`.
- `common.json`: `task_status` (`todo`, `inProgress`, `completed`), `task_urgency` (`low`, `normal`, `high`,
  `urgent`); event kind `topic.profile` in both kind lists.
- `events/objects.json` `body_task_meta`: optional sealed `github_org`, `github_repo` (`""` clears); new
  `body_topic_profile {name}`; `events/topic.profile.json`, `events/kinds.json` (org scope, users, `device_pull`
  false).
- `enums/event-kinds.json`: `topic.profile` (org scope, user, right `org_member`, header `wg`/`task` empty, `ref` a
  UUIDv7) and the right `org_member`.
- `audit/task.fields.json` (new action `task.fields`, `enums/audit-actions.json`); `audit/task.create.json`: optional
  `captured: true`.

Task dashboard (ADR-103, delivered with E-41), additive as well:

- `api/orgs-tasks-counts.{request,response}.json`, `api/INDEX.json`: `GET /v1/orgs/{org}/tasks/counts` (`org:member`)
  with the list filters and `wg`; answer `{by_status, by_urgency, total}`.
- `api/workgroup-tasks.request.json`: `order` also `urgency`; `overview` (`working`, `waiting`, `no_agent`: the start
  page's three groups of the open tasks, from the attention record), also on `api/orgs-tasks-counts.request.json`.
- `api/orgs-tasks-counts.response.json`: `by_overview {working, waiting, no_agent}` (required).
- `audit/read.org.tasks.counts.json` (new read action, `enums/audit-actions.json`).

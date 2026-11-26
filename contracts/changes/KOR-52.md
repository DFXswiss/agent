# KOR-52 – contract changes

ADR-027 section 3 (additive: every message that was valid stays valid). Decision: ADR-095 (main agents with names,
their people and their actors).

## Event kind `task.agent_name`
- `events/task.agent_name.json`, `events/objects.json#/$defs/body_task_agent_name` `{name}` (1–80 characters on one
  line, or null = back to the automatic name); `events/kinds.json`, `enums/event-kinds.json` (scope `wg`, author
  `user`, right `wg_write`, header `task` set, `session`/`ref` empty) and the two event-kind lists of `common.json`.
- `tasks/agent-name.json`: cases of the automatic name (hand-maintained data like `tasks/title.json`), consumed by
  the web core, the agent tool and the runner.
- `api/scope-events-get.request.json`, `api/device-pull.request.json`: the `kinds` filter allows `_` in a kind name, so
  `kinds=task.agent_name` is accepted (fixtures `valid/kinds-underscore.json`); every value valid before stays valid.

## API rows (`api/objects.json`)
- `task_row`: optional `number` (the task's number in its organization, the handle `#<number>`) and `name_event_id`
  (the newest `task.agent_name` event or null). Producer: the hub, in every task row (browser and device API).
- `session_row`: optional `agent_path` (`$defs/agent_path`, ordinals joined by dots; null for a main session).
- New `$defs`: `agent_path`, `agent_person`, `agent_item`, `audit_agent`.

## Endpoints
| Endpoint | Purpose |
|---|---|
| `GET /v1/orgs/{org}/people/agents` (`people-agents`) | per person the open main agents in the readable workgroups |
| `GET /v1/orgs/{org}/main-agents` (`main-agents`) | the open main agents of one person, paged |

## Audit
- `api/org-audit.response.json`, `api/me-audit.response.json`: `actor_kind` adds `main_agent` and `subagent`; rows may
  carry `on_behalf_of` (a user ID; absent when unknown). `org-audit` adds the optional map `actors` (session ID →
  `audit_agent`), outside the hashed rows.
- `api/org-audit.request.json`: optional `on_behalf_of`.

## Session platform API
- `mcp/objects.json#/$defs/output_get_context`: optional `agent` (kind, handle, name, name source, main agent,
  parent) and `children` (handle and state of the session's own children).

Fixtures: `events/task.agent_name/*`, `api/people-agents.{request,response}/*`, `api/main-agents.{request,response}/*`,
`api/org-audit.{request,response}/*` (agent actor, on behalf of), `api/me-audit.response/valid/agent-actor.json`,
`api/device-task-get.response/{valid/agent-handles,invalid/agent-path-trailing-dot}.json`,
`mcp/tools/get_context.output/*` (agent).

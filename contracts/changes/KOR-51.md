# KOR-51 – contract changes

ADR-027 section 3 (additive). Decision: ADR-093 (rights profiles and agent roles).

## Common (`common.json`)
- New `agent_profile {name, operations (≤ 256 operation types), reads}`, `agent_profile_name`
  (`^[a-z][a-z0-9_-]{0,39}$`), `agent_profile_reads` (`task`, `workgroup`, `all`), `agent_profile_scope` (`builtin`,
  `org`, `wg`).

## Operations (`ops/`)
- New data file `ops/classes.json`: the class of every operation type (`read`, `write`, `run`, `delegate`, `ask`,
  `scan`, `review`, `merge`); the built-in roles are derived from it.
- `subagent.request` takes `profile` (an `agent_profile_name`): the child's rights profile.

- New types (with `INDEX.json`, params/result schemas, `operation.params` variants and fixtures): `github.list_reviews`,
  `github.get_checks`, `github.merge` (dedupe repo, number, head_sha; 24 h), `review.verdict` (dedupe repo, number,
  head_sha, session; 30 days), `security.report`; dedupe vectors in `vectors/dedupe.json`.
- `classes.json` also lists every platform MCP tool (`tools`) and the head-changing types (`head_changing`).

## Events (`events/objects.json`)
- `settings.workgroup` adds the optional `merge_criteria {passed_verdicts, verdict_profile, checks_green,
  github_approvals}` and `separate_duties` (older runners refuse such a body and keep their former settings).

## API (`api/INDEX.json`, section 4.13 of 03-api.md)
| Endpoint | Right | Purpose |
|---|---|---|
| `GET /v1/workgroups/{wg}/agent-profiles` (`workgroup-agent-profiles`) | `wg:read` | the usable profiles and the type classes; read resource `workgroup.agent_profiles` |
| `PUT/DELETE /v1/workgroups/{wg}/agent-profiles/{name}` | `wg:write` | an own profile of the workgroup |
| `PUT/DELETE /v1/orgs/{org}/agent-profiles/{name}` | `org:admin` | an own profile of the organization |
| `PUT /v1/tasks/{task}/agent-profile` (`task-agent-profile-put`) | `wg:write` | assign or clear the task's profile; feature gate `agent.profiles` |

- `task_row` and `session_row` add the optional `agent_profile` (snapshot or null).
- `POST /v1/device/operations`: the request adds the optional `child_profile` (a name, `subagent.request`); the
  `accepted` answer adds `child_profile` (the child's effective profile or null) only when the request named one, so
  an older runner never sees it.
- `GET /v1/device/state` adds the section `agent_profiles {api: 1, sessions: {id: agent_profile|null}, workgroups:
  {wg: {name: agent_profile}}}`.

## Guest (`guest/sessions-create.request.json`)
- `POST /v1/sessions` adds the optional `profile` (agent_profile or null); sent only to guests 0.4.0 and later.

## Audit (`audit/`, `enums/audit-actions.json`)
- `agent_profile.set {scope, profile, reads, operations}`, `agent_profile.delete {scope, profile}` (group `settings`),
  `task.agent_profile {profile, reads}` (group `task`), `read.workgroup.agent_profiles` (group `read`).

## Device features
- The runner announces `agent.profiles`.

# KOR-25 – contract changes

ADR-027 section 3. Additive only: every message that was valid stays valid; new hubs, runners and guests refuse what
an older counterpart cannot know (an unknown operation type is `unknown_type` at the hub, H3, and in the guest).

## Operation types (`ops/`)

| Type | Params | Result | Dedupe |
|---|---|---|---|
| `github.list_issues` | `repo`, `state?` (open, closed, all), `labels?`, `sort?` (created, updated, comments), `direction?`, `per_page?` (1-100), `page?` (1-1000) | `issues` (one page, pull requests left out), `next_page`, `truncated` | none |
| `github.get_issue` | `repo`, `number`, `comments_page?`, `comments_per_page?` | issue fields, `pull_request`, `comments_total`, one page of `comments`, `comments_next_page`, `truncated` | none |
| `github.create_issue` | `repo`, `title`, `body?`, `labels?` | `number`, `url`, `id` | `repo`, `title` (sha256), `body` (sha256, empty when left out), 86400 s |
| `github.update_issue` | `repo`, `number`, at least one of `state`, `title`, `body`, `labels`; `state_reason` only with `state` (formal check of the runner) | `number`, `url`, `state`, `state_reason`, `title`, `labels` | none (setting values is idempotent) |
| `github.list_pull_requests` | `repo`, `state?`, `base?`, `sort?` (created, updated), `direction?`, `per_page?`, `page?` | `pull_requests`, `next_page`, `truncated` | none |
| `github.request_connection` | `{}` | `login` | none |

`github.comment` is unchanged: it already comments on issues and pull requests alike (GitHub's issue comment endpoint).
`ops/INDEX.json`, `ops/objects.json` (`result` union), `ops/dedupe-fields.json`, `events/objects.json`
(`body_operation_params`) and `vectors/dedupe.json` list the new types; fixtures per schema.

## `events/session.status`: state `waiting`

A third variant `{state:"waiting", waiting_for, operation_id, operation_type, repo?, until}`: the runner holds an
operation of the session because the task owner has no usable GitHub connection (`github_connection`) and asks the
owner in the task's chat. A repository outside a workgroup's (optional) repository list is rejected as before
(`repo_not_allowed`): there is no approval card (no list means every repository of the connection, ADR-054). It carries only
values the platform checked, never text of the session.

## `credentials/github.json`: `source`

Optional `source` (`device_flow`, `token`): how the connection was made. Records without it stay valid.

## `requests/github.connect`

The request takes an optional `token` (an existing GitHub token the device checks and stores; without it the device
flow runs as before). The `done` response may carry `scopes` and `source`. New failure codes `token_refused`,
`scope_missing`, `sso_required`.

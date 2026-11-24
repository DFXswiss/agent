# KOR-47 – contract changes

ADR-027 section 3 (additive). Decision: ADR-076 (GitHub identities).

## Credential body (`credentials/github.json`)
- `source` adds `operator` (the operator's token stored as a company account or as a copy for a listed user).
- New optional `name` (the account's display name) and `signing_key {alg:"ssh-ed25519", seed, public,
  github_key_id?}` (the identity's own SSH signing key).

## API (`api/INDEX.json`, `api/objects.json`)
| Endpoint | Purpose |
|---|---|
| `GET /v1/me/github-identities` | metadata of every identity the caller can open (own and company accounts) and the defaults |
| `GET/PUT/DELETE /v1/github-identities/{id}` | one identity record; PUT for an own record or, by an admin, a company record (with `key {epoch, wraps}`: a fresh epoch of the organization's credential key, wrapped for its devices only) |
| `PUT /v1/github-identities/{id}/primary` | make an own record the primary one |
| `PUT /v1/github-defaults/{scope}/{scope_id}` | default of an organization or workgroup (`identity_id` or null) |
| `PUT /v1/tasks/{task}/github-identity` | the task's identity (`identity_id` or null) |
| `GET /v1/device/github-identities?owner_id=` | metadata of an owner's records for an eligible device (company accounts: reader devices) |
| `GET/PUT /v1/device/github-identities/{id}` | device read (owner's `CK` or org key) and the device write of a user's record while answering `github.connect` |

Additive fields: `POST /v1/tasks` `github_identity`; `POST /v1/device-requests` `org_id`; `POST /v1/device/operations`
`github_identity`, `triggered_by`; `GET /v1/device/state` `sessions[].github_identity`,
`sessions[].task_github_identity`, `github_defaults`; `operation_row.github_identity`, `.triggered_by`;
`task_row.github_identity`; `session_row.github_identity`. New objects `github_identity_meta`, `github_default`;
`common.json` `github_default_scope`.

## Operations (`ops/`)
- Every `github.*` type except `github.request_connection` takes `github_identity`.
- `github.commit` takes `author`, `committer` (identity IDs), `co_authors` (user IDs) and `signing` (`none`, `key`).
- `subagent.request` takes `github_identity`.
- New type `github.list_identities` (`{}` → `{acting_user, current, identities[], co_authors[]}`), no dedupe.

## Device request `github.connect` (`requests/`)
Request: `org_id`, `signing_key`, `identity_id`. Final answer `done`: `source` adds `operator`; `identity_id`,
`signing` (`registered`, `scope_missing`, `refused`) and, for company accounts, `body` (the credential body for the
requesting admin's client, which seals, signs and stores it; the answer is end-to-end encrypted to that admin).

## Codes and audit
- Operation error codes `identity_forbidden`, `identity_unavailable`; API error code `identity_forbidden` (403).
- Audit actions `github.default`, `github.primary`, `task.github_identity`, `read.me.github_identities`,
  `read.github_identity`, `read.device.github_identity`, `read.device.github_identities`; `credential.put`/`credential.delete` details add
  `owner_kind`; every `operation.*` detail adds `github_identity`, `triggered_by` (all optional).
- Runner audit action `runner.operator_github_users` (`enums/outbox-audit-actions.json`, `outbox/objects.json`
  `body_audit` variant, `audit/runner.operator_github_users.json`).
- `events/objects.json` `body_operation_params` variant for `github.list_identities`; `requests/objects.json`
  `github_body` (the body of the `done` answer of a company connect, equal to `credentials/github.json`).

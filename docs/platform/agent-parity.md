# Parity matrix: web client ↔ agent tool (KOR-24, KOR-42, ADR-056 §4, `11-agent-access.md` §8.4)

Decision of the owner (2026-11-23): an AI agent may do everything a person with a passkey may do. Every user action
and every view of the web client (`web/src/ui/`) has its own command of `agent-platform` that has the same effect and
runs the same client-side checks, including the client's crypto. `agent-platform api <METHOD> <path>` (the generic
signed call) stays as a fallback, but it is the only way for no action.

The two tables are data for two tests. The parity test of the `agent` repository (`tests/platform/tool/test_parity.py`)
checks the Actions table against the tool: every row has a known state; every command of a `done` row exists in the
tool with the options it names; a `pending` row names the package that delivers it; an `n/a` row gives its reason. The
test of `agent-core` (`tests/platform/test_agent_parity.py`) checks both tables against the web source: every directory
under `web/src/ui/screens/` must be named as `screens/<directory>` and every directory under
`web/src/ui/settings/sections/` as `settings/<directory>` in the Screen column (the screen `settings` only hosts the
sections), and the Routes table must list exactly the hub routes the web client calls – every route of a
`request(METHOD, path)` or `anon(METHOD, path)` call and every `/v1/` path the client builds (also the stream and the
paths built by helpers), with `{param}` for template parts and without query strings – each with the action rows that
cover it. A new screen, section or route of the web client therefore fails a test until it has a row; a new button
inside an existing screen that calls no new route needs a new row by review.

States: `done` – the command exists, does what the web client does, and is tested; `pending` – another package
delivers the action in the web client and the tool (the row names it: "package KOR-xx delivers"); `n/a` – no action
for an agent (the row gives the reason, for example a passkey ceremony or the sign-out of a browser session).
Alternatives in a command are written `a\|b` (an escaped bar, as a table cell needs).

## Actions

| Action | Screen | Command | State |
|---|---|---|---|
| connect-agent | screens/connect | `pair` | done |
| register | screens/invite | `register` | done |
| login-unlock | screens/login | (the agent's own key replaces the passkey sign-in and the unlock of the identity) | n/a |
| recover | screens/recover | (passkey ceremony: a recovery registers a new passkey with the user's authenticator; a new code: `recovery new`) | n/a |
| sign-out | shell | (ends a browser session; an agent is disconnected with `agents revoke`) | n/a |
| me-view | shell | `status` | done |
| settings-language | settings/language | `settings set --locale` | done |
| settings-model | settings/model | `settings set --model-access --model` | done |
| settings-billing | settings/billing | `api GET /v1/orgs/{org}/billing`, `api GET /v1/tasks/{task}/charges` (task tab "Costs") | api |
| credential-list | settings/credentials | `credentials list` | done |
| credential-claude-login | settings/credentials | `claude connect`, `claude setup-token` | done |
| credential-claude-relay | settings/credentials | `claude login --device --mode --request --code --wait` | done |
| credential-api-key | settings/credentials | `credentials set-api-key` | done |
| credential-mailbox | settings/credentials | `credentials set-mailbox --imap-host --imap-port --imap-user --email` | done |
| credential-check | settings/credentials | `credentials check --kind --device --request --wait` | done |
| credential-delete | settings/credentials | `credentials delete --kind` | done |
| credential-rotate | jobs (CK re-wrap) | `credentials rotate` | done |
| quotas | settings/quotas | `quotas` | done |
| github-status | settings/github | `github status` | done |
| github-connect | settings/github | `github connect --device --request --wait` | done |
| github-disconnect | settings/github | `github disconnect` | done |
| passkey-list | settings/passkeys | `passkey list` | done |
| passkey-add | settings/passkeys | `passkey add` (opens the settings page in the user's browser: a passkey needs the user's authenticator) | done |
| passkey-remove | settings/passkeys | `passkey remove --id` | done |
| passkey-forget-browser | settings/passkeys | (forgets the unlock key this browser keeps without passkey PRF; an agent keeps no such key) | n/a |
| recovery-new | settings/passkeys | `recovery new` | done |
| agents-list | settings/agents | `agents list` | done |
| agents-pair | settings/agents | `pair` (the user's passkey approval replaces the main orchestrator; `pair` opens the page) | done |
| agents-revoke | settings/agents | `agents revoke --id --all` | done |
| agents-activity | settings/agents | `audit` | done |
| org-list | shell (org switcher) | `org list` | done |
| org-create | screens/home | `org create` | done |
| org-join | screens/invite (existing user) | `org join` | done |
| members-list | screens/members | `member list` | done |
| members-invite | screens/members | `org invite --role admin\|member` | done |
| members-admit | screens/members | `org admit --user <id\|account code> --role admin\|member --wg` | done |
| members-invitees | screens/members | `org invitees` | done |
| account-code | settings/passkeys | `status` (prints the account code) | done |
| members-invite-revoke | screens/members | `org invite-revoke` | done |
| members-role | screens/members | `member role` | done |
| members-remove | screens/members | `member remove` | done |
| members-leave | screens/members | `org leave` | done |
| key-rotation | jobs (owed rotations) | `org rotate` | done |
| key-catch-up | jobs (catch-up wrapping) | `org catch-up --org` | done |
| workgroups-list | screens/workgroups | `wg list` | done |
| workgroups-create | screens/workgroups | `wg create` | done |
| workgroups-rename | screens/workgroups | `wg rename --wg --name` | done |
| workgroups-grant | screens/workgroups | `wg grant` | done |
| workgroups-ungrant | screens/workgroups | `wg ungrant` | done |
| workgroups-archive | screens/workgroups | `wg archive` | done |
| wg-settings | screens/wg-settings | `repos list --wg`, `repos allow\|disallow --wg --repo`, `wg settings --wg --protected-branch --pr-draft-default` | done |
| devices-list | screens/devices | `device list` | done |
| devices-assign | screens/devices | `device assign` | done |
| devices-unassign | screens/devices | `device unassign` | done |
| devices-invite | screens/devices | `device invite` | done |
| devices-profile | screens/devices | `device rename` | done |
| tasks-list | screens/tasks | `task list` | done |
| tasks-overview | screens/tasks (overview of an org) | `task overview --org --state --mine --search` | done |
| task-create | screens/home, screens/tasks | `task create` | done |
| task-view | screens/task (chat) | `task show` | done |
| task-prompt | screens/task (composer) | `task prompt` | done |
| task-close | screens/task | `task close --task` | done |
| session-view | screens/session | `session show --session` | done |
| session-terminal | screens/session (terminal) | `session terminal --session --after` | done |
| session-control | screens/session | `session stop\|interrupt --session` | done |
| session-follow | screens/task, screens/session (live stream) | `task follow` | done |
| operations | screens/task (tabs) | `task operations --task --operation --before` | done |
| whiteboard-list | screens/whiteboard | `whiteboard list`, `whiteboard history` | done |
| whiteboard-write | screens/whiteboard | `whiteboard add\|edit\|delete --notify` | done |
| notifications-list | screens/notifications | `notifications list --unread --before` | done |
| notifications-read | screens/notifications | `notifications read --id --all` | done |
| monitoring | screens/monitoring | `monitoring --org --entity-kind --entity-id --metric --range` | done |
| audit | screens/audit | `audit --org --verify --action --actor --wg --task --session --before` | done |
| platform-invites | screens/platform-invites | `platform invite\|list\|revoke` | done |
| security | screens/security | `security` | done |
| community | – (not in the web client yet) | package KOR-26 delivers (community area with a group chat of all users) | pending |
| situation-view | – (not in the web client yet) | package KOR-39 delivers (situation view) | pending |
| credit-balance | – (not in the web client yet) | package KOR-31 delivers (prepaid credit balance) | pending |
| diagnose | – (agent only) | `diagnose` | done |
| error-pages | screens/errors, screens/notfound | (no action: every failure of the tool is one JSON object with error and next) | n/a |

## Routes

| Route | Actions |
|---|---|
| `GET /v1/agent-pairings/{pairing}` | agents-pair |
| `GET /v1/config` | connect-agent, settings-model |
| `POST /v1/device-requests` | github-connect, credential-claude-relay, credential-check |
| `GET /v1/device-requests/{request}` | github-connect, credential-claude-relay, credential-check |
| `POST /v1/events` | wg-settings, workgroups-rename, whiteboard-write, devices-profile, org-join |
| `POST /v1/invites/lookup` | register, org-join |
| `GET /v1/me` | me-view, passkey-list, notifications-list, credential-rotate |
| `GET /v1/me/agents` | agents-list |
| `POST /v1/me/agents` | agents-pair |
| `DELETE /v1/me/agents` | agents-revoke |
| `DELETE /v1/me/agents/{agent}` | agents-revoke |
| `GET /v1/me/audit` | agents-activity, audit |
| `GET /v1/me/bundles/recovery` | passkey-list, recovery-new, login-unlock |
| `PUT /v1/me/bundles/recovery` | recovery-new, recover |
| `PUT /v1/me/bundles/{slot}` | login-unlock, passkey-add |
| `GET /v1/me/credentials` | credential-list, github-status |
| `POST /v1/me/credentials/rotate` | credential-rotate |
| `GET /v1/me/credentials/{kind}` | credential-list, github-status |
| `PUT /v1/me/credentials/{kind}` | credential-api-key, credential-mailbox, credential-claude-login |
| `DELETE /v1/me/credentials/{kind}` | credential-delete, github-disconnect |
| `GET /v1/me/devices` | github-connect, credential-claude-relay, credential-check |
| `GET /v1/me/quotas` | quotas |
| `POST /v1/me/recovery/begin` | recovery-new |
| `DELETE /v1/me/registration` | register |
| `PUT /v1/me/settings` | settings-language, settings-model |
| `GET /v1/monitoring/overview` | monitoring |
| `GET /v1/monitoring/series` | monitoring |
| `GET /v1/notifications` | notifications-list |
| `POST /v1/notifications/read` | notifications-read |
| `GET /v1/operations/{operation}` | operations |
| `POST /v1/orgs` | org-create |
| `GET /v1/orgs/{org}/audit` | audit |
| `GET /v1/orgs/{org}/audit/verify` | audit |
| `GET /v1/orgs/{org}/catch-up` | key-catch-up |
| `GET /v1/orgs/{org}/devices` | devices-list |
| `GET /v1/orgs/{org}/invites` | members-list, members-invite-revoke |
| `POST /v1/orgs/{org}/invites` | members-invite, devices-invite |
| `POST /v1/orgs/{org}/invites/{invite}/revoke` | members-invite-revoke |
| `GET /v1/orgs/{org}/log` | org-list, members-list |
| `POST /v1/orgs/{org}/log` | org-join, members-role, members-remove, members-leave, key-rotation, workgroups-create, workgroups-grant, workgroups-ungrant, workgroups-archive, devices-assign, devices-unassign |
| `POST /v1/passkeys/login/begin` | login-unlock |
| `POST /v1/passkeys/login/finish` | login-unlock |
| `POST /v1/passkeys/register/begin` | register, passkey-add |
| `POST /v1/passkeys/register/finish` | register, passkey-add, recover |
| `DELETE /v1/passkeys/{credential}` | passkey-remove |
| `GET /v1/platform/invites` | platform-invites |
| `POST /v1/platform/invites` | platform-invites |
| `POST /v1/platform/invites/{invite}/revoke` | platform-invites |
| `GET /v1/platform/log` | devices-assign, platform-invites |
| `POST /v1/recovery/begin` | recover |
| `POST /v1/recovery/finish` | recover |
| `GET /v1/scopes/{scope}/events` | task-view, session-follow, session-terminal, operations, wg-settings, tasks-overview |
| `POST /v1/session/end` | sign-out |
| `GET /v1/sessions/{session}` | session-view, monitoring |
| `POST /v1/sessions/{session}/control` | session-control |
| `GET /v1/stream` | session-follow |
| `POST /v1/tasks` | task-create |
| `GET /v1/tasks/{task}` | task-view |
| `POST /v1/tasks/{task}/close` | task-close |
| `GET /v1/tasks/{task}/operations` | operations |
| `POST /v1/tasks/{task}/prompts` | task-prompt |
| `GET /v1/whiteboards` | whiteboard-list |
| `GET /v1/whiteboards/{entry}/history` | whiteboard-list |
| `GET /v1/workgroups/{wg}` | task-create |
| `GET /v1/workgroups/{wg}/device-candidates` | devices-assign, task-create |
| `GET /v1/workgroups/{wg}/tasks` | tasks-list, tasks-overview |
| `GET /v1/wraps` | task-view, credential-list, key-catch-up |
| `POST /v1/wraps` | devices-assign, key-catch-up, org-join |

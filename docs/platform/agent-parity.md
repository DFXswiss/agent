# Parity matrix: web client ↔ agent tool (KOR-24, ADR-056 §4, `11-agent-access.md` §8.4)

Decision of the owner (2026-11-23): an AI agent may do everything a person with a passkey may do. Every user action
and every view of the web client (`web/src/ui/`) has a command of `agent-platform` that does the same, including the
client's crypto. Until a convenient command exists, `agent-platform api <METHOD> <path>` reaches every route.

The table is data for two tests. The parity test of the `agent` repository (`tests/platform/tool/test_parity.py`)
fails when a row's command is `done` but missing in the tool (commands and the options they name). The test of
`agent-core` (`tests/platform/test_agent_parity.py`) fails when the web client gains a screen or a settings section
without a row: every directory under `web/src/ui/screens/` must be named as `screens/<directory>` and every directory
under `web/src/ui/settings/sections/` as `settings/<directory>` in the Screen column (the screen `settings` only hosts
the sections). The check works at the level of screens and sections, not of single buttons: a new action inside an
existing screen needs a new row by review, not by test. States: `done` – the command exists and is tested; `api` –
reachable through the generic signed call alone; `stage b` / `stage c` – not built yet (stage c: the action needs the
client's encryption (a task, a prompt, a whiteboard entry, a re-wrapped credential), which the generic signed call
alone cannot do). Alternatives in a command are written `a\|b` (an escaped bar, as a table cell needs).

| Action | Screen | Command | State |
|---|---|---|---|
| connect-agent | screens/connect | `pair` | done |
| register | screens/invite | `register` | done |
| login-unlock | screens/login | (not needed: the agent's key replaces the session) | done |
| recover | screens/recover | (passkey ceremony; recovery code via `recovery new`) | api |
| sign-out | shell | `api POST /v1/session/end` | api |
| me-view | shell | `status` | done |
| settings-language | settings/language | `settings set --locale` | done |
| settings-model | settings/model | `settings set --model-access --model` | done |
| credential-claude-login | settings/credentials | `claude connect`, `claude setup-token` | done |
| credential-claude-relay | settings/credentials | `claude login --device` (device request `claude.login`) | api |
| credential-api-key | settings/credentials | `credentials set-api-key` | done |
| credential-mailbox | settings/credentials | `credentials set-mailbox` | api |
| credential-check | settings/credentials | `credentials check` (device request `credential.check`) | api |
| credential-delete | settings/credentials | `credentials delete --kind` | api |
| credential-rotate | jobs (CK re-wrap) | `credentials rotate` | stage c |
| quotas | settings/quotas | `quotas` | api |
| github-connect | settings/github | `github connect` | api |
| github-disconnect | settings/github | `github disconnect` | api |
| passkey-list | settings/passkeys | `passkey list` | api |
| passkey-add | settings/passkeys | `passkey add` (opens the settings page in the user's browser) | api |
| passkey-remove | settings/passkeys | `passkey remove` | api |
| recovery-new | settings/passkeys | `recovery new` | api |
| agents-list | settings/agents | `agents list` | api |
| agents-pair | settings/agents | `pair` (browser only: the user's passkey approval replaces the main orchestrator; `pair` opens the page) | done |
| agents-revoke | settings/agents | `agents revoke [--all]` | api |
| org-list | shell (org switcher) | `org list` | done |
| org-create | screens/home | `org create` | done |
| org-join | screens/invite (existing user) | `org join` | done |
| members-list | screens/members | `member list` | api |
| members-invite | screens/members | `org invite --role admin\|member` | done |
| members-admit | screens/members | `org admit --user <id\|account code> --role admin\|member --wg` | done |
| members-invitees | screens/members | `api GET /v1/me/invitees` | api |
| account-code | settings/passkeys | `status` (prints the account code) | done |
| members-invite-revoke | screens/members | `org invite-revoke` | api |
| members-role | screens/members | `member role` | api |
| members-remove | screens/members | `member remove` (with rotation) | api |
| members-leave | screens/members | `org leave` | api |
| workgroups-list | screens/workgroups | `wg list` | done |
| workgroups-create | screens/workgroups | `wg create` | done |
| workgroups-grant | screens/workgroups | `wg grant` | done |
| workgroups-ungrant | screens/workgroups | `wg ungrant` (with rotation) | api |
| workgroups-archive | screens/workgroups | `wg archive` | api |
| wg-settings | screens/wg-settings | `repos list\|allow\|disallow`, `wg settings` | api |
| devices-list | screens/devices | `device list` | api |
| devices-assign | screens/devices | `device assign` | done |
| devices-unassign | screens/devices | `device unassign` (with rotation) | api |
| devices-invite | screens/devices | `device invite` | api |
| devices-profile | screens/devices | `device rename` | api |
| tasks-list | screens/tasks | `task list` | done |
| task-create | screens/home, screens/tasks | `task create` | done |
| task-view | screens/task (chat) | `task show` | done |
| task-prompt | screens/task (composer) | `task prompt` | done |
| task-close | screens/task | `task close` | api |
| session-view | screens/session | `session show` | api |
| session-terminal | screens/session (terminal) | `session terminal` | api |
| session-control | screens/session | `session stop\|interrupt` | api |
| session-follow | screens/task, screens/session (live stream) | `task follow` | done |
| operations | screens/task (tabs) | `task operations` | api |
| whiteboard-list | screens/whiteboard | `whiteboard list` | stage c |
| whiteboard-write | screens/whiteboard | `whiteboard add\|edit\|delete [--notify]` | stage c |
| notifications | screens/notifications | `notifications list\|read` | api |
| monitoring | screens/monitoring | `monitoring` | api |
| audit | screens/audit | `audit [--verify]` | api |
| platform-invites | screens/platform-invites | `platform invite\|list\|revoke` | api |
| security | screens/security | `guide` / build hash check | api |
| community-read | screens/community | `api GET /v1/community`, `api GET /v1/community/messages` (ciphertext; opening needs the community key, KOR-26) | stage c |
| community-post | screens/community | `api POST /v1/community/messages` (sealing under the community key and the key request flow, `12-community.md` §4.2) | stage c |
| community-delete-own | screens/community | `api DELETE /v1/community/messages/{id}` | api |
| community-moderate | screens/community | `api DELETE /v1/community/messages/{id}`, `api PUT\|DELETE /v1/community/mutes/{user}`, `api GET /v1/community/mutes` | api |
| community-read-position | screens/community | `api PUT /v1/community/read` | api |
| account-user-id | settings/account | `api GET /v1/me` | api |
| diagnose | – (agent only) | `diagnose` | done |
| error-pages | screens/errors, screens/notfound | (no action: every failure of the tool is one JSON object with error and next) | done |

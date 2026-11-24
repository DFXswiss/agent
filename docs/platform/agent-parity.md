# Parity matrix: web client ↔ agent tool (KOR-24, KOR-42, ADR-056 §4, `11-agent-access.md` §8.4)

Decision of the owner (2026-11-23): an AI agent may do everything a person with a passkey may do. Every user action
and every view of the web client (`web/src/ui/`) has its own command of `agent-platform` that has the same effect and
runs the same client-side checks, including the client's crypto. `agent-platform api <METHOD> <path>` (the generic
signed call) stays as a fallback, but it is the only way for no action.

The two tables are data for one test, `tests/platform/test_agent_parity.py`, which reads the three sources of this
checkout: the web client (`web/src`), the hub's registered routes (`agent_hub`, with their authentication class) and
the argparse tree of the tool. It requires: every directory under `web/src/ui/screens/` is named as
`screens/<directory>` and every directory under `web/src/ui/settings/sections/` as `settings/<directory>` in the
Screen column (the screen `settings` only hosts the sections); the Routes table lists every hub route the web client
calls – every route of a `request(METHOD, path)` or `anon(METHOD, path)` call and every `/v1/` path the client builds
(also the stream and the paths built by helpers), with `{param}` for template parts and without query strings – each
with the action rows that cover it; every route the client calls is registered at the hub; the table names no route
that neither the client nor the hub has; every route of the table that a person's browser calls at the hub has a
`done` row (its own command) or only `n/a`/`pending` rows with their reason; every command of a `done` row exists in
the tool with the options it names; and the agent guide (`agents.md`) has an example of every command of the tool. A
new screen, section or route of the web client therefore fails the test until it has a row and a command; a new
button inside an existing screen that calls no new route needs a new row by review.

States: `done` – the command exists, does what the web client does, and is tested; `pending` – another package
delivers the action in the web client and the tool (the row names it: "package KOR-xx delivers"); `n/a` – no action
for an agent (the row gives the reason, for example a passkey ceremony or the sign-out of a browser session).
Alternatives in a command are written `a\|b` (an escaped bar, as a table cell needs).

## Actions

| Action | Screen | Command | State |
|---|---|---|---|
| connect-agent | screens/connect | `pair` | done |
| request-message-agent | screens/connect, screens/invite (the agent's personal message on the page of the approval, rule E-28) | `pair --message`, `register --message` (an agent never approves a pairing, 11-agent-access.md §6) | done |
| register | screens/invite | `register` | done |
| register-own | screens/invite (an account of an agent alone, no passkey, rule E-27, KOR-62) | `register --own-account --display-name` | done |
| login-unlock | screens/login | (the agent's own key signs every request and its state holds the unlocked identity, also for an account of an agent alone: there is no sign-in step) | n/a |
| recover | screens/recover (an account without a passkey takes its account to this computer with the recovery code, KOR-62; a person's recovery registers a new passkey) | `recover --hub` | done |
| sign-out | shell | (ends a browser session; an agent is disconnected with `agents revoke`) | n/a |
| me-view | shell | `status` | done |
| settings-language | settings/language | `settings set --locale` | done |
| settings-model | settings/model | `settings set --model-access --model` | done |
| settings-billing | settings/billing (credit balance, prices, credit history, consumption per day and workgroup, KOR-31) | `billing --org` | done |
| task-charges | screens/task (tab "Costs": charges per session and the total, KOR-31) | `task charges --task` | done |
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
| github-token | settings/github (an existing token, a GitHub CLI login or a personal access token, checked by the device, ADR-065) | `github connect --token --device --wait` | done |
| github-disconnect | settings/github | `github disconnect` | done |
| passkey-list | settings/passkeys | `passkey list` | done |
| passkey-add | settings/passkeys | `passkey add` (opens the settings page in the user's browser: a passkey needs the user's authenticator) | done |
| passkey-remove | settings/passkeys | `passkey remove --id` | done |
| passkey-forget-browser | settings/passkeys | (forgets the unlock key this browser keeps without passkey PRF; an agent keeps no such key) | n/a |
| recovery-new | settings/passkeys | `recovery new` | done |
| agents-list | settings/agents | `agents list` | done |
| agents-pair | settings/agents | `pair` (the user's passkey approval replaces the main orchestrator; `pair` opens the page) | done |
| agents-confirm | screens/connect (the main orchestrator of an account without a passkey confirms a remote pairing itself, KOR-62) | `agents confirm --link --code` | done |
| agents-revoke | settings/agents | `agents revoke --id --all` | done |
| agents-activity | settings/agents | `audit` | done |
| org-list | shell (org switcher) | `org list` | done |
| org-create | screens/home | `org create` | done |
| org-join | screens/invite (existing user) | `org join` | done |
| members-list | screens/members (explicit and inherited access, ADR-058) | `member list` | done |
| members-invite | screens/members | `org invite --role admin\|member` | done |
| members-invite-message | screens/members, screens/invite (the inviter's personal message, rule E-28) | `org invite --message`, `org join` (prints the message) | done |
| members-admit | screens/members | `org admit --user <id\|account code> --role admin\|member --wg` | done |
| members-invitees | screens/members (accounts of the admin's own platform invitations) | `org invitees --org` | done |
| account-code | settings/passkeys | `status` (prints the account code) | done |
| members-invite-revoke | screens/members | `org invite-revoke` | done |
| members-invite-states | screens/members (the inviter's view: open, opened, registered, accepted, KOR-36) | `org show --org` | done |
| members-role | screens/members | `member role` | done |
| members-remove | screens/members | `member remove` | done |
| members-leave | screens/members | `org leave` | done |
| key-rotation | jobs (owed rotations) | `org rotate` | done |
| key-catch-up | jobs (catch-up wrapping) | `org catch-up --org` | done |
| workgroups-list | screens/workgroups (my access, also an admin's inherited read, ADR-058) | `wg list` | done |
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
| session-workvm | screens/session (work VM panel and runtime, KOR-29) | `session workvm --session` | done |
| task-workvm-items | screens/task (work VM runs and states in the chat) | `task show`, `task follow` | done |
| task-waiting | screens/task (held operations: the waiting card that asks the owner to connect GitHub, ADR-066) | `task show`, `task follow`, `github connect` | done |
| task-guest-errors | screens/task (errors the guest reported, `session.error`, ADR-064) | `task show`, `task follow` | done |
| task-runtime | screens/task (runtime of the task and its sessions, reachability of the session's device, KOR-40) | `task show`, `session show --session` | done |
| session-terminal | screens/session (terminal) | `session terminal --session --after` | done |
| session-control | screens/session | `session stop\|interrupt --session` | done |
| session-follow | screens/task, screens/session (live stream) | `task follow` | done |
| operations | screens/task (tabs) | `task operations --task --operation --before` | done |
| whiteboard-list | screens/whiteboard | `whiteboard list`, `whiteboard history` | done |
| whiteboard-write | screens/whiteboard | `whiteboard add\|edit\|delete --notify` | done |
| notifications-list | screens/notifications | `notifications list --unread --before` | done |
| notifications-read | screens/notifications | `notifications read --id --all` | done |
| monitoring | screens/monitoring (devices, VMs, sessions, work VMs) | `monitoring --org --entity-kind --entity-id --metric --range` | done |
| audit | screens/audit | `audit --org --verify --action --actor --wg --task --session --before` | done |
| platform-invites | screens/platform-invites | `platform invite\|list\|revoke` | done |
| security | screens/security | `security` | done |
| community-read | screens/community (messages, key, unread count) | `community status`, `community messages --before --after --limit --mark-read` | done |
| community-post | screens/community | `community post --text --name` | done |
| community-delete | screens/community (own message; moderators any message) | `community delete --id` | done |
| community-moderate | screens/community (mute, unmute, muted users) | `community mutes`, `community mute\|unmute --user` | done |
| community-key | screens/community (key request, serving requests of others; background job) | `community key --wait`, `community serve` | done |
| account-user-id | settings/account | `status` | done |
| situation-view | – (not in the web client yet) | package KOR-39 delivers (situation view) | pending |
| credit-balance | shell (header item "Credit": the balance, "Credit used up") | `billing --org` | done |
| diagnose | – (agent only) | `diagnose` | done |
| error-pages | screens/errors, screens/notfound | (no action: every failure of the tool is one JSON object with error and next) | n/a |

## Routes

| Route | Actions |
|---|---|
| `GET /v1/agent-pairings/{pairing}` | agents-pair |
| `GET /v1/config` | connect-agent, settings-model |
| `POST /v1/device-requests` | github-connect, github-token, credential-claude-relay, credential-check |
| `GET /v1/device-requests/{request}` | github-connect, github-token, credential-claude-relay, credential-check |
| `POST /v1/events` | wg-settings, workgroups-rename, whiteboard-write, devices-profile, org-join |
| `POST /v1/invites/lookup` | register, org-join |
| `GET /v1/me` | me-view, passkey-list, notifications-list, credential-rotate |
| `GET /v1/me/agents` | agents-list |
| `POST /v1/me/agents` | agents-pair, agents-confirm |
| `DELETE /v1/me/agents` | agents-revoke |
| `DELETE /v1/me/agents/{agent}` | agents-revoke |
| `GET /v1/me/audit` | agents-activity, audit |
| `GET /v1/me/bundles/recovery` | passkey-list, recovery-new, login-unlock |
| `PUT /v1/me/bundles/recovery` | recovery-new, recover |
| `PUT /v1/me/bundles/{slot}` | login-unlock, passkey-add |
| `GET /v1/me/credentials` | credential-list, github-status |
| `POST /v1/me/credentials/rotate` | credential-rotate |
| `GET /v1/me/credentials/{kind}` | credential-list, github-status, task-waiting |
| `PUT /v1/me/credentials/{kind}` | credential-api-key, credential-mailbox, credential-claude-login |
| `DELETE /v1/me/credentials/{kind}` | credential-delete, github-disconnect |
| `GET /v1/me/invitees` | members-invitees |
| `GET /v1/me/devices` | github-connect, github-token, credential-claude-relay, credential-check |
| `GET /v1/me/quotas` | quotas |
| `POST /v1/me/recovery/begin` | recovery-new |
| `DELETE /v1/me/registration` | register |
| `PUT /v1/me/settings` | settings-language, settings-model |
| `GET /v1/monitoring/overview` | monitoring |
| `GET /v1/monitoring/series` | monitoring |
| `GET /v1/community` | community-read |
| `POST /v1/community/join` | community-read |
| `GET /v1/community/messages` | community-read |
| `PUT /v1/community/read` | community-read |
| `POST /v1/community/messages` | community-post |
| `DELETE /v1/community/messages/{}` | community-delete |
| `GET /v1/community/mutes` | community-moderate |
| `PUT /v1/community/mutes/{}` | community-moderate |
| `DELETE /v1/community/mutes/{}` | community-moderate |
| `POST /v1/community/epochs` | community-key |
| `PUT /v1/community/request` | community-key |
| `GET /v1/community/requests` | community-key |
| `POST /v1/community/requests/wraps` | community-key |
| `POST /v1/community/request/reject` | community-key |
| `POST /v1/community/wraps` | community-key |
| `GET /v1/notifications` | notifications-list |
| `POST /v1/notifications/read` | notifications-read |
| `GET /v1/operations/{operation}` | operations |
| `POST /v1/orgs` | org-create |
| `GET /v1/orgs/{org}/audit` | audit |
| `GET /v1/orgs/{org}/audit/verify` | audit |
| `GET /v1/orgs/{org}/billing` | settings-billing, credit-balance |
| `GET /v1/orgs/{org}/catch-up` | key-catch-up |
| `GET /v1/orgs/{org}/devices` | devices-list, task-runtime |
| `GET /v1/orgs/{org}/invites` | members-list, members-invite-revoke, members-invite-states |
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
| `GET /v1/scopes/{scope}/events` | task-view, session-follow, session-terminal, operations, wg-settings, tasks-overview, session-workvm, task-workvm-items, task-waiting, task-guest-errors |
| `POST /v1/session/end` | sign-out |
| `GET /v1/sessions/{session}` | session-view, monitoring, session-workvm, task-runtime |
| `POST /v1/sessions/{session}/control` | session-control |
| `GET /v1/stream` | session-follow |
| `POST /v1/tasks` | task-create |
| `GET /v1/tasks/{task}` | task-view, task-runtime |
| `GET /v1/tasks/{task}/charges` | task-charges |
| `POST /v1/tasks/{task}/close` | task-close |
| `GET /v1/tasks/{task}/operations` | operations, session-workvm, task-waiting |
| `POST /v1/tasks/{task}/prompts` | task-prompt |
| `GET /v1/users/lookup` | members-admit |
| `GET /v1/users/{user}/identity` | members-admit |
| `GET /v1/whiteboards` | whiteboard-list |
| `GET /v1/whiteboards/{entry}/history` | whiteboard-list |
| `GET /v1/workgroups/{wg}` | task-create |
| `GET /v1/workgroups/{wg}/device-candidates` | devices-assign, task-create |
| `GET /v1/workgroups/{wg}/tasks` | tasks-list, tasks-overview |
| `GET /v1/wraps` | task-view, credential-list, key-catch-up |
| `POST /v1/wraps` | devices-assign, key-catch-up, org-join |

## Without a person

Rule E-27 (KOR-62): an AI agent can use the platform completely without a person. Every step of the agent tool that
needs a person – a fixed `next` text of the tool (`agent_platform.errors`, `diagnose.REASONS`, the `NEXT_*` texts of
`remote` and `alone`) that names the user, a person, a browser, a passkey or a login that a person approves, and every
action row above whose command opens the user's browser or needs the user's passkey – has a row here. Kind `built`:
the way without a person, naming its commands. Kind `outside`: the step lies outside the platform (a login at a third
party: model subscription, GitHub); the way names what works without a person (taking over the computer's existing
login, handing over a token). Kind `person`: the one step that stays with a person – it concerns only accounts of
persons. `tests/platform/test_without_person.py` keeps the table complete and every command of it existing.

| Step | Way without a person | Kind |
|---|---|---|
| action:connect-agent | `register --own-account` (an account of its own through an invitation), `recover --hub` (an account of its own on another computer) | built |
| action:register | `register --own-account` | built |
| action:agents-pair | `agents confirm` (an account without a passkey: its main orchestrator confirms the new agent) | built |
| action:passkey-add | `status` (an account of an agent alone needs no passkey: the agent signs with its key) | built |
| action:credential-claude-login | `claude connect` (the Claude Code login of this computer, read only), `credentials set-api-key`, `settings set --model-access` (the DFX model access); `claude setup-token` needs a person's click at claude.ai | outside |
| action:credential-claude-relay | `claude connect`, `credentials set-api-key` (the relayed login needs a person at claude.ai) | outside |
| action:task-waiting | `github connect --token` (the waiting card's next step `github connect` needs a person at GitHub's device page) | outside |
| action:github-connect | `github connect --token` (an existing token, e.g. the token of the GitHub CLI login; the device page needs a person at GitHub) | outside |
| next:not_paired | `register --own-account`, `recover --hub` | built |
| next:agent_revoked | `recover --hub` | built |
| next:pairing_timeout | `register --own-account` (no pairing at all) | built |
| next:pairing_failed | `agents confirm` | built |
| next:claim_lost | `agents revoke --id` (by the account's current agent), `recover --hub` | built |
| next:setup_token_failed | `claude connect`, `credentials set-api-key` | outside |
| next:no_write_access | `wg grant` (an admin of the organization, person or agent) | built |
| next:not_inviter | `platform invite` (an agent whose account holds the role creates invitations itself; the operator grants the role as for a person) | built |
| next:rotation_pending | `org rotate` (an admin or writer of the workgroup, person or agent) | built |
| next:community_key_missing | `community key --wait` (the client of any online member, person or agent, hands the key over) | built |
| next:not_own_account | the person's passkey approval of a new main orchestrator of an account with a passkey (concept idea 34): `pair --remote` | person |
| next:remote.NEXT_WAITING | `agents confirm` (an account of an agent alone) | built |
| next:remote.NEXT_CONFIRMED | `agents confirm` | built |
| next:hub_too_old | `register --own-account` (no pairing at all) | built |
| next:claude_login_missing | `claude connect` on a computer where Claude Code is logged in, `credentials set-api-key` | outside |
| next:claude_login_expired | `claude sync`, `credentials set-api-key` | outside |
| next:waiting.CONNECT | `github connect --token` (an existing token; the device page needs a person at GitHub) | outside |
| next:claude_missing | `claude connect`, `credentials set-api-key` | outside |
| next:claude_expired | `claude sync`, `credentials set-api-key` | outside |
| next:claude_unreadable | `claude connect` | outside |

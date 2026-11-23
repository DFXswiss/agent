# KOR-24 – delegated agent access (ADR-056, 11-agent-access.md)

Additive contract changes:

- `api/INDEX.json`: seven endpoints of section 4 of `11-agent-access.md` (section `11.4`, owner KOR-24) with their
  request and response schemas (`agent-pairings-create`, `agent-pairings-get`, `me-agents-confirm`, `me-agents-list`,
  `me-agents-revoke`, `agent-claim`, `agent-me`); new authentication class `agent` and rights
  `agent:confirmed`, `agent` (`tests/platform/contracts/spec/endpoints-kor24.json`).
- `api/passkeys-login-begin.request.json`: optional `pairing_id` (the login confirms that pairing). An old client
  sends `{}` as before.
- `api/org-audit.response.json`: optional `agent_id` of a row (present only when an agent's request wrote it).
- `enums/error-codes.json`: `pairing_unknown` (404), `pairing_gesture_required` (403), `claim_invalid` (401).
- `enums/audit-actions.json` and `audit/`: `agent.confirm`, `agent.claim`, `agent.revoke`, `read.me.agents`,
  `read.agent.me`.
- `credentials/claude_subscription.json`: `kind` is `setup_token` or `login_token`; optional `expires_at`. Existing
  records (`setup_token`) stay valid; runners read only `token`.
- `vectors/agent.json`: fingerprint, proof of possession, handover secret (bundle key, claim, claim hash), sealed
  identity and sealed label.

Addendum 1 (owner's decision: an agent may do everything its user may, ADR-056 §4):

- `api/INDEX.json`: the agent management routes have the right `user` (browser session or agent);
  `DELETE /v1/me/agents` (`me-agents-revoke-all`, `{revoked}`) disconnects every agent at once;
  `me-agents-list.response.json` names who paired an agent (`paired_by`: `passkey` or the agent's ID; replaced by
  `replaces` in addendum 3).
- `api/objects.json` `notification_row`: kind `agent.account_change` (account-changing action of an agent) with
  `org_id` null; every other kind keeps an organization.

Addendum 2 (second security review, hub fixes):

- `api/INDEX.json`: `GET /v1/me/audit` (`me-audit`, section `11.4`, right `user`, read resource `me.audit`): the
  caller's account audit (rows of the user outside any org and every row written by a request of one of the user's
  agents), paged like `GET /v1/orgs/{org}/audit`; `enums/audit-actions.json` and `audit/`: `read.me.audit`.
- `enums/audit-actions.json` and `audit/notification.read.json`: `notification.read {marked, scope}`, written when an
  agent marks notifications read.
- `api/objects.json` `notification_row.ref`: for `agent.account_change` the text
  `<action>:<acting agent ID or empty>:<target>`.
- Compatibility of account notifications (a web client of before KOR-24 never sees one): `GET /v1/notifications`
  lists them only with the new query flag `account=1` (`api/notifications-list.request.json`), `POST
  /v1/notifications/read {all:true}` marks them only with `account:true` (`api/notifications-read.request.json`), and
  the stream frame `notification` of one reaches only streams opened with `account=1` (`ws/sse/query.json`).

Addendum 3 (owner's decision "Nachtrag 2", 2026-11-23: one main orchestrator per user, concept idea 34, ADR-056 §4a):

- `api/me-agents-list.response.json`: the field `paired_by` is replaced by `replaces` (the ID of the main orchestrator
  this agent replaced at its confirmation, revoked then, or `null`); fixtures adjusted. The field came with addendum 1
  of the same unreleased package, so no released client reads `paired_by`.
- `POST /v1/me/agents` (`me-agents-confirm`): unchanged schema; only the browser session of the pairing-tagged login
  confirms (an agent gets `403 forbidden`); the confirmation revokes the user's previous confirmed or active agent.
- `api/objects.json` `notification_row.ref` of `agent.account_change`: the action `agent_paired` no longer occurs (an
  agent cannot pair); every other action stays.

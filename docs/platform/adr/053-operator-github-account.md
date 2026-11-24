# ADR-053: The operator's GitHub account for approved users (second way to connect GitHub)

Status: Accepted (2026-11-23); amended by ADR-070 (2026-11-24): the operator's GitHub account is the operator's own
connection and serves **exactly one user, the operator** - `OPERATOR_GITHUB_USERS` with more than one user is
`config_invalid` (`OPERATOR_GITHUB_USERS:one_user_only`). A personal connection must never be used on behalf of
another person nor handed to one; "approved users" below therefore means the one operator user. Handing the token to
further users, as the consequences below describe, is no longer possible.

## Context
- ADR-015 connects a user's GitHub account through the device flow of a GitHub App. It needs a registered App
  (`GITHUB_CLIENT_ID`) and a user who confirms a code at GitHub.
- On the platform device no App is registered; "Connect GitHub" ends with "The service refused the request"
  (`github_connect client_id_missing`). The owner wants approved users to work with the operator's GitHub account
  for AI work at once, without registering an App and without codes. The operator holds a valid token of that
  account.
- The web client and the hub must stay as deployed: the change is the runner's alone (no contract, no hub release, no
  golden). The web client takes `{step:"done", login}` as the first and only response of `github.connect`
  (`connectProgress` reads the last verified response; `checkResponseBody` accepts `done` without a preceding
  `device_code`); the hub never sees the step (sealed bodies).
- KOR-23 (the operator's subscriptions) set the pattern: a file on the platform device and an explicit
  approval list, fail-closed, refused on org devices.

## Decision
- Two settings of the runner, both or neither, platform devices only (refused on org devices with
  `config_invalid`): `OPERATOR_GITHUB_TOKEN_FILE` (absolute path) and `OPERATOR_GITHUB_USERS` (comma-separated user
  IDs).
- A `github.connect` whose verified request header names an approved user reads the file anew (the operator replaces
  it atomically), checks the token with `GET /user`, stores the credential `github {token, login, user_id, scopes,
  created_at}` under the user's `CK` exactly as the device flow does and answers `{step:"done", login}` (final).
  Every other user keeps the device flow unchanged.
- The file is opened only when its directory is owned by the runner's account and writable by nobody else, and the
  file is a regular file, not a symlink, owned by the runner's account and without rights for group and others; after
  opening it is checked again. `approved` also checks at run time that the device is a platform device. Its content is one line: the token. Anything else is not
  used (`credential_missing`), and the reason is logged once. A refused token ends `upstream_error`.
- `github.list_repositories` falls back to `GET /user/repos` when GitHub refuses `/user/installations` with `403`
  (a token without the App). All repository operations are REST calls on `/repos/...` that work with such a token
  unchanged.
- Observability: doctor line `operator_github` (`off`, `usable` or the reason), counters
  `agent_runner_operator_github_connects_total` and `_connect_failures_total`, log lines with `source=operator`. The
  token, the account and the approved users never appear in logs, answers, metrics or errors.

## Consequences
- **Approving a user hands the token to that user.** The credential is stored like every `github` credential (the
  package requires the existing way, `credentials.cache.put`, so that disconnect, cache purge and every operation work
  unchanged): sealed under the user's `CK`. The user's browser holds the `CK` and can open the record; the `CK` is
  also wrapped for the devices of every organization the user belongs to (04-crypto §5.2), which may be org devices
  outside the operator's control. An approved user can therefore read the token and use it outside the platform,
  where none of the protections below apply. Approval is a decision of trust in the person, not a technical
  boundary; withdrawing it or replacing the file does not take back a token already handed out - only revoking the
  token at GitHub does.
- **The token reaches as far as the account.** It is not limited to selected repositories like an App installation.
  Inside the platform the protection lies in: the approval per user (the request is bound to the signing user; a forged request is refused
  before any handler runs), the workgroup allow-list, when the admins set one, as the formal
  boundary of every operation (checked before GitHub is asked, audited; without a list the account's access is the
  boundary, ADR-054; it is set by the workgroup's admins, who need not be approved users: in an organization
  shared with others, a workgroup admin can allow any repository the account reaches for the tasks of an approved
  member), the formal checks of the operation pipeline (protected branches, fast-forward only,
  draft pull requests), and the audit of every operation. Choosing the account and its rights is the operator's
  responsibility; a dedicated account for AI work with only the needed repositories is recommended.
- Every approved user acts as the same GitHub account: commits, pull requests and comments carry its name.
- Withdrawing a user's approval stops new connects only. Existing connections stay until the user disconnects or the
  operator revokes the token at GitHub; then operations end `github_not_connected` until the next connect.
- The stored credential is a copy of the token: replacing the file does not change existing connections; users
  disconnect and connect again to take a new token (runbook).
- All approved users share one token and so one GitHub rate limit; the runner tracks the limit per user, so each
  learns of an exhausted limit separately (more `rate_limited` failures, no wrong results).

## Alternatives considered
- Register a GitHub App: the owner explicitly does not set anything up at GitHub.
- Let users paste a token (general "existing token" way): planned in KOR-25 with hub, web and golden changes; too
  large for an immediate fix, and it still needs every user to hold a token.
- Read the file at every operation instead of storing a copy (the token would never leave the platform device): it
  would bypass the credential model (`CK`, cache, disconnect) of ADR-015, need a new credential kind or a contract
  change, and work only on the platform device. A candidate for KOR-25 if the owner wants the token kept on the
  device.

## Deviation from DESIGN.md
Same as ADR-015 (§19.1, `docs/github-accounts.md`); additionally a second way beside ADR-015 (`spec-deviations.md`).

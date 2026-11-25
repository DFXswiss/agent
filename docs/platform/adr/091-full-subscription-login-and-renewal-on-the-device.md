# ADR-091: Further Claude subscriptions sign in with the full login – renewal by the measuring device

Status: Accepted (2026-11-25)

## Context
- Rules [E-30](../KONZEPT.md#e-30) (an overview of the quota of every subscription) and [E-31](../KONZEPT.md#e-31)
  (every subscription logged in centrally, nothing on the user's computer). [ADR-090](090-every-subscription-logged-in-centrally.md)
  logs further subscriptions in with `claude setup-token`. Such a token carries only the scope `user:inference`; the
  usage endpoint (`GET /api/oauth/usage`, 05-runner §10.6) needs `user:profile` and answers `403 permission_error`
  (several independent public reports). Every further subscription would show "not readable".
- The ordinary login of Claude Code (`claude auth login --claudeai [--email <address>]`, pinned 2.1.330) yields an
  access token with `user:profile` and `user:inference` (the token the usage endpoint answered in LIVE-03 and in
  operation) and a refresh token; the access token lives for hours.
- Probe of the pinned program on the test device up to the code input, without an account (KOR-71 report): the URL it
  hands to `BROWSER` redirects to `http://localhost:<port>/callback` (useless for a user who is not at the device);
  the screen shows a second link under `If the browser didn't open, visit:` (a short `https://claude.ai/oauth/authorize?…`
  link, also as an OSC 8 hyperlink) with the prompt `Paste code here if prompted`; that link leads to the code page.
  The program looks up the macOS keychain with `security` found through `PATH` (`find-generic-password … -s "Claude
  Code-credentials-<hash>"`) and keeps a plaintext store `$CLAUDE_CONFIG_DIR/.credentials.json` (`claudeAiOauth`:
  `accessToken`, `refreshToken`, `expiresAt` in ms, `scopes`, `subscriptionType`, `rateLimitTier`). It renews with
  `POST https://platform.claude.com/v1/oauth/token` (JSON `{grant_type: "refresh_token", refresh_token, client_id,
  scope}`, client ID `9d1c250a-e61b-44d9-88ed-5944d1962f5e`) and keeps the old refresh token when the answer carries
  none. It offers no command that renews without starting a session; `CLAUDE_CODE_CUSTOM_OAUTH_URL` is refused
  ("not an approved endpoint"), so the program cannot be pointed at a double.

## Decision

### 1. Login (`claude.login` with `subscription_id`)
- A login **with** `subscription_id` (manual and automatic) runs `SUBSCRIPTION_LOGIN_ARGV` (new setting, default
  `["claude","auth","login","--claudeai"]`), with `--email <address>` appended when an address is known (the address
  of an automatic login, else the address the record already holds). Without `subscription_id` nothing changes: the
  primary record keeps `LOGIN_ARGV` (`claude setup-token`) and its form.
- Same mechanics as the relay (05-runner §11.2): PTY, private `HOME`/`CLAUDE_CONFIG_DIR`/`TMPDIR` below
  `tmp/login-<request>`, allow-list environment, `BROWSER` pointing at the capture helper (so the device never opens
  a browser), cleanup on every path. The link answered to the user is the one **on the screen** after
  `If the browser didn't open, visit:` (rows joined, checked like every authorize URL), not the captured one. The
  automation (§11.3) opens the same link.
- **Keychain never written:** the login's `PATH` begins with a private directory holding `security`, a script that
  always exits 1 (and writes nothing). Every keychain access of the program goes through `security` in `PATH`; a
  refused keychain leaves the plaintext store as the only way. When `.credentials.json` is missing after the program
  ended, the login fails (`token_invalid`); nothing is read from anywhere else.
- After the code the program must **end** with status 0 within `token_wait`; the runner then reads
  `$CLAUDE_CONFIG_DIR/.credentials.json` (`O_NOFOLLOW`, regular file, at most 64 KiB) and, when present, the
  address of `$CLAUDE_CONFIG_DIR/.claude.json` (`oauthAccount.emailAddress`). A login is taken only when `scopes`
  contains `user:profile` **and** `user:inference`; otherwise `scope_missing` (new failure code; next step: log in
  again with the Claude subscription, not with a Console account).
- The record body (`contracts/credentials/claude_subscription.json`, additive) gets the kind `oauth_login` and
  `token` (access token), `refresh_token`, `expires_at`, `scopes`, `subscription_type`, `rate_limit_tier`, besides
  `email` and the kept `name`, `note`, `use`. All of it is sealed under the owner's `CK` like every record (the owner's
  clients may open it, as for every credential; they keep fields they do not know when they write).
  Records with a setup token stay valid ("not readable", with "log in again").

### 2. Renewal by the measuring device
- Only the device that measures the owner (`quota_poll_owners`, the elected device of 05-runner §10.6) renews, in
  the `quota` task: a record of kind `oauth_login` with a refresh token is renewed `RENEW_BEFORE_S` (1800 s) before
  `expires_at`, and at once when the usage endpoint answers `401`. Request: `POST CLAUDE_TOKEN_URL` (setting, default
  `https://platform.claude.com/v1/oauth/token`), JSON `{grant_type, refresh_token, client_id: CLAUDE_OAUTH_CLIENT_ID
  (setting), scope}` (the record's scopes), 30 s, no redirects.
- **Crash-safe order:** the answer's new access and refresh token are first sealed as the next record version (under
  the newest `CK` epoch, author the device) and stored in the runner's store (`runner.subscription_renewal`: the
  sealed new record and the sealed record it replaces – ciphertext only); then `PUT /v1/device/subscriptions/{id}`
  without a request writes it; only after the hub confirmed it is the row deleted. While such a row exists, the
  device writes it again on every tick and never calls the token endpoint for that record. A crash between the
  provider's answer and the store loses the new refresh token (the next renewal gets `invalid_grant` → "log in
  again"); there is no earlier moment at which it could be stored.
- **Concurrent writes:** a `version_conflict` (the owner renamed it, changed its use or logged in again
  meanwhile) opens the current record: if it still holds the refresh token the renewal used, the new tokens are
  merged into it (its name, note, use win) and written as its next version; otherwise a newer login won and the row is
  dropped. `404` drops the row. Transport errors and `forbidden` (the device is no longer the measuring one) keep it.
- **Answers:** `invalid_grant` (400) → the device writes the next version without `token` and `refresh_token`,
  with `relogin: "renewal_refused"`; the record is then "not logged in" and the overview and `subscriptions list`
  show "log in again". Other 4xx/5xx, a malformed answer or a network error → retry after `RENEW_RETRY_S` (60 s),
  until a valid answer or `invalid_grant`.
- **Hub:** `PUT /v1/device/subscriptions/{id}` without `request_id` is also allowed when the record is a non-primary
  subscription of the owner and the device is the one that polls the owner now (`polls_for`, the rule of the
  `quota` items); the new version is under the current epoch (`put_identity`). Nothing else changes for devices.
- **Never** is a login of another computer read, copied or renewed: the runner renews only records created by a
  login on a platform device (kind `oauth_login`).

### 3. Measuring and sessions
- The usage endpoint is read with the current access token every 900 s (05-runner §10.6) – all windows, no model
  call, no consumption. Setup-token records stay as they were ("not readable").
- Sessions (shared subscriptions, ADR-090 §3) get only `token`, the access token – never the refresh token. A renewal
  is a new record version: every device that uses the subscription re-reads it (`credentials` hint) and provisions the
  bound sessions anew before the old token expires, without an interruption (as for a renewed token file).

### 4. Tool and overview
`agent-platform subscriptions list` shows `expires_at`, `plan` (`subscription_type`, `rate_limit_tier`) and the
status `relogin` (with `next`: `subscriptions login --id …`) for a record the device could not renew; `not_readable`
also names `next`. The web overview (KOR-68) shows a record without token as "not logged in" with "Log in now".

## Consequences
- The refresh token lies, sealed, in the record at the hub (like every credential, readable only with the owner's
  `CK`: the owner's clients and eligible devices). It never reaches a guest, a log, a metric or the hub in plaintext.
  Keeping it only in one device's store would lose the subscription with that device.
- Two devices could renew the same record only when the hub's choice changed between a renewal and its write (the
  first device was offline longer than the online window); the conflict rule keeps the newest refresh token, and a
  token the provider no longer accepts leads to "log in again".
- Roll-out: hub first (accepts the renewal write), then runners (new settings have defaults), then the agent tool.

## Alternatives considered
- Renewal by the program (`claude` with a written `.credentials.json`): no command renews without a session start;
  starting a session costs model use.
- Refresh token only in the runner's store: the subscription would be lost with the device and could not move.
- Keeping setup tokens: the overview would stay empty (403).

## Deviation from DESIGN.md
None beyond [ADR-090](090-every-subscription-logged-in-centrally.md).

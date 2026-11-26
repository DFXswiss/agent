# ADR-092: Further Claude subscriptions sign in with the full login – renewal by the measuring device

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
  contains `user:profile` **and** `user:inference`; otherwise `scope_missing` (the existing failure code; next step: log in
  again with the Claude subscription, not with a Console account).
- The record body (`contracts/credentials/claude_subscription.json`, additive) gets the kind `oauth_login` and
  `token` (access token), `refresh_token`, `expires_at`, `scopes`, `subscription_type`, `rate_limit_tier`, besides
  `email` and the kept `name`, `note`, `use`. All of it is sealed under the owner's `CK` like every record (the owner's
  clients may open it, as for every credential; they keep fields they do not know when they write).
  Records with a setup token stay valid ("not readable", with "log in again"). The agent tool keeps every body field
  it does not know when it writes a record (a later version may add fields); the web client checks a closed list of
  fields (its design) that now includes those of the full login, and keeps them when it writes.

### 2. Renewal by the measuring device
- Only the device that measures the owner (`quota_poll_owners`, the elected device of 05-runner §10.6) renews, in
  the `quota` task (every 30 s): a record of kind `oauth_login` with **both** tokens is renewed `RENEW_BEFORE_S`
  (1800 s) before `expires_at`, and at once when the usage endpoint answers `401`. The record is fetched anew first
  and renewed only when that fresh version is still due (a newer login is left alone). Request: `POST CLAUDE_TOKEN_URL` (setting, default
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
  meanwhile) opens the current record: if it still holds the refresh token the renewal used, or another device marked
  it `relogin` after the provider refused that token (the race of a moved measuring duty), the new tokens are merged
  into it (its name, note, use win) and written as its next version; otherwise a newer login won and the row is
  dropped. `404` drops the row. Transport errors and `forbidden` keep it. The row is sealed anew under the newest
  epoch on every try (a key rotation meanwhile does not strand it).
- **Answers:** `invalid_grant` (400) → the device writes the next version without `token` and `refresh_token`,
  with `relogin: "renewal_refused"` – only while the current record still holds the refused refresh token (after a
  `version_conflict` it reads the record again; a newer login or renewal is never marked); the record is then "not
  logged in" and the overview and `subscriptions list` show "log in again". Other 4xx/5xx, a malformed answer or a
  network error → retry after 60 s (`RETRY_S`), until a valid answer or `invalid_grant`. An answer with a usable
  access token (and a usable or no refresh token) is never discarded for a secondary field: an `expires_in` out of
  form counts as 3600 s, scopes out of form are left out.
- **Hub:** `PUT /v1/device/subscriptions/{id}` without `request_id` is also allowed when the record is a non-primary
  subscription of the owner and the device is the one that polls the owner now (`polls_for`, the rule of the
  `quota` items) **or wrote the stored version itself** (it finishes a renewal it began before the measuring duty
  moved – the duty moves back to the smallest online device as soon as it returns); the new version is under the
  current epoch (`put_identity`). The runner writes such rows on every tick, also for owners it no longer measures.
- **Log out** (`subscriptions logout`) drops `token` **and** `refresh_token` (and `relogin`), so no device renews a
  logged-out subscription.
- **Never** is a login of another computer read, copied or renewed: the runner renews only records created by a
  login on a platform device (kind `oauth_login`).

### 3. Measuring and sessions
- The usage endpoint is read with the current access token every 900 s (05-runner §10.6) – all windows, no model
  call, no consumption. Setup-token records stay as they were ("not readable").
- Sessions (shared subscriptions, ADR-090 §3) get only `token`, the access token – never the refresh token. A renewal
  is a new record version: every device that uses the subscription re-reads it (`credentials` hint) and provisions the
  bound sessions anew before the old token expires (as for a renewed token file). "Without an interruption" did not
  hold: the provider revokes the former access token with the renewal itself, not at its expiry – see §5.

### 4. Tool and overview
`agent-platform subscriptions list` shows `expires_at`, `plan` (`subscription_type`, `rate_limit_tier`) and the
status `relogin` (with `next`: `subscriptions login --id …`) for a record the device could not renew; `not_readable`
also names `next`. The web overview (KOR-68) shows a record without token as "not logged in" with "Log in now".

### 5. Addendum (HOT-26): a session survives the renewal of its access
**Finding.** A regular renewal of a subscription (three hours before expiry) ended the running turn of a session on
it within seconds: "OAuth token revoked · Please run /login", and a prompt that waited met the same fate. The runner
provisioned the new token right after (`credential_renewed`), but the session stood still until a person wrote
"continue". The provider revokes the former access token **at the moment of the renewal**, so every renewal hits
every session that still holds the former token – on the measuring device and elsewhere, for the full logins of §2 as
for the operator's token files that are replaced from outside.

**Decision.** A refused access is one more cause of the existing fallback flow (ADR-081 §4, step `model_fallback`),
not a second mechanism (05-runner §10.7):
- **Detect.** The gateway's `usage` record with status `401` of a main session on a subscription (own or operator
  pool) marks the session refused (`model.revocation`, daemon memory: time, provisioning stamp, access label – never a
  token) and holds its prompts at once (`billing.pause.deferred`), so a waiting prompt does not run into the dead
  token. A later `2xx` of the session ends the mark (Claude Code retried successfully).
- **Continue.** Once the turn has ended (`idle`) and the credit allows it, the step renews the session's provisioning
  by the existing rules (`model.gateways.refresh`: the renewed record or file of the same subscription; a file refused
  outside the 60 s grace is blocked and the session moves to another usable file of the pool, ADR-052) and, as soon as
  a valid access is there – a provisioning other than the refused one, or one that arrived at most 60 s before the
  refusal – hands the session one fixed sentence ("The platform renewed this session's model access; the last request
  was refused with the former access. Continue the interrupted work.", stable prompt ID per refusal and try). The
  held prompts follow in their order. No person is needed; the session is never suspended for it.
- **Bounded.** At most 3 continuations without a successful answer in between; no valid access within 300 s; or the
  session ended: the refusal is given up, the error stays visible as before and the held prompts are delivered.
- **Told.** The runner writes the audit `session.model_switched` with the new cause `renewed` (metadata only: access
  before and after – often the same –, the model, `until: null`), only towards a hub with `model_fallback.api >= 2`;
  the task shows the quiet line "Zugang erneuert – weiter (…)". The hub creates no notification for it.
- **New access first, where possible.** On the device that renews (the measuring device of §2) the new access token
  goes to the sessions first: right after the answer is stored (§2 "crash-safe order"), before the hub write, the
  pool's entry `sub-<id>` takes the new token with the identity of the coming record version and the sessions bound to
  it are provisioned at once (`model.gateways.hand_over`). The time in which a request can still carry the former
  token shrinks to the time of the exchange and one provisioning.

**What cannot be done.** The former token cannot stay valid until the sessions have the new one: the provider revokes
it with the exchange, and the exchange alone yields the new token. Requests that are in flight at that moment, and
every session on another device (it learns of the new version through the `credentials` hint and the next refresh of
the pool, seconds later), can still be refused – the continuation above is the net for them. The same holds for a
token file the operator replaces from outside (the runner sees the file only after the replacement) and for the
owner's own subscription. The marks live in memory only: a daemon restarted between refusal and continuation leaves
the session as before HOT-26 (the error is visible, the next prompt is delivered); writing them to
`runner.model_pause` would replace the row of a quota pause of the same session and lose its `released` cause.
Subagent sessions are not continued (their one turn ends with the error, like a used-up access ends them).

## Consequences
- The refresh token lies, sealed, in the record at the hub (like every credential, readable only with the owner's
  `CK`: the owner's clients and eligible devices). It never reaches a guest, a log, a metric or the hub in plaintext;
  the owner's agent tool and browser open it in memory when they change the record (as they open every credential –
  the year-long setup token before), never store or show it. Keeping it only in one device's store would lose the
  subscription with that device.
- Two devices could renew the same record only when the measuring duty moved between a renewal and its write; the
  device that renewed may still write it (it wrote the stored version), the conflict rule keeps the newest refresh
  token, and only a token the provider no longer accepts leads to "log in again". A duty that moved twice before the
  write can still cost a new login: A renews and cannot write, B measures and is refused (`invalid_grant`, B is now
  the author), and the duty does not come back to A – A's row stays and is refused. On the platform there is one
  measuring device in practice; a hub rule that let A write over B's mark would need the hub to know which device
  renewed which version (not done, E-25).
- The exception "the device that wrote the stored version" has no time limit and also covers a device that answered
  a login: such a device can already open the record (it is eligible for the owner's `CK`), so trusting its write adds
  nothing (the reasoning of ADR-090 §3); the hub cannot tell kinds apart (the body is sealed).
- Sessions on other devices get the renewed token through the `credentials` hint of the new version; the 1800 s
  margin covers a lost hint (the reconcile step re-reads every owner's records) and the provisioning. A measuring
  device that is an older runner does not renew: roll the runners out before the first full login.
- A crash while a login runs leaves `tmp/login-<id>/config/.credentials.json` until the daemon removes stale login
  directories at its next start (as for every login, 05-runner §11.2).
- The keychain stays untouched only while the pinned program reaches it through `security` in `PATH`; a new pin is
  checked for this by the upgrade gate (`runbooks/claude-code-upgrade-gate.md`) like the screen labels.
- Roll-out: hub first (accepts the renewal write), then runners (new settings have defaults), then the agent tool.

## Alternatives considered
- Renewal by the program (`claude` with a written `.credentials.json`): no command renews without a session start;
  starting a session costs model use.
- Refresh token only in the runner's store: the subscription would be lost with the device and could not move.
- Keeping setup tokens: the overview would stay empty (403).

## Deviation from DESIGN.md
None beyond [ADR-090](090-every-subscription-logged-in-centrally.md).

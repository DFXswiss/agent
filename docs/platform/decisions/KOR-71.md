# KOR-71 decisions (full login of further subscriptions, renewal on the device)

Design: [ADR-092](../adr/092-full-subscription-login-and-renewal-on-the-device.md); rules [E-30](../KONZEPT.md#e-30),
[E-31](../KONZEPT.md#e-31), [E-25](../KONZEPT.md#e-25). Builds on [ADR-090](../adr/090-every-subscription-logged-in-centrally.md).

1. **Only logins with `subscription_id` change.** The primary record keeps `claude setup-token` and its body form
   (the deployed web client opens it); the work package asks to leave it as it is.
2. **The link comes from the screen.** The probe of the pinned program (2.1.330, no account) showed that the URL it
   hands to `BROWSER` redirects to `localhost`; the link of the code page is the one on the screen (also an OSC 8
   hyperlink). The capture helper stays (so the device never opens a browser), its URL is ignored for the full login.
3. **Keychain refused by a private `security`.** The program reaches the keychain only through `security` in `PATH`
   (probe: every lookup went to the private script). A refusing script first in `PATH` leaves the plaintext store;
   a missing store after the program ended is `token_invalid` – nothing is read from anywhere else.
4. **`scope_missing` is reused** (the closed list already had it for GitHub); no new failure code.
5. **Renewal through the token endpoint, not the program.** The program has no command that renews without a
   session start, and `CLAUDE_CODE_CUSTOM_OAUTH_URL` is refused, so the program cannot be driven against a double.
   The request is the one the pinned program sends (JSON, `grant_type`, `refresh_token`, `client_id`, `scope`);
   address and client ID are settings with the pinned values as defaults.
6. **Refresh token in the sealed record, pending renewals in the runner store as ciphertext.** Keeping it only on
   one device would lose the subscription with the device; the store holds the sealed next version and the sealed
   replaced version, never plaintext.
7. **Design review findings** (fresh context, before the build): the device that wrote the stored version may finish
   its renewal after the measuring duty moved (high); `invalid_grant` marks only while the record still holds the
   refused token (high); `logout` drops the refresh token too (high); the pending row is sealed anew under the
   newest epoch on every try, the record is fetched anew before a renewal, the runner renews only records with both
   tokens (medium); a marked record is merged by a pending renewal of the device that lost the race (medium). Not
   changed: preferring renewal-capable devices in the hub's election (would change the election of ADR-090 for all
   owners; the runners are rolled out before the first full login instead).
8. **The agent tool keeps fields it does not know.** It used to refuse any unknown body field; a body of a later
   version would then be "unverifiable" and could not be renamed or logged out. Known fields are still checked.
   The KOR-69 test that expected an unknown field to be refused now expects it to be listed.
9. **`expires_at` with milliseconds** (the contract time); the program's `expiresAt` (ms) is converted.
10. **Measuring after a `401` renewal** is due at the next tick (30 s), not after 900 s.
11. **The web client's dialogs (KOR-69, not yet merged) must accept the full login.** On the branch of KOR-69 the
    web core checks a closed list of body fields with the kinds `setup_token`/`login_token` only and "Log out" drops
    only the token: a full login would show as "cannot be opened" there and a logged-out one would keep its refresh
    token. The change (kind `oauth_login`, the fields `refresh_token`, `scopes`, `subscription_type`,
    `rate_limit_tier`, `relogin`; "Log out" drops `token`, `refresh_token`, `relogin`) was built and tested on that
    branch (`web/src/core/credentials/subscriptions.ts`, commit "Let the web dialogs open full logins and drop both
    tokens on log out." of branch `ad5f2026-kor-71` before its rebase onto `develop`); it goes in with KOR-69's web
    part, which is merged after this package. A record marked `relogin` has no token and shows "Log in now".

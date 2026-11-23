# HUB-03 – Passkeys, browser sessions, `/v1/me`, identity bundles, settings

Decisions taken where `03-api.md` section 4.1, 11, 12, `04-crypto.md` sections 4.1, 4.9, 5.1, 5.4, `02-data-model.md`
section 2.1 and ADR-004/006/026 were silent or ambiguous.

## Ceremonies (`auth/ceremonies.py`)

1. **One attempt per ceremony.** `…/finish` marks the ceremony used in its own committed transaction before the
   WebAuthn verification runs, so a refused finish consumes it (as `take_sign_in` of the legacy passkey hub). Only a
   body whose shape does not fit the ceremony kind (first-passkey fields for an add or recovery ceremony and vice
   versa) or a wrong caller of an `add_passkey` ceremony is refused before consumption. Unknown, used, expired and
   wrong-kind ceremonies are all `400 ceremony_invalid`.
2. **Login challenge column.** `ceremony.challenge` is NOT NULL; a login ceremony stores its hub value there as well
   (`challenge = hub_value`); the WebAuthn challenge of a login is SHA-256(statement) and never stored.
3. **Creation options.** `rp.name` is `agent.dfx.swiss` (protocol name, not a host); `user.name` and `displayName`
   are the user ID text (zero knowledge); `attestation: "none"`, `requireResidentKey: true` and an empty
   `excludeCredentials` are sent in addition to the required members (all allowed by the contract and accepted by
   `web/src/core/passkey/options.ts`).

## Registration (`auth/register.py`)

4. **Invitation states.** An invitation can register a user while `kind = 'user'`, not used, not revoked, not
   expired and without `registered_user`; platform and org invitations alike. Everything else – unknown, device
   invitation, used, revoked, expired, already bound – is `404 invite_unknown` at begin and again at finish (the
   invitation is re-checked under `FOR UPDATE`, so of two parallel ceremonies for one invitation exactly one binds
   it). `invite_registered` stays the code of `accept` (HUB-11).
5. **Invitation with a session.** `register/begin {invite_id}` is accepted with or without a browser session (the
   right column is "anon (+invite)"); a signed-in person may create a second account on a shared browser.
6. **`{add:true}`.** Anonymous → `401 unauthorized` (R-1.1, no self-registration). Signed: handed to
   `agent_hub.auth.passkeys_manage.begin_add(context, principal)` when HUB-11 has created that module, else
   `403 forbidden`. HUB-11 does not need to edit `register.py`. This is a package-local hook between two owners of
   `auth/`, not a new platform-wide extension point of ADR-027 section 4 (the review asked to name it as such).
7. **Further passkeys.** `register/finish` already finishes ceremonies of kind `add_passkey` (only the ceremony's
   user, signed) and `recovery` (anonymous; the ceremony is the proof) with the second form of the request: the
   passkey is added to the existing user (`auth.passkey_add`, or `auth.recovery {step: "finish"}` for a recovery
   ceremony). HUB-11 only creates these ceremonies. The used recovery ceremony is the proof that allows the one
   replacement of the recovery slot (decision 19).
8. **Refusal codes.** A malformed `sig_pub` (not an Ed25519 point) is `400 params_invalid` with pointer
   `/identity/sig_pub`; a `cred_wrap` whose signature does not verify with the new identity over
   `P‖"wrap\0"‖canonical({epoch:1, recipient:user, scope:"cred", scope_id:user, wrap})` is `400 wrap_unauthorized`
   (rule W1/W3); a `recovery_id` used by another user is `409 id_conflict`; a credential ID that is already
   registered is `400 credential_invalid`. The legacy `verify_registration` also refuses credential IDs shorter than
   16 bytes (`credential_invalid`), stricter than the contract's 1 byte; kept (every platform authenticator uses ≥ 16).
9. **Defaults.** Registration creates `user_settings` with the column defaults (`auto`, no model, `en`); the
   passkey starts with `prf_seen = false` and the attested sign count.
10. **Audit.** `auth.register` (target the passkey) and `invite.register` (target the invitation). The audit
    detail schema of `auth.register`/`auth.login`/`auth.passkey_add` requires `credential_id`, but the plaintext
    word list of `audit/chain.py` (HUB-01) refuses every detail key containing `credential`, and credential IDs can be
    longer than the 256-character detail limit. The credential is therefore named in `target_kind = "passkey"`,
    `target_id` = its b64u ID (or `sha256:<32 hex>` of it when longer than 128 characters), and the detail carries
    only `invite_id`. Reconciling the word list with the contract is left to the package that enforces the audit
    detail schemas (HUB-16); the report lists it.

## Login and sessions (`auth/login.py`, `auth/keys.py`, `auth/sessions.py`)

11. **Lifetime.** `verify_certificate` runs with the legacy bound `SESSION_KEY_MAX_SECONDS` (43200 s); the stored
    `browser_session.expires_at` is `min(statement expiry, hub now + 43080 s)`. The browser writes its own clock plus
    43080 s (`web/src/core/passkey/statement.ts`), so a browser clock up to 120 s ahead is accepted and the session
    still never outlives the 43080 s the hub issues (03-api.md section 12).
12. **Hub value.** After V1–V11 the statement's hub value must equal the ceremony's (`400 challenge_mismatch`).
13. **Unknown credential.** An unknown or removed passkey, or a disabled user, is `400 credential_invalid` (no
    `credential_unknown` code exists in section 11).
14. **`prf_expected`.** True once the passkey's slot was uploaded (`passkey.prf_seen`, set by
    `PUT /v1/me/bundles/passkey:<id>`: only a browser that had the PRF output can derive `KEK_i`). A login without
    PRF output while `prf_expected` is true is the `prf_missing` case of 06-web.md.
15. **Refusal audit.** Every refusal of `login/finish` after the ceremony was taken is audited as
    `auth.refused` (`result = denied`, actor anonymous) with the code and the session key ID the statement would
    have had; the row is written in its own transaction. An unknown, used or expired ceremony is refused without an
    audit row, so random ceremony IDs cannot flood the audit chain (review finding).
16. **Resolver.** `auth/keys.py:RESOLVERS = {"session.": resolve_session}`; `key_revoked` also when the session's
    passkey was removed or the user disabled (HUB-11 removes passkeys).
17. **Session end.** `POST /v1/session/end` takes the empty body `{}`; audit `auth.session_end` with `key_id`.

## Orphan job (`auth/jobs.py`)

18. "Followed by accept or private genesis" is read from the invitation: both mark it used (`used_at`). Registrations
    older than 24 h on an unused invitation are deleted (sessions, bundles, passkeys, ceremonies, wraps and the
    `cred` epoch, credentials, quota snapshots, notifications, settings, identity, principal), `registered_user` is
    reset and `invite.release` is audited (actor `hub`). A user who meanwhile is an org member, holds a platform role
    or owns a task is kept (an existing user may have joined through another invitation, 04-crypto.md section 5.2a).
    Each orphan is released in its own transaction, re-checked under the locks of the invitation and the principal
    row, with the audit row last (no audit lock held while other users' rows are deleted). Interval 5 min; ceremony
    purge hourly.

## Identity (`me/routes.py`)

19. **`PUT /v1/me/bundles/{slot}`.** `passkey:<id>` must name an active passkey of the caller (else `403 forbidden`)
    and takes no `new_recovery_id`; `recovery` requires `new_recovery_id` (`400 request_invalid` with pointer) and
    replaces `user_identity.recovery_id` (another user's ID, also in a race: `409 id_conflict`). 04-crypto.md
    section 5.5 rotates the recovery code only in the recovery flow, so the replacement is allowed once per finished
    recovery: a `recovery` ceremony of the user must have been used after the recovery slot was last written
    (`403 forbidden` otherwise; ceremonies are purged after a day, which bounds the window). A stolen session can
    therefore neither plant a recovery code of its own nor overwrite the recovery bundle (review finding).
    Superseded by KOR-01 (decisions 12–16): a replacement with a fresh passkey assertion is allowed at any time.
20. **Settings.** `model_id` null is always allowed; otherwise it must be in the catalog list of the chosen access
    kind, for `auto` in any list (`400 params_invalid`, pointer `/model_id`).
21. **`GET /v1/me` order.** Orgs newest first (`org.created_at DESC, id DESC`), passkeys newest first, platform roles
    by name; removed memberships and passkeys are not listed. A session without `user_identity` (impossible through
    the API) answers `404 not_found`.
22. **Request bodies** are strict pydantic models with the patterns of `contracts/common.json`
    (`auth/fields.py`); unknown fields and wrong types are `400 request_invalid` with pointers.

## Tests

23. `tests/platform/hub/auth/authenticator.py` is the Python fake authenticator (ES256, UV/UP flags, counter, simulated
    PRF as HMAC over the PRF input) and fake browser. Two HUB-02 tests were adapted because the real session resolver
    replaces the in-memory stand-in the HUB-02 conftest announced: `test_read_log` now creates the session of a
    non-v7 user in the database instead of mutating the in-memory principal, and `test_registry` checks that the core
    routers and the nonce job are discovered instead of being the only ones.
24. E2E-01 and E2E-30 have no spec yet (`e2e/platform/specs/` holds only the smoke spec); they are written by the
    packages that own them.

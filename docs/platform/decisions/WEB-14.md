# WEB-14 – decisions

Recovery, passkey management, `prf_missing` and the browser device key (06-web.md §2.2, §3.2, §3.3, §3.4, §3.5, §3.6,
§13; 04-crypto.md §3, §4.1, §5.4, §5.5, §5.6; 03-api.md §4.1; ADR-006).

## Device key (`core/persist/device-key.ts`)

1. **Own database.** The device key lives in the IndexedDB database `agent-platform-device` (store `keys`, one entry per
   user ID), not in WEB-02's `agent-platform` database: its store list is fixed at version 1 and owned by WEB-02, and
   sign-out clears the session stores while the device key must survive until "Forget" (06 §3.3: per browser profile).
2. **Format (local only).** A non-extractable AES-256-GCM `CryptoKey` (encrypt/decrypt only, generated in WebCrypto,
   stored by structured clone) and `b64u(nonce12 ‖ AES-GCM(key, bundle plaintext, AAD))` with the bundle plaintext of
   04 §4.1 and `AAD = P ‖ "device-bundle\0" ‖ user_id`. 04-crypto.md defines no format because nothing leaves the
   browser; the label keeps the bundle from being confused with a hub slot. Entries that are malformed, carry an
   extractable or foreign key, or name another user are treated as absent; an entry that does not open is forgotten.
3. **Residual risk.** Browsers persist non-extractable keys inside the profile; whoever copies the profile can use it.
   That is the trade-off of 06 §3.3 option (b); the UI says "Do not use this on shared devices".
4. **Where it is offered.** (a) In the `prf_missing` guidance as a checkbox next to the recovery-code unlock (applied
   after the unlock); (b) right after the recovery code when a registration (platform or org invitation) or a recovery
   ended without a passkey slot (`ui/prf-missing/OnboardingRecovery.tsx`, `RememberBrowser.tsx`); (c) in `/settings`
   ("This browser" with the date and "Forget", or "Remember this browser"). Only the signed-in user's entry is shown.
5. **Order on sign-in.** PRF slot → device key → `prf_missing` guidance. After an unlock without PRF output (device key
   or recovery code) a PRF-capable passkey whose slot is missing gets its slot (best effort, one helper
   `uploadMissingSlot` for `/login`, the existing-account step and the guidance).

## `prf_missing` (`ui/prf-missing/`)

6. **One component for every sign-in path.** `PrfMissing` replaces the recovery-code forms of `/login` (WEB-03) and of the
   existing-account step of org invitations (WEB-08), as the package text asks ("vom Login- und Registrierungspfad
   eingebunden"). Its test IDs are `prf-missing.*` (`page`, `explain`, `recovery.input|submit`, `remember`, `error`,
   `other-device`, `cancel`; the offer: `prf-missing.offer`, `.offer.remember|skip|failed`). The tests of WEB-03/WEB-08
   that used `login.prf-missing`, `login.recovery.*`, `invite.prf-missing`, `invite.recovery.*` were adapted; the
   texts are in `passkeys.<locale>.json` (keys `prf-missing.*`; the package owns no `prf-missing` catalog).
7. **Shared files touched (integration only).** `LoginScreen.tsx`, `InviteScreen.tsx`, `org/OrgInvite.tsx` mount the
   components; the "Lost your passkeys?" link on `/login` leads to `/recover`. `e2e/helpers/onboard.ts` declines the
   remember offer unless `remember: true`; `e2e/invite-platform.spec.ts` expects the offer for the authenticator without
   PRF.

## Recovery (`core/account/recover.ts`, `ui/screens/recover/`)

7a. **Core modules.** The flows of the owned screens need framework-free code with the core's 100 % Vitest gate and
    vector tests (acceptance criterion): `core/account/recover.ts` and `core/account/passkeys.ts` are new files next to
    WEB-03's sign-in flows (`login.ts`, `register.ts`), which they reuse; no existing core file changed except the
    additive `allowCredential` of `signIn` and the two `AppSession` getters.

8. **The new code is stored at once.** Right after the sign-in with the new passkey (restricted to it with
   `allowCredential`, its credential ID checked) the new recovery code is drawn and stored (`PUT
   /v1/me/bundles/recovery`), and only then shown once without "Skip": the replacement is mandatory (04 §5.5) and
   cannot be bypassed by leaving the page. Until the user retyped its last group the "Recovery code not confirmed"
   reminder is set, so a user who leaves early is sent to `/settings` to confirm the code (review finding). A failed
   save is retried with the same body before the code is shown; a repeat refused with `forbidden` after a
   stored-but-unanswered first attempt counts as done when `GET /v1/me/bundles/recovery` returns our slot.
9. **Waiting time.** `429 rate_limited` carries `Retry-After`; WEB-03's anonymous client drops headers, so the recovery
   requests run through a `fetch` wrapper that remembers the header (`recoveryClient`). Shown as whole minutes below
   two hours, whole hours beyond, rounded up; without the header "Try again later".
10. **Cancelled gestures.** The proof ceremony is single-use (HUB-11 decision 13): after a cancelled or failed passkey
    creation the next attempt begins again with the code still in the screen's state (counts against the limit).
11. **Unknown code.** `404 not_found` of `recovery/begin` is one text "No account was found for this recovery code".
12. **Test hub.** `scripts/hub_test_server.py` raises (by a keyword `functools.partial` of `SourceLimit`) the per-source limit of `recovery/begin` (5 per hour from the one
    loopback address of the suite) like WEB-03 raised the anonymous limit; the per-recovery-ID limit (20 per day, in
    the database) is untouched, so `e2e/recover.spec.ts` gets a real `429` from the hub.

## Passkeys (`core/account/passkeys.ts`, `ui/settings/sections/passkeys/`)

13. **The `get()` after adding (04 §5.6).** It is a full sign-in restricted to the new credential (`signIn` gained the
    optional `allowCredential`, which `requestOptions` already supported): the hub verifies the new passkey and
    certifies a session key, the PRF output opens nothing yet but derives `KEK_i` for the new slot. The browser ends its
    previous session (`POST /v1/session/end`) and continues with the new one; then the slot is uploaded. The
    confirmation is a separate button (a user gesture, as WEB-03 does for gesture 2).
14. **"On another device".** `authenticatorAttachment: "cross-platform"` with the hints `hybrid`, `security-key`: the
    platform offers its QR/hybrid and security-key UI. In tests this selects the `transport: "usb"` authenticator.
    `InvalidStateError` (the chosen authenticator already holds a passkey of the account, `excludeCredentials`) has
    its own message.
15. **Removal.** Not offered for the only passkey; a stale list gets the hub's `last_passkey`/`not_found` message and
    reloads. Removing the passkey of this session (marked "used for this sign-in"; `AppSession.credentialId`, a new
    getter) warns in the dialog and signs the browser out afterwards (the hub ends that session with the passkey).
16. **`AppSession` additions.** `credentialId` (the passkey of the current session, from the sign-in or the stored
    session) and `fetch` (for the recovery client). Additive; WEB-03's file.

## New recovery code from `/settings` (06 §3.2) – deviation

17. 06 §3.2 says a new code can be generated in `/settings`. The hub allows replacing the recovery slot only once after
    a finished recovery (HUB-03 decision 19, HUB-11 decision 13: a stolen session must not plant its own code or
    overwrite the recovery bundle), and changing the hub is outside this package. `/settings` therefore offers
    **confirming** the code instead: the typed code is checked in the browser against the signed recovery slot and the
    identity in memory (nothing is rotated), which clears the "Recovery code not confirmed" reminder of WEB-03. A new
    code is issued by every recovery (`/recover`). Open item for the hub owner: a replacement path that proves the
    identity (e.g. a recovery proof over a fresh challenge) would allow a new code from `/settings` without weakening
    decision 19.

## Tests

18. **Simulated hub.** `tests/core/account/hub.ts` (shared test helper) gained the recovery and passkey endpoints with the
    hub's rules (single-use proof, one rotation after a recovery, `last_passkey`, sessions of a removed passkey end,
    `Retry-After`), and `POST /v1/session/end` now ends only the caller's session, as `agent_hub.auth.sessions` does.
19. **Vectors.** `tests/core/account/recover.test.ts` runs the recovery steps against `contracts/vectors/recovery.json`
    and `bundle.json`: code text → `recovery_id`, the recovery slot opens to the identity whose key signs exactly the
    vector proof bytes.
20. **E2E-30** has no spec yet (SCN-03 owns `e2e/platform/specs/e2e-30-*`); the web-level specs
    `e2e/{recover,passkeys,prf-missing}.spec.ts` cover the same steps against the real hub (second `usb` authenticator,
    `hasPrf: false`, recovery after losing every passkey, removal incl. `last_passkey`, refused UV last).

## Review findings kept (low)

21. A device-key identity that the hub's registered keys reject (`decrypt_failed` in `AppSession.unlock`) leads to the
    guidance but the entry is not deleted; "Forget" in `/settings` removes it. Entries that do not decrypt are deleted.
22. "Add passkey" ends the previous session before the new one is stored; if storing the new session key fails, the
    browser has to sign in again (no data is lost).
23. The label `device-bundle\0` is local to the browser (decision 2) and is not part of 04-crypto.md §3, whose labels
    are protocol labels shared by hub, runner and browser.
24. The catalog keys of the replaced recovery forms were removed from `login.<locale>.json` and
    `invite-org.<locale>.json`.

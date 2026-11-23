# WEB-03 – decisions

App shell, platform invitation, passkey registration with PRF, recovery code, sign-in and unlock, private
organization, settings frame (06-web.md §2.4–§2.6, §3.1 platform path, §3.1.1, §3.2, §3.4, §11 job 7, §12.5, §12.6
basic form, §13; 04-crypto.md §5.1, §5.3, §5.4; 03-api.md §4.1, §4.3; ADR-006, ADR-026).

1. **Account flows in the core.** The registration, sign-in/unlock and private-genesis flows are framework-free in
   `web/src/core/account/` (`anon.ts`, `ceremony.ts`, `register.ts`, `login.ts`, `genesis.ts`, `session.ts`,
   `prefs.ts`) so that Vitest covers them at 100 % without a browser (07-testing §3); the screens only drive them.
   `navigator.credentials` is passed in as a structural `Credentials` (core stays free of the DOM, 06 §2.2).
2. **Anonymous requests** (`lookup`, `register/*`, `login/*`) go through `anonClient` (no credentials, no cache, no
   redirects) and map errors with the same `errorOf` as signed requests. The hub does not sign anonymous answers
   (03-api §1); nothing in them is trusted beyond the client's own checks (options, statement, `key_id` =
   SHA-256(statement), bundle decryption, verified logs).
3. **Cancellation.** `AbortError`/`NotAllowedError` and a `null` credential are `CeremonyCancelled`. Gesture 1 cancelled
   → back to the intro with "try again" (the next attempt starts a new `register/begin`); gesture 2 cancelled → the
   same step again (the user is already registered, nothing is registered twice). A passkey of another account at
   gesture 2 is refused before the session is used.
4. **Gesture 2 is a button.** Browsers (Safari) require a user activation for `get()`; the flow shows "Confirm once
   more to sign in" with a button instead of starting the ceremony automatically.
5. **PRF.** The passkey slot is uploaded after gesture 2 only if that `get()` returned PRF output (04 §5.1 step 5);
   `create()`'s PRF output is not used. Without PRF the onboarding continues (the identity is in memory) and the
   `session` slice records `prfMissing` for WEB-14's guidance. At `/login`, a missing PRF output, a missing slot or a
   slot that does not open (`decrypt_failed`) lead to the recovery-code unlock (no rotation); with a PRF-capable
   passkey whose slot is missing, the slot is uploaded after that unlock.
6. **Identity check on unlock.** `AppSession.unlock` compares the unlocked identity with `GET /v1/me.identity`
   (`decrypt_failed` otherwise) before using it, then awaits the `login` jobs (log + wrap verification) of every org.
7. **Reload = locked.** A stored, unexpired session key restores the phase `locked`; identity and scope keys need
   the passkey again (06 §2.3). Every route with a right other than `anon` (`/o/…`, `/settings`, `/notifications`,
   `/platform/…`) redirects a locked or anonymous browser to `/login?next=<path>`; `next` is honoured only for local
   paths (`/x`, never `//host` or `/\host`).
8. **Platform invitation that already registered somebody** (`lookup.registered: true`): shown as `invite_registered`
   on a fresh page. Resuming another browser's unfinished registration is not possible without its identity; the hub
   releases the invitation after 24 h (ADR-026). Device invitations on `/invite` are `invite_unknown`.
9. **Fragment.** Read once and removed with `history.replaceState(history.state, …)` (React Router's state kept). A
   second read for the same history entry (React's double render in development) returns the remembered code; the
   memory is cleared when the flow is done. The code is held only in component state.
10. **Genesis retry.** The prepared `POST /v1/orgs` body (IDs, entries, wraps, events) is kept in the `accept` step; a
    retry sends the identical body (idempotent `replayed`). After success the client stores the log and keys from the
    batch it built, pins founder and head in IndexedDB (L3/L9) and reloads `/v1/me`.
11. **Names.** Private org "Private"/"Privat" and default workgroup "Default"/"Standard" follow the UI locale at the
    time of genesis. Display name: 1–80 code points after trimming (the contract allows 100).
12. **Recovery-code reminder and last org in `localStorage`** (`agent-platform.recovery-unconfirmed.<user>`,
    `agent-platform.last-org.<user>`): non-secret flags/IDs, outside the IndexedDB store list of `persist/` (owned by
    WEB-02, "nothing else"). A refusing `localStorage` only loses them. Generating a **new** code in `/settings` (06
    §3.2) needs a hub path that HUB-03 restricted to after a recovery ceremony (decision HUB-03 19); the banner links
    to `/settings`, where WEB-14's recovery section belongs.
13. **Renewal.** "Sign in again" runs the login ceremony in place: same user → new session key (`begin`), identity
    stays; another user → sign-out and `/login`. A sign-in of another user via `begin` always clears the previous
    user's memory first.
14. **Live channel.** After unlock, the org of the current `/o/:org` route is activated (`AppSession.activate`), which
    starts `connectLive` for it (one org at a time). `/v1/stream` does not exist before HUB-07; a 404 stops the channel
    (fatal) without affecting the screens.
15. **Unsupported browser** (06 §12.6): checked at boot (`PublicKeyCredential`, `ReadableStream`, X25519 and Ed25519
    key generation) and when IndexedDB cannot be opened; shows a page with the supported browsers. Blocked orgs
    (§11 job 1) show the blocking screen with the evidence (stored head, served head, `lh`) instead of the screen.
16. **Settings sections.** The frame renders the discovered sections; `language` has order 90 so that the sections of
    later packages (passkeys, recovery, model, credentials, GitHub, quotas) come first. Sign-out is part of the frame.
17. **Rate limit of the test hub.** All Playwright tests reach the hub from 127.0.0.1, and the anonymous limit (30 per
    minute per source address, 03-api §1) refused the suite's ceremonies after a few tests. `scripts/hub_test_server.py`
    now starts the hub through a small launcher that raises `PER_MINUTE["anonymous"]` in that process only; the limit
    itself stays covered by the hub's tests.
18. **Merged coverage.** (a) `src/core/**` is gated by Vitest's own thresholds and left out of the merge
    (`isBrowserGated`): once the app bundle executes core code, the bundler's reprint (e.g. single-statement `if`
    bodies without braces) produces branch ranges that do not line up with Vitest's and count as uncovered. The
    "missing data" check still covers every file of `src/`. (b) Vitest uses the production JSX transform
    (`oxc.jsx.development: false`) like the build. (c) Source rules for UI code that runs in both suites: no braces
    around single-statement `if` bodies that only one suite executes; JSX branches reached by only one suite are
    covered in the other too.
19. **Foreign test files adapted.** WEB-01/WEB-02 tests that asserted "empty so far" registries or the exact slice
    list now assert containment; WEB-01 specs that used `/` as "a page showing not-found" use `/nowhere`, and the
    not-found link now ends on `/login` (the start route redirects anonymous visitors). `boot` takes the platform
    objects (`credentials`, `indexedDB`, `storage`, optional `supported`/`store`) as inputs.
20. **E2E helpers** (new, `web/e2e/helpers/`): platform invitations by root-signed requests with the public test root
    seed (decision WEB-01 12), the CDP virtual authenticator, the onboarding sequence, and cancelled ceremonies via an
    init script at the WebAuthn API boundary. E2E-01/E2E-30 (`e2e/platform/specs/`) do not exist yet; SCN-01 writes
    them on top of these screens and test IDs.
21. **Renewal reminder without requests** (§11 job 7): `core/account/expiry.ts` watches `me.session.expiresAt` and
    sets `expiring` five minutes before and `expired` at the end, also when no request is made; the shell starts it.
22. **No skip without a passkey slot.** If gesture 2 returned no PRF output, no passkey slot exists and the recovery
    code is the only way to unlock (until WEB-14's browser key): the onboarding then offers no "Skip".
23. **Independent review – handled.** High: skip without PRF (22). Medium: renewal reminder only on requests (21).
    Low, fixed: recovery-code unlock now confirms the identity before uploading a missing passkey slot; an identity
    that is not the registered one after a valid sign-in leads to the recovery-code option instead of a dead end; the
    remembered fragment is dropped as soon as the screen holds the code, and the history-entry comparison no longer
    relies on object identity; the skip dialog traps Tab and returns focus; a test that the PRF output never reaches
    the hub. Medium, kept with reasons: (a) the merged-coverage change (18) – the core is gated at 100 % by Vitest as
    07-testing §3 prescribes, the "missing data" check still includes it, and without the change no package whose
    screens run core code in the browser can pass the WEB-01 gate; (b) files outside the listed ownership – the
    changes are the minimum that adding the app shell and the first routes requires (19), `core/account/`,
    `e2e/helpers/` and `screens/settings/routes.tsx` are new files of this package's areas (the router discovers
    screens only under `screens/*/routes.tsx`). Low, kept: the rate-limit launcher (17) patches the limit table that
    the limiter reads per request (no cached copy); Vitest's production JSX transform (18b) loses React's
    development warnings in UI tests (lint and types still run); a session certified for another account at gesture
    2 or renewal is dropped without `POST /v1/session/end` (it expires at the hub; its key never left memory); the
    genesis-retry spec stubs the first answer (the hub's `replayed` path is HUB-05's test); a registration whose
    finish the client rejects after the hub accepted it shows `invite_registered` on the next try (released after
    24 h); the reminder flag is per browser (12); React Router keeps the original location (with the fragment) in
    memory only.

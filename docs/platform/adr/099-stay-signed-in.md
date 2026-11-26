# ADR-099: Stay signed in – the browser's device key opens the identity at start-up, the session renews itself with the still valid session key

Status: Accepted (2026-11-26, KOR-74)

Implements [E-40](../KONZEPT.md#e-40) (stay signed in on agent.dfx.swiss after a reload, in a new tab, after closing
and reopening the tab and after a browser restart, until the user signs out), under [E-25](../KONZEPT.md#e-25) (change
existing bounds only as far as needed, no additional protection layer) and [E-12](../KONZEPT.md#e-12) (PRF passkeys).
Amends [ADR-006](006-passkey-prf-key-management.md) (the optional browser device key becomes the default way a browser
keeps the identity, not only a substitute for passkeys without PRF) and the session lifetime of 04-crypto.md §4.9 as
applied by the hub (the passkey certificate keeps its 12 h bound).

## Context

- Before this decision the browser kept only the session key (P-256, non-extractable, IndexedDB); the identity and the
  scope keys lived in memory only (06-web.md §2.3). After a reload or in a new tab the browser was `locked` and needed
  the passkey; after the session's 43080 s (`SESSION_KEY_ISSUE_SECONDS` of `agent_core.passkey.encoding`, published as
  `session_certificate_seconds`) every tab showed "Sign in again" (`core/account/expiry.ts`).
- The building block for a browser that opens the identity without a gesture exists: the device key of ADR-006 and
  06-web.md §3.3 ("Remember this browser", `web/src/core/persist/device-key.ts`), a non-extractable AES-256-GCM key in
  IndexedDB that encrypts the identity bundle plaintext locally; until now it was offered only to passkeys without PRF
  and used only at the next sign-in.
- The hub is the only verifier of browser session keys (`agent_hub.auth.keys.resolve_session`). The passkey statement
  `agent-session-v1` is checked once, at `login/finish`; afterwards the hub trusts the `browser_session` row (its
  public key, `expires_at`, `ended_at`, the passkey and the user still active).

## Decision

### 1. The device key keeps the identity; `restore()` unlocks with it

- "Stay signed in" is a choice of the browser, **on by default**: a checkbox on `/login` (kept per browser in
  `localStorage`, `Prefs.staySignedIn`; switched off it stays off for the next sign-in on this browser). Every
  `AppSession.unlock` – login, invitation, recovery code – applies it: on, `DeviceKeys.remember` seals the identity with
  a new device key (the existing format and AAD of ADR-006); off, this browser's entry of the user is deleted, so a
  reload is `locked` exactly as before.
- At start-up `restore()` loads the stored session as before; if it is unexpired and the device-key entry of its user
  opens, it runs the unlock (the identity must match `GET /v1/me`, logs and wraps are verified) and goes to `unlocked`
  without a gesture. An entry that does not open, or holds another identity than the registered one, is deleted; a hub
  that cannot be reached leaves the browser `locked` (the passkey still unlocks) and keeps the entry.
- `/settings` shows the entry under "Stay signed in" with its date and "Forget on this browser" (deletes the entry and
  stops the renewal of §2; the session then simply ends and a reload asks for the passkey), or "Stay signed in on this
  browser" when there is none. The "Remember this browser" option of the `prf_missing` guidance writes the same entry
  and therefore also keeps the browser signed in.
- "Sign out" ends the session at the hub, deletes the session entries and this browser's device-key entry of the user
  (before: the device key survived a sign-out). Nothing keeps the browser signed in afterwards.

### 2. The session renews itself, signed by the still valid session key

- New endpoint `POST /v1/session/renew` (`user`, body `{}`, answer `{key_id, expires_at}`), signed like every request
  with the session key. The gate has already refused an expired (`session_expired`), ended, passkey-removed or
  user-disabled (`key_revoked`) key. The hub moves `expires_at` of **that** `browser_session` row to its clock plus
  `SESSION_RENEW_SECONDS` = 30 days (never earlier than before; a row ended between the gate and the update is
  `key_revoked`); key, statement, `created_at` and `pairing_id` stay. An agent is admitted like on every browser route
  (ADR-056 §4, `agent_hub.agents.scope`): it has no browser session and its key does not expire, so it gets its key ID
  with `expires_at` null and nothing changes. Every renewal is audited as `auth.session_renew` (detail `key_id`; every write leaves
  its own row, ADR-063 section 1) – at most one row per browser and hour.
- Why the same key and not a new one: a renewal that certifies a new session key would need a second key in the
  browser, a hand-over between the tabs that still sign with the old one and a grace period for the old key at the
  hub – more moving parts for no protection gain, since the key is non-extractable and only this browser holds it.
- Why 30 days: the requirement is "until the user signs out", also after the browser was closed overnight or over a
  weekend. A renewal can only be signed by a still valid key, so the lifetime of a renewed session is the longest time
  a browser can stay unused and still come back without a passkey. Keeping 12 h would bring the passkey back every
  morning; 30 days is the usual bound of "stay signed in" and the smallest round value that covers holidays. It is the
  only bound that changes; the 12 h of the passkey certificate (`SESSION_KEY_MAX_SECONDS`, V11) and of a fresh login
  stay, and a browser with "Stay signed in" off is never renewed.
- The browser (`AppSession.renewIfDue`, every minute while it stays signed in, and once at start-up and after the
  sign-in) renews when this browser's last renewal is an hour old or the session ends within the five minutes of
  `RENEW_BEFORE_MS`; the new end and the time of the renewal (`renewedAt`) are stored with the session in IndexedDB.
  A sign-in therefore turns the 12 h session into a 30-day one at once, and the "Sign in again" prompt does not appear
  for a browser that stays signed in.

### 3. Several tabs: one renews, the others take over

- The renewal runs under the page's Web Lock `agent-platform.session-renew` (`navigator.locks`; without it each tab
  renews on its own, which is harmless: renewals of the same row only move its end). Inside the lock a tab first reads
  the stored session: a later end written by another tab is taken over without asking the hub (and then the renewal
  is not due), a new key of the same user written by another tab's sign-in replaces this tab's key; a session of
  another user is left alone.

### 4. Bounds that do not change

- No additional protection layer ([E-25](../KONZEPT.md#e-25)): no second factor, no device binding at the hub, no
  absolute maximum of renewals. What ends a browser that stays signed in is what ended a session before: sign-out,
  removal of its passkey (ends all its sessions at once), a disabled user – plus 30 days without use.
- The threat-model row "Stolen phone or laptop" (04-crypto.md §9) names the residual risk: a browser that stays signed
  in opens without a gesture until one of these happens; the user chooses it on the sign-in page and switches it off
  on shared devices.

## Consequences

- Web: `core/account/session.ts` (`restore`, `unlock`, `renewIfDue`, `noteRemembered`, `signOut`), `core/account/prefs.ts`
  (`staySignedIn`), `core/persist/index.ts` (`renewedAt`), `ui/screens/login/LoginScreen.tsx` (checkbox),
  `ui/settings/sections/passkeys/ThisBrowser.tsx` (texts, renewal on and off), `ui/app/boot.tsx` (device keys and Web
  Locks); catalogs `login.*`, `passkeys.browser.*`.
- Hub: `agent_hub/auth/sessions.py` (`renew_session`), route in `auth/routes.py`; contracts `api/session-renew.*` and
  the audit action `auth.session_renew` (`contracts/changes/KOR-74.md`).
- Tests: hub `tests/platform/hub/auth/test_login.py` (renewal); web `tests/core/account/session.test.ts` (restore,
  renewal, tabs, sign-out); Playwright `web/e2e/stay-signed-in.spec.ts` (reload, new tab, reopened tab, restarted
  browser, silent renewal of a session about to end – the device double sets the hub's session end, `POST
  /users/<id>/sessions/expiring` – "Stay signed in" off, sign-out). The specs written before E-40 run with the choice
  off (`staySignedIn` fixture), so they keep testing the locked reload; `prf-missing.spec.ts` follows §1 (the remembered
  browser stays signed in; sign-out deletes the entry).
- Docs: 06-web.md §2.3, §2.6, §3.3, §3.4; 04-crypto.md §2, §4.9, §5.4, §9; 03-api.md §4.1; key model; agent parity
  (`stay-signed-in`, n/a for agents).

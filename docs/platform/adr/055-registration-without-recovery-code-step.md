# ADR-055: Passkeys must deliver PRF; the registration has no recovery-code step

Status: Accepted (2026-11-23)

## Context
- ADR-006 made the recovery slot mandatory and had the user confirm the code at registration: after the passkey the
  screen "Your recovery code" showed the code, "Copy", "Download as text file", asked to re-type the last group and
  offered "Skip" behind a consequence dialog; skipping left a red banner in the shell until the code was checked in
  the settings. The same screen followed every recovery and the settings' "Generate a new recovery code".
- At the first real sign-in the owner rejected this step as a bad user experience (KOR-22).
- A colleague then registered with a passkey of a password manager without PRF. The registration went through, but the
  passkey could never unlock the keys; the account depended on the recovery code and had to be disabled and invited
  again by hand. The owner's rule: if PRF is needed, a passkey without PRF must fail **at registration**, with a clear
  hint that a passkey with PRF is needed and that Bitwarden does not support it.
- The hub requires a recovery slot in the first `register/finish` (03-api.md §4.1, "exactly one recovery slot per
  user", 04-crypto.md §4.1). Whether a passkey delivers PRF is known for sure only at gesture 2 (the first sign-in),
  after `register/finish`; `create()` reports `prf.enabled` in current browsers, but a report is not an output.
- Constraint: no state in which a user believes to have a code that was never shown; no half account.

## Decision
1. **PRF is required.** A passkey whose sign-in gives no PRF output is not accepted – at registration, when a passkey
   is added in the settings and for the new passkey of a recovery. One message ("This passkey cannot unlock your
   data. You need a passkey that supports PRF.") names what works (the device's built-in passkeys: iCloud Keychain
   from iOS 18 / macOS 15, Google Password Manager) and what does not (Bitwarden currently does not support PRF for the
   passkeys it stores), asks to delete the unusable passkey in the password manager and offers "Try again with another
   passkey". The provider list lives in one place, the `passkeys` text catalog (`de`, `en`).
2. **No half account.** The order stays (register, then sign in, then upload the passkey slot), because only the
   sign-in proves PRF; a missing output is cleaned up at once: `DELETE /v1/me/registration` (new, additive) deletes
   the unfinished registration – the same rows the 24 h orphan job deletes – and frees the invitation, only while
   nothing depends on it (invitation unused, no org membership, no platform role, no task; else `403 forbidden`).
   The browser then signs out and the same link works again. An older hub without the endpoint answers `404`; the
   message then says that the invitation is usable again after 24 h at the latest (orphan job). An older client never
   calls it. Adding a passkey: the previous session removes the new passkey (`DELETE /v1/passkeys/{id}`, which ends
   its sessions too). Recovery: the new passkey removes itself with its own session (the lost ones still count, so it
   is never the last), the browser signs out, and the recovery begins again with the same code, which nothing replaced.
3. **No recovery-code step at registration.** Every new account has a PRF passkey, so the passkey unlocks. The
   required recovery slot is an **unset** slot: sealed under `KEK_rec` of a random code that the browser drops at once,
   over zero bytes of the bundle's length, with the nonce `HKDF(ikm=ed25519 seed, info=P‖"recovery-unset\0"‖user_id)
   [:12]` (04-crypto.md §3, §4.1). Nobody can open or find it; the slot carries no mark the hub can read. The browser
   with the unlocked identity recognises the nonce and knows that no code exists. No code is shown, nothing reminds.
4. **Settings, voluntarily.** "Recovery code": one quiet line "You have no recovery code yet." while the slot is the
   unset one, and "Create recovery code" ("Create a new recovery code" once one is known to exist). The code is shown
   with "Copy", "Download as text file" and one "Done", which stores it after one passkey assertion (the KOR-01
   replacement); an old code stays valid until then. Nothing is re-typed; no banner, no reminder flag (an old flag in
   `localStorage` is deleted at sign-in).
5. **Recovery** shows the new code that replaces the used one with one "Continue".
6. **Existing accounts keep the `prf_missing` guidance** (06-web.md §3.3): accounts made before this change with a
   passkey without PRF, and PRF passkeys that sign in from a browser or password manager that gives no PRF output (a
   synced passkey in a browser without PRF support). They unlock with their recovery code (if they have one) or a
   remembered browser, or add a PRF passkey from another device. Nothing here removes a passkey or a slot of an
   existing account, so no existing user is locked out by this change. The "Remember this browser" offer on a
   registration screen is gone with the code step.

## Consequences
- The normal registration is two passkey gestures and lands in the organization; the code step, the skip dialog, the
  shell banner, the reminder flag, the settings' code check and the separate "Remember this browser?" screen are gone.
- Users of password managers without PRF cannot register or add such a passkey; they are told why and what works.
- A new user has no code until creating one; for a single-member private organization the loss of every passkey then
  loses its data (04-crypto.md §5.5 step 4). The settings say in one line that no code exists.
- The hub can guess from metadata it already sees (attestation, a passkey slot right after registration, no code
  replacement later) which users probably have no code; an active hub that serves another slot makes the settings
  say that a code exists (availability only). 04-crypto.md §9 lists both.
- The `prf_missing` guidance offers the code entry even to a user without code (before the unlock the browser cannot
  tell the unset slot from a real one).
- Contract: one additive endpoint (`contracts/changes/KOR-22.md`); no migration. Old and new clients and hubs stay
  compatible as described in decision 2.
- Amends ADR-006 ("mandatory recovery slot … confirmed by the user at registration"; "Without PRF: recovery-code
  unlock"): the slot stays mandatory, the code does not, and new passkeys must deliver PRF.

## Alternatives considered
- **Prove PRF before `register/finish`** (only trust an output already returned by `create()`): most platform
  passkeys report `prf.enabled` at `create()` without an output, so almost every user would be refused or need a third
  gesture; trusting `prf.enabled` alone lets exactly the faulty cases through.
- **Leave the account to the 24 h orphan job:** the invitation would be blocked for a day (`invite_registered`).
- **Keep accounts without PRF with a mandatory code screen** (the first version of this ADR): the owner rejected it
  after the real case above.
- **Recovery slot optional at the hub:** exact, but a contract change of `register/finish` and a migration of the
  running hub; the unset slot gives the browser the same knowledge.
- **A mark the hub can read** (fixed nonce): tells the hub which users have no code. **A code derived from the
  identity:** turns a leak of the identity seed into an account takeover through `/recover`.

## Deviation from DESIGN.md
None.

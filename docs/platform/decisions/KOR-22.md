# KOR-22 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-22 – Registration without the recovery-code step; passkeys must deliver PRF (shared documentation)

The change lives in the agent-core repository (web client and hub, see its `decisions/KOR-22.md`). This repository
takes the shared documents unchanged: ADR-055 (with the note in ADR-006 and the index), `spec/03-api.md` (§4.1
`DELETE /v1/me/registration`), `spec/04-crypto.md` (§2, §3, §4.1, §5.1, §5.5, §5.6, §9), `spec/06-web.md` (§3.1,
§3.1.1, §3.2, §3.3, §3.5, §3.6), `spec/01-requirements.md` (R-1.6), `spec/07-testing.md` (E2E-01, E2E-30),
`spec/00-overview.md`, `PLATFORM-DESIGN.md` and `spec-deviations.md`, and the contracts (`contracts/changes/KOR-22.md`:
one additive endpoint). The plans `spec/09-milestones.md` and `spec/10-work-packages.md` keep their historical wording.
The runner is not affected: it never reads the recovery slot and does not call the new endpoint.

## KOR-22 – Registration without the recovery-code step; passkeys must deliver PRF

Owner's requests after the first real sign-ins: (1) the screen "Your recovery code" after the passkey (show,
copy/download, re-type the last group, "Continue" or "Skip" with a dialog) has to go; (2) a passkey without PRF must
fail at registration with a clear hint ("You need a passkey that supports PRF; Bitwarden does not support PRF").
ADR-055 records the design; this file records the smaller decisions.

### Decisions

1. **Where PRF is proven.** Only the first sign-in (gesture 2) yields a PRF output; `create()` reports `prf.enabled`
   at best. The order register → sign in → upload the passkey slot stays; without an output the browser withdraws the
   registration at once (`DELETE /v1/me/registration`, `abandonRegistration`) and signs out (`withdrawRegistration`).
   `prfAtCreate` stays informational.
2. **The withdrawal reuses the orphan job.** `agent_hub.auth.jobs.abandon_registration` deletes the same rows as the
   24 h job (`USER_ROWS`) under the same "unfinished" condition (`UNFINISHED`: invitation unused, no org member, no
   platform role, no task) and resets `registered_user`; audit `invite.release` with actor `user` and the existing
   detail (no contract change of the audit detail). A finished account gets `403 forbidden` (an existing code, no new
   error code). The endpoint is in the auth area's router (it acts on the registration), path under `/v1/me`.
3. **Compatibility.** The endpoint is additive (`contracts/changes/KOR-22.md`). An older hub answers `404`: the client
   still shows the message and adds that the invitation is usable again after 24 h at the latest
   (`passkeys.prf-required.later`). Older clients never call it.
4. **Adding a passkey.** The PRF output of the confirming sign-in is checked before the browser switches to the new
   session; without one the previous session removes the new passkey (which ends its session at the hub), the passkey
   list is reloaded and the message offers another try.
5. **Recovery.** The new passkey without PRF removes itself (`DELETE /v1/passkeys/{id}` with its own session; the
   lost passkeys still exist, so it is never the last), the browser signs out, "Try again" returns to the code step
   with the code still typed in – nothing has replaced it yet.
6. **One message, one text place.** `PrfRequired` (`ui/prf-missing/PrfRequired.tsx`) for all three contexts; texts
   `passkeys.prf-required.*` in `de` and `en`. Provider statements are limited to what has a source (report): built-in
   passkeys of Apple (iCloud Keychain, iOS 18 / macOS 15 and later) and Google (Password Manager), and Bitwarden
   "currently" without PRF for stored passkeys. 1Password and Windows Hello are not named (sources weaker or newer).
7. **The unset recovery slot** (`nonce_unset = HKDF(ed25519 seed, P‖"recovery-unset\0"‖user_id)[:12]`, zero
   plaintext of the bundle's length) is now the slot of every new account. The label exists only in the browser
   (`labels.ts`); hub and runner neither produce nor read it, so the shared vectors do not cover it.
8. **Removed:** the code step of the registration, the skip dialog, the shell banner (`RecoveryBanner`), the reminder
   flag (`session.recoveryUnconfirmed`, `Prefs.recoveryUnconfirmed`; an old flag is deleted at sign-in by
   `Prefs.forgetRecoveryReminder`), the settings' code check (`checkRecoveryCode`), `NewRecoveryCode`, the separate
   "Remember this browser?" screen (`RememberBrowser`), `OnboardingRecovery`, the registration's code (`codeSlot`,
   `Registration.recoveryCode`).
9. **Settings component.** `RecoveryCode` reads the status once per unlocked identity; a failed read shows no hint
   ("unknown"); only a code known to exist is called "current"/"old". The code is shown before the passkey gesture, so
   an old code stays valid until the hub accepted the new one (KOR-01 order); the button says "Done".
10. **Existing accounts.** The `prf_missing` guidance at sign-in is unchanged (code, remembered browser, PRF passkey
    from another device). Nothing in this change removes a passkey or slot of an existing account.
11. **Tests in the repository are adapted, workspace scripts are not.** The unit, browser (`web/e2e`) and full-chain
    (`e2e/platform`) tests follow the product. Helpers that open an identity in Node with the recovery code create one
    in the settings after onboarding (`createRecoveryCode`); `onboard(..., { code: false })` skips it; `withoutPrf`
    moves an account's passkeys into an authenticator without PRF (existing account in a browser without PRF);
    `giveRecoveryCode` gives a simulated account a code in the unit tests. E2E-39 aborts the `accept` request to leave
    before the join; E2E-30 checks that a PRF-less passkey is refused when added and signs in an existing passkey from
    a PRF-less authenticator.
12. **The acceptance record is history.** `acceptance/FINAL.md` describes the accepted state and is not rewritten;
    `spec/09-milestones.md` and `spec/10-work-packages.md` keep their historical wording.

### Test IDs (`data-testid`) of the web client

- **Gone:** `recovery-code.confirm`, `recovery-code.mismatch`, `recovery-code.skip`, `recovery-code.skip-dialog`,
  `recovery-code.skip-confirm`, `recovery-code.skip-cancel`, `shell.recovery-banner`, `shell.recovery-banner.settings`,
  `prf-missing.offer`, `prf-missing.offer.remember`, `prf-missing.offer.skip`, `prf-missing.offer.failed`,
  `passkeys.recovery.unconfirmed`, `passkeys.recovery.input`, `passkeys.recovery.submit`, `passkeys.recovery.ok`,
  `passkeys.recovery.error`, `passkeys.recovery-new` (+ `.start`, `.working`, `.notice`, `.cancel`),
  `recover.new-code`.
- **Kept:** `recovery-code.step`, `recovery-code.code`, `recovery-code.copy`, `recovery-code.copied`,
  `recovery-code.copy-failed`, `recovery-code.download`, `recovery-code.continue` (the only button: "Continue", in the
  settings "Done"), `passkeys.recovery` (`data-status` `loading|none|set|unknown`, `data-step` `idle|show`).
- **New:** `recovery-code.explain`, `passkeys.recovery.none`, `passkeys.recovery.create`, `passkeys.recovery.working`,
  `passkeys.recovery.notice`, `passkeys.recovery.done`, `passkeys.recovery.cancel`, `prf-required` (`data-context`
  `register|add|recover`), `prf-required.retry`, `prf-required.later`. `passkeys.add` has the new `data-step`
  `prf_required`.

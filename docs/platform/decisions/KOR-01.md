# KOR-01 – Invitation bound to the invitee, renewable recovery code, separate ceremonies

Corrections from AUD-01 (A-02, A-14, B-06, B-21). Architecture: ADR-043.

## A-02 – registration needs a proof of the invitation code

1. **Root cause, not the symptom.** The hole was that registration trusted the `invite_id`, which several readers see
   (platform log, inviter listing, membership log). Hiding the ID from the platform log (`H(invite_id)`) would have left
   the other readers; the fix is a proof of the code at `register/finish` (ADR-043).
2. **Uniform for all invitation kinds.** Org invitations require `inv_sig` as well, though their `accept` was already
   protected: one browser path, and a member can no longer block an invitee by registering first.
3. **Where the hub finds `pk_inv`.** Platform invitations: new column `invite.pk_inv` (migration 0007), filled by the
   platform-log projection (`device_invite`, `user_invite`) and by the inviter's request. Org invitations: the `pk_inv`
   of the stored membership-log `invite` entry (the `invites` area that creates org invitation rows is not changed).
4. **Old rows.** A platform user invitation without a key (created before migration 0007) cannot be registered through
   (`404 invite_unknown`, the code for every invitation that is not open). There are no deployed hubs.
5. **Inviter key check.** `POST /v1/platform/invites` refuses a `pk_inv` that is no valid Ed25519 public key with
   `400 params_invalid` (pointer `/pk_inv`): such an invitation could never be used. An identical repeat (same ID,
   expiry and key) is idempotent; another key under the same ID is `409 id_conflict`.
6. **Refusal consumes the ceremony.** A wrong `inv_sig` is `401 signature_invalid` after `take_ceremony`, so a
   challenge gets one attempt, like the WebAuthn checks.
7. **Test browser.** The hub tests' `FakeBrowser` knows the codes of the invitations the test support created
   (`remember_invite`), as an invitee's browser knows the code from the link; the web test hub (`SimHub`) hands out
   code texts and verifies `inv_sig` with the real web crypto.
8. **Early refusal of keyless invitations.** `register/begin` already answers `invite_unknown` for a platform
   invitation without a key (review finding), so nobody creates a passkey for an invitation that cannot be used;
   `finish` keeps the check for a key that vanished in between.
9. **Not bound to the identity keys (review, low, not adopted).** `inv_sig` covers `user_id` and the ceremony
   challenge, not the new identity's `sig_pub`: the hub stores the identity in the same request whose ceremony the
   proof binds, and a client cannot later check a platform registration against the code anyway (no package, no
   `accept`). Org invitations still bind the identity to the code through `accept.inv_sig`.

## A-14 – `register/finish` refuses the recovery proof ceremony

10. The proof ceremony of `recovery/begin` is the `recovery` ceremony with `hub_value` set (HUB-11). `register/finish`
    refuses it in the check that runs before the ceremony is consumed (`400 ceremony_invalid`), so a refused attempt
    neither adds a passkey nor marks it used (a used recovery ceremony is what allows the one replacement of the
    recovery slot) and the user's own proof can still be presented.

## B-21 – parallel `register/finish`

11. The behaviour was already right (`take_ceremony` locks the row `FOR UPDATE` and consumes it in its own
    transaction); only the test was missing. The new test finishes one ceremony from two threads at once, with the
    same and with two credentials: exactly one `ok`, the other `ceremony_invalid`, one principal, passkey, identity and
    binding. Without the row lock and the `used_at` condition the test fails (checked by a temporary mutation).

## B-06 – a new recovery code at any time

12. **Spec conflict.** 06-web.md §3.2 requires "generate a new recovery code" in `/settings`; 03-api.md §4.1 and
    04-crypto.md §5.5 allowed a replacement only after a recovery. The web requirement wins (R-1.6: a user who lost
    the code must be able to get a new one before losing the last passkey); the replacement keeps the protection the
    old rule was for (a stolen session must not plant a code) by requiring a fresh passkey assertion. DOC-01: 03 §4.1
    (new route, `assertion`), 04 §5.5 (last sentence of step 3), 06 §3.2 (flow below), 02 §2.1 (ceremony kind
    `recovery_rotate`), 02 §2.9 (`auth.recovery` step `rotate`).
13. **Identity proof.** `POST /v1/me/recovery/begin` (signed) opens a ceremony of the new kind `recovery_rotate`
    (migration 0008) with a 32-byte challenge and answers it with the login request options (discoverable credentials,
    UV, PRF), so the browser reuses its sign-in code. `PUT /v1/me/bundles/recovery` takes `assertion` (the login
    certificate's fields without the statement plus `ceremony_id`); the hub checks it with the reused
    `agent_core.passkey.webauthn` functions (client data type/challenge/origin, RP ID, UP, UV, signature, counter)
    against an active passkey of the caller, locked in the same transaction that replaces the slot. The ceremony is
    consumed by the attempt; a foreign or other ceremony is `ceremony_invalid` and stays untouched.
13a. **PRF (review, medium; deviation from the work package wording "assertion with PRF").** The options ask for PRF
    like every sign-in, but the replacement does not require or use a PRF output: the new slot is sealed with the
    identity already unlocked in this session, and the hub cannot observe PRF anyway. Requiring it would lock exactly
    the users out whose passkeys have no PRF (exported passkeys, non-PRF managers) – they depend most on a valid
    recovery code. The assertion is the identity proof; the session's unlocked identity is what is sealed.
14. **Order in the browser.** The browser seals the new slot with the identity in memory, shows the code, requires the
    re-typed last group (no skip) and only then runs the gesture and the `PUT`; the old code stays valid until the
    hub accepted the new one. A cancelled gesture keeps the shown code.
15. **Audit.** Both replacements (with assertion, after a recovery) are audited as `auth.recovery {step: "rotate"}`
    (new value of the existing detail; before, the replacement after a recovery was not audited at all).
16. **After a recovery (review of A-14).** The replacement without assertion now needs a used `recovery`
    registration ceremony without `hub_value` whose finish added a passkey (`passkey.created_at = ceremony.used_at`),
    so neither the proof ceremony nor a finish that failed after consuming its ceremony opens it.
17. **Migration outside the area paths.** `0008_ceremony_recovery_rotate.sql` widens the ceremony kind check; new
    migrations take the next free number (ADR-027 §5), no central list is edited.
18. **Review points not adopted (low).** `allowCredentials` stays empty (the web's request-options check and the hub
    contract are those of a login; a passkey of another account is refused with `credential_invalid` and costs the
    attempt only); `POST /v1/me/recovery/begin` has no own rate limit (like `register/begin {add:true}`: signed,
    ceremony rows are purged after a day); refused replacements are not audited (as refused registrations and passkey
    additions; sign-in refusals are, because they are anonymous attempts).

# ADR-060: Admins admit existing accounts directly (`admit`), with the identity proven through the platform log

Status: Accepted (2026-11-23)

## Context
An org membership came into being only through an `invite` entry that the invitee accepts with its own signature
(`accept`, 04-crypto.md §4.7, §5.2a): every person with an account had to open a second link and take part. The owner
decided otherwise (2026-11-23): whoever has an account is admitted by an admin of the org (or by the admin's agent,
decision 10a) – done, without the admitted person doing anything. Concept idea 30: "zero knowledge is a goal, but not
at the price of complicated user interactions"; key management stays invisible.

Two facts shape the design:

1. Without `accept` there is no signature of the admitted person in the log. Its public keys (`pub{kem,sig}`) would
   have to come from the hub – and a hub that may swap a public key can read everything wrapped to it. The keys need
   an anchor the hub cannot forge.
2. No such anchor existed for existing accounts. `register/finish` proves the code only over `user_id ‖ challenge`
   (ADR-043), not over the identity keys, and the hub does not keep that proof; the `genesis` of a private org is
   signed by the new identity alone. The only key that the hub cannot forge and that every client already verifies is
   the **platform root** (ADR-023): devices are bound to their keys this way (`device_certify`,
   `device_assign.platform_seq`, §5.9).

## Decision
1. **Identity anchors in the platform log** (04-crypto.md §4.8). `(user, pub)` is proven by the platform entry at a
   `seq` if it is
   - (a) a root-signed **`user_certify {user, pub}`** (new type; at most one valid per user, ended by the new
     `user_decertify`; a user ID is never also a device ID). The operator writes it with
     `agent-hub platform certify-user --user ID [--code XXXX-XXXX-XXXX] …`: the CLI reads the registered identity
     (`GET /v1/platform/users/{user}`, root-signed), prints its account code and how the account registered, and signs
     it. With `--code` – the code the person reads from its own settings, computed by its browser from its unlocked
     identity – the certification does not rest on the hub's word (`account_code_mismatch` otherwise). There is no
     "all" form. This is the path for every account that exists today and for accounts registered through an org
     invitation; or
   - (b) the account's own platform invitation `user_invite {invite_id, pk_inv}` plus the **identity proof**
     `id_sig = Ed25519_{sk_inv}(P‖"invite-identity\0"‖canonical({pub, user}))`, where `user` must be the **user ID
     derived from that invitation** (UUIDv7 with the milliseconds of the root-signed entry and the random bits
     `SHA-256(P‖"user-id\0"‖invite_id)[:10]`). The derivation binds a code to exactly one account, so the holder of
     another code cannot sign for someone else's account. The verifiers implement (b) now; the registration side (the
     hub allocating the derived ID at `register/begin` of a platform invitation, the browser sending `id_sig` with
     `register/finish`, the hub keeping it in `user_identity.id_sig/id_invite_seq`) follows separately – until then
     new accounts are certified like existing ones.
   The hub answers `GET /v1/users/{user}/identity`, `GET /v1/users/lookup?code=` and `GET /v1/me/invitees` with
   public keys, the platform seq, the proof and the account code; an unknown, disabled or unanchored account is the
   same `404 identity_unproven`. Clients verify every answer against the platform log they verified with the compiled
   root key and never take keys from the hub unverified.
2. **New membership-log entry `admit`** (admin only): body `subject, pub{kem,sig}, platform_seq, id_sig, by_platform_seq,
   by_id_sig, role, wgs` (proofs inside the body, `""` when unused, so the entry key set is always `{body, sig}`).
   Rule L5 against the state before the entry, platform state required: every `wgs` workgroup writable and not the
   default one (`log_rights`); then (`log_admit`) the subject is not an active principal (a former **user** with the
   same keys may be admitted again), the subject's identity is anchored at `platform_seq` with `id_sig`, and the
   **admitting admin's** keys (as in the state) are anchored at `by_platform_seq` with `by_id_sig`. Effect as `accept`
   of a user invitation with `role` and `wgs`; R is empty.
3. **No consent, no credentials.** A user that entered by `admit` has not consented (`consented` false until it joins
   by its own `accept`). The devices of such an org are no recipients of that user's credential key (W2 for `cred`,
   04-crypto.md §6.3), in hub, runner and web alike: being admitted never sends the user's GitHub token or Claude
   login to devices an unknown admin controls. Reading, writing and leaving work as for every member; sessions of the
   org use the org's own access (operator subscriptions, platform model access).
4. **Hub checks at append** (besides L1–L8): `pub` equals the subject's registered identity of an active user, the
   anchors are still valid (`log_admit`), the batch wraps the current org epoch and default-workgroup epoch for the
   subject (`log_admit`; other workgroup keys may follow by catch-up, §5.11). Audit `log.admit`; notification
   `org.admitted` to the subject (`ref` = the admitting admin; names are rendered by the client from the encrypted
   profiles).
5. **The admitted person's client anchors the org** like any other: its own principal carries its own keys, and L5
   proved the admitter's identity through the root-signed platform log, offline and for every later reader (second
   browser, agent tool). A hub cannot sign an `admit` in the name of a real, proven account and therefore cannot
   invent an org for a user. The agent tool's trust rule accepts an own `genesis`, `accept` or `admit`.
6. **Finding accounts without a directory.** By user ID (the agent, the operator), by the account code every user sees
   in its settings (`base32(SHA-256(P‖"account-code\0"‖user‖"\0"‖sig‖kem))[:12]`, `XXXX-XXXX-XXXX`; the admitting
   client recomputes it from the proven keys, so a typed code also checks the keys), and "people I invited" (accounts
   registered through the caller's own platform invitations). No search by name, no list of all accounts; every lookup
   is in the read log.
7. **Introduction without breakage.** An unknown log type makes an old verifier refuse the whole log (`log_type`):
   - runners announce `log.admit` in the header `Agent-Features` of the live-channel upgrade (a header, because an
     older hub refuses unknown query parameters but ignores unknown headers); the hub replaces `device.features` on
     every connection;
   - the hub refuses an `admit` batch with `409 peer_outdated` (`detail.devices`) while an active device of the org
     has not announced it, and a `device_assign` of such a device into an org whose log holds an `admit`;
   - the hub also refuses `user_certify`/`user_decertify` (`POST /v1/platform/log`, `409 peer_outdated`) while an
     active **platform** device has not announced it: an older runner verifies the whole platform log and would stop
     at the unknown type, missing every later decertification;
   - a device that is switched off and has not connected since the hub release counts as not announced: it blocks
     admissions into its orgs (and user certifications, for platform devices) until it connects once or is unassigned;
   - the web bundle ships with the hub; a tab still running an older bundle shows that org as blocked until it
     reloads. The new web marks a refusal for an unknown type as "a newer version is available – reload";
   - order: runner(s) → hub with web → agent tool → certification of the accounts → first `admit`. A hub rollback
     after the first `admit` is not supported (the older hub refuses its own log).
8. **Invitations stay** for people without an account (KOR-36 makes them one click).

## What the hub sees, what a malicious hub can do
The hub sees what it already sees for invitations (the `admit` entry: subject, public keys, role, accesses), the
certifications in the platform log, the announced device capabilities and who looked up which account. It never sees
a key. A malicious hub can withhold the admission or the wraps (denial), refuse lookups or call an account unproven.
It cannot make an admin wrap keys for a key pair of its choice: the admitting client accepts only keys anchored by the
root (a) or by the account's own invitation code (b). It cannot invent an org for a user (5), and an admission it
fakes from a real admin's account does not exist without that admin's signature.

Residual risks, stated plainly: the **certification of existing accounts** without `--code` rests on the hub's records
at that moment – a hub already compromised then could have a false identity certified; the act is visible in the
platform log and can be repeated with `--code`. The **holder of a platform invitation code** (its inviter) could, with
the hub, have the account of that invitation admitted under keys of its choice (b) – the trust the code already gives
at registration. **Consent:** the admitted person did not agree to join; it sees the org and a notification, can leave
at once (the rotation is owed by the remaining holders as for every leaver), shares no credentials (3), and the admitter
learns its display name only once it opens the org.

Privacy: the platform log is readable by every signed-in user (`GET /v1/platform/log`); its `user_certify` entries
therefore list the certified accounts with their public keys, from which anyone can compute their account codes. This
is no directory of names, but it is a list of admissible account IDs that bypasses the read-logged lookups; an admin
of any org could admit such an account, which then gets a notification and can leave at once without sharing anything
(3). Certifying only the accounts that should be admissible keeps the list short.

## Consequences
- Contracts: platform types `user_certify`/`user_decertify`, log type `admit`, labels `invite-identity\0`,
  `account-code\0`, `user-id\0`, error codes `log_admit`, `identity_unproven`, `peer_outdated`,
  `account_code_invalid`, audit actions, notification kind `org.admitted`, four endpoints, error detail `devices`;
  vectors for every rule in hub, runner and web (`contracts/changes/KOR-37.md`).
- migration 0013: `device.features`, `user_identity.id_sig/id_invite_seq`, notification kind.
- KOR-34 (admins read every workgroup) is orthogonal: an admitted admin inherits read like any admin; KOR-34 changes
  the same `cred_readers` function (explicit membership) and the two rules combine.

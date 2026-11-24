# Key model

Status: binding description of the state on `develop` (KOR-54, 2026-11-24), with the gaps that the cryptography
packages close. It answers [concept §5 point 5](KONZEPT.md#k-5-b5) ("key derivation, release and device distribution
are still to be defined technically"), [§13 point 1](KONZEPT.md#k-13-1), the open question of
[idea 30](KONZEPT.md#k-idee-30) ("inheritance of rights, key distribution and revocation need a concept") and the
"key model" of [§16 point 7](KONZEPT.md#k-16-b7) in one place.

**What this document is.** The formats, labels, flows and verification rules are in
[04-crypto.md](spec/04-crypto.md) and the ADRs it names; this document does not repeat them. It says, closed over
every principal: who holds which key, which scope a principal receives when, how each case of revocation and rotation
works, what stays with a principal that has read something, where plaintext lies, and what the hub sees. Each rule has
an ID `KM-NN` and names the shared test vectors (or, for a rule that only one component enforces, the tests) that pin
it in [§9](#9-rules-and-their-evidence). [§10](#10-concordance-with-the-concept) maps every place of the concept and
every rule of the client to this document; [§11](#11-gaps-and-the-requirements-on-the-follow-up-packages) gives the
gaps with the exact requirement on the package that closes them.

**Checked by** `tests/platform/test_key_model.py`: every rule names evidence that exists (a vector of
`contracts/vectors/` by file and name, a test file, or a gap with its package), every concept anchor of
[KONZEPT.md](KONZEPT.md) appears in the concordance, every concordance row points to a section of this document, and
every gap package has its requirement section. A rule that is added later must bring its evidence (rule E-19 of the
client: a gap is closed with a check that keeps it closed).

Terms: *scope key* = a 32-byte key with an epoch (`OK_e`, `WK_e`, `CK_e`; [04 §2](spec/04-crypto.md#2-key-inventory));
*wrap* = an HPKE seal of one scope epoch to one recipient's X25519 key, signed by the wrapper; *holder* of an epoch =
whoever can open it; *entitled* = the verified log makes a principal a reader of that scope now.

## 1. Principals

| Principal | Identified by | Own long-term keys | Acts through | Built |
|---|---|---|---|---|
| **Human user** | user ID (UUIDv7), registered identity `pub{kem,sig}`, passkeys | user identity (X25519 + Ed25519) in the identity bundle | browser sessions, its agent | yes |
| **Browser session** of a user | P-256 session key in a 12 h session certificate signed by a passkey assertion | none of its own: holds the user's unlocked identity in memory while the tab is open | the user's identity | yes |
| **Execution device** – org device (enrolled by an org invitation) or platform device (certified by the platform root) | device ID, `pub{kem,sig}` in the membership log (org device) or the platform log (platform device) | device identity (X25519 + Ed25519), runner storage key derived from it | the runner; the sessions it runs | yes |
| **Agent of the user** (main orchestrator, [idea 34](KONZEPT.md#k-idee-34), [ADR-056](adr/056-delegated-agent-access.md)) | agent ID, its own key pair (request signatures only) | agent key pair; **plus a copy of the user's identity** (sealed handover) | the user's identity; requests signed with the agent key | yes (local); on DFX hardware: [gap KOR-57](#kor-57--own-identity-of-the-agent-real-revocation-orchestrator-on-dfx-hardware) |
| **Platform session** (a Claude Code session, its subagents) | session ID, bearer session token inside the VM | **none** | the device that runs it (device-signed events, operations and reads) | yes |
| **Work, test and browser VM** ([E-08](KONZEPT.md#e-08), ideas [27](KONZEPT.md#k-idee-27), [28](KONZEPT.md#k-idee-28)) | VM ID | none (no scope key, no credential) | platform operations of its session | work VM yes; test/browser VM: KOR-43, KOR-44 |
| **Platform root** (operator) | compiled public key | Ed25519 platform root, offline | root-signed platform-log entries and operator requests | yes |
| **Hub** | its response key | Ed25519 response key (signs responses only) | – | yes |
| **Invitation holder** (transient) | `invite_id` | none: the 16-byte code derives `package_key` and `sk_inv` | proof of the code at registration, accept or enrollment | yes |
| **Support person** ([idea 29](KONZEPT.md#k-idee-29)) | – | – | – | [gap KOR-56](#kor-56--support-as-a-principal) |
| **Community member** ([E-20](KONZEPT.md#e-20)) | the user | the user identity; community key `CMK_e` | the user's identity | designed in KOR-26 (not on `develop`) |

A **subagent** is a platform session like its parent ([A1](KONZEPT.md#a-1)): same workgroup, same device, same rights,
no keys. A **human that instructs another user's session** ([idea 36](KONZEPT.md#k-idee-36)) acts with its own
identity (its prompt is an event signed by itself); the session reads with the rights of the task owner
([§8](#8-reading-across-workgroups)).

## 2. Keys per principal

| Key | Holder | At rest | Never at |
|---|---|---|---|
| PRF output `prf_i`, `KEK_i` | browser, per passkey and login | memory only | hub, device, agent |
| Recovery code, `KEK_rec` | the user (written down); browser at use | – | hub (only `recovery_id`) |
| User identity (private) | browser memory after unlock; the user's agent (copy) | hub: only sealed bundle slots (passkey, recovery) and the agent's sealed handover | device, VM |
| Browser session key | browser | IndexedDB, non-extractable | anywhere else |
| Device identity, storage key | the runner of that device | `keys/device.key` 0600; storage key derived, memory only | hub (public keys only), VM |
| Agent key | the agent tool on the user's computer | login keychain or 0600 file | hub (public keys only) |
| `OK_e` | org members; devices assigned to any workgroup of the org ([ADR-007](adr/007-devices-receive-org-key.md)) | wraps at the hub; runner storage-key encrypted | hub in clear, VM |
| `WK_e` | members of the workgroup; **every org admin** (inherited read, [ADR-058](adr/058-admins-read-every-workgroup.md)); devices assigned to the workgroup | as above | hub in clear, VM |
| `CK_e` (per user) | the user; devices assigned to a workgroup in which the user has **explicit** access, in orgs the user joined by its own entry ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)) | as above | hub in clear, VM; admins of other users |
| Derived keys `k_event`, `k_cred`, `dk_e` (and, with KOR-33, `k_ctx`, `pk_e`) | whoever holds the scope key | transient | hub |
| Platform root (private) | operator, offline | operator file | hub, devices, browsers (public key compiled in) |
| Hub response key | hub | hub environment | – |
| Model and GitHub credentials (values) | execution device while in use; guest memory of a running session (model credential only) | `CK`-encrypted records at the hub; never on disk in clear | hub, VM disk |
| Operator subscriptions and operator GitHub token ([ADR-052](adr/052-operator-subscriptions.md), [ADR-053](adr/053-operator-github-account.md)) | platform device only | 0600 files of the runner account | hub, org devices, browsers |

## 3. Scopes

| Scope | Key | Protects | Readers (entitlement) | Writers |
|---|---|---|---|---|
| Organization | `OK_e` | org profile, workgroup names, member and device profiles, org whiteboard | members; devices assigned to ≥ 1 workgroup | admins (member: its own profile) |
| Workgroup | `WK_e` | prompts, session output, tool and file events, terminal, task titles, workgroup and task whiteboards, operation parameters and results, workgroup settings, platform reads | explicit read/write members, members on the default workgroup, every admin (read), assigned devices | explicit `write` members; assigned devices for device kinds |
| Credentials | `CK_e` of one user | that user's credential records (GitHub, API key, Claude subscription, mailbox) | the owner; devices assigned to a workgroup with the owner's explicit access, in consented orgs | the owner; an eligible device acting on the owner's device request |
| Dedupe | `dk_e` from `WK_e` | operation fingerprints (opaque tags) | `WK_e` holders | assigned devices |
| Context objects (KOR-33, not on `develop`) | `k_ctx` from `WK_e`, per object | session context checkpoints, PR → task tags | `WK_e` holders (a restoring device must be assigned to the workgroup) | the authoring device |
| Community (KOR-26, not on `develop`) | `CMK_e` | community messages and display names | every user, by anonymous key request | every user |

There is no key per task or per session: a task's content is workgroup content (the hierarchy is only organization →
workgroup → task, [Grundprinzip](KONZEPT.md#k-gp)).

## 4. Who receives which scope, when

| Trigger (log entry or action) | Receives | Delivered by |
|---|---|---|
| Registration ([§5.1](spec/04-crypto.md)) | the user: `CK_1` (created once per user, here) | self-wrap in `register/finish` |
| Private org genesis | founder: `OK_1`, `WK_1` of the default workgroup | self-wraps in the genesis batch |
| `accept` of a user invitation | invitee: current `OK`, `WK` of the invited and the default workgroup | sealed package; later epochs by catch-up; own self-wraps |
| `accept` of an admin invitation | as above (the package carries no inherited keys) | the other workgroups by catch-up after the visible accept ([ADR-058](adr/058-admins-read-every-workgroup.md)) |
| `admit` (direct admission, [E-18](KONZEPT.md#e-18)) | subject: current `OK`, default `WK`, listed `WK`; **no** `CK` sharing to the org's devices | admitting batch (must wrap `OK` and default `WK`); rest by catch-up |
| `grant` | subject: current `WK` (older on request) | granting batch or catch-up |
| `role` → admin | new admin: every epoch of every workgroup it reads by inheritance | appointing batch, then catch-up of all epochs |
| `wg_create` | creator, every admin, granted members: `WK_1` | the creating batch (refused without these wraps) |
| `device_assign` (and `accept` of a device invitation) | device: `WK` of the workgroup, `OK`, `CK` of the assigning user; `CK` of every other user with explicit access there | assigning batch / package; catch-up by the users' own clients |
| Pairing of the user's agent | agent: the user's identity, hence everything the user holds | sealed handover (`r` + agent key); the agent opens the user's existing wraps |
| Passkey added / recovery | the new passkey slot of the same identity | browser upload after a PRF login |
| Platform session started | nothing: the device decrypts and gives the VM plaintext of its own workgroup only | runner |
| Community (KOR-26) | every user: `CMK_e` | anonymous key request served by any holder |

**Catch-up** ([04 §5.11](spec/04-crypto.md#511-catch-up-wrapping)) is what makes all of this work without anybody
online: every holder (browser on sync, device on reconcile) wraps for every entitled recipient that has no wrap yet,
as far as rules W1/W2 allow; the hub stores only what W3 allows in its own log state, and each recipient checks again.

## 5. Revocation and rotation, case by case

The general rule: losing the right to read a scope puts that scope into the rotation set
`R(before, after, subject)` ([04 §4.7](spec/04-crypto.md#47-membership-log)); the triggering entry, one `rotate` per
element of R and the wraps of the new epochs for every remaining reader go to the hub in **one** batch, which the hub
refuses otherwise. Rotation protects what is written **after** it; what a principal read before stays with it
([idea 30](KONZEPT.md#k-idee-30), last sentence) – the client says so where it matters.

| Case | Rotates | Who | What the revoked principal keeps |
|---|---|---|---|
| Member removed | `OK`; every `WK` it could read (incl. by inheritance) | the removing admin, same batch | everything read and every old epoch it held |
| Member leaves | same, as `pending` | remaining admins/writers in the background | same |
| `ungrant` | that `WK` (not if the member still reads it as admin) | admin, same batch | same, for that workgroup |
| Admin demoted (`role` → member) | every `WK` it read only by inheritance | the demoting admin; self-demotion: `pending` | same |
| Device unassigned from its last workgroup / removed | `WK` of that workgroup; `OK` when no workgroup is left | admin, same batch | plaintext and keys already on the device |
| Device loses eligibility for a user's `CK` | `CK` of that user (never in R) | the user's browser (re-encrypts the records) | old credential values |
| Platform device decertified | as unassignment in every org that uses it | next admin browser (`rotation_pending`); the hub refuses the device at once | as unassignment |
| Invitation revoked | nothing (no key was released: the package is deleted) | – | – |
| Agent revoked or replaced | **nothing** today: the agent key is refused, its sealed identity deleted | the user (or the agent itself) | **a copy of the user's identity**, which opens future wraps to the user from a database copy – [gap KOR-57](#kor-57--own-identity-of-the-agent-real-revocation-orchestrator-on-dfx-hardware) |
| Passkey removed | nothing: the slot is deleted; the identity stays | the user | – |
| Recovery code replaced | nothing: the slot is resealed | the user | – |
| All passkeys and the code lost | the identity is lost; an admin removes and re-invites (rotation as removal) | admin | – |
| Admitted without consent ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)) | – (no `CK` was shared with the org's devices) | – | – |
| Support release revoked | [gap KOR-56](#kor-56--support-as-a-principal) | | |

Old epochs are never deleted and never re-encrypted: a removed principal with a copy of the hub's ciphertexts can
open what it could open before, never what is written after the rotation.

## 6. Where plaintext lies

| Place | Plaintext of | Why |
|---|---|---|
| Browser (open tab) | everything the user can read, after unlock | the user reads it |
| The user's computer with the agent tool | everything the user can read (the agent holds the identity) | [E-04](KONZEPT.md#e-04), [A3](KONZEPT.md#a-3) |
| Execution device (runner) | every scope of every workgroup assigned to it, the org scope, the credentials of eligible users, the operator's tokens on platform devices; at rest under the storage key, in memory in clear | the device executes ([§5 point 4](KONZEPT.md#k-5-b4), [A7](KONZEPT.md#a-7)) |
| Session VM (guest) | its own workgroup's content that the runner hands over, the model credential of running sessions in memory | the session works on it |
| Work, test and browser VM | what the session copies into it (no keys, no platform credentials) | [E-08](KONZEPT.md#e-08), [E-09](KONZEPT.md#e-09) |
| Model provider, GitHub | prompts and code the session sends | inherent, the user's choice of access |
| Hub | nothing confidential ([§7](#7-what-the-hub-sees)) | [§5 point 2](KONZEPT.md#k-5-b2) |

Assigning a device to a workgroup (or, with KOR-41, to the whole organization) therefore gives **that device** the
keys: the assignment steers computing capacity for people ([idea 35](KONZEPT.md#k-idee-35)), but where a task runs,
its content lies in clear ([A7](KONZEPT.md#a-7)). This is stated in the assignment dialog and the security page.

## 7. What the hub sees

The exhaustive list is [04 §7](spec/04-crypto.md#7-what-the-hub-sees-in-plaintext-exhaustive). For keys it means: the
hub stores public keys, the two logs, wraps and signatures, the sealed bundle slots and the agent's sealed handover;
it never holds or derives a scope key, a private key, a PRF output, a code or a credential value (enforced by the
zero-knowledge canary scan, `e2e/platform/specs/e2e-09-zero-knowledge.spec.ts`). It sees the metadata of rights and
key distribution (who is entitled to which epoch, who wrapped for whom, when a rotation happened) and the counters of
[A4](KONZEPT.md#a-4). An active hub can withhold data and serve an old log (detected per client, KM-08), and – today –
present an invented organization to a platform device ([gap KOR-55](#kor-55--trust-anchor-of-the-runner)).

## 8. Reading across workgroups

Today ([ADR-024](adr/024-session-read-scope.md)) a session reads with the rights of its task owner **intersected
with its own workgroup**; other workgroups are visible by name only. The device holds exactly the keys of its assigned
workgroups (KM-14), so this is also what it *can* read. KOR-45 lifts the intersection with the workgroup. The key model
requires of it:

1. **Rights:** a session reads what both the task owner **and** the human whose instruction it is working on may read
   (owner ∩ instructing human; without another human's instruction the owner alone), evaluated at call time on the
   verified log, never more than either. A workgroup outside that set stays `not_found`, its name included where it
   is not already visible to the reader as org metadata.
2. **Keys only by existing entitlement:** the device may read another workgroup only with a `WK` it already holds
   because it is **assigned** to that workgroup (directly, or through an organization-wide assignment of KOR-41).
   KOR-45 must not introduce a new wrap rule, a new scope or a wrap from a human to a device that is not entitled by
   the log; it uses only the existing wraps (its own scope boundary: "the log verifiers are only used through existing
   wraps"). A workgroup whose key the device does not hold is answered as unreadable with a clear reason and the
   action that would make it readable (assign the device, E-05), not silently empty.
3. **No extra step:** nothing for the human to confirm when a task is created (E-11). Hence, if a device should read
   more workgroups than it runs, that is an assignment decision of an admin, visible in the log and on the security
   page – not an implicit side effect of a task.
4. **What lies where:** content of another workgroup that a session read is plaintext in that session's VM and in its
   context checkpoints (KOR-33), which are sealed under the **session's** workgroup key. So reading across workgroups
   copies content into the reading workgroup's scope: every reader of the reading workgroup can then see it. KOR-45
   must therefore (a) limit what the read tools hand to the session to the intersection of point 1 **and** to what
   every current reader of the session's workgroup may read, or (b) state it plainly in the harness and the security
   page and record every such read with source and target workgroup in the read log. The model prefers (a); (b) only
   with a decision recorded in its ADR.
5. **Audit:** every cross-workgroup read writes a read-log row with source workgroup, target workgroup, owner,
   instructing human and the reading session (§8 point 6 of the concept).
6. **Check:** vectors for the intersection function (owner, instructing human, admin inheritance, a removed
   instructing human) in hub and runner, and an attack test "member B instructs A's session to read a workgroup only
   A reads" → `not_found`.

## 9. Rules and their evidence

Evidence: `file.json#name` is a vector of `contracts/vectors/` (a negative vector by its `name`, a positive case by its
`name`, or a top-level entry); every implementation – the hub reference `agent_hub`, the runner copy
`agent_runner.crypto` and the web core `web/src/core` – runs every vector of the log and crypto files. `test:` names a
test file of a rule that only one component enforces. `gap KOR-NN` marks a rule that is not built yet.

| Rule | Statement | Enforced by | Evidence |
|---|---|---|---|
| KM-01 | A user identity is drawn by the browser at registration; the hub stores only public keys and bundle slots sealed under a KEK from one passkey's PRF output (32 bytes) or from the 16-byte recovery code, bound to user and slot. | web; hub stores | `bundle.json#other_slot`; `bundle.json#other_user`; `kdf.json#prf_31_bytes`; `kdf.json#recovery_code_15_bytes` |
| KM-02 | Only a passkey that yields PRF is kept: a registration (or an added passkey) without PRF output is withdrawn ([E-12](KONZEPT.md#e-12)); no recovery-code step at registration ([E-13](KONZEPT.md#e-13)). | web, hub | test:`tests/platform/hub/auth/test_register.py`; test:`web/e2e/passkeys.spec.ts` |
| KM-03 | The identity bound to a code is proven, not asserted: registration proves the invitation code (`inv_sig`), an identity anchor is a root `user_certify` or an `id_sig` of the platform invitation of exactly that account. | hub, all verifiers | `invite.json#register_other_code`; `identity.json#id_sig`; `log.json#admit_proof_for_other_account` |
| KM-04 | Every scope key is random (never derived from another), has an epoch, and reaches a recipient only as an HPKE wrap whose info binds scope, scope ID, epoch and recipient, signed by the wrapper. | all | `wrap.json#other_recipient_id`; `wrap.json#other_epoch`; `wrap.json#other_scope`; `wrap.json#signature_other_wrapper` |
| KM-05 | A wrap is accepted only for a recipient the verified log entitles now (W2) and from the recipient itself (W1) or a holder of that epoch; the hub stores it only under its own log state (W3), recipients check again. | all | `log-wraps.json#recipient_not_entitled`; `log-wraps.json#hub_forges_wrap`; `log-wraps.json#epoch_in_future`; `log-wraps.json#W2_former_holder_of_old_epoch` |
| KM-06 | Only principals of the verified logs receive wraps: no key, account or person outside the log (an agent key, a support person without a release) can be made a recipient by the hub. | all | `log-wraps.json#principal_outside_log_is_no_recipient`; `log-wraps.json#hub_forges_wrap` |
| KM-07 | Membership is decided only by the signed, hash-chained org log, verified against the pinned founder key (L1–L8); the hub cannot inject a member, swap keys or forge an entry. | all | `log.json#L4_hub_injects_member`; `log.json#accept_swapped_keys`; `log.json#L3_founder_not_pinned`; `log.json#admit_forged_by_hub` |
| KM-08 | A client stores the verified head and refuses a log that does not extend it (L9); an event beyond the head forces a refresh, a missing head is a fork (L10). | all | `log.json#L9_shorter`; `log.json#L9_fork`; `log.json#L10_lh_beyond_head` |
| KM-09 | Platform devices, platform invitations, user certifications and platform access exist only as root-signed platform-log entries; the hub cannot extend that log. | all | `platform-log.json#P4_hub_certifies_device`; `platform-log.json#P3_root_not_pinned`; `log.json#assign_without_platform_access` |
| KM-10 | Org key: members and devices assigned to at least one workgroup of the org; a device without a workgroup is no reader of the org key ([ADR-007](adr/007-devices-receive-org-key.md)). | all | `log-wraps.json#W2_org_older_epoch_for_device`; `log-wraps.json#org_key_for_device_without_workgroup` |
| KM-11 | Workgroup key: explicit members, members on the default workgroup, every active admin by inheritance (read only, all epochs, archived workgroups too), assigned devices. | all | `log-wraps.json#W2_admin_by_inheritance_for_member`; `log-wraps.json#W2_for_admin_on_archived_workgroup`; `log-wraps.json#admin_catch_up` |
| KM-12 | Writing a workgroup needs explicit `write` at the author's `lh` and (hub) at receipt; the inherited read never writes. | all | `log-events.json#E5_reader_prompts`; `log-events.json#hub_E5_at_receipt`; `log-events.json#E5_archived_workgroup` |
| KM-13 | Credential key: the owner and devices assigned to a workgroup with the owner's **explicit** access; never through an admin's inherited read. | all | `log-wraps.json#cred_owner_for_device`; `log-wraps.json#cred_admin_inherited_read_not_eligible`; `log-wraps.json#cred_user_not_owner` |
| KM-14 | A device holds keys only of what it is assigned to: never the `WK` of another workgroup; a decertified platform device is no recipient. | all | `log-wraps.json#recipient_not_entitled`; `log-wraps.json#recipient_decertified`; `log.json#assign_decertified_device` |
| KM-15 | An organization that admitted a user without the user's consent ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)) gets none of the user's credentials: its devices are no `CK` recipients. | all | `log.json#cred_device_of_org_without_consent` |
| KM-16 | A direct admission names two identity anchors in the platform log (the subject's and the admitting admin's) and carries the wraps of the current org and default-workgroup epochs. | all | `log.json#admit_hub_swaps_keys`; `log.json#admit_admitter_unproven`; `log.json#admit_batch_without_wraps` |
| KM-17 | Losing the right to read a scope rotates it in the same atomic batch (R); leaving and self-demotion leave R `pending` for the others; the hub refuses any other batch. | all | `log-rotation.json#rotate_missing`; `log-rotation.json#rotate_extra`; `log-rotation.json#admin_demoted_without_rotation`; `log-rotation.json#leave_without_rotation` |
| KM-18 | New epochs are wrapped for every remaining reader in the same batch, never for the removed principal. | all | `log-rotation.json#wrap_missing_for_remaining_recipient`; `log-rotation.json#wrap_for_removed_member`; `log-rotation.json#admin_demoted_wrap_for_demoted` |
| KM-19 | A new workgroup's first epoch is wrapped in the creating batch for every reader, every admin included. | all | `log-rotation.json#wg_create_without_wraps`; `log-rotation.json#wg_create_without_admin_wrap` |
| KM-20 | `CK` is never in R; a device losing eligibility makes the owner's browser rotate `CK` and re-encrypt the records. | web | `log-rotation.json#main_15_device_unassign`; test:`web/tests/core/jobs/credential-rewrap.test.ts` |
| KM-21 | A removed principal authors nothing (entries, events, new wraps); the previous epoch is accepted 600 s after a rotation only from authors entitled to the new one. | all | `log.json#L8_removed_author`; `log-events.json#author_removed_at_lh`; `log-wraps.json#removed_wrapper_at_hub`; `log-events.json#hub_E6_grace_over` |
| KM-22 | Every event is encrypted under a per-event key from the scope key, AAD = canonical header, signed by its author over header and raw ciphertext; kinds are bound to scope and author kind. | all | `event.json#header_changed_after_signing`; `event.json#other_author_key`; `log-events.json#E3_prompt_by_device`; `log-events.json#E3_output_by_user` |
| KM-23 | A platform session has no key: what it produces is sealed and signed by the device that runs it, only for a session on that device. | all; runner | `log-events.json#hub_E7_session_on_other_device`; `log-events.json#E7_reader_other_session_device`; `log-events.json#E5_device_not_assigned` |
| KM-24 | Credential records are sealed under `k_cred` from the owner's `CK` and signed by the owner or an eligible device acting on the owner's request; device requests are sealed to one device and signed by the user. | all | `credential.json#other_ck`; `credential.json#version_bumped`; `request.json#signed_by_device`; `request.json#other_device_key` |
| KM-25 | The runner keeps scope keys and outbox content only under its storage key, derived from the device identity. | runner | `kdf.json#storage_key`; test:`tests/platform/runner/crypto/test_storage.py` |
| KM-26 | Invitation code: one use, expiry, revocation; the sealed package opens only with the code and carries `OK` and the keys of the invited and default workgroups – an admin invitation no inherited keys. | all; web | `invite.json#package_other_code`; `log.json#L7_accept_twice`; `log.json#invite_revoked`; `log.json#invite_expired`; test:`web/tests/core/org/invites.test.ts` |
| KM-27 | Org and platform device identities are bound to their invitation by `enroll_sig` and `inv_sig` over the enrollment text. | all | `enroll.json#other_invite_key`; `enroll.json#enroll_sig_missing`; `platform-log.json#certify_swapped_key` |
| KM-28 | The user's agent receives the user's identity only sealed to its own key **and** bound to a handover secret the hub never sees; its label is sealed to the user. | all (agent tool, hub, web) | `agent.json#identity_without_secret`; `agent.json#identity_other_agent`; `agent.json#label_to_agent_key` |
| KM-29 | Exactly one active main orchestrator per user; a new pairing needs a passkey gesture and replaces the previous one; revocation refuses its key at once and deletes its sealed identity. | hub | test:`tests/platform/hub/agents/test_pairing.py`; test:`tests/platform/hub/agents/test_revoke.py` |
| KM-30 | Revoking or replacing the agent rotates every scope it could read, so its copy opens nothing written afterwards. | – | gap KOR-57 |
| KM-31 | A platform device trusts an org (founder, platform access) only on evidence it verifies itself against the root key. | runner | gap KOR-55 |
| KM-32 | Write access to the default workgroup can be reduced to read by an admin (a rotation like every demotion); new members still start with write. | – | gap KOR-55 |
| KM-33 | A support person reads or writes a workgroup only through an explicit, revocable release in the log; revocation rotates. | – | gap KOR-56 |
| KM-34 | A session reads across workgroups only within owner ∩ instructing human and only with keys its device already holds by assignment (§8). | – | gap KOR-45 |
| KM-35 | Credentials owned by an organization (a company identity) are readable by the org's devices and the admins who manage them, never by every member. | – | gap KOR-47 |
| KM-36 | The hub never holds a scope key, private key, PRF output, code or credential value; a full flow leaves no canary in its database or logs. | hub | test:`e2e/platform/specs/e2e-09-zero-knowledge.spec.ts`; test:`tests/platform/hub/requests/test_devices_and_zero_knowledge.py` |
| KM-37 | VMs (session, work, test, browser) hold no scope key, no device key and no platform credential at rest. | runner, guest | test:`tests/platform/atk/test_atk_36_tokens.py`; test:`e2e/platform/specs/e2e-23-execution-ban.spec.ts` |

## 10. Concordance with the concept

Every section and idea of [part A](KONZEPT.md#teil-a--das-konzept), every rule of part B and every interpretation of
part C, with its bearing on the key model. Status: **carried** (the key model supports it as built), **carried
(pending KOR-NN)** (designed in a package not yet on `develop`), **gap → KOR-NN** (the named package must close it, §11),
**none** (no bearing on keys).

| Concept | Bearing on the key model | Status | Section |
|---|---|---|---|
| [Grundprinzip](KONZEPT.md#k-gp) | Encrypted platform; hierarchy organization → workgroup → task gives the scopes; no task key. | carried | [§3](#3-scopes) |
| [§1](KONZEPT.md#k-1) | Users and devices only by invitation code; device keys "similar to SSH". | carried | [§4](#4-who-receives-which-scope-when), KM-26, KM-27 |
| [§2](KONZEPT.md#k-2) | Private org at the first login: genesis gives the founder `OK_1`, `WK_1`. | carried | [§4](#4-who-receives-which-scope-when) |
| [§3](KONZEPT.md#k-3) | DFX or own devices; several VMs and devices per workgroup: every device of the workgroup is a `WK` reader. | carried | [§6](#6-where-plaintext-lies), KM-14 |
| [§4](KONZEPT.md#k-4) | Own Claude or API credentials under `CK`; DFX access is the operator's token on platform devices. | carried | [§2](#2-keys-per-principal), KM-24 |
| [§5](KONZEPT.md#k-5) | Encryption and trust boundaries: the whole document; points 1–5 in the rows below. | carried | this document |
| [§5 point 1](KONZEPT.md#k-5-b1) | Client-side encryption of prompts, answers, whiteboards, credentials. | carried | KM-22, KM-24 |
| [§5 point 2](KONZEPT.md#k-5-b2) | The hub sees no confidential plaintext. | carried | [§7](#7-what-the-hub-sees), KM-36 |
| [§5 point 3](KONZEPT.md#k-5-b3) | Decryption only by authorised users and devices; a hub-invented org on a platform device remains. | gap → KOR-55 | KM-05, KM-31 |
| [§5 point 4](KONZEPT.md#k-5-b4) | The chosen execution device sees plaintext. | carried | [§6](#6-where-plaintext-lies) |
| [§5 point 5](KONZEPT.md#k-5-b5) | Derivation, release and device distribution defined. | carried | this document |
| [§6](KONZEPT.md#k-6) | Events encrypted locally, synced encrypted; counters in clear ([A4](KONZEPT.md#a-4)). | carried | KM-22, [§7](#7-what-the-hub-sees) |
| [§7](KONZEPT.md#k-7) | Monitoring: metrics are metadata, no keys. | none | [§7](#7-what-the-hub-sees) |
| [§8 point 2](KONZEPT.md#k-8-b2) | Member = read (org scope), admin = write. | carried | [§3](#3-scopes) |
| [§8 point 3](KONZEPT.md#k-8-b3) | Default workgroup write "by default": today not reducible. | gap → KOR-55 | KM-32 |
| [§8 point 4](KONZEPT.md#k-8-b4) | Rights enforced in the backend and by every reader. | carried | KM-07, KM-12 |
| [§8 point 5](KONZEPT.md#k-8-b5) | Sessions read within their rights, no blanket view of other workgroups. | gap → KOR-45 | [§8](#8-reading-across-workgroups), KM-34 |
| [§8 point 6](KONZEPT.md#k-8-b6) | Reads logged; reads of plaintext happen on the device and are logged there. | carried | [§8](#8-reading-across-workgroups) |
| [§8](KONZEPT.md#k-8) | Persons at org and workgroup level: `accept`/`admit` and `grant` entitle. | carried | [§4](#4-who-receives-which-scope-when) |
| [§9](KONZEPT.md#k-9) | GitHub token under `CK`, used only on the device. | carried | KM-13, KM-24 |
| [§10](KONZEPT.md#k-10) | Sessions execute nothing and hold no keys. | carried | KM-23, KM-37 |
| [§11](KONZEPT.md#k-11) | Operation parameters encrypted, dedupe tags from `dk_e`. | carried | [§3](#3-scopes) |
| [§12](KONZEPT.md#k-12) | Whiteboards per level: org whiteboard under `OK`, workgroup and task whiteboards under `WK`; pings are content-free. | carried | [§3](#3-scopes) |
| [§13 point 1](KONZEPT.md#k-13-1) | Passkey-based key management and distribution of decryption rights. | carried | this document |
| [§13](KONZEPT.md#k-13) | Other open decisions: no key bearing. | none | – |
| [§14](KONZEPT.md#k-14) | Repositories. | none | – |
| [§15](KONZEPT.md#k-15) | The extensions 26–47: one row per idea below. | none | – |
| [§16 point 7](KONZEPT.md#k-16-b7) | "Synchronisation and key model" still to be checked: this document; cross-device resume needs the `WK` on the second device. | carried (pending KOR-33) | [§11](#11-gaps-and-the-requirements-on-the-follow-up-packages) |
| [§16](KONZEPT.md#k-16) | Other final decisions. | none | – |
| [Idea 26](KONZEPT.md#k-idee-26) | Subagents are platform sessions: no keys, rights of the parent ([A1](KONZEPT.md#a-1)). | carried | [§1](#1-principals) |
| [Idea 27](KONZEPT.md#k-idee-27) | Test VMs hold no keys; results stored as workgroup content. | carried | KM-37 |
| [Idea 28](KONZEPT.md#k-idee-28) | Browser VMs hold no keys. | carried | KM-37 |
| [Idea 29](KONZEPT.md#k-idee-29) | Support release with cryptographic rights and revocation. | gap → KOR-56 | KM-33 |
| [Idea 30](KONZEPT.md#k-idee-30) | Encryption by default, passkey the only visible release, admins read every workgroup, inheritance, distribution and revocation, read plaintext stays. | carried | [§4](#4-who-receives-which-scope-when), [§5](#5-revocation-and-rotation-case-by-case), KM-11 |
| [Idea 31](KONZEPT.md#k-idee-31) | Humans, main agents and subagents distinguishable: humans and devices are principals, agents act as their user (own identity: KOR-57), sessions have none. | gap → KOR-57 | [§1](#1-principals) |
| [Idea 32](KONZEPT.md#k-idee-32) | Autonomous-first: catch-up needs no human online. | carried | [§4](#4-who-receives-which-scope-when) |
| [Idea 33](KONZEPT.md#k-idee-33) | Analysis session reads authorised logs: a platform session, same rules. | carried | KM-23 |
| [Idea 34](KONZEPT.md#k-idee-34) | One delegated orchestrator with own identity, passkey approval, audit, revocation; local or on DFX hardware. | gap → KOR-57 | KM-28, KM-29, KM-30 |
| [Idea 35](KONZEPT.md#k-idee-35) | Own hardware for a workgroup or the whole org: the device holds the keys of what it serves. | carried (pending KOR-41) | [§6](#6-where-plaintext-lies) |
| [Idea 36](KONZEPT.md#k-idee-36) | Every writer can continue a session: writers hold `WK`, their prompts are signed by themselves. | carried | KM-12, KM-22 |
| [Idea 37](KONZEPT.md#k-idee-37) | GitHub identities of the org: company credentials need an org credential scope. | gap → KOR-47 | KM-35 |
| [Idea 38](KONZEPT.md#k-idee-38) | Device choice: only devices assigned to the workgroup hold its key. | carried | KM-14 |
| [Idea 39](KONZEPT.md#k-idee-39) | Cleanup: keys stay on the device; the VM held none. | carried | KM-37 |
| [Idea 40](KONZEPT.md#k-idee-40) | Resume on another VM: context checkpoints under a key from `WK`. | carried (pending KOR-33) | [§3](#3-scopes) |
| [Idea 41](KONZEPT.md#k-idee-41) | PR conflict fix by resume; PR → task tag from `WK`. | carried (pending KOR-33) | [§3](#3-scopes) |
| [Idea 42](KONZEPT.md#k-idee-42) | Roles restrict operations, not keys. | none | – |
| [Idea 43](KONZEPT.md#k-idee-43) | Fresh context per task; "authorised platform data can still be retrieved" across workgroups. | gap → KOR-45 | [§8](#8-reading-across-workgroups) |
| [Idea 44](KONZEPT.md#k-idee-44) | Resume after device failure needs a second device that holds the workgroup's key. | carried (pending KOR-33) | [§11](#11-gaps-and-the-requirements-on-the-follow-up-packages) |
| [Idea 45](KONZEPT.md#k-idee-45) | Free with own resources. | none | – |
| [Idea 46](KONZEPT.md#k-idee-46) | Usage counters in clear ([A4](KONZEPT.md#a-4)). | none | [§7](#7-what-the-hub-sees) |
| [Idea 47](KONZEPT.md#k-idee-47) | Prepaid credit. | none | – |
| [E-01](KONZEPT.md#e-01) | The full concept is the basis of this concordance. | carried | this section |
| [E-02](KONZEPT.md#e-02) | No upper limit: catch-up and wraps scale with principals, no fixed count. | carried | [§4](#4-who-receives-which-scope-when) |
| [E-03](KONZEPT.md#e-03) | The user creates only a passkey; the agent sets up everything with the user's identity. | carried | KM-28 |
| [E-04](KONZEPT.md#e-04) | The agent can do everything the user can: it holds the user's identity. | carried | KM-28, [§6](#6-where-plaintext-lies) |
| [E-05](KONZEPT.md#e-05) | A missing key is shown as "being unlocked" and delivered by catch-up, not explained. | carried | [§4](#4-who-receives-which-scope-when) |
| [E-06](KONZEPT.md#e-06) | Operator GitHub account on platform devices, outside the scope keys. | carried | [§2](#2-keys-per-principal) |
| [E-07](KONZEPT.md#e-07) | Operator subscriptions on platform devices, outside the scope keys. | carried | [§2](#2-keys-per-principal) |
| [E-08](KONZEPT.md#e-08) | Work VM without credentials and keys. | carried | KM-37 |
| [E-09](KONZEPT.md#e-09) | Nothing monitored in the work VM; it holds no platform key. | carried | [§6](#6-where-plaintext-lies) |
| [E-10](KONZEPT.md#e-10) | Egress of work VMs. | none | – |
| [E-11](KONZEPT.md#e-11) | No release as precondition: key distribution needs no confirmation (catch-up); KOR-45 adds no step. | carried | [§8](#8-reading-across-workgroups) |
| [E-12](KONZEPT.md#e-12) | Only PRF passkeys. | carried | KM-02 |
| [E-13](KONZEPT.md#e-13) | No recovery-code step; the unset recovery slot. | carried | KM-02 |
| [E-14](KONZEPT.md#e-14) | Credit set by the operator. | none | – |
| [E-15](KONZEPT.md#e-15) | Prices. | none | – |
| [E-16](KONZEPT.md#e-16) | USD. | none | – |
| [E-17](KONZEPT.md#e-17) | CHF equivalent. | none | – |
| [E-18](KONZEPT.md#e-18) | Direct admission with identity anchors, no credential sharing without consent. | carried | KM-15, KM-16 |
| [E-19](KONZEPT.md#e-19) | Every rule here names its check; new vectors close found gaps. | carried | [§9](#9-rules-and-their-evidence) |
| [E-20](KONZEPT.md#e-20) | Community key for all users. | carried (pending KOR-26) | [§3](#3-scopes) |
| [E-21](KONZEPT.md#e-21) | Task overview. | none | – |
| [E-22](KONZEPT.md#e-22) | One repository: one set of vectors for three verifiers. | carried | [§9](#9-rules-and-their-evidence) |
| [E-23](KONZEPT.md#e-23) | Concept file. | none | – |
| [E-24](KONZEPT.md#e-24) | No access layer in front: the platform protects itself (passkey, signed requests, keys). | carried | KM-07, KM-36 |
| [A1](KONZEPT.md#a-1) | Subagents have the parent's rights and no keys. | carried | [§1](#1-principals) |
| [A2](KONZEPT.md#a-2) | Conflict fix by merge commit. | none | – |
| [A3](KONZEPT.md#a-3) | The agent holds the user's identity; own identity with rotation later. | gap → KOR-57 | KM-28, KM-30 |
| [A4](KONZEPT.md#a-4) | Counters in clear at the hub. | carried | [§7](#7-what-the-hub-sees) |
| [A5](KONZEPT.md#a-5) | Limits as settings. | none | – |
| [A6](KONZEPT.md#a-6) | Test and browser VMs as platform operations, no keys inside. | carried | KM-37 |
| [A7](KONZEPT.md#a-7) | Content of a task on a DFX device lies there in clear; support is different. | carried | [§6](#6-where-plaintext-lies) |

## 11. Gaps and the requirements on the follow-up packages

Each requirement is binding for the named package; the package replaces the `gap` evidence of its rule in §9 with
vectors (in all three verifiers) or tests and sets the concordance row to "carried".

### KOR-55 – Trust anchor of the runner

1. **Anchor for every org a platform device serves** (KM-31): the runner accepts an org copy only if its platform
   access is proven by a root-signed entry it verifies itself – for a private org a root-signed `user_invite` whose
   invited user ID (or a `user_certify` of the founder's keys) equals the genesis founder, for a team org an
   `org_platform_access` entry (or the root-signed switches and lists of KOR-49). `genesis.private` alone, which only
   the hub binds to an invitation, is no longer enough. The founder key is bound to that anchor instead of "first use"
   ([ADR-040](adr/040-platform-device-founder-pinning.md) is superseded).
2. **Vectors** (all three verifiers): an invented private org (genesis `private:true` without root anchor) refused;
   a swapped founder refused; a valid anchored private org and team org accepted; an old head refused (KM-08 already
   holds; add the platform-device variant).
3. **Default workgroup reducible** (KM-32): a log entry (or an extended `grant` on the default workgroup) sets a member
   to `read` there; R then contains the default workgroup's `WK` (rotation like every demotion); the default for new
   members stays `write`. Vectors for the entry, R and the reader rule E5.
4. **Hub key pinning**: the platform device pins the hub response key from `/v1/config` on first use; the anchor of
   point 1 makes a swapped hub key detectable only by its effects. Either anchor the hub key in the platform log or
   document the residual risk in [04 §9](spec/04-crypto.md#9-threat-model).

### KOR-56 – Support as a principal

1. **Principal and keys** (KM-33): a support person is a user identity (passkey, PRF) with a root-signed platform
   role `support` (the platform log, not a hub table: the runner and every browser must verify it). Wrap **per
   person**, not to a shared support key: a shared key cannot be revoked for one person without a new key for all
   releases, and it hides who read.
2. **Release**: a new membership-log entry `support_grant {subject, wg, access read|write}` by an admin or a writer of
   the workgroup (decide in the ADR; the model allows both because both hold the key), whose subject must hold the
   valid platform role at the entry (vector: a subject without the role is refused). It makes the person a reader
   (and with `write` a writer) of that workgroup only: never `OK` (the org scope carries names of all workgroups and
   members) – the release needs the org-level data it shows (workgroup name) in its own encrypted body – and never
   `CK`. KM-06 must keep holding: no wrap for support without this entry.
3. **Revocation**: `support_revoke` (and the end of the platform role, `role_revoke`) puts that `WK` into R; same
   atomic batch; a role revocation makes the hub mark the affected orgs `rotation_pending` like a decertification.
   Personnel change: revoking one person's role rotates only the workgroups that person had; no re-wrap of other
   releases.
4. **Vectors**: grant, revoke, R with support, W2 for a support recipient, E5 for support writes, the forged entry by
   the hub, a grant to a non-support user, and a wrap for support without a release.

### KOR-57 – Own identity of the agent, real revocation, orchestrator on DFX hardware

1. **Sub-principal** (KM-30): the agent gets its own identity as a sub-principal of its user in every org log the user
   is in, appended with the user's signature (passkey gesture at pairing); it is entitled to exactly what its user is
   entitled to (`OK`, `WK`, `CK`; inheritance included) and to nothing else; it never holds the user's bundle. The
   rule "the active agent of U receives what U receives" must be one function in all three verifiers, used by W2,
   catch-up, R and the batch rule "every remaining reader has a wrap".
2. **"Exactly one"** in the logs: at most one active sub-principal per user per org; a new one replaces the old one in
   one batch, which carries R for the old one.
3. **Revocation**: removal of the sub-principal puts every scope it could read into R (the user's org scopes, every
   workgroup, `CK` of the user – here `CK` *is* rotated by the user's browser in the same flow); the existing
   `KM-06` vector `principal_outside_log_is_no_recipient` changes from "agent is no recipient" to "a revoked agent is
   no recipient".
4. **Migration** of paired agents without user action: a new pairing with the agent's own keys, the old sealed
   identity deleted; until the user rotates nothing the old agent copy keeps its old reach (say so).
5. **Identity rotation of the user** (lost device with an unlocked identity, leaked bundle): in the ADR evaluate a
   `rekey` entry (new `pub` signed by the old and the new key, R = every scope of the user); recommended as part of
   this package, because KM-30 alone does not help when the user's own identity leaked.
6. **On DFX hardware**: the key pair is drawn in the session VM ([11 §11](spec/11-agent-access.md)); the VM then holds
   a private key – the rule KM-37 gets the stated exception "the orchestrator session's own agent key", and the
   device that runs it sees the identity's reach (as a local computer does).
7. **Vectors**: sub-principal entry, forged by the hub, entitlement inheritance, rotation on revocation, replacement.

### KOR-45 – Reading across workgroups

The requirements of [§8](#8-reading-across-workgroups) points 1–6 (KM-34).

### KOR-47 – Credentials of the organization

1. **Scope** (KM-35): a company credential (e.g. a GitHub account of the organization) cannot be sealed under `OK`
   (every member holds `OK`, so every member's browser could read the token) and not under one user's `CK` (it must
   survive that user). Introduce an org credential scope (`ocred`, key `OCK_e`) whose readers are the org's admins
   (they manage it) and the devices assigned to any workgroup of the org; rotation when an admin is demoted or
   removed and when a device leaves the org's last workgroup. Alternatively: device-only readers with admins writing
   by sealing to the devices (no admin can read it back) – decide in the ADR.
2. **Vectors** for readers, W2, R and the record format.

### Further requirements found by the comparison

- **KOR-41 (organization-wide device assignment, designed on its branch):** an org-wide device is a reader of every
  `WK` incl. later workgroups and of the `CK` of every member with explicit access somewhere; `wg_create` wraps `WK_1`
  for it; removing the org-wide assignment puts every `WK` it read and `OK` into R. Vectors for these four points.
  The design on the branch meets them; the dialog must say that the device can read every workgroup.
- **KOR-33 (context checkpoints) and KOR-58 (device failure):** a restoring device must hold the session's `WK`; a
  second device that should take over must be assigned to the workgroup **before** the failure (or wait for catch-up
  from an online holder – the failed device may be the only one). KOR-58 must state which, and the scheduler must not
  place a resumed session on a device without the key.
- **KOR-26 (community):** when merged, add the rules (anonymous request, key check, per-user rejection) to §9 with its
  vectors and remove "pending" in §3 and §10.
- **PRF on real devices:** the device check of [manual-checks/prf-devices.md](manual-checks/prf-devices.md) is open;
  until it is done, KM-02 is verified with virtual authenticators only.

## 12. Order of the cryptography packages

The comparison found two requirements that touch the order: KOR-47 needs a new key scope (it depends on this model and
should follow KOR-55, whose verifier files it shares), and KOR-45 must not add a wrap rule (it can run in parallel
with KOR-55/56/57 as long as it stays within §8). Recommended order: KOR-54 → KOR-55 → KOR-57 → KOR-56 → KOR-47 (key
part), with KOR-45 in parallel after KOR-54 and KOR-41 before KOR-45 (organization-wide assignment is the way to give
a device more keys).

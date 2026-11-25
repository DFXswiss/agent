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
gaps with the exact requirement on the package that closes them and the decisions this comparison leaves open.

**Checked by** `tests/platform/test_key_model.py`: every rule names evidence that exists (a vector of
`contracts/vectors/` by file and name, a test by file and test name, or a gap with its package), every concept anchor of
[KONZEPT.md](KONZEPT.md) – every section, idea, rule and interpretation and the key-bearing points of §1, §3, §5, §8,
§13 and §16 – appears in the concordance, every concordance row points to a section or rule of this document and its
status agrees with the rules it cites, and every gap package has its requirement section. A rule that is added later
must bring its evidence (rule E-19 of the client: a gap is closed with a check that keeps it closed).

Terms: *scope key* = a 32-byte key with an epoch (`OK_e`, `WK_e`, `CK_e`; [04 §2](spec/04-crypto.md#2-key-inventory));
*wrap* = an HPKE seal of one scope epoch to one recipient's X25519 key, signed by the wrapper; *holder* of an epoch =
whoever can open it; *entitled* = the verified log makes a principal a reader of that scope now.

## 1. Principals

| Principal | Identified by | Own long-term keys | Acts through | Built |
|---|---|---|---|---|
| **Human user** (optionally with the root-signed platform role `inviter`) | user ID (UUIDv7), registered identity `pub{kem,sig}`, passkeys | user identity (X25519 + Ed25519) in the identity bundle | browser sessions, its agent | yes |
| **Browser session** of a user | P-256 session key in a 12 h session certificate signed by a passkey assertion | the session key; it holds the user's unlocked identity in memory while the tab is open; a **remembered browser** also keeps it at rest under a non-extractable IndexedDB key (`persist/device-key`, [06 §3.3](spec/06-web.md#33-prf-required-prf_missing-guidance)) | the user's identity | yes |
| **Execution device** – org device (enrolled by an org invitation) or platform device (certified by the platform root) | device ID, `pub{kem,sig}` in the membership log (org device) or the platform log (platform device) | device identity (X25519 + Ed25519), runner storage key derived from it | the runner; the sessions it runs | yes |
| **Agent of the user** (main orchestrator, [idea 34](KONZEPT.md#k-idee-34), [ADR-056](adr/056-delegated-agent-access.md)) | agent ID (key ID `agent.<hex>` at the hub), a sub-principal of kind `agent` with its user as parent in every log of the user ([ADR-082](adr/082-agent-own-identity.md)) | agent key pair = its identity (X25519 opens its wraps, Ed25519 signs requests, entries, events, wraps, records); a delegation certificate by the user's identity; the registering agent of an account without a person holds the account's identity ([E-27](KONZEPT.md#e-27)) | its own identity with its user's rights | yes (local); on DFX hardware: in the session VM under the account `agent-orch` (KM-41) |
| **Platform session** (a Claude Code session, its subagents) | session ID, bearer session token inside the VM | **none** | the device that runs it (device-signed events, operations and reads) | yes |
| **Work, test and browser VM** ([E-08](KONZEPT.md#e-08), ideas [27](KONZEPT.md#k-idee-27), [28](KONZEPT.md#k-idee-28)) | VM ID | none (no scope key, no credential) | platform operations of its session | work VM yes; test/browser VM: KOR-43, KOR-44 |
| **Platform root** (operator) | compiled public key | Ed25519 platform root, offline | root-signed platform-log entries and operator requests | yes |
| **Hub** | its response key | Ed25519 response key (signs responses only) | – | yes |
| **Invitation holder** (transient) | `invite_id` | none: the 16-byte code derives `package_key` and `sk_inv` | proof of the code at registration, accept or enrollment | yes |
| **Support person** ([idea 29](KONZEPT.md#k-idee-29), [ADR-096](adr/096-support-release.md)) | user ID with the root-signed platform role `support`, bound to the root certification of its identity keys (`user_certify`) | its user identity (passkey, PRF) | its own identity, in person only (its agent is no support principal) | yes |
| **Community member** ([E-20](KONZEPT.md#e-20)) | the user | the user identity; community key `CMK_e` | the user's identity | designed in KOR-26 (not on `develop`) |

A **subagent** is a platform session like its parent ([A1](KONZEPT.md#a-1)): same workgroup, same device, same rights,
no keys. A **human that instructs another user's session** ([idea 36](KONZEPT.md#k-idee-36)) acts with its own
identity (its prompt is an event signed by itself); the session reads with the rights of the task owner
([§8](#8-reading-across-workgroups)).

## 2. Keys per principal

| Key | Holder | At rest | Never at |
|---|---|---|---|
| PRF output `prf_i`, `KEK_i` | browser, per passkey and login; bound to the relying-party ID (a domain change loses every passkey slot, [ADR-004](adr/004-invitation-identity-without-keys-file.md)) | memory only | hub, device, agent |
| Recovery code, `KEK_rec` | the user (written down); browser at use | – | hub (only `recovery_id`) |
| User identity (private) | browser memory after unlock; the agent of an account without a person (it is the account's holder) | hub: only sealed bundle slots (passkey, recovery); **a remembered browser**: IndexedDB under a non-extractable key; **the agent tool of an account without a person**: its state holds the sealed identity and the handover secret next to the agent key | device, VM, the agent of a person (since ADR-082; agents paired before are converted and delete their copy) |
| Browser session key | browser | IndexedDB, non-extractable | anywhere else |
| Support person's wraps of a released `WK_e` ([ADR-096](adr/096-support-release.md)) | the person (its own identity) | wraps at the hub, sealed to the key of its root certification | `OK`, any `CK`, an unreleased workgroup |
| Device identity, storage key | the runner of that device | `keys/device.key` 0600; storage key derived, memory only | hub (public keys only), VM |
| Agent key | the agent tool on the user's computer | login keychain or 0600 file | hub (public keys only) |
| `OK_e` | org members; devices assigned to any workgroup of the org ([ADR-007](adr/007-devices-receive-org-key.md)) | wraps at the hub; runner storage-key encrypted | hub in clear, VM |
| `WK_e` | members of the workgroup; **every org admin** (inherited read, [ADR-058](adr/058-admins-read-every-workgroup.md)); devices assigned to the workgroup; **every support person while the workgroup is released to support** (every epoch, [ADR-096](adr/096-support-release.md)) | as above | hub in clear, VM |
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
| Workgroup | `WK_e` | prompts, session output, tool and file events, terminal, task titles, workgroup and task whiteboards, operation parameters and results, workgroup settings, platform reads, the names a support person needs (`support.info`) | explicit read/write members, members on the default workgroup, every admin (read), assigned devices, the support roster while released (ADR-096) | explicit `write` members; assigned devices for device kinds; support persons under a `write` release for prompts and the workgroup whiteboard |
| Credentials | `CK_e` of one user | that user's credential records (GitHub, API key, Claude subscription, mailbox) | the owner; devices assigned to a workgroup with the owner's explicit access, in consented orgs | the owner; an eligible device acting on the owner's device request |
| Dedupe | `dk_e` from `WK_e` | operation fingerprints (opaque tags) | `WK_e` holders | assigned devices |
| Context objects (KOR-33, not on `develop`) | `k_ctx` from `WK_e`, per object | session context checkpoints, PR → task tags | `WK_e` holders (a restoring device must be assigned to the workgroup) | the authoring device |
| Community (KOR-26, not on `develop`) | `CMK_e` | community messages and display names | every user, by anonymous key request | every user |

There is no key per task or per session: a task's content is workgroup content (the hierarchy is only organization →
workgroup → task, [Grundprinzip](KONZEPT.md#k-gp)).

## 4. Who receives which scope, when

| Trigger (log entry or action) | Receives | Delivered by |
|---|---|---|
| Registration ([04 §5.1](spec/04-crypto.md)) | the user: `CK_1` (created once per user, here) | self-wrap in `register/finish` |
| Private org genesis | founder: `OK_1`, `WK_1` of the default workgroup | self-wraps in the genesis batch |
| `accept` of a user invitation | invitee: current `OK`, `WK` of the invited and the default workgroup; the org's eligible devices become due for the invitee's `CK` | sealed package (AES-GCM under the code's `package_key`, not a wrap); later epochs by catch-up; own self-wraps; `CK` by catch-up from the invitee's clients |
| `accept` of an admin invitation | as above (the package carries no inherited keys) | the other workgroups by catch-up after the visible accept ([ADR-058](adr/058-admins-read-every-workgroup.md)) |
| `admit` (direct admission, [E-18](KONZEPT.md#e-18)) | subject: current `OK`, default `WK`, listed `WK`; **no** device of this org becomes due for the subject's `CK` | admitting batch (must wrap `OK` and default `WK`); rest by catch-up |
| `grant` | subject: current `WK` (older on request); with the first explicit access in a workgroup with devices, those devices become due for the subject's `CK` | granting batch or catch-up |
| `role` → admin | new admin: every epoch of every workgroup it reads by inheritance (never `CK` of others) | appointing batch, then catch-up of all epochs |
| `wg_create` | creator, every admin, granted members: `WK_1` | the creating batch (refused without these wraps) |
| `device_assign` (and `accept` of a device invitation) | device: `WK` of the workgroup, `OK`; the `CK` of every user with explicit access there in an org that user joined by its own entry – the assigning user's own `CK` only under that condition | assigning batch / package; other `CK` by catch-up from the owners' clients or from devices already eligible for them |
| Pairing of the user's agent (`agent_add` in every log of the user, [ADR-082](adr/082-agent-own-identity.md)) | agent: every epoch of every scope its user reads and the user's `CK` (KM-40) | the confirming batches (wraps of every epoch the browser holds), later epochs by catch-up from any holder |
| Passkey added / recovery | the new passkey slot of the same identity | browser upload after a PRF login |
| "Remember this browser" | the browser profile: a local copy of the identity | IndexedDB, never sent |
| Platform session started | nothing: the device decrypts and gives the VM plaintext of its own workgroup only | runner |
| Community (KOR-26) | every user: `CMK_e` | anonymous key request served by any holder |
| `support_grant {wg, access}` ([ADR-096](adr/096-support-release.md)) | every person of the support roster: every epoch of that `WK` (never `OK`, never `CK`); a person who joins support later gets the existing releases | the releasing batch (wraps of every epoch the releasing client holds), then catch-up from any holder (browsers, devices, agents) |

**Catch-up** ([04 §5.11](spec/04-crypto.md#511-catch-up-wrapping)) is what makes all of this work without anybody
online: every holder (browser on sync, device on reconcile) wraps for every entitled recipient that has no wrap yet,
as far as rules W1/W2 allow; the hub stores only what W3 allows in its own log state, and each recipient checks again.

## 5. Revocation and rotation, case by case

The general rule: losing the right to **read** a scope puts that scope into the rotation set
`R(before, after, subject)` ([04 §4.7](spec/04-crypto.md#47-membership-log)); the triggering entry, one `rotate` per
element of R and the wraps of the new epochs for every remaining reader go to the hub in **one** batch, which the hub
refuses otherwise. Losing only the right to write rotates nothing (the reader rule E5 refuses the writes). Rotation
protects what is written **after** it; what a principal read before stays with it ([idea 30](KONZEPT.md#k-idee-30),
last sentence) – the client says so where it matters.

| Case | Rotates | Who | What the revoked principal keeps |
|---|---|---|---|
| Member removed | `OK`; every `WK` it could read (incl. by inheritance) | the removing admin, same batch | everything read and every old epoch it held |
| Member leaves | same, as `pending` | remaining admins/writers in the background | same |
| `ungrant` | that `WK` (not if the member still reads it as admin) | admin, same batch | same, for that workgroup |
| Admin demoted (`role` → member) | every `WK` it read only by inheritance | the demoting admin; self-demotion: `pending` | same |
| Device unassigned from one of several workgroups | that `WK` | admin, same batch | that workgroup's plaintext and keys already on the device |
| Device unassigned from its last workgroup / removed | that `WK` and `OK` | admin, same batch | plaintext and keys already on the device |
| Device loses eligibility for a user's `CK` | `CK` of that user (never in R, **not atomic**: until the owner's browser is online the old `CK` stays current) | the owner's browser (re-encrypts the records) | old credential values |
| Platform device decertified | as unassignment in every org that uses it | next admin browser (`rotation_pending`); the hub refuses the device at once | as unassignment |
| `org_platform_access` withdrawn ([ADR-026](adr/026-platform-device-entitlement.md)) | nothing: existing platform-device assignments stay until an admin unassigns them (then as unassignment) | admin | until unassigned: everything it is assigned to |
| `user_decertify` | nothing: it ends the identity anchor for **new** admissions; memberships stay | operator | – |
| `wg_archive` | nothing: readers keep reading the archived workgroup | – | – |
| Invitation revoked | nothing | – | **if the code leaked and its package was looked up before the revocation**: the package epochs (`OK`, `WK` of the invited and default workgroup) until the next rotation of those scopes – [decision D2](#decisions-for-the-orchestrator) |
| Agent revoked or replaced | every scope it read (= its user's), in the removing or replacing batch of every log; afterwards the user's `CK` | the user's client (browser or active agent), the agent itself as `pending`, an admin | everything read and every old epoch it held; an agent paired before ADR-082 also a copy of the user's identity it may have made (no rekey); under equal rights it may have replaced the recovery code or registered a passkey through recovery (the user checks the passkey list) |
| Remembered browser forgotten | nothing: "Forget" deletes the local key; it works only on that browser | the user, on that browser | a lost or stolen remembered browser keeps the identity: as a leaked identity (next row) |
| Leaked user identity (lost unlocked device, remembered browser, a passkey or recovery code used by somebody else, the agent's copy) | **nothing exists today**: identities do not rotate | – | everything the identity can open and sign, now and later – [gap KOR-57](#kor-57--own-identity-of-the-agent-real-revocation-orchestrator-on-dfx-hardware) point 5 |
| Passkey removed | nothing: the slot is deleted; the identity stays | the user | – (if the passkey was compromised before: as a leaked identity) |
| Recovery code replaced | nothing: the slot is resealed | the user | – (if the code was used by somebody else: as a leaked identity) |
| All passkeys and the code lost | the identity is lost; an admin removes and re-invites (rotation as removal) | admin | – |
| Admitted without consent ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)) | – (no device of that org was due for the user's `CK`) | – | – |
| Platform root lost or compromised | no rotation path: a new root needs a new build and new certifications ([ADR-023](adr/023-platform-root-key.md), runbook `platform-root-key.md`) | operator | a compromised root can certify devices and identities until the new build is everywhere |
| Hub response key replaced | org devices take it from the next invitation package; platform devices pinned it on first use from `/v1/config` | operator | – ([gap KOR-55](#kor-55--trust-anchor-of-the-runner) point 4) |
| Support release revoked (`support_revoke`, [ADR-096](adr/096-support-release.md)) | that `WK` (always, computed from the org log alone) | the revoking admin or writer, same batch | everything read and every epoch it held |
| Person leaves support (`role_revoke support`, `user_decertify`) | every released `WK` whose current epoch was wrapped to the person (`rotation_pending`, derived by the hub from its stored wraps) | the next admin or writer client in the background; the hub refuses the person at once | as above; a hub that withholds the role end and still serves the person is the residual of ADR-096 §3 (as for decertified devices) |

Old epochs are never deleted and never re-encrypted: a removed principal with a copy of the hub's ciphertexts can
open what it could open before, never what is written after the rotation.

## 6. Where plaintext lies

| Place | Plaintext of | Why |
|---|---|---|
| Browser (open tab) | everything the user can read, after unlock; a remembered browser keeps the identity at rest | the user reads it |
| The user's computer with the agent tool | everything the user can read (the agent receives every scope of its user, KM-40) | [E-04](KONZEPT.md#e-04), [A3](KONZEPT.md#a-3) |
| Execution device (runner) | every scope of every workgroup assigned to it, the org scope, the credentials of eligible users, the operator's tokens on platform devices; at rest under the storage key, in memory in clear | the device executes ([§5 point 4](KONZEPT.md#k-5-b4), [A7](KONZEPT.md#a-7)) |
| Session VM (guest) | its own workgroup's content that the runner hands over, the model credential of running sessions in memory; with KOR-45 also what it read from other workgroups ([§8](#8-reading-across-workgroups)); with KOR-57 on DFX hardware the orchestrator session's own agent key and everything its user can read | the session works on it |
| Work, test and browser VM | what the session copies into it (no keys, no platform credentials) | [E-08](KONZEPT.md#e-08), [E-09](KONZEPT.md#e-09) |
| Model provider, GitHub | prompts and code the session sends | inherent, the user's choice of access |
| Hub | nothing confidential ([§7](#7-what-the-hub-sees)) | [§5 point 2](KONZEPT.md#k-5-b2) |

Assigning a device to a workgroup (or, with KOR-41, to the whole organization) therefore gives **that device** the
keys: the assignment decides where people's tasks run, but where a task runs, its content lies in clear
([A7](KONZEPT.md#a-7)). The security page says so; the assignment dialog does not yet
([KOR-41](#kor-41--organization-wide-device-assignment)). How this reads against [idea 35](KONZEPT.md#k-idee-35)
("the assignment steers computing capacity, not data access") is [decision D1](#decisions-for-the-orchestrator).

## 7. What the hub sees

The exhaustive list is [04 §7](spec/04-crypto.md#7-what-the-hub-sees-in-plaintext-exhaustive). For keys it means: the
hub stores public keys, the two logs, wraps and signatures, the sealed bundle slots and the agent's sealed handover;
it never holds or derives a scope key, a private key, a PRF output, a code or a credential value (enforced by the
zero-knowledge canary scan, KM-36). It sees the metadata of rights and key distribution (who is entitled to which
epoch, who wrapped for whom, when a rotation happened) and the counters of [A4](KONZEPT.md#a-4). The read log of
sessions is metadata too (IDs only): which session read which workgroup or task when and on whose instruction
([ADR-089](adr/089-sessions-read-like-their-human.md) §4).

An **active** hub can do more, and the model does not hide it ([04 §9](spec/04-crypto.md#9-threat-model)):
withhold data and serve an old log (detected per client, KM-08); **serve malicious JavaScript**, which receives every
key of every browser that loads it, the identity included (not solvable for web end-to-end encryption; mitigated by
CSP, SRI and the reproducible build); for accounts registered before ADR-060, have the operator certify a false
identity unless the account code is used ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)); and – today –
present an invented organization to a platform device ([gap KOR-55](#kor-55--trust-anchor-of-the-runner)).

## 8. Reading across workgroups

Until KOR-45 ([ADR-024](adr/024-session-read-scope.md)) a session read with the rights of its task owner
**intersected with its own workgroup**; other workgroups were visible by name only. The device holds exactly the keys
of its assigned workgroups (KM-14), so this is also what it *can* read. KOR-45 lifted the intersection with the
workgroup ([ADR-089](adr/089-sessions-read-like-their-human.md): points 1, 2 and 4-6 below are built, way B of point 3
is not). The key model requires of it:

1. **Rights:** a session reads what both the task owner **and** the human whose instruction it is working on may read
   (owner ∩ instructing human; without another human's instruction the owner alone), evaluated at call time on the
   verified log, never more than either. A workgroup outside that set stays `not_found`, its name included where it
   is not already visible to the reader as org metadata.
2. **Keys – way A, existing entitlement (default, no change of the verifiers):** the device reads another workgroup
   with a `WK` it already holds because it is **assigned** to that workgroup (directly, or through the
   organization-wide assignment of KOR-41). KOR-45 may not add a wrap rule, a new scope or a wrap from a human to a
   device that the log does not entitle – its own scope boundary says the log verifiers are only used through existing
   wraps, and a wrap outside the log is refused by every verifier (KM-05, KM-06). A workgroup whose key the device does
   not hold is answered as unreadable with the reason and the triggered action (E-05: the admin is asked to assign the
   device, or to give it to the organization), never silently empty. Because the key is technically required, this is
   the exception of [E-11](KONZEPT.md#e-11), not a release the user must give in advance.
3. **Keys – way B, a read release per task (only as a cryptography package):** KOR-45's text also names "a release the
   human gives when creating the task, without an extra step". In the key model that is a **new entitlement**: a
   signed membership-log entry by the owner (and, for an instruction of another human, by that human) "device D may
   read workgroup B for task T", visible in the log, ended by the task's end or a revocation, with B's `WK` in R at
   the end; W2, catch-up, R and the batch rule must know it in all three verifiers, with vectors. That is Spur-K work
   outside KOR-45's boundary; whether it is wanted is [decision D3](#decisions-for-the-orchestrator).
4. **What lies where:** content of another workgroup that a session read is plaintext in that session's VM, in what it
   writes into its own workgroup (chat, whiteboard, results) and in its context checkpoints (KOR-33), which are sealed
   under the **session's** workgroup key. Reading across workgroups therefore copies content into the reading
   workgroup's scope, the way a human copying a text does ([idea 30](KONZEPT.md#k-idee-30), last sentence). Required:
   the harness tells the session that every reader of its workgroup can see what it writes there; the security page
   says it; every such read is in the read log (point 5). A stricter mode – hand the session only what every current
   reader of its workgroup may read – may be offered as a voluntary workgroup setting (E-11: restrictions are
   optional), never as the default.
5. **Audit:** every cross-workgroup read writes a read-log row with source workgroup, target workgroup, owner,
   instructing human and the reading session ([§8 point 6](KONZEPT.md#k-8-b6)).
6. **Check:** vectors for the intersection function (owner, instructing human, admin inheritance, a removed
   instructing human) in hub and runner, and an attack test "member B instructs A's session to read a workgroup only
   A reads" → `not_found`.

## 9. Rules and their evidence

Evidence: `file.json#name` is a vector of `contracts/vectors/` (a negative vector by its `name`, a positive case by its
`name`, or a top-level entry); every implementation – the hub reference `agent_hub`, the runner copy
`agent_runner.crypto` and the web core `web/src/core` – runs every vector of the log and crypto files. `test:` names a
test of a rule that only one component enforces, as `path::test name`. `gap KOR-NN` marks a rule that is not built yet.

| Rule | Statement | Enforced by | Evidence |
|---|---|---|---|
| KM-01 | A user identity is drawn by the browser at registration; the hub stores only public keys and bundle slots sealed under a KEK from one passkey's PRF output (32 bytes) or from the 16-byte recovery code, bound to user and slot. | web; hub stores | `bundle.json#other_slot`; `bundle.json#other_user`; `kdf.json#prf_31_bytes`; `kdf.json#recovery_code_15_bytes` |
| KM-02 | Only a passkey that yields PRF is kept: a registration (or an added passkey) without PRF output is withdrawn ([E-12](KONZEPT.md#e-12)); no recovery-code step at registration ([E-13](KONZEPT.md#e-13)). | web, hub | test:`web/e2e/invite-platform.spec.ts::a passkey without PRF is refused`; test:`tests/platform/hub/auth/test_jobs.py::test_the_registered_user_withdraws_an_unfinished_registration`; test:`web/tests/ui/passkeys.test.tsx::adds a passkey on this device; another passkey answering and refusals are reported` |
| KM-03 | The identity bound to a code is proven, not asserted: registration proves the invitation code (`inv_sig`), an identity anchor is a root `user_certify` or an `id_sig` of the platform invitation of exactly that account. | hub, all verifiers | `invite.json#register_other_code`; `log.json#admit_invitation_without_proof`; `log.json#admit_proof_of_other_code`; `log.json#admit_proof_for_other_account` |
| KM-04 | Every scope key is random (never derived from another) and has an epoch; it reaches a recipient as an HPKE wrap whose info binds scope, scope ID, epoch and recipient, signed by the wrapper – or, for an invitee, inside the invitation package sealed under the code's `package_key` (KM-26). | all | `wrap.json#other_recipient_id`; `wrap.json#other_epoch`; `wrap.json#other_scope`; `wrap.json#signature_other_wrapper`; `invite.json#package_other_code` |
| KM-05 | A wrap is accepted only for a recipient the verified log entitles now (W2) and from the recipient itself (W1) or a holder of that epoch; the hub stores it only under its own log state (W3), recipients check again. | all | `log-wraps.json#recipient_not_entitled`; `log-wraps.json#hub_forges_wrap`; `log-wraps.json#epoch_in_future`; `log-wraps.json#W2_former_holder_of_old_epoch` |
| KM-06 | Only principals of the verified log receive wraps: an ID outside it (an agent, a support person without a release, anything the hub names) is never a recipient, while the same wrapper and epoch are valid for a principal of the log. | all | `log-wraps.json#principal_outside_log_is_no_recipient`; `log-wraps.json#W2_creator_for_device` |
| KM-07 | Membership is decided only by the signed, hash-chained org log, verified against the pinned founder key (L1–L8); the hub cannot inject a member, swap keys or forge an entry. | all | `log.json#L4_hub_injects_member`; `log.json#accept_swapped_keys`; `log.json#L3_founder_not_pinned`; `log.json#admit_forged_by_hub` |
| KM-08 | A client stores the verified head and refuses a log that does not extend it (L9); an event beyond the head forces a refresh, a missing head is a fork (L10). | all | `log.json#L9_shorter`; `log.json#L9_fork`; `log.json#L10_lh_beyond_head` |
| KM-09 | Platform devices, platform invitations, user certifications, the role `inviter` and the platform access of a **team** org exist only as root-signed platform-log entries; the hub cannot extend that log. (A private org's platform access comes from its genesis, which only the hub binds to an invitation: KM-31.) | all | `platform-log.json#P4_hub_certifies_device`; `platform-log.json#P3_root_not_pinned`; `log.json#assign_without_platform_access` |
| KM-10 | Org key: members and devices assigned to at least one workgroup of the org; a device without a workgroup is no reader of the org key ([ADR-007](adr/007-devices-receive-org-key.md)). | all | `log-wraps.json#W2_org_key_for_device_with_workgroup`; `log-wraps.json#org_key_for_device_without_workgroup` |
| KM-11 | Workgroup key: explicit members, members on the default workgroup, every active admin by inheritance (read only, all epochs, archived workgroups too), assigned devices. | all | `log-wraps.json#W2_admin_by_inheritance_for_member`; `log-wraps.json#W2_for_admin_on_archived_workgroup`; `log-wraps.json#admin_catch_up` |
| KM-12 | Writing a workgroup needs explicit `write` at the author's `lh` and (hub) at receipt; the inherited read never writes. | all | `log-events.json#E5_reader_prompts`; `log-events.json#hub_E5_at_receipt`; `log-events.json#E5_archived_workgroup` |
| KM-13 | Credential key: the owner and devices assigned to a workgroup with the owner's **explicit** access; never through an admin's inherited read. | all | `log-wraps.json#cred_owner_for_device`; `log-wraps.json#cred_admin_inherited_read_not_eligible`; `log-wraps.json#cred_user_not_owner` |
| KM-14 | A device holds keys only of what it is assigned to: never the `WK` of another workgroup; a decertified platform device is no recipient. | all | `log-wraps.json#recipient_not_entitled`; `log-wraps.json#recipient_decertified`; `log.json#assign_decertified_device` |
| KM-15 | An organization that admitted a user without the user's consent ([ADR-060](adr/060-direct-admission-of-existing-accounts.md)) gets none of the user's credentials: its devices are no `CK` recipients, while a consented member's `CK` reaches the same device. | all | `log.json#cred_device_of_org_without_consent`; `log-wraps.json#cred_device_of_org_with_consent` |
| KM-16 | A direct admission names two identity anchors in the platform log (the subject's and the admitting admin's) and carries the wraps of the current org and default-workgroup epochs. | all | `log.json#admit_hub_swaps_keys`; `log.json#admit_admitter_unproven`; `log.json#admit_batch_without_wraps` |
| KM-17 | Losing the right to read a scope rotates it in the same atomic batch (R); leaving and self-demotion leave R `pending` for the others; the hub refuses any other batch. | all | `log-rotation.json#rotate_missing`; `log-rotation.json#rotate_extra`; `log-rotation.json#admin_demoted_without_rotation`; `log-rotation.json#leave_without_rotation` |
| KM-18 | New epochs are wrapped for every remaining reader in the same batch, never for the removed principal. | all | `log-rotation.json#wrap_missing_for_remaining_recipient`; `log-rotation.json#wrap_for_removed_member`; `log-rotation.json#admin_demoted_wrap_for_demoted` |
| KM-19 | A new workgroup's first epoch is wrapped in the creating batch for every reader, every admin included. | all | `log-rotation.json#wg_create_without_wraps`; `log-rotation.json#wg_create_without_admin_wrap` |
| KM-20 | `CK` is never in R; a device losing eligibility makes the owner's browser rotate `CK` and re-encrypt the records. | web | `log-rotation.json#main_15_device_unassign`; test:`web/tests/core/jobs/credential-rewrap.test.ts::finds devices that lost eligibility and when` |
| KM-21 | A removed or excluded principal authors nothing (entries, events, new wraps); an event in the previous epoch is accepted for 600 s after a rotation only from an author that still has the right at receipt. | all | `log.json#L8_removed_author`; `log-events.json#author_removed_at_lh`; `log-events.json#E5_decertified_device`; `log-wraps.json#removed_wrapper_at_hub`; `log-events.json#hub_E5_at_receipt`; `log-events.json#hub_E6_grace_over` |
| KM-22 | Every event is encrypted under a per-event key from the scope key, AAD = canonical header, signed by its author over header and raw ciphertext; kinds are bound to scope and author kind. | all | `event.json#header_changed_after_signing`; `event.json#other_author_key`; `log-events.json#E3_prompt_by_device`; `log-events.json#E3_output_by_user` |
| KM-23 | A platform session has no key: what it produces is sealed and signed by the device that runs it, only for a session on that device. | all; runner | `log-events.json#hub_E7_session_on_other_device`; `log-events.json#E7_reader_other_session_device`; `log-events.json#E5_device_not_assigned` |
| KM-24 | Credential records are sealed under `k_cred` from the owner's `CK` and signed by the owner or an eligible device acting on the owner's request; device requests are sealed to one device and signed by the user. | all | `credential.json#other_ck`; `credential.json#version_bumped`; `request.json#signed_by_device`; `request.json#other_device_key` |
| KM-25 | The runner keeps scope keys and outbox content only under its storage key, derived from the device identity. | runner | `kdf.json#storage_key`; test:`tests/platform/runner/crypto/test_storage.py::test_storage_key_matches_the_shared_vector` |
| KM-26 | Invitation code: one use, expiry, revocation; the sealed package opens only with the code and carries `OK` and the keys of the invited and default workgroups. | all | `invite.json#package_other_code`; `log.json#L7_accept_twice`; `log.json#invite_revoked`; `log.json#invite_expired`; `log.json#L5_invite_default_wg` |
| KM-27 | Org and platform device identities are bound to their invitation by `enroll_sig` and `inv_sig` over the enrollment text. | all | `enroll.json#other_invite_key`; `enroll.json#enroll_sig_missing`; `platform-log.json#certify_swapped_key` |
| KM-28 | The agent of a person never receives the person's identity: it holds its own key pair and receives the user's public keys and its delegation certificate sealed to its key **and** bound to a handover secret the hub never sees; its label is sealed to the user ([ADR-082](adr/082-agent-own-identity.md)). | all (agent tool, hub, web) | `agent.json#user_anchor_other_agent`; `agent.json#cert_other_user`; `agent.json#cert_other_gen`; `agent.json#label_to_agent_key` |
| KM-29 | Exactly one active main orchestrator per user; a new pairing needs a passkey gesture and replaces the previous one; revocation refuses its key at once and deletes its sealed identity. | hub | test:`tests/platform/hub/agents/test_pairing.py::test_a_new_confirmation_replaces_the_main_orchestrator`; test:`tests/platform/hub/agents/test_pairing.py::test_concurrent_confirmations_of_one_user_leave_exactly_one_main_orchestrator`; test:`tests/platform/hub/agents/test_revoke.py::test_the_session_revokes_one_agent_at_once` |
| KM-30 | Revoking or replacing the agent rotates it out: its removal (or its replacement by a higher generation) puts every scope it read into R of the same batch, a removed, replaced or superseded agent is no recipient of any scope or of `CK`, and the user's client rotates `CK` afterwards. An agent that held the user's identity before its conversion keeps that copy (no rekey, [ADR-082](adr/082-agent-own-identity.md) §5). | all | `log-rotation.json#agent_removed_by_user_without_rotation`; `log-rotation.json#agent_replaced_by_higher_gen`; `log-wraps.json#wrap_to_removed_agent`; `log-wraps.json#wrap_to_replaced_agent`; `log-wraps.json#cred_superseded_agent_is_no_reader` |
| KM-31 | A platform device trusts an org (founder, platform access) only on evidence it verifies itself against the root key. | runner | gap KOR-55 |
| KM-32 | Write access to the default workgroup can be reduced to read by an admin (R stays empty: the member still reads; E5 refuses its writes); new members still start with write. | – | gap KOR-55 |
| KM-33 | A support person reads or writes a workgroup only through an explicit, revocable release of that workgroup to support in the log plus a valid root-signed support role bound to its root certification; the release is given only by an admin or a writer, support never receives `OK` or `CK`, writes only prompts and the workgroup whiteboard under a `write` release, and revocation rotates. | all | `log.json#support_grant_by_reader`; `log.json#support_grant_forged_by_hub`; `platform-log.json#support_role_without_certification`; `log-wraps.json#support_wrap_without_release`; `log-wraps.json#support_wrap_org_scope`; `log-wraps.json#support_wrap_unknown_to_reader`; `log-wraps.json#support_wrap_after_role_end`; `log-wraps.json#support_person_reads_released_workgroup`; `log-rotation.json#support_revoke_without_rotation`; `log-rotation.json#support_revoke_new_epoch_wrapped_to_support`; `log-events.json#support_event_read_release`; `log-events.json#support_writes_support_info`; `log-events.json#support_event_hub_after_role_end`; test:`tests/platform/hub/support/test_support.py::test_revocation_rotates_and_shuts_the_person_out` |
| KM-34 | A session reads across workgroups only within owner ∩ instructing human and only with keys its device already holds by assignment ([§8](#8-reading-across-workgroups)). | runner | test:`tests/platform/runner/queries/test_cross_workgroup.py::test_the_decision_table`; test:`tests/platform/runner/queries/test_cross_workgroup.py::test_member_b_cannot_read_through_a_session_of_a_what_only_a_reads`; test:`tests/platform/runner/queries/test_cross_workgroup.py::test_the_human_captured_when_the_read_arrived_is_used` |
| KM-35 | Credentials owned by an organization (a company identity) are readable by the devices that run its tasks, never by every member: the organization's `cred` scope has its assigned member devices as its only readers; an admin wraps each epoch without being a reader ([ADR-078](adr/078-github-identities.md)). | all | `log-wraps.json#cred_org_admin_for_device`; `log-wraps.json#cred_org_device_for_device`; `log-wraps.json#cred_org_admin_is_no_recipient`; `log-wraps.json#cred_org_member_is_no_wrapper`; `log-wraps.json#cred_org_removed_admin_is_no_wrapper` |
| KM-36 | The hub never holds a scope key, private key, PRF output, code or credential value; a full flow leaves no canary in its database or logs. | hub | test:`e2e/platform/specs/e2e-09-zero-knowledge.spec.ts::the hub sees no confidential content over the full flows`; test:`tests/platform/hub/requests/test_devices_and_zero_knowledge.py::test_canaries_never_reach_hub_storage` |
| KM-37 | VMs hold no scope key and no platform credential at rest: the guest has no cryptography library at all, and a work VM holds none of the platform's credentials. Stated exception (KM-41, [ADR-082](adr/082-agent-own-identity.md) §9.7): the system account `agent-orch` of a session VM holds the main orchestrator's agent tool with its cryptography library in `/opt/agent-orch` (`0750` of root and that account): the packages `agent_platform` and `agent_runner` (the tool imports its `crypto`, `encoding`, `errors`, `install`, `login` and `sync` modules; the rest of the package is shipped there unused), `agent_cli.keys` and the wheels pinned in `images/versions.json` (`cryptography`, `cffi`, `httpx` and their dependencies) and, while that session is the orchestrator, the agent's own key (`/var/lib/agent-orch`, `0700`); the guest package itself stays stdlib-only, and the session (Claude Code) can neither read the key nor start the tool. | runner, guest | test:`tests/platform/test_import_boundaries.py::test_guest_and_doubles_use_the_standard_library_only`; test:`tests/platform/images/test_workvm_attack_probes.py::test_credential_search_reports_counts_and_paths_never_the_secret`; test:`tests/platform/images/test_provisioning.py::test_the_agent_tool_wheels_cover_its_third_party_imports`; test:`tests/platform/guest_linux/test_orchestrator_linux.py::test_a_session_user_cannot_read_the_key_or_reach_the_relay` |
| KM-38 | A remembered browser keeps the identity only under a non-extractable key of that browser profile; "Forget" deletes it there. | web | test:`web/tests/core/persist/device-key.test.ts::stores the key itself non-extractable`; test:`web/tests/core/persist/device-key.test.ts::remembers, survives a reload, unlocks the same identity and forgets` |
| KM-39 | The agent is a sub-principal of its user in every membership log: added only with a delegation certificate signed by the user's identity (by the user or by the certified agent itself), at most one active agent per user with a strictly higher generation than any earlier one, never re-added after a removal other than together with its user; an agent cannot add another agent and a hub cannot insert one. | all | `log.json#agent_add_cert_by_other_key`; `log.json#agent_add_cert_for_other_pub`; `log.json#agent_adds_another_agent`; `log.json#agent_add_second_agent_same_gen`; `log.json#agent_add_lower_gen`; `log.json#agent_add_removed_agent_again` |
| KM-40 | The active agent receives exactly what its user receives and authors with exactly its user's rights: every scope and epoch (also older ones and inherited reads), the user's `CK`, nothing its user cannot read. | all | `log-wraps.json#W2_user_for_its_agent`; `log-wraps.json#W2_older_epoch_for_agent`; `log-wraps.json#agent_receives_no_scope_its_user_cannot_read`; `log-wraps.json#cred_owner_for_its_agent`; `log-events.json#prompt_by_agent_of_writer`; `log.json#agent_of_member_invites` |
| KM-41 | The orchestrator on platform hardware draws its key in the session VM under an account of its own, only in the user's private organization on a platform device, receives the handover signed by the user and ends with the session. | runner, guest | test:`tests/platform/runner/orchestrator/test_work.py::test_keys_only_for_a_running_main_session_of_a_private_org_on_a_platform_device`; test:`tests/platform/runner/orchestrator/test_work.py::test_a_handover_not_signed_by_the_task_owner_is_refused`; test:`tests/platform/runner/orchestrator/test_relay.py::test_a_relay_of_any_other_session_is_refused_and_never_reaches_the_hub`; test:`tests/platform/guest/orchestrator/test_orchestrator.py::test_keys_only_for_an_active_running_main_session`; test:`tests/platform/guest/orchestrator/test_routes.py::test_only_the_claimed_orchestrator_reaches_the_tool`; test:`tests/platform/guest/orchestrator/test_orchestrator.py::test_the_orchestrator_ends_with_a_stopped_or_failed_session_only`; test:`tests/platform/guest_linux/test_orchestrator_linux.py::test_a_session_user_cannot_read_the_key_or_reach_the_relay`; test:`tests/platform/runner/contexts/test_orchestrator_session.py::test_an_orchestrator_session_is_never_checkpointed` |

## 10. Concordance with the concept

Every section and idea of [part A](KONZEPT.md#teil-a--das-konzept), every rule of part B and every interpretation of
part C, with its bearing on the key model. Status: **carried** (the key model supports it as built), **carried
(pending KOR-NN)** (designed in a package not yet on `develop`), **gap → KOR-NN** (the named package must close it, §11),
**decision needed** (a tension this comparison cannot resolve, [§11](#decisions-for-the-orchestrator)), **none** (no
bearing on keys).

| Concept | Bearing on the key model | Status | Section |
|---|---|---|---|
| [Grundprinzip](KONZEPT.md#k-gp) | Encrypted platform; hierarchy organization → workgroup → task gives the scopes; no task key. | carried | [§3](#3-scopes) |
| [§1](KONZEPT.md#k-1) | Users and devices only by invitation code. | carried | [§4](#4-who-receives-which-scope-when), KM-26 |
| [§1 point 4](KONZEPT.md#k-1-b4) | Devices authenticate with a device key "similar to SSH". | carried | KM-27 |
| [§2](KONZEPT.md#k-2) | Private org at the first login: genesis gives the founder `OK_1`, `WK_1`. | carried | [§4](#4-who-receives-which-scope-when) |
| [§3](KONZEPT.md#k-3) | Several VMs and devices per workgroup: every device of the workgroup is a `WK` reader. | carried | [§6](#6-where-plaintext-lies), KM-14 |
| [§3 point 3](KONZEPT.md#k-3-b3) | DFX devices by default, own hardware optional: both are devices of §1. | carried | [§1](#1-principals) |
| [§4](KONZEPT.md#k-4) | Own Claude or API credentials under `CK`; DFX access is the operator's token on platform devices. | carried | [§2](#2-keys-per-principal), KM-24 |
| [§5](KONZEPT.md#k-5) | Encryption and trust boundaries: the whole document; points 1–5 in the rows below. | carried | this document |
| [§5 point 1](KONZEPT.md#k-5-b1) | Client-side encryption of prompts, answers, whiteboards, credentials. | carried | KM-22, KM-24 |
| [§5 point 2](KONZEPT.md#k-5-b2) | The hub sees no confidential plaintext (an active hub serving malicious JavaScript remains a residual risk). | carried | [§7](#7-what-the-hub-sees), KM-36 |
| [§5 point 3](KONZEPT.md#k-5-b3) | Decryption only by authorised users and devices; a hub-invented org on a platform device remains. | gap → KOR-55 | KM-05, KM-31 |
| [§5 point 4](KONZEPT.md#k-5-b4) | The chosen execution device sees plaintext. | carried | [§6](#6-where-plaintext-lies) |
| [§5 point 5](KONZEPT.md#k-5-b5) | Derivation, release and device distribution defined. | carried | this document |
| [§6](KONZEPT.md#k-6) | Events encrypted locally, synced encrypted; counters in clear ([A4](KONZEPT.md#a-4)). | carried | KM-22, [§7](#7-what-the-hub-sees) |
| [§7](KONZEPT.md#k-7) | Monitoring: metrics are metadata, no keys. | none | – |
| [§8](KONZEPT.md#k-8) | Persons at org and workgroup level: `accept`/`admit` and `grant` entitle. | carried | [§4](#4-who-receives-which-scope-when) |
| [§8 point 1](KONZEPT.md#k-8-b1) | Adding persons at both levels entitles them to the scopes. | carried | [§4](#4-who-receives-which-scope-when) |
| [§8 point 2](KONZEPT.md#k-8-b2) | Member = read (org scope), admin = write. | carried | [§3](#3-scopes) |
| [§8 point 3](KONZEPT.md#k-8-b3) | Default workgroup write "by default": today not reducible. | gap → KOR-55 | KM-32 |
| [§8 point 4](KONZEPT.md#k-8-b4) | Rights enforced in the backend and by every reader. | carried | KM-07, KM-12 |
| [§8 point 5](KONZEPT.md#k-8-b5) | Sessions read within their rights, no blanket view of other workgroups. | carried | [§8](#8-reading-across-workgroups), KM-34 |
| [§8 point 6](KONZEPT.md#k-8-b6) | Reads logged; reads of plaintext happen on the device and are logged there. | carried | [§8](#8-reading-across-workgroups) |
| [§9](KONZEPT.md#k-9) | GitHub token under `CK`, used only on the device. | carried | KM-13, KM-24 |
| [§10](KONZEPT.md#k-10) | Sessions execute nothing and hold no keys. | carried | KM-23, KM-37 |
| [§11](KONZEPT.md#k-11) | Operation parameters encrypted, dedupe tags from `dk_e`. | carried | [§3](#3-scopes) |
| [§12](KONZEPT.md#k-12) | Whiteboards per level: org whiteboard under `OK`, workgroup and task whiteboards under `WK`; pings are content-free. | carried | [§3](#3-scopes) |
| [§13](KONZEPT.md#k-13) | Other open decisions: no key bearing. | none | – |
| [§13 point 1](KONZEPT.md#k-13-1) | Passkey-based key management and distribution of decryption rights. | carried | this document |
| [§14](KONZEPT.md#k-14) | Repositories. | none | – |
| [§15](KONZEPT.md#k-15) | The extensions 26–47: one row per idea below. | none | – |
| [§16](KONZEPT.md#k-16) | Other final decisions. | none | – |
| [§16 point 7](KONZEPT.md#k-16-b7) | "Synchronisation and key model" still to be checked: this document; cross-device resume needs the `WK` on the second device. | gap → KOR-58 | [§11](#kor-58--resume-after-a-device-failure) |
| [Idea 26](KONZEPT.md#k-idee-26) | Subagents are platform sessions: no keys, rights of the parent ([A1](KONZEPT.md#a-1)). | carried | [§1](#1-principals) |
| [Idea 27](KONZEPT.md#k-idee-27) | Test VMs hold no keys; results stored as workgroup content. | carried | KM-37 |
| [Idea 28](KONZEPT.md#k-idee-28) | Browser VMs hold no keys. | carried | KM-37 |
| [Idea 29](KONZEPT.md#k-idee-29) | Support release with cryptographic rights and revocation. | carried | KM-33 |
| [Idea 30](KONZEPT.md#k-idee-30) | Encryption by default, passkey the only visible release, admins read every workgroup, inheritance and distribution described; revocation of the agent and of support built; read plaintext stays. | carried | KM-11, KM-30, KM-33, [§5](#5-revocation-and-rotation-case-by-case) |
| [Idea 31](KONZEPT.md#k-idee-31) | Humans, main agents and subagents distinguishable: humans and devices are principals with keys, main agents and subagents are platform sessions without keys; names and GitHub mapping are not keys (KOR-52, KOR-47). | carried | [§1](#1-principals) |
| [Idea 32](KONZEPT.md#k-idee-32) | Autonomous-first: catch-up needs no human online. | carried | [§4](#4-who-receives-which-scope-when) |
| [Idea 33](KONZEPT.md#k-idee-33) | Analysis session reads authorised logs: a platform session, same rules. | carried | KM-23 |
| [Idea 34](KONZEPT.md#k-idee-34) | One delegated orchestrator with own identity, passkey approval, audit, revocation; local or on DFX hardware. | carried | KM-28, KM-29, KM-30, KM-39, KM-41 |
| [Idea 35](KONZEPT.md#k-idee-35) | Own hardware for a workgroup or the whole org: the device necessarily holds the keys of what it serves, which is data access of the device (not of people). | decision needed | [§6](#6-where-plaintext-lies) |
| [Idea 36](KONZEPT.md#k-idee-36) | Every writer can continue a session: writers hold `WK`, their prompts are signed by themselves. | carried | KM-12, KM-22 |
| [Idea 37](KONZEPT.md#k-idee-37) | GitHub identities of the org: company credentials live in the organization's `cred` scope, readable by its devices only (KOR-47). | carried | KM-35 |
| [Idea 38](KONZEPT.md#k-idee-38) | Device choice: only devices assigned to the workgroup hold its key. | carried | KM-14 |
| [Idea 39](KONZEPT.md#k-idee-39) | Cleanup: keys stay on the device; the VM held none. | carried | KM-37 |
| [Idea 40](KONZEPT.md#k-idee-40) | Resume on another VM: context checkpoints under a key from `WK`. | carried (pending KOR-33) | [§3](#3-scopes) |
| [Idea 41](KONZEPT.md#k-idee-41) | PR conflict fix by resume; PR → task tag from `WK`. | carried (pending KOR-33) | [§3](#3-scopes) |
| [Idea 42](KONZEPT.md#k-idee-42) | Roles restrict operations, not keys. | none | – |
| [Idea 43](KONZEPT.md#k-idee-43) | Fresh context per task; "authorised platform data can still be retrieved" across workgroups. | carried | [§8](#8-reading-across-workgroups), KM-34 |
| [Idea 44](KONZEPT.md#k-idee-44) | Resume after device failure needs a second device that holds the workgroup's key. | gap → KOR-58 | [§11](#kor-58--resume-after-a-device-failure) |
| [Idea 45](KONZEPT.md#k-idee-45) | Free with own resources. | none | – |
| [Idea 46](KONZEPT.md#k-idee-46) | Usage counters in clear ([A4](KONZEPT.md#a-4)). | none | – |
| [Idea 47](KONZEPT.md#k-idee-47) | Prepaid credit. | none | – |
| [E-01](KONZEPT.md#e-01) | The full concept is the basis of this concordance. | carried | this section |
| [E-02](KONZEPT.md#e-02) | No upper limit: catch-up and wraps scale with principals, no fixed count. | carried | [§4](#4-who-receives-which-scope-when) |
| [E-03](KONZEPT.md#e-03) | The user creates only a passkey; the agent sets up everything with its own identity and its user's rights. | carried | KM-28, KM-40 |
| [E-04](KONZEPT.md#e-04) | The agent can do everything the user can: as a sub-principal it receives and authors exactly what its user does. | carried | KM-39, KM-40, [§6](#6-where-plaintext-lies) |
| [E-05](KONZEPT.md#e-05) | A missing key is shown as "being unlocked" and delivered by catch-up; a key the device cannot get triggers the assignment. | carried | [§4](#4-who-receives-which-scope-when), [§8](#8-reading-across-workgroups) |
| [E-06](KONZEPT.md#e-06) | Operator GitHub account: a company account of the operator's organization in its device-only `cred` scope (KOR-47, ADR-078 §7); copies for listed users outside it stay in their `CK`. | carried | [§2](#2-keys-per-principal) |
| [E-07](KONZEPT.md#e-07) | Operator subscriptions on platform devices, outside the scope keys. | carried | [§2](#2-keys-per-principal) |
| [E-08](KONZEPT.md#e-08) | Work VM without credentials and keys. | carried | KM-37 |
| [E-09](KONZEPT.md#e-09) | Nothing monitored in the work VM; it holds no platform key. | carried | [§6](#6-where-plaintext-lies) |
| [E-10](KONZEPT.md#e-10) | Egress of work VMs. | none | – |
| [E-11](KONZEPT.md#e-11) | No release as precondition: key distribution needs no confirmation (catch-up); a key a device cannot hold is technically required, so its absence triggers the step (E-05). | carried | [§8](#8-reading-across-workgroups) |
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
| [E-25](KONZEPT.md#e-25) | No unordered protection layers. | none | – |
| [E-26](KONZEPT.md#e-26) | Simple and efficient use by AI agents. | none | – |
| [E-27](KONZEPT.md#e-27) | An agent account without a human: created by invitation, signed in with the agent's own key, no passkey. | carried (pending KOR-62) | [§1](#1-principals) |
| [E-28](KONZEPT.md#e-28) | Personal message of the requester at the consent. | none | – |
| [E-29](KONZEPT.md#e-29) | GitHub with every repository the account can reach (the token stays a `github` credential under the user's `CK`). | none | – |
| [E-30](KONZEPT.md#e-30) | Overview of all own subscriptions (readings only; the hub sees numbers and times, no address or name). | none | – |
| [E-31](KONZEPT.md#e-31) | Every subscription is a `claude_subscription` record under the user's `CK`, logged in and renewed by an eligible device; its use is sealed in the record (ADR-090). | none | – |
| [E-32](KONZEPT.md#e-32) | Working modes chosen per session or task, nothing on the user's computer; the keys a mode needs are decided with that mode (KOR-72). | none | – |
| [E-33](KONZEPT.md#e-33) | Platform operations in parallel, each in its own isolated environment; a mailbox credential belongs to its subscription (KOR-73 decides its keys). | none | – |
| [E-30](KONZEPT.md#e-30) | Self-hosted CI runs through the platform (concept): CI VMs hold no scope key and no device key; the CI app key never lies in a VM or at the hub. | none | – |
| [E-31](KONZEPT.md#e-31) | One shared capacity: a device holds the keys of the workgroups it serves, whatever else runs on it. | none | – |
| [A1](KONZEPT.md#a-1) | Subagents have the parent's rights and no keys. | carried | [§1](#1-principals) |
| [A2](KONZEPT.md#a-2) | Conflict fix by merge commit. | none | – |
| [A3](KONZEPT.md#a-3) | The hardening is built: the agent has its own identity as a sub-principal and is rotated out on revocation (ADR-082); the rekey of the user's identity for copies made before is a decision of the owner. | decision needed | KM-28, KM-30, KM-39 |
| [A4](KONZEPT.md#a-4) | Counters in clear at the hub. | carried | [§7](#7-what-the-hub-sees) |
| [A5](KONZEPT.md#a-5) | Limits as settings. | none | – |
| [A6](KONZEPT.md#a-6) | Test and browser VMs as platform operations, no keys inside. | carried | KM-37 |
| [A7](KONZEPT.md#a-7) | Content of a task on a DFX device lies there in clear; support is different. | carried | [§6](#6-where-plaintext-lies) |
| [A8](KONZEPT.md#a-8) | A test run's log is encrypted workgroup content of the operation; the VM holds no key. | carried | KM-37 |
| [A9](KONZEPT.md#a-9) | One throw-away VM per test run. | none | – |
| [A10](KONZEPT.md#a-10) | Subagents continue: still platform sessions without keys. | carried | KM-23 |
| [A11](KONZEPT.md#a-11) | Reading across workgroups within owner ∩ instructing human. | carried | [§8](#8-reading-across-workgroups), KM-34 |
| [A12](KONZEPT.md#a-12) | Support is its own principal with a platform role; the device is not support. | carried | KM-33 |
| [A13](KONZEPT.md#a-13) | Default-workgroup write is a default an admin can lower to read (no rotation: the member still reads). | gap → KOR-55 | KM-32 |
| [A14](KONZEPT.md#a-14) | No fixed limits. | none | – |
| [A15](KONZEPT.md#a-15) | The instructing human's own credentials are usable on the session's device: a writer of the workgroup has explicit access, so the device is eligible for that human's `CK` – except in an org that admitted the human without consent (then E-05: the connection is asked for). | carried | KM-13, KM-15 |
| [A16](KONZEPT.md#a-16) | Workflow files are not blocked. | none | – |
| [A17](KONZEPT.md#a-17) | Protected branches. | none | – |
| [A18](KONZEPT.md#a-18) | Blocked attempts are logged. | none | – |
| [A19](KONZEPT.md#a-19) | One throw-away VM per CI job. | none | – |

## 11. Gaps and the requirements on the follow-up packages

Each requirement is binding for the named package; the package replaces the `gap` evidence of its rule in §9 with
vectors (in all three verifiers) or tests and sets the concordance row to "carried".

### KOR-55 – Trust anchor of the runner

1. **Anchor for every org a platform device serves** (KM-31): the runner accepts an org copy only if its platform
   access is proven by a root-signed entry it verifies itself, and binds the founder key to it instead of "first use"
   ([ADR-040](adr/040-platform-device-founder-pinning.md) is superseded):
   - private org: the genesis founder `(by, pub)` is an **identity anchor** of [04 §4.8](spec/04-crypto.md#48-platform-log-new)
     – a `user_certify` of exactly these keys, or the `user_invite` whose invited user ID is the founder **together with
     an `id_sig` of (founder, pub) under that invitation's `pk_inv`** (carried by the org, e.g. in the genesis or in a
     first log entry). An invited user ID alone is not enough: a hub could reuse it with keys of its own;
   - team org: an `org_platform_access` entry granting it (or the root-signed switches and lists of KOR-49).
   `genesis.private`, which only the hub binds to an invitation, no longer suffices; KM-09 then covers private orgs
   too.
2. **Vectors** (all three verifiers): an invented private org (genesis `private:true` without anchor) refused; an
   anchor with the founder's user ID but other keys refused; a swapped founder refused; an anchored private org and a
   team org accepted; an old head on a platform device refused; a session whose task owner the hub binds to a principal
   that is not a writer of the workgroup refused (the "foreign owner binding" of KOR-55).
3. **Default workgroup reducible** (KM-32): a log entry (or an extended `grant` on the default workgroup) sets a member
   to `read` there. R stays **empty** (the member keeps reading; `R` is about losing read); E5 at `lh` and at receipt
   refuses its writes. The default for new members stays `write`. Vectors for the entry and E5.
4. **Hub key pinning**: platform devices pin the hub response key from `/v1/config` on first use. Either anchor it in
   the platform log or state the residual risk in [04 §9](spec/04-crypto.md#9-threat-model).

### KOR-56 – Support as a principal

**Status (KOR-56, [ADR-096](adr/096-support-release.md)):** built – points 1–6 with KM-33. Decisions where this
section left a choice: releases by an admin or a writer of the workgroup; names through a `support.info` event under
`WK` (no `OK`); support writes prompts and the workgroup whiteboard only; a person's role end owes the rotation of the
released workgroups whose current epoch it held (derived by the hub, residual stated in ADR-096 §3).


1. **Principal** (KM-33): a support person is a user identity (passkey, PRF) with a root-signed platform role
   `support` in the platform log (not a hub table: the runner and every browser must verify it).
2. **Release to the team, keys per person:** the workgroup is released to "DFX support" ([idea 29](KONZEPT.md#k-idee-29)),
   not to a named person: a membership-log entry `support_grant {wg, access read|write}` by an admin or a writer of the
   workgroup (decide in the ADR) and `support_revoke {wg}`. Entitlement = valid support role ∧ active release, so the
   wraps go **to each support person** (W2 and catch-up know the rule): a person who gets the role later receives the
   keys of existing releases by catch-up, and no shared support key exists that would have to be replaced for
   everybody when one person leaves.
3. **What support gets:** the released workgroup's `WK` only – never `OK` (it carries every workgroup name, all member
   profiles and the org whiteboard) and never `CK`. The support view therefore needs what it shows in the release
   itself (workgroup name, the display names of the workgroup's members, sealed under that `WK` by the releasing
   client) or shows authors by their short ID mark – decide in the ADR.
4. **Revocation:** `support_revoke`, and the end of a person's role (`role_revoke`), put that `WK` into R for the
   affected person(s), in the same atomic batch; a role revocation marks the affected orgs `rotation_pending` like a
   decertification. Personnel change rotates only the workgroups that person could read.
5. **Audit:** every read and write of support is in the read log and the audit with actor "support <name>" (§8 point 6);
   the org sees it.
6. **Vectors:** grant and revoke entries, the forged entry by the hub, a grant without a valid role at the reader,
   W2 for a support recipient, a support wrap without a release (KM-06 holds), R on revoke and on role end, E5 for
   support writes.

### KOR-57 – Own identity of the agent, real revocation, orchestrator on DFX hardware

**Status (KOR-57, [ADR-082](adr/082-agent-own-identity.md)):** points 1–3 and 7 built (KM-39, KM-40, KM-30); point 4
built as an automatic conversion whose residual (a copy made earlier) is stated; point 5 evaluated and not built – a
decision of the owner ([E-25](KONZEPT.md#e-25)); point 6 built with the orchestrator session (stage b).


1. **Sub-principal** (KM-30): the agent gets its own identity as a sub-principal of its user in every org log the user
   is in, appended with the user's signature (passkey gesture at pairing); it is entitled to exactly what its user is
   entitled to (`OK`, `WK` incl. inheritance, and the user's `CK`) and to nothing else – in particular not to a support
   release of its user (support is a separate principal). The rule "the active agent of U receives what U receives"
   must be one function in all three verifiers, used by W2, catch-up, R and the batch rule "every remaining reader has
   a wrap".
2. **"Exactly one"** in the logs: at most one active sub-principal per user per org; a new one replaces the old one in
   one batch, which carries R for the old one.
3. **Revocation:** removal of the sub-principal puts every org and workgroup scope it could read into R (atomic
   batch). `CK` stays outside R (KM-20 holds): the user's browser rotates `CK` in the same flow as a separate step. The
   vector `principal_outside_log_is_no_recipient` keeps holding; add "a revoked agent is no recipient".
4. **Agents paired so far held the user's identity** (§5): rotating scopes does not shut them out, because new epochs
   are wrapped to that same identity. For every migrated agent the **rekey of the user's identity (point 5) is
   mandatory**; until then the migration must say that the old agent copy keeps its reach.
5. **Identity rotation of the user** (`rekey`: new `pub` signed by the old and the new key, R = every scope of the
   user, the bundle slots resealed, a passkey gesture): required, not optional – it is also the only answer to a lost
   unlocked device, a lost remembered browser, a used recovery code and the agent copies of point 4 (§5 "leaked user
   identity").
6. **On DFX hardware**: the key pair is drawn in the session VM ([11 §11](spec/11-agent-access.md)); KM-37 gets the
   stated exception "the orchestrator session's own agent key". Such a session opens every scope of its user, so its
   VM holds plaintext of all of the user's organizations; its context checkpoints (KOR-33) must therefore **not** be
   sealed under the workgroup key of the workgroup the session runs in (every reader of that workgroup could open
   them) but under a key only the user (and its agent) holds, or not be written.
7. **Vectors**: sub-principal entry, forged by the hub, entitlement inheritance, rotation on revocation, replacement,
   rekey.

### KOR-45 – Reading across workgroups

The requirements of [§8](#8-reading-across-workgroups) points 1, 2 and 4–6 (KM-34); point 3 only after
[decision D3](#decisions-for-the-orchestrator). Built by KOR-45 with way A ([ADR-089](adr/089-sessions-read-like-their-human.md));
the intersection is evaluated only by the runner (the hub never evaluates a session read), so its cases are a runner
test, not a shared vector.

### KOR-47 – Credentials of the organization

1. **Scope** (KM-35): a company credential (an account of the organization, also the operator account of E-06) cannot
   be sealed under `OK` (every member holds `OK`, so every member's browser could read the token) and not under one
   user's `CK` (it must survive that user). As KOR-47 says ("only the execution device opens them"): an org credential
   scope whose **readers are devices only** – the devices assigned to any workgroup of the owning org and, for the
   operator account used for other organizations' tasks, the platform devices that the root-signed list of KOR-49
   allows; admins write a new version by sealing it to these devices and cannot read it back. Rotation when a device
   stops being a reader.
2. **Vectors** for readers, W2, R and the record format.

Built by KOR-47 ([ADR-078](adr/078-github-identities.md), KM-35): readers are the organization's assigned member
devices, admins wrap every new epoch and write each version under a fresh epoch; the operator account for other
organizations' tasks is used only by devices that are readers of the operator's organization (the root-signed list
stays KOR-49's).

### KOR-41 – Organization-wide device assignment

An org-wide device is a reader of every `WK` incl. later workgroups and of the `CK` of every member with explicit
access somewhere (consented orgs only); `wg_create` wraps `WK_1` for it; removing the org-wide assignment puts every
`WK` it read and `OK` into R. Vectors for these four points. The design on the branch meets them. The assignment
dialog must say that the device can then read every workgroup (today it says only "The device can then run tasks of
this workgroup").

### KOR-58 – Resume after a device failure

A restoring device must hold the session's `WK` (context checkpoints of KOR-33 are sealed under a key from it). A
second device that should take over must be assigned to the workgroup **before** the failure, or the session waits
for catch-up from another online holder (the failed device may have been the only one). KOR-58 states which; the
scheduler never places a resumed session on a device without the key, and the waiting state says why (E-05).

### Further notes

- **KOR-26 (community):** when merged, add its rules (anonymous request, key check, per-user rejection) to §9 with its
  vectors and remove "pending" in §3 and §10.
- **KOR-33 (context checkpoints):** they carry whatever the session read (§8 point 4, KOR-57 point 6); KOR-33 is the
  place where the checkpoint key is chosen.
- **PRF on real devices:** the procedure [manual-checks/prf-devices.md](manual-checks/prf-devices.md) is open; until it
  is done, KM-02 is verified with virtual authenticators only.

### Decisions for the orchestrator

- **D1 – Idea 35 and the device's keys.** "Die Zuweisung steuert Rechenkapazität, nicht Datenzugriff" cannot hold for
  the device itself: whatever runs on it is plaintext there (A7, technically required). Proposed interpretation for
  part C: the assignment gives no **person** access to data; the assigned device holds the keys of what it serves, and
  the assignment dialog says so.
- **D2 – Revoking an invitation whose package was looked up.** The package carries the current `OK` and `WK` of the
  invited and default workgroups. The hub knows whether a lookup happened. Options: (a) accept as residual risk (code
  leaks are rare; the accept is visible), (b) a revocation after a lookup triggers the rotation of those scopes in the
  same batch. Recommended: (b) as a small Spur-K item.
- **D3 – Read release per task for KOR-45 (way B of §8 point 3).** Wanted, or is way A (assignment) enough?
  Recommended: way A now; way B only if a real case needs a session to read a workgroup its device must not serve.

## 12. Order of the cryptography packages

Current dependencies → proposed:

| Package | Current dependencies | Proposed |
|---|---|---|
| KOR-55 | KOR-54, KOR-49, KOR-41, KOR-42 | unchanged |
| KOR-57 | KOR-55, KOR-42, KOR-33 | unchanged; the rekey (point 5) becomes part of the package, not optional |
| KOR-56 | KOR-57, KOR-26 | unchanged |
| KOR-45 | KOR-46, KOR-49, KOR-39, KOR-41, KOR-35, KOR-54, KOR-60 | unchanged; its acceptance test "session in WG A reads WG B" needs a device assigned to both (KOR-41) under way A |
| KOR-47 | KOR-25, KOR-42 | **add KOR-54 and KOR-55**: the org credential scope is Spur-K work in the same verifier files; or split the scope into its own Spur-K item before KOR-47 |
| KOR-58 | KOR-46, KOR-41, KOR-43 | unchanged; note the key requirement above |

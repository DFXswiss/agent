# ADR-058: Organization admins read every workgroup; key handling stays out of sight

Status: Accepted (2026-11-23, after an independent review of the draft: cryptography, rights, privacy)

## Context
- Concept idea 30: "Organization administrators automatically get read access to all workgroups. Inheritance of
  rights, key distribution and revocation need a concept. […] Key management stays invisible. […] Plaintext already
  copied cannot be revoked afterwards." Concept §8: an org member reads, an admin writes; rights are enforced in the
  backend and in the user interface.
- What was built is the opposite: "an admin … has no implicit workgroup access, except the creating admin" (the three
  verifiers `agent_hub.log.state`, `agent_runner.crypto.log.state`, `web/src/core/log/state.ts`; 04-crypto.md §4.7;
  the workgroup screen "Other admins get no access unless you choose them here"; E2E-18). Nobody on the owner's side
  decided that; the concept applies (found by an audit of the build against the concept).
- Consequence of the old rule beyond reading: when the only key holder of a workgroup leaves, no admin can grant it to
  anybody (a grant needs the key).
- The user interface also speaks of keys, rotation, wrapping and catch-up ("Rotating keys 2/5", "Wrapping for 7
  recipients", "Key rotation required in …", "You hold the key of no workgroup …"), which users neither understand
  nor need to act on.
- Constraints: the hub never sees a scope key in plaintext (ADR-003); the hub, every browser and every runner apply
  the same rules to the same signed membership log and the shared vectors (04-crypto.md §10); the platform is in
  production with real users, so hub, web and runner must be deployable in mixed versions without locking anybody
  out; existing organizations must work without anybody doing anything.

## Decision

### 1. Rights: one derived read right, three sources
The workgroup access of a user is the **effective** access, computed by the same function in all three verifiers:

| Source | Access | Lost by |
|---|---|---|
| default workgroup (every member) | write | leaving / removal |
| explicit (`invite.wgs` at `accept`, `grant`, the creating admin at `wg_create`) | read or write | `ungrant`, removal |
| **inherited: active admin of the org** | **read** on every workgroup of the org (archived ones too, they stay readable) | `role` → member, removal, leaving |

- Effective access = explicit access if there is one (write beats the inherited read), else `read` for an active
  admin, else none. `OrgState.wg_access` returns the effective access; `explicit_access` the old value and
  `inherited(user, wg)` tells that the access comes from the admin role only.
- The inherited right is **read only**: tasks, chats, whiteboards, terminal, overviews and the audit of the
  workgroup. Writing (prompts, task titles, whiteboard entries, workgroup settings) still needs explicit `write`, as
  concept §8 says ("Administrator = Schreiben" is about the organization: management, members, devices, org
  whiteboard). An admin who wants to write grants itself `write` with a visible `grant` entry, as before. Nothing in
  the concept or in a decision of the owner asks for more.
- Explicit memberships are independent: a demoted admin keeps every explicit grant; an `ungrant` of an admin's
  explicit access leaves the inherited read. A `grant` of `read` to an admin makes the access explicit (it then
  survives a demotion).
- **The credential scope does not inherit.** The `cred` readers (W2 for `CK`, `credentials.eligibility`, the
  subscription polling) keep using the explicit access: a device is eligible for a user's credentials only through a
  workgroup in which the user can start sessions. An admin's Claude or GitHub token therefore does not spread to every
  device of the organization, and a demotion never forces a `CK` rotation.
- The user's agent (KOR-24) reads through the hub's rights API and inherits what the user may read; it gains no write.
  **Platform sessions do not inherit:** a Claude Code session runs in one workgroup and acts in the context of its
  assigned rights (concept §8, "no blanket view into other workgroups"); the runner's session checks and session
  tools (owner check B2, harness, `list_workgroups`, `list_members`, read scope) keep using the explicit access, and
  the hub never starts a session for an owner who only reads (prompts need `write`).
- **Anchor for support (idea 29, own package):** the derived right is one function with a list of sources
  (default, explicit, admin). A support release will be one more source with its own log entries (release with read or
  write, revoke) and will reuse everything below unchanged: R for revocation, W2/catch-up for distribution, the hub
  projection with its `inherited` marker generalised to a `source`.

### 2. Key distribution: the log entitles, every holder delivers
Entitlement to a scope key is the read right (04-crypto.md §6.3), so admins are entitled to every `WK`. Nothing new is
needed on the wire; the existing mechanisms deliver:

- **New workgroup:** the creating admin's batch must already contain wraps of `WK_1` for every reader (batch rule 4),
  which now includes every admin. The creating browser has the fresh key and wraps it (HPKE to each admin's X25519
  key from the verified log); the hub refuses a batch without them (`rotation_required`).
- **Admin appointed (`role` → admin):** the appointing browser wraps every epoch it holds of every workgroup the new
  admin reads by inheritance in the same batch (`buildBatch`, like a grant "with history"). An admin invitation's
  package stays as it is (default workgroup and the invited workgroups): an intercepted code opened without `accept`
  must not reveal more keys than before; the keys follow by catch-up after the visible `accept`.
- **History:** an admin reads the whole workgroup. Catch-up therefore gives a recipient that reads a workgroup by
  inheritance **every** epoch from 1 to the current one (`catch_up_epochs`, the same function in hub, runner and web;
  for everybody else catch-up stays at the current epoch). A wrapper offers only epochs it physically holds.
- **Existing workgroups and existing admins (back-fill), and everything the appointing browser could not wrap:**
  catch-up (04-crypto.md §5.11). Every holder – member browsers on every sync, assigned runner devices on every
  reconcile run (platform devices are always online), other admins – computes from the verified log the entitled
  recipients without a wrap and uploads wraps it may create (W1/W2). The hub stores a wrap only if W1/W2 hold in its
  own log state (W3) and serves it to the recipient; recipients re-check W1/W2. The hub sees only HPKE ciphertext and
  signatures; it never holds or derives a scope key. No new endpoint, no migration of key material.
- **Until it has happened:** the admin sees the workgroup, its name, tasks and members (metadata the hub serves by
  right) and, instead of content, one calm line: "Being unlocked – the content appears as soon as a member or device of
  this workgroup has been online." No error, no code, nothing to do. It disappears with the key of the current epoch.
  If the verified log shows that no active principal besides admins can still hold the key (no explicit member, no
  assigned device, the epoch's author gone), the line says so honestly instead: older content of this workgroup can no
  longer be opened, and new content becomes readable shortly: the rotation job of an admin's browser (06-web.md job
  4) rotates such a workgroup silently, so the new epoch is wrapped for every reader. The rotation loses nothing:
  whoever still holds an older epoch (e.g. an admin the log cannot tell from the others) keeps delivering it by
  catch-up. Archived workgroups are not rotated.
- **Admins become holders:** an admin holding a `WK` may wrap it for others (W2). Grants, device assignments and
  catch-up keep working for epochs an admin holds even when the last explicit member has left.
- **Transition rule for devices (until every runner verifies ADR-058):** a runner of an older version does not count
  an admin as a holder of an epoch it did not create, ignores such a wrap to itself, and the hub keeps the first wrap of
  a slot – the device would miss that epoch. Platform devices are updated before the hub (§6); devices that users
  enrolled themselves are not under the operator's control. So a browser does not wrap a `wg` key **for a device**
  when its user reads that workgroup only by inheritance and did not create the epoch (`compatSkip` in the web
  catch-up and `buildBatch`); explicit members, other devices and the epoch's author deliver instead. The rule only
  withholds wraps the verifiers allow; it can be removed once every enrolled device reports a version with ADR-058.

### 3. Revocation: the existing rotation obligation, silently
R(state before, state after, subject) (04-crypto.md §4.7) is computed from the read right, so it now covers the
inherited read with no new rule:

- **Demotion** `role` admin → member: R = every workgroup the subject read only as admin. The demoting admin's browser
  sends `role`, one `rotate` per element of R and the wraps of the new epochs for every remaining reader in one atomic
  batch; the hub computes R itself and refuses anything else (`rotation_required`, as for `remove`/`ungrant`). The
  demoted user keeps the explicit workgroups and the default workgroup.
- **Self-demotion** (an admin sets its own role to member) is treated like leaving: the subject cannot rotate the
  workgroups it no longer reads, so R becomes `pending` (`OrgState.pending`, hub `rotation_pending`) and the remaining
  admins or writers rotate in the background (06-web.md job 4). The same holds, as before, for an admin who leaves.
- **Removal** of an admin: R already contains everything the subject could read, now including the inherited
  workgroups.
- The hub refuses the subject immediately (rights projection), new content is encrypted under keys the subject never
  receives. Content the subject has already read or copied cannot be taken back (concept idea 30); the confirmation
  dialog says so in one sentence.
- All of this runs without dialogs about keys: the browser shows "Saving …" while the batch is built; pending rotations
  run silently; the in-app notification "Key rotation required" is no longer created (an existing row renders as a
  neutral "Access in … is being updated").

### 4. Invisible key handling
Texts about keys, rotation, wrapping, epochs and catch-up leave the user interface (de, en); where the user really
has to do something, one sentence in everyday language replaces them. The security page (`/security`) keeps its
technical explanation. The list of changed texts is in the report of KOR-34. The workgroup creation note says "Every
admin of the organization can read it. You can write."; the members screen shows "reads as admin" for an inherited
access; granting a workgroup no longer requires the granting admin to hold its key (the key holders deliver it).

### 5. Hub projection and audit
- `hub.wg_access` (the effective access) gets the column `inherited boolean NOT NULL DEFAULT false` (migration
  `0011_wg_access_inherited`). The incremental projection and the rebuild write inherited rows like every other
  access row (`granted_seq` = the entry that made the access effective in its current form, `revoked_seq` at a
  demotion). The migration back-fills the rows of today's admins as the rebuild writes them (its test checks the
  back-filled rows of a seeded org row by row), so a deployment needs no extra command.
- Rights, streams, audit query, whiteboards, notifications (a whiteboard entry sent with "notify" reaches every
  reader of its level, admins included), workgroup lists and monitoring read the effective access unchanged;
  credential eligibility and subscription polling add `NOT inherited`.
- Every change is a signed log entry (`role`, `grant`, `rotate`, …) that every client verifies; the hub audits each
  append (`log.append`) and every read of a workgroup by an admin like any read (concept §8).

### 6. Mixed versions and order of deployment
The rule changes derived state only: every log that was valid stays valid for old and new verifiers; no entry type, no
field and no wire format changes.

| Combination | Effect |
|---|---|
| new runner, old hub | the runner also plans wraps for admins; the old hub refuses them (`wrap_unauthorized`); the runner isolates refused wraps by halving. A refused slot is retried after a log change **or after 15 minutes** (new in the runner catch-up), so after the hub deployment the back-fill starts by itself. No effect on anything else. |
| old runner, new hub/web | an old runner does not count an inheriting admin as a holder; the transition rule of §2 keeps browsers from wrapping such keys for devices, so no slot is taken by a wrap the device would ignore. Platform devices are updated first anyway. |
| old browser tab, new hub | every batch whose rules changed is refused with `rotation_required` until the tab reloads: creating a workgroup, any `rotate` (manual, pending, after `remove`/`ungrant`/`device_unassign`; the new epoch needs wraps for every admin), removing or demoting an admin, ungranting an admin's explicit access. An old tab cannot build the new batch (it retries only `seq_conflict`), so the user sees the action fail with the generic error and a reload fixes it. Sign-in, reading and writing content are unaffected. An old tab ignores wraps for its user made by an inheriting admin until reloaded. |
| new hub, existing data | the migration back-fills `hub.wg_access`; logs replay unchanged. A historical self-demotion or leave of an admin would now owe a rotation of the workgroups it read only as admin (pointless, since it never held those keys, but harmless: one silent rotation each; and admins get history, so nothing becomes unreadable). Production data did not contain such an entry when this was decided. |

Order: (1) runner (verifier, vectors, refusal retry), (2) hub with its web build and the migration (one deployment),
(3) nothing else: the back-fill of the keys runs by itself within 15 minutes through the assigned devices, or at the
next sign-in of an explicit member. Optionally `agent-hub rebuild-projections` after the migration makes the
projection byte-equal to a rebuild (it adds historical, revoked rows no right reads).
Rollback of the hub: run `agent-hub rebuild-projections` with the previous build **before** it takes traffic (drops
the inherited rows; the column stays and is ignored). Without it the old hub would count inherited rows for credential
eligibility, and its upsert, which does not reset `inherited`, would fail a `grant` of `write` to an admin on the
column's check. Residual risk of a rollback: keys already delivered stay with the admins, and an old hub
computes R without the inherited read, so a demotion made after the rollback rotates nothing and the demoted admin
keeps reading new content of those epochs until the next rotation; an admin can make it rotate by ungranting a member or unassigning a device.

## Consequences
- Every admin of an organization reads every workgroup without anybody doing anything, including existing workgroups;
  every admin of an existing organization sees all its workgroups.
- Admins become key holders: grants, device assignments and catch-up keep working after the last explicit member
  left.
- Demoting an admin costs one rotation per workgroup the admin read only as admin (run by the demoting browser, or in
  the background after a self-demotion).
- More recipients per workgroup key (one wrap per admin and epoch); negligible at the sizes of the concept.
- An admin's credentials do not spread with the inherited read; write still requires an explicit grant.
- Vectors, the three verifiers, the hub projection, the web texts and E2E-18 change together; the old statement "an
  admin has no implicit workgroup access" is gone from code, spec and tests.

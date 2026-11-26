# ADR-096: Release of a workgroup to DFX support – a root-signed support role, keys per person, revocation with rotation

Status: Accepted (2026-11-25, KOR-56; refined after the design review with fresh context, §7). Implements concept [idea 29](../KONZEPT.md#k-idee-29) ("Nutzer können eine
Workgroup dem DFX-Support mit Lese- oder Schreibrechten freigeben und die Freigabe widerrufen. Ohne Freigabe hat
Support keinen Zugriff. Kryptografische Berechtigungen und Audit-Logging gelten auch hier."), the last sentence of
[idea 30](../KONZEPT.md#k-idee-30) for support, [§8 point 4](../KONZEPT.md#k-8-b4) (rights enforced at the hub and in
the client) and [§8 point 6](../KONZEPT.md#k-8-b6) (reads are logged), and interpretations
[A7](../KONZEPT.md#a-7) and [A12](../KONZEPT.md#a-12). Respects [E-04](../KONZEPT.md#e-04),
[E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19). Key model: `key-model.md` §11
"KOR-56" and rule KM-33. Builds on ADR-023 (platform root), ADR-058 (admins read every workgroup), ADR-060 (root
certification of a user's identity keys) and ADR-082 (one entitlement function in three verifiers).

## Context
- Until now nobody outside an organization could read one of its workgroups: entitlement to a scope key is a function
  of the verified membership log of the org (`OrgState.can_read` → `readers`), and only principals of that log
  (users, devices, agents) are in it. "DFX support" did not exist as a principal (key model §1, gap KOR-56).
- A support person is a human working for the operator. It must be able to help a customer **only** after the
  customer released a workgroup, only in the scope of that release (read, or read and write), and must lose access to
  everything written after a revocation. Which persons are support is the operator's decision (the platform root),
  not the customer's – staff come and go, and a customer cannot be asked to re-release every workgroup when they do.
- The execution device of a task (an org device or a DFX platform device) is something else: it holds the plaintext
  of what it runs because it runs it ([A7](../KONZEPT.md#a-7)). This ADR does not change devices.

## Decision

### 1. Principal: a user with the root-signed platform role `support`
- A support person is an ordinary **user account** (passkey with PRF, its own identity `pub{kem,sig}`, its own
  private organization) that holds the platform role `support`: a `role_grant {subject, role: "support"}` in the
  **platform log**, signed by the platform root. `support` joins `inviter` and `operator` in the closed list of
  platform roles. The hub cannot create the role (P4), and every verifier – hub, runner, browser – reads it from its own
  verified platform log.
- **Keys come from the root, not from the hub.** A `role_grant support` is bound to the root certification of the
  person's identity keys that is valid at that entry (`user_certify`, ADR-060); without one it is refused
  (`log_rights`, platform type rule; a second grant while one is active as well). The person stops being support when
  the role is revoked (`role_revoke support`) or the bound certification ends (`user_decertify`); a renewed
  certification needs a new grant (revoke, grant). The **support roster** of a platform state is
  `{user → pub{kem,sig}}` of every user whose role is active and whose bound certification is still its valid one; the
  keys are those of that certification. A hub that swaps a support person's keys is caught by every verifier. The
  **support authors** (history) are every user ever bound, with the keys of its latest bound certification.
- The operator grants and ends the role with the operator CLI: `agent-hub platform certify-user --user ID --code …`
  (if not yet certified), then `agent-hub platform grant-role support --user ID --name NAME` and
  `revoke-role support --user ID`. The package builds the role but gives it to nobody (who gets it is the owner's
  decision).
- **Display name.** `--name` stores the person's display name at the hub (`hub.support_person`, root-signed request
  `PUT /v1/platform/support/{user}`): the hub sees the names of support staff in clear (they are the operator's own
  staff, not customer content; listed in 04 §7). The name is shown next to the ID mark `#XXXX` derived from the user
  ID, so a name the hub changes cannot hide which certified person acted.

### 2. Release to "DFX support", keys to each person
- **Two new membership-log entries** (04 §4.7): `support_grant {wg, access: read|write}` and `support_revoke {wg}`.
  The workgroup is released to the support **team** (idea 29: "dem DFX-Support"), never to a named person. State:
  `OrgState.support: wg → (access, seq)`.
- **Who.** An active admin of the org or a user with effective `write` on the workgroup (explicit write, or a member
  of the default workgroup); an agent acts with its user's rights (`acting`). A writer can already copy everything in
  the workgroup, so requiring an admin would be an unnecessary restriction ([E-11](../KONZEPT.md#e-11)); a reader may
  not give away more than it may do. The same principals revoke; a revocation needs an active release.
- **Rules (L5, `log_rights`).** `support_grant`: the workgroup exists and is not archived, and the access differs from
  the current release (a new `support_grant` changes read ↔ write). `support_revoke`: an active release exists
  (archived workgroups included).
- **Entitlement** – one function `support_access(state, roster, person, wg)` in all three verifiers: the person is in
  the roster **and** the workgroup has an active release; `write` additionally needs `access = write` and a writable
  workgroup. A support person is a reader of the **workgroup scope only**: never `OK` (it carries every workgroup
  name, all member profiles and the org whiteboard), never a user's `CK`, never an org `cred` scope, never the dedupe
  or context keys beyond what `WK` derives. Agents of members never receive a release (support is a separate
  principal, ADR-082 §1); a support person acts only in person (§6).
- **Wraps per person, no shared support key.** W2 accepts a wrap of `(wg, W, e)` to a support person for every epoch
  `1 ≤ e ≤ current` while the release is active and the person is in the roster of the verifier's platform state; the
  wrapper is a holder of that epoch from the org (support persons never wrap, so the wrapper lookup is unchanged).
  The releasing client wraps every epoch it holds to every person of the roster in the granting batch; **catch-up**
  (`missing_wraps`) adds, for every released workgroup, every epoch to every roster person without a wrap – so a person
  who gets the role later receives the keys of every existing release from the next online holder (a member's browser
  or the runner of an assigned device), and nothing has to be re-released when staff changes.
- **The batch rule is unchanged:** a new epoch (`rotate`, `wg_create`) must be wrapped in its batch for every reader
  of the org log; support persons receive it by catch-up. The roster lives in another log and changes independently of
  the org; making the org's batch depend on it would let a concurrent role change refuse unrelated batches.

### 3. Revocation and the end of a person's role
- **`support_revoke` rotates.** `R(support_revoke {wg}) = {(wg, wg)}`, always – computed from the org log alone, so
  every verifier agrees whatever its platform head. The revoking client sends `support_revoke`, `rotate` and the wraps
  of the new epoch for every remaining org reader in **one atomic batch** (`rotation_required` otherwise), exactly like
  an `ungrant`. Afterwards no verifier accepts a wrap of the new epoch to a support person, and the hub refuses every
  request of support to the workgroup at once.
- **Write → read** rotates nothing (losing only the write right never does, 04 §4.7); E5 refuses support writes from
  the entry on.
- **A person leaves support** (`role_revoke support` or `user_decertify`): the hub refuses the person at once (its
  rights check reads the role from the platform projection); the hub derives the owed rotation from its stored wraps –
  a released workgroup whose current epoch is wrapped to a recipient that is neither a reader of the org log nor in
  the roster – and lists it in `org.rotation_pending` (after the platform-log append and on every org append); the next admin or writer browser rotates them in the background (06 §11 job 4), and the agent tool's
  `pending` job does the same. Only workgroups that person could read rotate; the other persons keep their wraps and
  receive the new epoch by catch-up. **No release has to be given again** – this is why the keys go to each person and
  not to a shared support key.
- **What a revoked person keeps** ([idea 30](../KONZEPT.md#k-idee-30), last sentence): everything it read and every
  epoch it held. The release dialog says so before the release is given.
- **Residual, the same as for a decertified platform device:** the obligation after a role end is computed by the hub
  (`rotation_pending`), and a client's roster is the platform log the hub serves. A hub that withholds the
  `role_revoke` from clients **and** serves the former person anyway can keep it reading new epochs; an honest hub
  refuses the person at once. Anchoring the platform head in the org log (so clients compute the obligation
  themselves) is the hardening for both cases and belongs to the trust-anchor package (KOR-55).
- A release is the organization's: it stays when the user who gave it leaves. A release given again after a revocation
  hands over the whole history again (every epoch), and the dialog says so.

### 4. What support sees and does
- **Release info.** Support never holds `OK`, so it cannot read the names of the org, the workgroup or the members.
  The releasing client therefore writes, in the releasing batch (`events` of the append), one event of the new kind `support.info` (scope `wg`,
  author a user with the release right, header `wg = scope_id`, `task`/`session`/`ref` empty) with
  `{org_name, wg_name, members: {user_id: name}}` sealed under the workgroup key; a later release or "update" writes a
  new one, the newest counts. It is an ordinary event (format 04 §4.3): the hub sees neither content nor names.
- `support.info.members` lists only principals that read the workgroup (explicit members, default-workgroup members,
  admins); the support view shows the event's author next to the names.
- **The support area** (`/support`, only for an account in the roster of its verified platform log) lists the active
  releases (`GET /v1/support/releases`: org, workgroup, access, since) and opens a workgroup in a support view: the
  tasks of the workgroup with their titles and their conversation, read-only for `read`; for `write` the composer of a
  task. Authors are shown by the names of `support.info`, else by their mark. The workgroup whiteboard is not shown
  there (a later view); support reads it through `GET /v1/whiteboards` and writes it with a write release through
  `POST /v1/events`, like any client of the API.
- **What support sees beyond the content:** the org's membership log (IDs and public keys of members and devices,
  roles, workgroup IDs and grants, invitations, releases – no names). User IDs are global; the release dialog says
  that support sees technical identifiers of the members.
- **Hub.** Every right of the vocabulary `wg:read`, `wg:write`, `task:read`, `task:write` (03 §3) is also held by a
  person of the hub's roster projection on a released workgroup in the scope of the release; `org:*` and `device:*`
  rights never are. Two reads outside the workgroup are allowed for verification: the org's membership log (public
  keys, IDs and rights metadata – no names) and the org's catch-up/wraps **addressed to the person**. A workgroup or
  task without a release is `404 not_found` for support, as for any outsider.
- **Writing** (`access = write`): support writes **prompts into existing tasks** and the **workgroup whiteboard**
  (`SUPPORT_KINDS`: `prompt`, `whiteboard.entry|edit|delete` in the workgroup scope) under its own ID and key – no new
  task (whose model access and connections would belong to nobody of the organization) and no workgroup settings
  (repository list, protected branches). E3 maps the author kind `support` to `user` for these kinds only
  (`kind_not_allowed` otherwise, also for `support.info`); E5 checks the release at `lh`, the hub again at receipt with
  its current roster. A member that is also support writes if its member right **or** the release allows it (org log
  first, then roster). Readers take the author's key from the support authors of their platform log; the runner
  refreshes its platform log once before it refuses an unknown author (a prompt is never lost), the browser shows an
  event of a person granted after its last platform-log refresh with the next refresh. A prompt of support runs in the task with
  the task owner's model access and is billed to the organization; the prompt's human (ADR-083, ADR-089) is the
  support person, so the session reads owner ∩ support – at most the released workgroup – and GitHub operations on its
  instruction use only identities the support person may use in that organization (none: they are held, E-05 asks
  the organization).

### 5. Audit, notifications, history
- **Audit** (ADR-063, actor kind `user` – the support person –, shown as "Support <Name> #XXXX"): `log.support_grant`, `log.support_revoke`
  (actor: the releasing user or agent), `support.people` (operator; also in the audit of every org with an active
  release, because its reader set changes), and for every request of a support person that reads workgroup data –
  task list, task, events, whiteboard, the org log – the read-log row of that route (`read.<resource>`, ADR-063 §3)
  with the support person as actor, placed in the organization by the workgroup or task it names; the person's own
  wraps (`GET /v1/wraps`) are an account read without organization. Every event it appends is audited as
  `support.write` (kind, workgroup, task) instead of `event.append`. The organization's audit view lists these rows by
  `org_id` and labels the actor "Support <Name> #XXXX" (the actor is a support person and no member). **The live
  channel stays members-only:** it multiplexes the whole organization (every workgroup, the org scope, member
  presence), so a support person's view reads by polling the workgroup's routes every few seconds – every poll is a
  read in the read log (repeats inside the read window are counted, ADR-063), which covers "inkl. Live-Kanal"
  without opening the org channel to support.
- **Notifications** (ADR-020, in-app) to the org's admins and to the user that released: on a release, on a change of
  its access, on a revocation, on the **first access** of a support person after a release (once per release and
  person), and when a person joins or leaves support while a release is active. Every roster person is notified of a
  new release.
- **The release dialog** (workgroup settings) says, before the release: what support then sees ("Support sieht alle
  Aufgaben, Gespräche und das Whiteboard dieser Workgroup – auch frühere – sowie technische Kennungen der
  Mitglieder; mit Schreibrecht kann Support in bestehenden Aufgaben schreiben."), the names of the current support
  persons, that what support has read stays read after a revocation (idea 30), and that tasks on DFX devices are
  processed there in clear independently of this release (A7).
- **History.** An event that support wrote stays verifiable after the person left support: readers check a support
  author against the support authors (§1), and E5 against the release at `lh`. The hub checks E5 at receipt against
  its current roster, so new writes stop at once. **Residual:** the org log and the platform log cannot be compared,
  so a browser accepts an event of a former support person at any `lh` while a write release is active; a hub
  colluding with that person could inject such events for browsers (unlike a removed member, who fails E5 at every
  later `lh`). The runner – the only reader that acts on events – accepts only the **current** roster for support
  authors, so an injected prompt is never executed. Anchoring the platform head in the org log (KOR-55) closes this
  for browsers as well. A person whose certification was renewed keeps the same identity keys, so its earlier events
  verify under the latest bound certification.
- **Concurrency at the hub.** A batch reads the roster after locking the org row; a platform-log append that ends a
  role marks the orgs with a release under their row locks in sorted order. A first release given concurrently with a
  role end in another org may still store wraps to the leaving person; the hub refuses the person's reads at once and
  `owed` lists the workgroup at that org's next append.

### 6. Agent parity
- The releasing side is complete for agents ([E-04](../KONZEPT.md#e-04)): `agent-platform wg support grant --wg ID
  --access read|write`, `revoke --wg ID`, `list` build the same batches (including the rotation) and the
  `support.info` event.
- The support side is personal in this package: a release is trust in the operator's persons, and the agent of a
  support person is a sub-principal in that person's own organizations, not in the roster. [E-04](../KONZEPT.md#e-04)
  has no exception, so this is **a decision for the owner** (proposed interpretation: a support person's agent may act
  as support under the same roster check, audited as "Support <Name> (Agent)" – a later package); `agent-parity.md`
  lists the rows as `open` with this ADR.

## Threat model (additions to 04 §9)
| Attack | Defence |
|---|---|
| Support asks for wraps without a release (catch-up, `POST` of wraps, guessing workgroup IDs) | Support never wraps; the hub answers catch-up for a person only with slots W2 allows, refuses wraps (`wrap_unauthorized`) and every workgroup route without a release (`404`). Vector `support_wrap_without_release`. |
| The hub adds a support person without a log entry (a wrap to a person for an unreleased workgroup, a release the log does not contain, an event by a support author) | Every verifier recomputes entitlement from the org log and its own platform log: `wrap_unauthorized`, `author_not_writer`. Vectors `support_wrap_without_release`, `support_event_without_release`. |
| A support person writes `support.info`, a task or workgroup settings | E3: `kind_not_allowed`. Vector `support_writes_support_info`. |
| A reader without the person's role in its platform log | No support recipient, no support author: `wrap_unauthorized`, `author_not_writer`. Vector `support_wrap_unknown_to_reader`. |
| The hub forges `support_grant` | The entry is signed by a member with the release right (L4/L5): `log_signature`/`log_rights`. Vector `support_grant_forged_by_hub`. |
| The hub makes somebody support (role, or swapped keys) | Root-signed role and certification (P4); the keys of a support recipient come from `user_certify`. Vectors `support_role_without_certification`, `support_wrap_to_swapped_keys`. |
| A revoked release keeps reading new content | `support_revoke` puts the workgroup in R of the same batch; new epochs are never wrapped to support while no release is active. Vectors `support_revoke_without_rotation`, `support_wrap_after_revoke`. |
| A person that left support keeps reading | The hub refuses at once; the released workgroups become `rotation_pending` and are rotated by the next admin/writer client; no verifier wraps a new epoch to a person outside the roster. Vector `support_wrap_after_role_end`. |
| A reader replaying an old platform log keeps a former person in its roster | Same residual as for decertified devices (KM-08 for the platform log head; the hub refuses the person anyway). |
| Support writes with a read release, or into an archived workgroup | E5 at `lh` and at receipt (`author_not_writer`). Vector `support_event_read_release`. |
| A member's agent claims a release | Support entitlement is roster ∧ release; agents are never in the roster. |
| What support read before a revocation | Stays with it (idea 30); said before the release. |

**What the hub sees in addition:** release metadata (workgroup, access, who released, when), roster metadata (which
users are support, their display names), every support read and write as audit metadata. Never the content.

## 7. Design review (fresh context) – how the findings were taken
- Role end without an honest hub (high): stated as the residual of §3, the same as for decertified devices; hardening
  with the platform head in the org log belongs to KOR-55.
- One platform head for every verifier (high): the grant is bound to a certification (§1); readers refresh the platform
  log on an unknown author (§4); old-`lh` injection stated like for removed members (§5). An event field `ph` was not
  added (format change of every event for one author kind).
- Member and support (medium): either right suffices, order fixed (§4); vector `support_author_also_member`.
- Race platform append ↔ batch (medium): lock order (§5).
- `settings.workgroup` and credentials of support tasks (medium): support writes prompts and the whiteboard only (§4).
- E-04 (medium): open decision for the owner (§6).
- Privacy of the org log (medium): said in the dialog, `members` limited, reads of log and wraps audited (§4, §5).
- Roster changes (medium): audit and notification in orgs with a release, names in the dialog (§5).
- UI texts, live channel, vectors (medium): §5 and the vector list below.
- Epochs after a re-release, release after the releasing user left, archived workgroups, P5 code, `support.info`
  author, status (low): §3, §4 and §1.

## Consequences
- Every verifier needs its platform state for support: `may_wrap`, `missing_wraps`, `check_event` and the hub's E5 take
  a roster (empty by default, which keeps every existing caller and vector unchanged).
- Vectors in `log.json`, `log-wraps.json`, `log-rotation.json`, `log-events.json`, `platform-log.json`, generated by
  the hub reference and replayed by the runner copy and the web core.
- `key-model.md`: the support principal in §1–§5, KM-33 built with its vectors, the gap section marked closed.
- Not here: execution devices (A7), admin rights (ADR-058), the agent's identity (ADR-082), community moderation.

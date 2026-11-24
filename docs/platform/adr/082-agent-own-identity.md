# ADR-082: The main orchestrator's own identity, real revocation and the orchestrator on platform hardware

Status: Accepted (2026-11-24, KOR-57; refined after the design review, §9 takes precedence). Supersedes ADR-056 §2 (key handover of the user's identity) and §9 (design of
the orchestrator on platform hardware) and amends ADR-056 §4a, §5, ADR-067 and ADR-071 where named. Implements concept
idea 34 ("eigene kryptografische Session-/Geräteidentität ähnlich SSH-Schlüsseln … Widerruf … Der Orchestrator kann
auf DFX-Hardware oder lokal laufen"), the open part of idea 30 ("Rechtevererbung, Schlüsselverteilung und Widerruf")
for the agent, and closes the "later hardening" of interpretation A3. Respects E-03, E-04, E-11, E-19, E-25, E-27.
Key model: `key-model.md` §11 "KOR-57" (rules KM-28, KM-29, KM-30, KM-37).

## Context
- Since ADR-056 a paired agent receives the **user's identity bundle** (sealed to its key and to the handover secret
  `r`). Revocation refuses its key at the hub and deletes the sealed copy there, but the agent's own copy keeps
  opening every wrap made to the user, also future ones (from a copy of the hub's data or through a hub that colludes),
  and it can sign entries, events and wraps as the user. Identities never rotate.
- The verifiers (hub reference `agent_hub.log`, the runner's checked copy `agent_runner.crypto.log`, web
  `web/src/core/log`) know two principal kinds in a membership log: users and devices. Entitlement to a scope epoch is
  one function of the verified state (`OrgState.can_read` → `readers`), used by W2, catch-up, the rotation set R and
  the batch rule "every reader of a new epoch has a wrap".
- An agent must stay able to do everything its user can do (E-04), without a person for each step (E-03, E-26), and
  an account of an agent alone (E-27, ADR-071) keeps working without any person.
- KOR-55 (trust anchor of the runner) is deferred by the owner (E-25). This ADR builds on today's trust of the runner
  in the hub; the places where KOR-55 would add something are listed in the report of KOR-57.

## Decision

### 1. The agent is a sub-principal of its user in every membership log of the user
- The agent's existing key pair (X25519 `kem` + Ed25519 `sig`, drawn by the agent tool, ADR-056 §1) **is** its
  identity. It never receives the user's identity any more (accounts of persons).
- **Delegation certificate.** At the confirmation (the passkey gesture) the user's browser signs once
  `cert = Ed25519_user(P‖"agent-cert\0"‖canonical({"agent":agent_id,"pub":{kem,sig},"user":user_id}))`.
  It is the user's signature that makes the key an agent of the user; every verifier checks it under the user's
  signing key from its verified log. The hub keeps it in the agent row (public, not secret).
- **New entry type `agent_add`** `{subject: agent_id, user: user_id, pub{kem,sig}, cert}` (plus the common fields).
  `by` is the user (signed with the user's identity) or the agent itself (`by == subject`, signed with the agent's
  key from `pub`). L5 (`log_agent` on failure): `user` is an active user member; `cert` valid under the user's key in
  the state before the entry; `subject` was never a principal of this org (an agent ID never comes back) and is not a
  user or device ID; the user has **no active agent** in this org (exactly one, §3). Nobody else may append it: an
  agent cannot add another agent (it has no certificate for a key other than its own, and only the user's identity
  signs certificates), and a hub cannot insert a sub-principal (it holds no user identity).
- **Optional `agent` member in `genesis` and `accept`** (user invitations): `{id, pub{kem,sig}, cert}`. Then `sig` of
  the entry is made by the agent's key (and `inv_sig` as before); the entry also adds the agent (with the same L5
  rule). This lets the agent create an organization or accept an invitation for its user without the user's private
  key (E-03); `subject`/`pub` stay the user's and are checked as before.
- **State.** `Principal(kind="agent", role="agent", parent=user)`. One resolution function `acting(p)` (the parent of
  an agent, else `p`) is used by every right: an agent is active while it is not removed **and** its parent is an
  active member; `remove` of a user removes its agent at the same `seq` (no dormant agent, §9.2); `is_admin`,
  `explicit_access`, `wg_access`, `inherited`, `can_write_wg`, `can_read` of an agent are those of its parent. An agent never has a role, explicit access or
  assignments of its own: `grant`, `ungrant`, `role`, `admit`, `invite` subjects and `member.name` targets stay users.
- **Entitlement ("the active agent of U receives what U receives").** Because `can_read` resolves through `acting`,
  the agent is in `readers(scope)` exactly when its user is: W2, catch-up, R and the batch rule need no second rule.
  `cred_readers(owner)` (credential key) adds the owner's active agents in every org the owner is an active member of.
  The agent is never a reader of anything its user does not read (not of a future support release either, which is a
  separate principal).
- **Authoring.** An active agent authors entries, events, wraps and credential records under its own ID with its own
  key and the rights of its user: L4 checks its `sig` key, L5/E5 check `acting(by)`; for "self" rules (`remove` with
  `subject == by` = leaving, `role` self-demotion, `member.profile` `ref == author`) the comparison uses `acting`.
  E3 treats an agent as a user. Readers show the author as "Agent of <user>".
- **Hub attribution.** A request of an agent with its own identity is authorised as its user (scope, routes and
  rights unchanged, ADR-056 §4) but its **cryptographic actor** is the agent: log entries must have `by` = the agent
  (or the user, when the agent still signs with the user's identity during conversion, §5), events `author`, wraps
  `wrapped_by` and credential records `author` = the agent; the hub checks them with the agent's key from the
  verified log (credential records: from the agent row's certificate, checked under the user's registered key).
- **Community** (E-20): an agent with its own identity is a community author of its own ("Agent of <user>"): it asks
  for the community key with its own key like any member and signs its messages with its own key; the hub serves its
  public keys from the agent row (with the certificate) where it serves a user's identity today.
- **What needs the user's identity itself** stays with the user's clients: a new recovery code (it seals the
  identity) – the agent tool opens the settings page in the user's browser, as it does for adding a passkey
  (ADR-056 §4); the parity table says so.
- **Device requests** of an agent carry the agent's ID in `header.user` and are signed by the agent; the device takes
  the owner (whose `CK`, whose connection) as `acting(user)` and encrypts the response to the agent's `kem` from the
  log. HOT-14 ("acting user" = author of the prompt) maps an agent author to its user.

### 2. Pairing hands over no identity
- `POST /v1/me/agents` (confirmation) takes instead of `bundle_ct`: `cert` and `batches` – one append batch per
  organization of the user (`{org, expected_seq, entries, wraps}`), containing (a) when the user's previous main
  orchestrator is in that log: `remove{subject: previous}` + one `rotate` per element of R + the wraps of the new
  epochs for every remaining reader, then (b) `agent_add{by: user}` + wraps of every epoch the browser holds of every
  scope the user reads, to the new agent – and `cred_wraps`: every epoch of the user's `CK` the browser holds, wrapped
  to the agent. The hub checks every batch with the ordinary batch rules (`check_batch`, W3 with `wrapped_by` = the
  user) and stores the confirmation, the replacement and all batches in **one transaction**: either the new agent is
  in every log with its keys and the previous one is out of every log with its scopes rotated, or nothing changed.
- **The agent's anchor: its user's public keys, bound to `r`.** Instead of the identity the browser seals the user's
  **public** identity keys and the certificate the same way the identity was sealed (`k` from `r`, HPKE to the
  agent's key; new label `agent-user\0`, plaintext `canonical({"cert","kem","sig"})`, stored in `bundle_ct`'s place as
  `user_ct`). A hub that does not know `r` can neither swap the user nor hand the agent an account of its choosing;
  the agent verifies every log against these keys (its user's `genesis`/`accept`/`admit` and its own `agent_add` with
  that certificate), as it verified them against the identity before.
- The handover over the loopback (or, remote, sealed by ADR-067) still carries `r`. After the claim the agent reads
  its own wraps (`recipient = agent_id`) through the ordinary endpoints.
- An old web tab that still sends `bundle_ct` is accepted (the old form, ADR-056); the agent tool then converts the
  agent at once (§5), so the copy is short-lived and the hub keeps no sealed identity afterwards.
- `credential.json`-style formats, events and wraps are unchanged: only the author and recipient IDs are new values.

### 3. Exactly one, also in the logs
- At the hub: unchanged (`agent_one_per_user`, confirmations of one user serialised); a pairing of a platform session
  (§6) is a pairing like any other, so "exactly one" holds over local and platform form.
- In every log: at most one active agent per user (L5 of `agent_add`). A replacement is `remove` of the old one
  before `agent_add` of the new one in the same batch.

### 4. Real revocation
- **What revocation does.** The hub refuses the key at once (as before). In every log in which the agent is active,
  `remove{subject: agent}` puts every org and workgroup scope it read into R (= everything its user reads), and the
  batch must rotate all of them and wrap the new epochs for every remaining reader (existing rule). The user's `CK`
  is never in R (KM-20): the user's client rotates `CK` in the same flow as a separate step and re-encrypts the
  user's credential records. After that, nothing new is wrapped to the agent's key: its copy of the hub's data opens
  only what it could open before.
- **Who removes.** `remove{subject: agent}` may be appended by the agent itself (it leaves: R becomes `pending` and is
  rotated in the background by its user's clients or the admins, as for a member that leaves), by its user, by another
  active agent of the same user (the new orchestrator cleans up after the old one) or by an admin. Every client of
  the user (browser, active agent tool) runs the same background job: a revoked agent of the user still active in a
  log is removed with its R; a pending rotation is done (existing job).
- **The revocation dialog** says honestly what the agent keeps: everything it has already read, and – for an agent
  paired before this change that held a copy of the user's identity – that this copy keeps opening what is
  encrypted for the user (§5).
- Revocations: "Disconnect" in the settings and `agents revoke` (both the hub row and the log removal), the
  replacement by a new pairing (in the confirmation batches, §2), the end of an orchestrator session (§6.5), the
  recovery to a new agent of an account without a passkey (ADR-071 §3).

### 5. Agents paired before: converted without anyone doing anything
- The hub marks an agent row that still holds a sealed identity of an account **with a passkey** as `migrate` in
  `GET /v1/agent/me`. On its next command the agent tool converts itself: it signs the certificate with the user's
  identity it still holds (that identity reached it through the user's passkey-confirmed pairing, so the signature is
  the user's), builds per organization `agent_add{by: user}` + wraps of every epoch it holds to its own key, and the
  `CK` wraps, and sends them with `POST /v1/agent/identity`; the hub stores all batches, the certificate and
  `own_identity` in one transaction and deletes the sealed identity. The tool then deletes the user's identity from
  its state. From then on it works exactly like a newly paired agent.
- **Residual risk (stated in the settings and the revocation dialog):** a copy of the user's identity that an agent
  made before the conversion keeps opening what is wrapped to the user. Closing this needs the rotation of the user's
  identity (below), which is not built.
- **Rotation of the user's identity (`rekey`) – evaluated, not built (E-25).** It needs a new log entry signed by the
  old and the new key, R = every scope of every org of the user, the hub's registered identity and the platform
  anchors (`user_certify`, ADR-060) renewed, the recovery slot replaced by a new code and every passkey slot resealed –
  which needs the PRF output of **every** passkey (one gesture per passkey, or the other passkeys lose the unlock) –
  plus re-pinning in the community. That is not small; it is a separate package if the owner orders it (also the
  answer to a lost unlocked device or remembered browser, `key-model.md` §5).

### 6. The main orchestrator on platform hardware
1. **Request.** On the page of a running main session of the user (the user is the task owner) the user chooses
   "Make this session my main orchestrator". The web client sends `POST /v1/sessions/{id}/orchestrator` (browser
   session of the task owner; an agent may not ask for it: it is a step of the person, §6.4). The hub records the
   request and hints the session's runner.
2. **Keys in the VM.** The runner asks the guest (control op `orchestrator.keys`) to start the agent tool inside the
   VM (`agent-platform pair --session`): it draws the key pair there and keeps the private part only in the VM
   (`$AGENT_PLATFORM_HOME` in the session's home). The guest returns `{pairing_id, pub, pop}`. The runner registers
   the pairing (`POST /v1/device/orchestrator-pairings`, signed by the device: the hub binds it to that session, its
   device and its task owner) together with `device_sig = Ed25519_device(P‖"orchestrator-key\0"‖canonical({"fp",
   "pairing_id","session"}))`.
3. **Approval.** The page shows the session, its device and "replaces <name>", checks `device_sig` under the device's
   key from the verified log (platform device: the platform log) and the keys of the pairing against `fp`, and runs
   the login ceremony tagged with the pairing; then it confirms exactly as §2 (same replacement, same batches) and
   seals the handover `{k, r}` to the session's key in the remote format of ADR-067
   (`P‖"agent-handover\0"‖ids`). The hub relays it; the tool in the VM claims as a remote agent does.
4. **Gesture only from the person.** `POST /v1/sessions/{id}/orchestrator` and the confirmation are refused for any
   principal other than the browser session of the task owner (the confirmation needs the pairing-tagged login, as
   every pairing). A session cannot make itself the orchestrator: it has no browser session, and the runner can only
   register keys, never confirm them.
5. **Channel and tools.** The tool in the VM reaches the hub only **through the runner**: it signs every request with
   its key and hands it to the guest (`POST /v1/hub-relay` on the session's local API); the runner forwards it
   unchanged to the hub and returns the hub's signed answer, which the tool verifies with its pinned hub key. The
   runner relays only for a session that has an orchestrator pairing and only to the hub. Claude Code in that session
   gets the tool's local MCP server (`agent-platform mcp`, KOR-63) as an additional MCP server: these are the
   platform tools of the orchestrator – every call is a signed hub request with the hub's checks and audit
   ("controlled operations"). Nothing else changes for the session (no execution right, the sandbox stays).
6. **End.** When the session ends (any end state, VM reset, device unassignment), the hub revokes the orchestrator
   agent at once (reason `session_ended`); the log removal follows by the user's clients (§4). The keys never leave
   the VM and do not outlive it. The orchestrator session writes **no context checkpoint** (KOR-33): it reads every
   scope of its user, and a checkpoint sealed under its workgroup's key would show that to every reader of the
   workgroup (`key-model.md` §11 KOR-57 point 6). Billing as for every session (KOR-31).
7. **Audit.** Every request of the orchestrator is an agent request: actor "Agent of <user>", `agent_id` set, as for a
   local agent; the agent list shows where it runs (`session`).

### 7. Accounts of an agent alone (E-27, ADR-071)
Unchanged: the registering agent holds the account's identity because it **is** the account's only holder; there is no
person whose identity could be protected from it. A pairing confirmed by such an agent (`agents confirm`) builds the
same certificate and batches as a browser (§2), so the new agent is a sub-principal. Recovery to a new agent keeps
ADR-071 §3; the recovering agent then holds the identity again.

### 8. Threat model (additions to `04-crypto.md` §9 and `11-agent-access.md` §9)
| Attacker | Protection | Residual risk |
|---|---|---|
| Active hub inserts a sub-principal | `cert` under the user's key from the verified log; `agent_add` by user or by the certified agent only | – |
| Agent pairs another agent | certificates only from the user's identity; confirmation only from the pairing-tagged passkey login | an account without a passkey (E-27) confirms by its agent by design |
| Revoked agent with a copy of the hub's data | atomic `remove` + R in every log, `CK` rotation; hub refuses the key | what it read before; with a colluding hub it can add itself to an org its user joins **later** (certificates are not time-bound; the user's clients remove it from every log they see, and the hub refuses its key) |
| Agent paired before this change | conversion deletes the hub's and the tool's copy | a copy made earlier keeps opening wraps to the user (no `rekey`, §5) |
| Session makes itself the orchestrator | request and confirmation only by the task owner's passkey-tagged browser session | – |
| Runner or device of the orchestrator session | `device_sig` over `fp`; dialog names session and device; requests signed in the VM, answers signed by the hub | the device sees the VM at run time (it executes the session; accepted as for any session); it can withhold or delay (denial) |
| Hub serves another session key | `device_sig` of the session's device | (KOR-55 not built: the runner trusts the hub's session binding) |

## Consequences
- New log entry type and optional members: all three verifiers and the vectors change together (`log.json`,
  `log-wraps.json`, `log-rotation.json`, `log-events.json`, `agent.json`); readers of an older version refuse a log
  with `agent_add` (`log_type`): hub, web build and runner must be released before an agent with its own identity
  exists. Order: hub + web (one release), runners, golden image (agent tool in the guest), then the agent tool.
- Migration (hub): agent row columns `own_identity`, `cert`, `session_id`, `revoked_reason` (additive).
- The agent tool keeps two modes: own identity (every agent of a person) and the account's identity (the registering
  agent of an account alone).
- Rejected: a shared "agent scope" key (one more key to rotate, same trust); a per-user agent log (a second log kind
  in every verifier, while the org logs are where rights live); time-bound certificates (a renewal step for nothing a
  rotation does not already give); the rotation of the user's identity (§5).

## 9. Refinements after the design review (binding; they take precedence over §1–§8)

1. **Generation.** The certificate text is `P‖"agent-cert\0"‖canonical({"agent","gen","pub","user","v":1})` with
   `gen` ≥ 1, one more than the highest generation the hub has issued for the user (the hub counts, the browser signs
   it). In a log, `agent_add` (and the `agent` member) needs `gen` greater than every generation of an agent of the
   same user in that log – except the same agent ID coming back after its user rejoined (`gen` equal to its own). An
   `agent_add` whose user has an active agent with a lower generation **replaces** it: the old agent is removed at
   the same `seq` and its R is owed in the same batch (so a new orchestrator that is not yet in a log cleans up there
   itself). Across logs, one pure function of the verified states, `revoked_agents(states)`, names every agent that is
   superseded (a higher generation of its user exists in any log) or was removed other than together with its user
   in any log; such an agent is no credential reader (`cred_readers`), the user's clients remove it from every log
   where it is still active, and a log whose only proof of the user (`genesis`/`accept`) was signed by such an agent
   is not trusted by the user's clients and gives no consent (no device of that org becomes a credential reader).
2. **No dormancy.** `remove` of a user removes its active agent at the same `seq` ("with its user"). The same agent
   may come back when the user rejoins (`agent` member of `accept`, or `agent_add` with its own generation), never an
   agent that was removed otherwise.
3. **`acting` applies to `by` only**, never to `subject`. `remove{subject: agent}` by anyone but the agent itself owes
   R in the batch; by the agent itself it is pending. `remove{subject: user}` by its own agent is the user leaving
   (pending).
4. **Inheritance of every slot.** Catch-up and W2 follow one rule: every wrap slot its user is entitled to (every
   epoch, also older ones delivered on request or after a promotion) is a slot of the user's active agent. Holders
   wrap all epochs they hold for the agent; `explicit_access`/`inherited` resolve through `acting`.
5. **`CK` in every revocation.** Every revocation and replacement ends with the rotation of the user's `CK` and the
   re-encryption of the user's credential records by the user's client – browser **or** the active agent tool (E-04) –
   after the removal from the logs; records an old agent authored are thereby re-signed. Readers accept a credential
   record authored by an agent that is the owner's active, not revoked agent in the verified logs.
6. **Orchestrator session only in the user's private organization and only on platform devices.** Its workgroup's
   readers are then only the user, the user's agent and the executing devices, so the session's events (prompts,
   output, tool results) do not show the user's other organizations to anybody else, and only the user (or its agent)
   can prompt it (idea 36 does not apply in a private organization). The dialog and the hub refuse every other case.
7. **The key in the VM.** The guest starts the orchestrator's agent tool under its own system account (not the
   session's), with its state directory `0700` of that account; Claude Code reaches its MCP server through a socket
   that the guest offers to that session only. Claude Code can neither read the key nor start the tool. This is a
   stated exception to KM-37 (the tool and its cryptography library are in the guest image, used only by that
   account); the session itself still holds no key and executes nothing new.
8. **Signed handover.** The browser signs the sealed handover with the user's identity
   (`P‖"orchestrator-handover\0"‖canonical({"ct","pairing","session"})`); the runner checks the signature under the
   task owner's key from its verified log before it passes the ciphertext and the user's public keys into the VM. A
   hub cannot hand the session an account of its choosing. (The binding of the session to its task owner is the
   hub's; one of the places KOR-55 would anchor.)
9. **"Ruht" is a revocation.** Idea 34 says the delegation rests at the session's end; since no key outlives the VM,
   resting and revocation are the same: the hub revokes, the user's clients remove it with R. A new orchestrator needs
   a new gesture.
10. **The old form ends.** The hub refuses the old confirmation form with `bundle_ct` for an account with a passkey
    (`409 client_outdated`); an old tab reloads. Agents still holding the identity convert on their next command;
    agents of an account alone that later gets a passkey are converted the same way.
11. **Rekey** stays unbuilt (§5) and is reported to the owner as a decision: without it a copy of the identity that
    an agent made before its conversion keeps its reach. The key model states this residual for KM-30.
12. Device-request follow-ups compare requesters through `acting`; the community accepts an agent's messages only
    while it is its user's active agent at the hub (residual: a colluding hub). Confirmations retry a batch on
    `seq_conflict` with a fresh head.

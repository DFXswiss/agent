# ADR-056: Delegated agent access – the user's own AI agent sets up the account

Status: Accepted (2026-11-23); amended 2026-11-23 by the owner's decision "Nachtrag 2" (one main orchestrator
per user, concept idea 34; §4a, §9)

## Context
- Only the user's browser can set up an account today: connect the Claude subscription (OAuth dialog with a code),
  connect GitHub, create an organization, invite members, allow repositories. A user without technical knowledge
  fails there, and the user's own AI agent (for example Claude Code on the user's computer) can do none of it.
- The owner's requirement (KOR-24): every customer lets their AI agent set everything up; **the only thing the user
  does is create a passkey**. Connecting the subscription by transferring the existing auth token of the local
  Claude Code login is mandatory and must be uncomplicated.
- Constraints that stay: the hub is zero-knowledge (`04-crypto.md` §7, ADR-003): it never sees a credential, a scope
  key or a private identity key. Every confirmation is a passkey gesture. A link that a third party sends must never
  put an agent or credentials into an account. Running accounts, passkeys and logs must not break (additive
  migrations, an old web tab against the new hub keeps working).
- Membership-log entries, wraps, credential records and device requests are signed by the user's identity
  (`04-crypto.md` §4); organization setup therefore needs the identity's private keys, not only a scope key.
- The full concept (`KONZEPT.md`, binding since 2026-11-23) states the target as idea 34, "one delegated main
  orchestrator per user": a user may delegate all of their platform rights, within their own rights, to **exactly
  one** active session; passkey approval, an own cryptographic session or device identity similar to SSH keys, full
  audit and revocation are required; the orchestrator may run on DFX hardware or locally; it too uses only controlled
  platform operations. Idea 30: passkeys are the only approval the user sees, key management stays invisible.

## Decision

### 1. An agent is a delegated access of one user with its own key pair
- The agent generates its own key pair on the user's computer (X25519 `kem` + Ed25519 `sig`, like a device) and keeps
  the private part there (the login keychain where one exists, else a `0600` file in a `0700` directory of the agent
  tool); it never leaves the computer. Neither protects it from other processes of the same user, which is why
  revocation, visibility and notifications carry the weight (§5).
- An agent signs its requests with `agent-request-v1` under the key ID `agent.<32 hex>` (the agent ID without
  dashes). The hub resolves the key to a principal of kind `agent` that acts **for its user** (`principal_id` = user
  ID), with the agent ID as attribute.
- The hub keeps one row per agent: public keys, user, state (`open` → `confirmed` → `active`, or `revoked`), times
  (created, confirmed, claimed, last used, revoked) and two ciphertexts it cannot read (the sealed identity and the
  sealed label, §2). The agent's name and computer name are never in clear at the hub.

### 2. Key handover: the user's identity, bound to a secret the hub never sees
- At confirmation the browser draws a handover secret `r` (32 bytes). From `r` it derives the bundle key `k` and the
  claim; the hub stores only the hash of the claim. The browser encrypts the user's identity bundle plaintext
  (`04-crypto.md` §4.1) under `k` and seals the result with HPKE to the agent's X25519 key
  (`11-agent-access.md` §3). `r` goes only to the agent, over the loopback (§3). Opening needs the agent's private
  key **and** `r`, so neither a hub that swapped the key nor a hub that put its own key into a phishing link can open
  it, and a hub cannot give the agent an identity of its own choosing.
- The label (`{"name","host"}` from the link) is sealed with HPKE to the **user's** identity key, so only the user's
  browsers and the agent read it.
- With the identity the agent opens the wraps that already exist for the user (OK, WK, CK) through the ordinary
  endpoints. Nothing else changes: no new wraps, no log entry, no new scope; readers and runners are untouched. The
  agent tool verifies what it uses itself (logs L1–L8 against founders the identity created or accepted, only its own
  self-wraps of `CK`, the platform log against the root key of the web build), like a browser.
- Rejected: the agent as its own principal in every organization's log (changes the rights model, every reader and
  every log of the user's organizations); only `CK` for the agent (enough for the subscription, but organization
  setup, invitations, workgroups and device assignment need the identity's signatures); a new wrap scope for agents
  (more formats, the same trust).
- Consequence: the agent holds what an unlocked browser tab of the user holds and may do what the user may do (§4);
  revocation ends its access at once (§5), but what it has read stays with it and its copy of the identity would open
  later wraps of a future database leak; identities do not rotate in this version (roadmap item).

### 3. Pairing: three bindings
A pairing is created by the agent and confirmed by the user in the browser:

1. **Key binding against the hub.** The agent registers fresh public keys with a proof of possession
   (`POST /v1/agent-pairings`, anonymous, rate-limited per source) and opens a link whose **fragment** carries the
   pairing ID, the key fingerprint, its loopback port and its label. The fragment never reaches the hub. The browser
   compares the keys the hub returns with the fingerprint.
2. **Passkey gesture.** The page shows what is confirmed (the agent's name and computer as the link states them, the
   age of the request, that the agent becomes the user's **main orchestrator** ("Hauptorchestrator") with **full
   access** to the account and can do everything the user can, and – when the user already has one – "replaces
   <name>", §4a) with the warning to confirm only a connection the user just asked for, in plain words without key
   jargon (idea 30), and runs a **login ceremony tagged with the pairing**. The gesture logs in, unlocks the identity
   (PRF) and confirms in one. `POST /v1/me/agents` is accepted only from the browser session created by that ceremony
   and within 300 s of its creation, so an old open tab cannot confirm without a new gesture; an agent cannot confirm
   with its agent key (§4a; for the way around through recovery see Consequences).
3. **Loopback handover.** After the confirmation the same tab submits a top-level form POST (`p`, `u`, `r`, `k`) to
   `http://127.0.0.1:<port>/agent-pairing` (the port from the fragment; `r` never appears in a URL, so it stays out
   of the browser history; the content security policy allows `form-action 'self' http://127.0.0.1:*`). The claim
   deadline shrinks to 60 s after the confirmation. Only the agent listening
   there gets `r`; it sends the claim (`POST /v1/agent/claim`, signed with its own key) and opens the identity with its
   key and `r`. A link sent by a third party points to a computer on which the third party's agent does not listen:
   the pairing stays unclaimed, the identity unopenable, and it expires (10 min). Top-level navigation from an https
   page to the loopback address works in current Chromium, Firefox and WebKit (it is what OAuth for native
   applications uses); an agent on another computer cannot be paired (no code fallback, on purpose).

A pairing is single-use: confirmed once, claimed once. A new account (invitation link) uses the same three bindings
inside the registration (§6).

### 4. Scope: an agent can do everything its user can (decision of the owner)
- **Decision of the owner (2026-11-23, 17:40 CET, verbatim):** „ganz wichtig: auch ein ki agent der sich nur mit ssh
  verbindet (oder wie du das nennen willst, also ich meine die "nicht passkey" variante, soll alles tun können was ein
  mensch der sich mit passkey verbindet tun kann. zwingend!“ This overrides the limited scope of the first draft
  (allow-list with body filters, written after the first security review); it is not renegotiated.
- The request pipeline admits an `active` agent on **every** route a browser session of its user is admitted to, with
  the same rights and no filter on body or query (`agent_hub/agents/scope.py`). Equality is the default: a browser
  route added later is open to agents without any change. The only exceptions are the WebAuthn ceremonies themselves
  (`POST /v1/passkeys/register/begin|finish`, `POST /v1/me/recovery/begin`), which need an authenticator, and the
  confirmation of a main orchestrator (`POST /v1/me/agents`), whose essence is the user's passkey approval (§4a); a
  test checks every registered route against this rule. An agent replaces the recovery code with its signed request (the
  proof a browser gives with a passkey assertion), and where the web client needs a passkey gesture (adding a passkey)
  the agent tool opens the page in the user's browser and waits.
- The orchestrator acts **only through the platform's controlled interfaces** (the agent tool and the hub API, each
  request signed with its own key, checked by the hub's rules and written to the audit with its `agent_id`); there is
  no side path around these checks and the audit – no direct database access, no runner or device channel, no
  operator back door (idea 34: "Auch er verwendet nur kontrollierte Plattformoperationen"; section 16: authorised
  actions run through the platform).
- The agent stays an access **for its user**: never more than the user, and never less. Agents use the rate limit of
  a browser session (600/min per key).

### 4a. Exactly one main orchestrator per user (owner's decision "Nachtrag 2", concept idea 34)
- **Decision of the owner (2026-11-23, 19:35 CET, "Nachtrag 2", verbatim excerpt):** „**Genau ein aktiver
  Hauptorchestrator je Nutzer.** Zwei gleichzeitig aktive gibt es nie. Eine neue Kopplung **ersetzt** den bisherigen –
  nur mit Passkey-Freigabe des Nutzers und klarer Anzeige („ersetzt <Name>“); der bisherige ist damit sofort
  widerrufen. […] **Ein Agent koppelt keine weiteren Agenten.** Dieser Punkt aus Nachtrag 1 entfällt: Das Konzept
  verlangt die Passkey-Freigabe. Die Gleichstellung aus Nachtrag 1 bleibt im Übrigen vollständig […] **„Nur
  kontrollierte Plattformoperationen“:** Der Orchestrator handelt ausschließlich über die Schnittstellen der Plattform
  (dein Werkzeug, die API) – im ADR festhalten; kein Seitenweg an Prüfung und Audit vorbei.“
- A user has at most one `confirmed` or `active` agent: the main orchestrator. The hub enforces it with the unique
  index `agent_one_per_user` on `hub.agent (user_id) WHERE state IN ('confirmed','active')`, and confirmations of one
  user are serialised.
- The passkey confirmation of a new pairing **replaces** the previous main orchestrator: in the same transaction the
  previous row is revoked (sealed identity deleted, `key_revoked` from its next request, its open live streams closed)
  and the new row records it in `replaces`. The approval page names the one it replaces ("replaces <name>", the name
  opened from the user's own sealed label).
- An agent can **no longer confirm a pairing with its agent key** (the corresponding point of the first amendment is
  dropped): `POST /v1/me/agents` is on the exception list next to the WebAuthn ceremonies. The agent tool's `pair`
  opens the pairing page in the user's browser; the user approves with the passkey. Revoking stays open to agents (an
  agent may disconnect itself). Everything else of the equal rights (§4) stays unchanged.
- This is not a hard barrier against a hostile agent: under the equal rights it may replace the recovery code
  (signed request, no assertion), run the anonymous recovery with a software authenticator to register a passkey of
  its own, sign in with it, tagged with a pairing, and confirm that pairing. The rule keeps a well-behaved agent from
  pairing others; against a stolen or hostile agent the protection is visibility (Consequences).
- The previous column `paired_by_agent` and the list field `paired_by` are replaced by `replaces`; the notification
  action `agent_paired` is removed (only a passkey gesture of the user can pair).

### 5. Visible and revocable
- Settings "Connected agents" shows the active main orchestrator ("Hauptorchestrator": name and computer from the
  sealed label, created, last used) with "Disconnect", and the history of earlier ones (state, which one each
  replaced) with "Disconnect all agents". `DELETE /v1/me/agents/{id}` and `DELETE /v1/me/agents` (user: browser
  session or agent) revoke at once: the key resolver refuses the key with
  `key_revoked` from the next request on; the sealed identity is deleted from the row; the agent tool deletes its own
  state when it sees `key_revoked`.
- Audit: every row written while an agent request is processed carries `agent_id` (new nullable audit column; the
  hashed row contains the key only when it is set, so the hashes of all existing rows stay valid). The audit view
  shows "by agent <name>" (the name is opened from the label by the user's browser; other admins see "by agent").
- Notification of the user for account-changing actions of an agent (a passkey removed, the recovery code replaced, an
  agent revoked, …) and for every finished recovery (a passkey added through recovery): an in-app notification `agent.account_change` with the action and the agent, so that a stolen
  agent key cannot change the account unnoticed.
- `last_used_at` is updated by the key resolver at most once per minute. The agent tool keeps its private key in the
  login keychain where one exists (macOS), else in the `0600` file.

### 6. New account: invitation link plus pairing, the user creates the passkey and nothing else
- The agent opens `/invite#<code>&p=…&f=…&c=…&a=…&h=…&u=<display name>` (the invitation link is given to the tool on
  stdin, never on the command line). The registration takes the display name
  from the link (no name field; it can be changed later), creates the passkey and runs the login of the registration
  as the pairing-tagged ceremony. After the account is complete (private organization or org acceptance) the browser
  confirms the pairing and hands over to the loopback.
- The screens are those of KOR-22 (no recovery-code step); a pairing parameter only removes the name field and adds
  the agent block and the handover.

### 7. The Claude subscription: transfer of the local login token
- The agent tool reads the access token of the local Claude Code login **read-only** (macOS: the login keychain item
  of Claude Code through `/usr/bin/security find-generic-password … -w`, then the credentials file of the profile
  directory; Linux: `$CLAUDE_CONFIG_DIR/.credentials.json`, default `~/.claude/.credentials.json`). It takes the access
  token and its expiry, drops the parsed refresh token at once and never stores, sends or prints it. It never runs a
  login, logout or refresh of the local program.
- It stores the token as the user's `claude_subscription` record (encrypted on the user's computer under the user's
  `CK`; the hub sees owner, kind and version only) with the new body kind `login_token` and `expires_at`
  (`contracts/credentials/claude_subscription.json`, additive). `setup_token` records stay as they are.
- Keeping it current: the local token is short-lived; the agent pushes a new record when the local token changed
  (command `claude sync`, and optionally a user-level background service that does the same every few minutes). It
  never replaces a `setup_token` record with a short-lived login token unless told to. Running sessions pick up the
  new version as before (the runner re-provisions on a new record version). The login token carries the scopes of
  the Claude Code login (broader than the inference-only setup token) and stays fresh only while Claude Code is used
  on that computer; the guide therefore recommends the setup token as the lasting way.
- The long-lived way: `claude setup-token` run by the agent tool on the user's computer; the probe against the real
  program (2.1.330) showed that the local run redirects to `http://localhost:<port>/callback`, so the user only
  approves in the browser and copies no code; the token is read from the program's screen with the runner's screen
  reader (ADR-051) and stored as a `setup_token` record.
- The settings show the kind ("from the Claude Code login" / "long-lived token") and the expiry of the stored token.

### 8. The agent tool
- A separate CLI in the `agent` repository, `agent-platform` (package `agent_platform`), made for AI agents, with a
  command for every action and view of the web client (including the client's crypto: reading and writing encrypted
  tasks, prompts, answers, whiteboards; following the live stream) and a generic signed call
  `agent-platform api <METHOD> <path>` that reaches every route at once; a parity matrix (document and test) maps every
  user action of the web client to its command:
  every output is one JSON object on stdout, every failure has a stable `error` code and a `next` step from fixed
  lists, no secret is ever printed (the invitation link of `org invite` is the one marked secret field). It reuses
  `agent_runner.crypto` (formats) and the runner's signed HTTP client and pins the hub key at pairing.
- A public guide for agents (`docs/platform/agents.md`; served by the hub as `/agents.md` and shipped inside the
  tool as `agent-platform guide`, so an agent never needs to take instructions from the hub) says step by step what
  an agent does when its user says "set up agent.dfx.swiss for me".

### 9. Orchestrator on platform hardware (design; not built in this package, follow-up package)
Idea 34 allows the main orchestrator to run on DFX hardware: a **platform session** of the user on an execution
device becomes the main orchestrator. Same rights, same revocation, same audit, same "exactly one" rule (it replaces
a local one and is replaced by one); only the key generation and the handover differ (`11-agent-access.md` §11).
- **Own identity in the session.** The runner starts a small agent-tool process inside the session VM, which draws
  the key pair there (X25519 + Ed25519, as §1); the private part never leaves the VM (not to the runner, the device
  or the hub). The public keys and the proof of possession go through the runner to the hub as a pairing bound to the
  session (`session_id`); the runner signs the request with its device key, so the hub knows which device and session
  it came from.
- **Approval in the web client.** On the session page the user chooses "Make this session my main orchestrator"
  ("Diese Session als Hauptorchestrator"); the page shows the session, its device and "replaces <name>" and runs the
  passkey login ceremony tagged with that pairing (as §3.2). The browser checks the pairing's keys against the
  fingerprint the runner reported for the session in a frame signed by the device (instead of the link fragment).
- **Sealed handover.** As §2, the browser draws `r`, seals the identity bundle under `k` and to the session's key.
  The loopback is replaced by a channel authenticated through the runner and device: the browser seals `r` (with the
  user ID and the platform root key) by HPKE to the session's X25519 key and sends it as a session input; the runner
  forwards the ciphertext unopened into the VM; only the session's private key opens it. The hub and the runner see
  only ciphertexts; the claim and the opening are as in §3.3.
- **Threats.** A malicious runner or device can substitute keys (mitigated: the fingerprint comes in a
  device-signed frame and the browser shows device and session; a compromised device equals a compromised computer
  of a local agent), observe the VM at run time (accepted: the device the user assigned already sees the session's
  plaintext), or withhold the handover (denial only). A malicious hub cannot open `r` or the
  bundle. Session end, VM reset or device unassignment revoke the orchestrator (no key survives the VM).
- Why a follow-up package: it needs runner and guest changes (key generation in the VM, a new session input kind,
  a device-signed fingerprint frame) and a new web flow; nothing of it changes the local form built here.

## Consequences
- Zero-knowledge holds, also against a malicious hub: the hub stores public keys, states, times, the claim hash and
  two ciphertexts it cannot open (the identity needs `r`); the label is encrypted. New hub-visible metadata: the
  existence of an agent, its public keys and times, and for ten minutes the source address of an open pairing
  (`04-crypto.md` §7).
- **Residual risk (follows from decision 10a – the agent may do everything its user may – and the project's reading A3 of the concept audit AUD-04 W5–W7: one main orchestrator, pairing and replacing only with a passkey gesture, the agent holds the user's identity; an own identity with rotation is a later hardening, AUD-04 N15).** An agent key together with the agent's state equals the
  user's passkey plus the unlocked identity. Whoever steals it holds full and lasting control of the account and can
  lock the user out: replace the recovery code and use it to register an own passkey, remove all
  of the user's passkeys, invite itself into the user's organizations with any role, replace credentials, read
  everything the user can read. "Disconnect all agents" ends hub access from the next request (open live streams are
  closed with the revocation); it does not undo recovery codes, passkeys, members, invitations or credentials the
  agent changed, and it does not take back what it read or its copy of the identity (identities do not rotate, so the
  copy also opens later wraps of a future database leak). The countermeasures do not limit the agent: the pairing only
  over the loopback with a fresh passkey gesture (which also replaces and so revokes a stolen orchestrator); a stolen
  key cannot confirm a pairing itself, but it can add a passkey of its own through recovery and pair with that
  (§4a), so it is a full account takeover including new passkeys and pairings; the list of the one main orchestrator with its history and last use; the account
  audit "by agent" (`GET /v1/me/audit`, shown in the settings); in-app notifications of account-changing agent
  actions and of every finished recovery; the keychain where one exists. There is no out-of-band channel (e-mail,
  push) in this version (roadmap): a user who is already locked out does not see the in-app notices. If an attacker removed the user's
  passkeys and replaced the recovery code, the only way back is an operator-assisted recovery
  (`runbooks/agent-account-takeover.md`); a private-only account without it is lost.
- The pairing needs the agent on the same computer as the browser (loopback). An agent on another computer cannot be
  paired in this version (a code-based fallback was rejected because it re-opens the phishing path); the session on
  platform hardware (§9) is the follow-up for a remote orchestrator.
- One main orchestrator per user: a user who wants agents on two computers at once cannot have them; pairing the
  second disconnects the first (owner's decision, §4a).
- Rollout: hub and web build in one release (the hub serves the web client), then the agent tool; runners need no
  change (they read only `token` of a subscription record). A tab of the old web client that stays open shows a
  `login_token` record as unreadable and its `CK` re-wrap job stops at it until the tab is reloaded. Recommended
  order: deploy, verify (migration, audit verification, an org export, a test pairing), then announce agent access.
- Rollback: the forward migration 0010 is additive and safe, but not fully backward compatible. The previous hub
  starts on the new schema, yet its org export and restore refuse to run (its table list lacks `hub.agent`), its
  audit verification reports rows that carry `agent_id` as broken (it hashes without that key), and it lists the
  account notifications to every client. These degrade until the hub is upgraded again. Rollback procedure: revoke
  all agents and keep the schema (the migration is not reverted; `11-agent-access.md` §12). A new web client meets an
  old hub only in a rollback, since the hub serves its own build; a tab left open then is not reloaded automatically,
  and its `?account=1` on the notification list is ignored by the old hub.
- Code structure: the agent package is cross-cutting, not a self-contained area. The shared request pipeline
  (`http/pipeline.py`: `agents.scope`, `agents.keys.touch`) and the auth, membership, credential, request, platform
  and invitation modules import `agent_hub.agents` (notifications, revocation, the pairing check of the login); the
  key resolver `agent.` is registered through `agents/keys.py:RESOLVERS` like the other areas.

## Alternatives considered
Device-code flow without loopback (the classic consent-phishing path: a third party's link confirmed by a careless
user would put its agent into the account); the agent as a runner device (devices do not act as users and cannot sign
log entries); handing the agent a browser session (needs a passkey on the agent's side).

## Deviation from DESIGN.md
None beyond ADR-001.

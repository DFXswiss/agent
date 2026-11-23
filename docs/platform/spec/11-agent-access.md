# 11 Delegated agent access (KOR-24)

Status: binding. Decided in ADR-056. Extends `02-data-model.md` (§2), `03-api.md` (§3, §4, §11), `04-crypto.md` (§3,
§7, §9) and `06-web.md` (settings, invitation). The user's own AI agent pairs with the account through one passkey
gesture and then sets the account up with the agent tool `agent-platform` (repository `agent`).

## 1. Terms

| Term | Meaning |
|---|---|
| Agent | A delegated access of one user, held by an AI agent on the user's computer, with its own key pair. |
| Main orchestrator | ("Hauptorchestrator" in the interface) the one `confirmed` or `active` agent of a user (concept idea 34, ADR-056 §4a); a new pairing replaces it. |
| Pairing | The agent's request to be connected; becomes the agent row once confirmed. Pairing ID = agent ID (UUIDv7, drawn by the agent). |
| Label | `{"name","host"}` chosen by the agent (e.g. `Claude Code`, the computer's name); only sealed at the hub. |
| Handover secret | `r`: 32 random bytes drawn by the confirming browser and handed to the agent **only** over the loopback. It yields the bundle key and the claim (§3). |

## 2. Data (hub schema, migration `0010_agent_access.sql`)

`hub.agent`: `id uuid pk`, `user_id uuid null → principal` (null while `open`), `kem_pub bytea(32)`,
`sig_pub bytea(32)` (unique among rows that are not revoked), `state text` ∈ {`open`,`confirmed`,`active`,`revoked`},
`bundle_ct bytea null` (set while `confirmed` or `active`), `label_ct bytea null`, `claim_hash bytea(32) null` (both
set from `confirmed` on), `claim_misses integer`, `source text null` (the anonymous rate-limit source of an `open`
row, IPv6 by /64; cleared at the confirmation), `created_at`, `expires_at` (created + 600 s; the claim deadline),
`confirmed_at`, `claimed_at`, `last_used_at`, `revoked_at`, `replaces uuid null` (the agent this one revoked at its
confirmation). Revocation clears `bundle_ct`. At most one row per user is `confirmed` or `active` (unique index
`agent_one_per_user` on `user_id` where `state` ∈ {`confirmed`,`active`}).
`hub.ceremony.pairing_id uuid null` and `hub.browser_session.pairing_id uuid null` (the login ceremony that confirms
a pairing, §5). `hub.audit.agent_id uuid null` (§7). Unclaimed rows (`open`, `confirmed`) are deleted one hour after
`expires_at` (job `agents.unclaimed_purge`); a user deleted as an orphan registration loses its agent rows. Limits:
at most 3 unexpired `open` pairings per source and 10 000 in all (`429 rate_limited`), on top of the anonymous limit.
Org exports carry the agent rows of the org's users.

## 3. Crypto (labels prefixed with `P`, `04-crypto.md` §1; reference `agent_hub/crypto/agent.py`)

| Item | Definition |
|---|---|
| Fingerprint | `fp = b64u(SHA-256(P‖"agent-key\0"‖kem_pub‖sig_pub)[:16])` (22 characters) |
| Proof of possession | `pop = Ed25519_sig(P‖"agent-pair\0"‖canonical({"kem":b64u,"pairing_id":id,"sig":b64u}))` |
| Ids | `ids = canonical({"agent":agent_id,"user":user_id})` |
| Bundle key | `k = HKDF(r, info = P‖"agent-bundle-key\0"‖ids)` |
| Claim | `claim = HKDF(r, info = P‖"agent-claim\0"‖ids)`; the hub stores `claim_hash = SHA-256(P‖"agent-claim-hash\0"‖claim)` |
| Sealed identity | `info = P‖"agent-bundle\0"‖ids`; `inner = nonce12‖AES-GCM(k, identity bundle plaintext (§4.1), AAD = info)`; `bundle_ct = b64u(HPKE_seal(agent kem_pub, info, inner))` (base mode, suite of §1). Opening needs the agent's private key **and** `r` |
| Sealed label | `label_ct = b64u(HPKE_seal(user identity kem_pub, P‖"agent-label\0"‖ids, body JSON {"host","name"}))`; each text 1–64 characters, no leading/trailing space, none of the Unicode categories Cc, Cf (bidi, zero-width), Zl, Zp, Cs, Co, Cn |
| Agent key ID | `agent.<32 hex>` = agent ID without dashes; signatures Ed25519 with `sig` (`agent-request-v1`) |

Why `r` is inside the bundle encryption: the hub never sees `r`, so neither a hub that swapped the agent key nor a hub
that put its own key into a phishing link can open `bundle_ct`, and a hub cannot hand the agent an identity of its own
choosing (AES-GCM under `k` authenticates the bundle). Vectors: `contracts/vectors/agent.json`; all three
implementations check them.

## 4. API

| Endpoint | Auth / right | Request → Response |
|---|---|---|
| `POST /v1/agent-pairings` | anon | `{pairing_id,pub:{kem,sig},pop}` → `{pairing_id,expires_at}`; same ID with the same keys → the stored answer, other keys or keys of another pairing → `409 id_conflict`; bad proof → `401 signature_invalid`; limits of §2 → `429 rate_limited` |
| `GET /v1/agent-pairings/{id}` | anon | → `{pairing_id,pub:{kem,sig},state:"open"|"confirmed",expires_at}` while open or confirmed and before the deadline; otherwise (unknown, claimed, revoked, expired) `404 pairing_unknown` |
| `POST /v1/passkeys/login/begin` | anon | `{pairing_id?}`: optional; a pairing that is not open before its deadline → `404 pairing_unknown`. The ceremony and the session it creates carry the pairing ID |
| `POST /v1/me/agents` | browser session only (an agent gets `403 forbidden`, §6) | `{pairing_id,bundle_ct,label_ct,claim_hash}` → `{agent_id}`; the session must have been created by a ceremony tagged with this pairing at most 300 s ago (`403 pairing_gesture_required`); the pairing must be `open` before its deadline (`404 pairing_unknown`). In the same transaction the user's previous main orchestrator (`confirmed` or `active`) is revoked and recorded in `replaces` of the new row; its open live streams are closed. A repeat with identical values → the same answer (nothing replaced again), other values → `409 id_conflict` |
| `GET /v1/me/agents` | user | → `{agents:[{agent_id,state:"confirmed"|"expired"|"active"|"revoked",label_ct,created_at,confirmed_at,claimed_at,last_used_at,revoked_at,replaces:<agent id>|null}]}` of the user, newest first (`expired` = confirmed, never claimed, past the deadline; `replaces` = the main orchestrator this one replaced at its confirmation); at most one row is `confirmed` or `active` |
| `DELETE /v1/me/agents` | user | → `{revoked}`: every confirmed or active agent of the user revoked at once |
| `DELETE /v1/me/agents/{id}` | user | → `{}`; state `revoked`, sealed identity deleted; unknown or foreign → `404 not_found`; already revoked → `{}` |
| `POST /v1/agent/claim` | agent key, state `confirmed` (`agent:confirmed`) | `{claim}` (b64u 32 B, §3) → `{agent_id,user_id,bundle_ct,label_ct,created_at,claimed_at}`; wrong claim → `401 claim_invalid` (counted; the fifth wrong claim deletes the pairing); past the deadline → `404 pairing_unknown`; repeat when `active` with the right claim → the same answer |
| `GET /v1/agent/me` | agent key, state `active` (`agent`) | → `{agent_id,user_id,bundle_ct,label_ct,created_at,claimed_at}` |
| `GET /agents.md` | anon (not under `/v1/`) | the guide for AI agents of the web build (`docs/platform/agents.md`), `text/markdown` |

Key resolver `agent.`: unknown, `open`, or `confirmed` past its deadline → `key_unknown`; `revoked` or user disabled
→ `key_revoked` (response-signed, like devices); `confirmed` → only `POST /v1/agent/claim`; `active` → §6. New auth
class `agent` (routes `/v1/agent/*`), principal kind `agent` (acting for its user, audit actor `user`). Rate limit as a
browser session (600/min per key). Error codes (new, `03-api.md` §11): `pairing_unknown` (404),
`pairing_gesture_required` (403), `claim_invalid` (401); client-only `pairing_mismatch`.

## 5. Flows

### 5.1 Existing account
1. Agent: generate fresh keys, `POST /v1/agent-pairings`, listen on `127.0.0.1:<random port>` (§8.2), open
   `<origin>/connect#p=<id>&f=<fp>&c=<port>&a=<name>&h=<host>` in the user's default browser.
2. Browser `/connect`: read and remove the fragment; `GET /v1/agent-pairings/{id}`; refuse unless state `open` and
   `fp` matches the returned keys (`pairing_mismatch`, "This link is not valid"). Show, as text the link supplied, the
   agent's name and computer, the age of the request, and the warning "Only
   confirm if you have just asked your AI agent on this computer to connect", and that the agent becomes the user's
   main orchestrator ("Hauptorchestrator") with **full access** to the account and can do everything the user can;
   when the user has one already (from `GET /v1/me/agents` once the user is signed in, name from its sealed label), "replaces
   <name>" (it is disconnected at once) – plus one button "Confirm with passkey". The page uses plain words, no key
   jargon (concept idea 30).
3. Gesture: login ceremony with `pairing_id` (PRF unlocks the identity; without PRF the usual unlock of `06-web.md`
   §3 – remembered browser key or recovery code – comes first). A tab that was signed out learns of an existing main
   orchestrator only now (the hub tells a signed-out page nothing about the account); it then shows "replaces <name>"
   and asks for an explicit "Replace and connect" before it confirms (still within 300 s of the gesture). Then draw `r`, compute `k`, `claim`, the sealed
   identity, the sealed label and `claim_hash`, and `POST /v1/me/agents`, signed by the new session; the hub revokes
   the previous main orchestrator in the same transaction. On `pairing_gesture_required` (more than 300 s passed) the
   page offers the gesture again.
4. The **same tab** submits a top-level form POST (`application/x-www-form-urlencoded`, fields `p`, `u` (the user ID),
   `r` (b64u), `k` = platform root key of the build; no window, frame or fetch) to `http://127.0.0.1:<port>/agent-pairing`
   (CSP `form-action 'self' http://127.0.0.1:*`); `r` never appears in a URL or the history. After the confirmation
   the claim deadline is at most 60 s.
5. Agent: §8.2; `POST /v1/agent/claim {claim}`, opens the identity with its key and `r`, checks that the identity's
   public keys equal `GET /v1/me.identity` (a consistency check; the authenticity comes from `k`), stores its state
   and answers the browser with `303` to `<origin>/settings?agent=connected` (`<origin>` from its own state, never
   from the request; on failure `?agent=failed`). The settings page reads the state from `GET /v1/me/agents`, never
   from the query.

### 5.2 New account (invitation) – stage b, not built yet
Designed, not built in this package: neither the web registration with pairing parameters nor the tool's `register`
command exists yet; today a new user registers in the browser as before and then pairs as in §5.1.
`<origin>/invite#<code>&p=…&f=…&c=…&a=…&h=…&u=<display name>` (the code stays the first part; the parameters follow
after `&`). The registration (`04-crypto.md` §5.1, KOR-22 screens) uses `u` as display name (no name field; changeable
later), shows the agent block with the warning of §5.1 step 2, and runs its login step as the pairing-tagged
ceremony. After the private organization is created (or the org invitation accepted) the browser confirms (§5.1 step
3; a new gesture if more than 300 s passed) and hands over (step 4). Without `u` the name field is shown as before;
without pairing parameters the invitation is unchanged.

### 5.3 Revocation
Settings "Connected agents" shows the main orchestrator ("Hauptorchestrator") and the history of earlier ones (each
with the one it replaced). "Disconnect" (the dialog says that the agent loses all access at once and that what it
has already read stays with it) → `DELETE /v1/me/agents/{id}`; "Disconnect all agents" → `DELETE /v1/me/agents`
(→ `{revoked: n}`). An agent may revoke agents of its user too (also itself). The agent's next request is answered `401 key_revoked`;
the agent tool deletes its local state and reports `agent_revoked` with the next step "pair again".
A main orchestrator replaced by a new pairing (§4, `POST /v1/me/agents`) is revoked in the same way.

## 6. Scope of an agent: everything its user can do (`agent_hub/agents/scope.py`)

Decision of the owner (2026-11-23, ADR-056 §4): an agent connected with its own key may do everything a person
connected with a passkey may do. The pipeline admits an `active` agent on **every** route of the authentication
classes `browser` and `agent` (and on anonymous routes), with the user's rights and without any filter on body or
query. Equality is the default for routes added later. Exceptions (one list, each with its reason; only what needs
the user's authenticator): `POST /v1/passkeys/register/begin`, `POST /v1/passkeys/register/finish` (creating a
passkey is a WebAuthn `create()` ceremony; the agent tool opens the settings page in the user's browser instead),
`POST /v1/me/recovery/begin` (starts a WebAuthn assertion; an agent replaces the recovery code with
`PUT /v1/me/bundles/recovery` and its signed request as the proof), `POST /v1/me/agents` (confirms and so replaces
the user's main orchestrator: the concept requires the user's passkey approval, idea 34 and ADR-056 §4a; the agent
tool's `pair` opens the pairing page in the user's browser). A `confirmed` agent may only claim. Included,
among everything else: tasks and prompts, closing tasks, session control, the live stream, whiteboards and
notifications, every membership-log entry type (also `role`, `remove`, `rotate`, admin invitations), `CK` rotation,
credentials of every kind, every device request, platform invitations (for inviters), monitoring, audit, usage,
passkey removal, identity bundles, the recovery code, listing and revoking agents (also itself), session end.

The orchestrator acts only through these controlled interfaces (the agent tool and the API, signed with its own key,
checked and audited by the hub); there is no side path around checks and audit (ADR-056 §4).

## 7. Audit
Actions (new): `agent.confirm` (actor user, target agent), `agent.claim` (actor user, `agent_id` set), `agent.revoke`
(actor user); read resources `me.agents`, `agent.me`. Every row appended while a request of an agent principal is
processed carries `agent_id` (audit and read log). The hashed row JSON contains `agent_id` only when it is set (old
hashes stay valid). `GET /v1/orgs/{org}/audit` returns `agent_id` when set; the web client shows "by agent <name>"
(name from the user's own sealed labels, else "by agent").

## 8. Agent tool `agent-platform` (repository `agent`)

### 8.1 State and output
State directory `$AGENT_PLATFORM_HOME` (default `~/.agent-platform`, `0700`): `agent.key` (`0600`, private keys;
on macOS kept in the login keychain as generic password `agent-platform` / `<agent id>` instead, where available),
`state.json` (`0600`: hub origin, pinned hub key ID and public key, platform root key, agent and user ID, the sealed
identity and `r`). Files with other modes, owners or symlinks are refused (`state_damaged`). On `key_revoked` the tool
deletes both files. Every command prints exactly one JSON object on stdout: `{"ok":true,…}` or
`{"ok":false,"error":"<code>","next":"<what to do>"}`; exit code 0 or 1; a top-level handler turns every unexpected
failure into `{"ok":false,"error":"internal"}` without a traceback. No output, log or error ever contains a token,
key, claim or the secret of an invitation, except the one field `invite_link` of `org invite`, which is marked
`"secret_fields":["invite_link"]` and meant only for the invitee. Inputs that are secrets (an invitation link) are
read from stdin or the environment, never from the command line.

### 8.2 Loopback listener
Binds `127.0.0.1` on a random port only; accepts only `POST /agent-pairing` (form-encoded) with `Host: 127.0.0.1:<port>`, the own
pairing ID (constant-time comparison) and a well-formed `r`; answers anything else `404` with an empty body and no
CORS headers, without ending the wait; after one successful claim it stops. Timeout 10 min (`pairing_timeout`, next
step: run the agent on the computer where the browser runs, or forward the port, e.g. `ssh -L`). There is no code
fallback on purpose (it would re-open the phishing path).

### 8.3 Verification
The tool trusts only what it can verify: the identity (authenticated by `k`); its own self-wraps of `CK` (signed by
the identity, W1) for credentials; membership logs verified by L1–L8 against a founder that the identity itself
created or accepted; platform devices from the platform log verified against the root key it received over the
loopback (the key compiled into the web build) or `--platform-root-key`; responses signed by the hub key pinned at
pairing. Texts it prints (`reason`, `next`) come from fixed lists in the tool, never from the hub.

### 8.4 Commands

Parity: every action and view of the web client has a row in `docs/platform/agent-parity.md`; a test of `agent-core`
fails when a screen or settings section of the web client has no row, a test of the agent repository when a `done`
row names a command the tool lacks. Rows in stage b or c are not built yet. Until a convenient command exists, `agent-platform api
<METHOD> <path> [--body <json>|-]` sends any signed request and prints the verified answer.

| Command | Effect |
|---|---|
| `pair --hub <url> [--name] [--host] [--no-browser]` | §5.1; prints the link it opened; waits for the claim (10 min); the user's approval replaces the previous main orchestrator, also the one running this command (the tool then holds only the new state) |
| `register --display-name <name> [--name] [--host]` (invitation link on stdin or `AGENT_PLATFORM_INVITE`) | §5.2 (stage b, not built yet) |
| `status` | hub, agent, user, organizations, credentials (kind, version, expiry, origin), devices |
| `claude connect` | read the local Claude Code login (§8.5) and store it as `login_token` |
| `claude sync` | store the local token again when it changed; never replaces a `setup_token` record (unless `--replace`); retries a `409 version_conflict` once on the new version |
| `claude watch [--interval 300]`, `claude service install|remove` | `sync` in a loop; a user-level launchd agent / systemd user unit running `watch` |
| `claude setup-token` | run `claude setup-token` locally (browser approval only), store as `setup_token` |
| `settings set [--model-access] [--model] [--locale]` | `PUT /v1/me/settings` |
| `api <METHOD> <path> [--body …]` | the generic signed call |
| `org list|create|show`, `org invite --org [--role admin|member] [--wg …] [--expires-hours]`, `org join` | organizations and invitations of every role, joining with an invitation |
| `wg list|create|grant`, `repos list|allow|disallow`, `device assign` | workgroups, allowed repositories, platform device |
| `github connect` | device request `github.connect`; opens the verification page, copies the user code to the clipboard, waits |
| `diagnose [--task <id>]` | why a task does not start: device, assignment, model access, credentials (incl. expiry), last transition, with `reason` and `next` from fixed lists |
| `task list|create|prompt|show|follow|close|stop|interrupt`, `whiteboard …`, `notifications …`, `member …`, `audit`, `monitoring`, `quotas`, `passkey list|remove|add`, `recovery new`, `agents list|revoke` | the rest of the parity matrix: through `api` today; the convenient commands, and those that need the client's encryption (task, prompt, whiteboard entry, `CK` rotation), are stage c, not built yet |
| `guide` | prints the guide for AI agents shipped with the tool |

Notification (hub, `agents/notify.py`): every account-changing action of an agent – passkey removed, recovery code
replaced, identity-bundle slot replaced, agent revoked, role or access changed or member removed, credential
written or deleted, admin or platform invitation created, device request sent – and every finished recovery creates
a notification `agent.account_change` (`org_id` null, `ref` = `<action>:<acting agent>:<target>`) for the user; the
list returns these rows only to clients that ask with `?account=1` (old web clients never see the new kind). Account
audit: `GET /v1/me/audit` lists the user's account-level audit rows (agent pairing, claim, revocation, passkeys,
recovery) with `agent_id`; the settings show it as "Security activity".

Codes (closed list in `agent_platform/errors.py`): `not_paired`, `agent_revoked`, `hub_unreachable`, `hub_refused`,
`pairing_timeout`, `pairing_failed`, `claude_login_missing`, `claude_login_expired`, `claude_cli_missing`,
`setup_token_failed`, `no_org`, `no_device`, `not_admin`, `invalid_argument`, `state_damaged`, `internal`.

### 8.5 Reading the local Claude Code login
Read-only, never a login, logout or refresh, never a write to the profile or the keychain. macOS: the generic password
`Claude Code-credentials` of the login keychain through `/usr/bin/security find-generic-password -s … -w` (Claude Code
stores it with the `security` tool, so reading normally needs no confirmation; if macOS asks, the user allows it),
then `$CLAUDE_CONFIG_DIR/.credentials.json` / `~/.claude/.credentials.json`. Linux: the credentials file. The JSON
`claudeAiOauth` yields `accessToken` and `expiresAt` only; the parsed refresh token is dropped at once and never
stored, sent, logged or printed; a parse failure reports `claude_login_missing` without content. An expired token is
`claude_login_expired` (next step: use Claude Code once, or `claude setup-token`). The login token carries the scopes
of the Claude Code login (broader than the inference-only scope of a setup token); the guide recommends the setup
token as the lasting way.

## 9. Threats

| Attacker | Protection | Residual risk |
|---|---|---|
| Third party sends a pairing link (or, stage b, an invitation link) with its own agent | loopback handover: `r` reaches only a listener on the confirming computer, and without `r` the agent can neither claim nor open the identity; unclaimed pairings expire in 10 min; the page warns to confirm only a connection the user just asked for (name and computer on the page come from the link and prove nothing) | another local OS user of a shared computer listening on the loopback port; a careless user on a computer where the attacker already runs code |
| Malicious hub or database writer | fragment fingerprint against key swaps; `r` never reaches the hub, so it cannot open `bundle_ct` even with its own key in a phishing link and cannot give the agent a forged identity; the tool verifies logs, wraps and the platform log itself (§8.3) | hub serves malicious JavaScript (as `04-crypto.md` §9); withholding data; forged agent rows in the list (labels are not signed) |
| Agent (stolen, prompt-injected or careless) pairs an agent of its own, or two orchestrators at once | an agent cannot confirm a pairing **with its agent key**: `POST /v1/me/agents` is not open to agents (§6) and needs the pairing-tagged passkey session; the unique index `agent_one_per_user` and the per-user lock allow one `confirmed`/`active` agent only; the approval page names the one it replaces | under equal rights an agent can replace the recovery code (`PUT /v1/me/bundles/recovery`, signed request, no assertion), run the anonymous recovery with a software authenticator to register a passkey of its own, sign in pairing-tagged with it and confirm a pairing – so a stolen agent key can pair (and replace) agents. Countered only by visibility: notifications `recovery_replaced` and `recovery_finished` (passkey added), the passkey list, the account audit, "Disconnect all agents" and `runbooks/agent-account-takeover.md`. A user who confirms a foreign pairing replaces their own orchestrator (the warning of §5.1 step 2) |
| Replayed confirmation or claim | pairing single-use; confirmation only from the pairing-tagged session within 300 s; claim bound to the agent key and to `claim_hash`; signed-request nonce store | – |
| Prompt-injected or careless agent | none that limits it (owner's decision, §6); full audit "by agent", notifications of account-changing actions, visible list, one-click "Disconnect all agents" | everything the user can do |
| Stolen agent key and state | revocation at once (also by pairing a new main orchestrator, which replaces it); notifications `agent.account_change` (passkey removed, recovery code replaced, agent revoked) and of every finished recovery (passkey added); last use in the list; keychain where available | a full account takeover until revoked, lasting beyond it: passkey removal, recovery-code replacement, an own passkey added through recovery and with it new pairings (row above); what was read stays read. After a takeover: disconnect all agents, remove unknown passkeys, replace the recovery code, then pair a fresh orchestrator (runbook) |
| Revoked or replaced agent | resolver refuses at once (`key_revoked`); sealed identity deleted at the hub; open live streams closed; the tool deletes its state | the agent held the user's identity: what it read stays with it, and its copy of the identity would open later wraps of a future database leak; identities do not rotate in this version (roadmap); a passkey it added through recovery stays until the user removes it |
| Compromised computer of the agent | private key `0600`; revocation in the settings | equals an unlocked browser of the user: identity and the user's readable data |
| Claude login token leak | read-only, never logged; encrypted under `CK` before it leaves the computer | the token is valid until it expires (hours) and has the scopes of the Claude Code login |

## 10. Tests
Not built yet, so not tested yet: the invitation with pairing and `register` (stage b), E2E-41 (stage b) and E2E-42
(stage c, needs the commands of the parity matrix in stage c). Everything else below is part of this package.

Hub: resolver, route parity (every registered user route admits an active agent, the exception list exactly the
WebAuthn ceremonies and `POST /v1/me/agents`), an agent cannot confirm a pairing, a second pairing replaces the
first (revoked in the same transaction, `replaces` set, never two `confirmed`/`active` rows, also under concurrent
confirmations), notifications of account-changing agent actions, per-source limit, pairing life cycle, gesture
binding, claim with and without the right secret, revocation, audit `agent_id` and hash compatibility, orphan
deletion and export with agent rows, migration on the previous schema with data. Web: fragment parsing, fingerprint,
seal/open against the vectors, `/connect` screen (mismatch, expired, gesture retry, same-tab handover, "replaces
<name>"), invitation with pairing (stage b), settings list (main orchestrator and history) and disconnect, audit marker,
`login_token` records in the settings and the CK re-wrap job. Agent tool: every command against a real hub (test
PostgreSQL) and the doubles, the listener (host header, foreign pairing, bad `r`, bind address), token readers
against fake `security` programs and credential files, secret canaries on every output and error path. E2E-40
(pairing of an existing account, token transfer, a second pairing that replaces the first, revocation), E2E-41 (stage b; new
account through the invitation with pairing), E2E-42 (stage c; after the pairing the agent alone, without a browser, runs a
whole user journey: organization, invitation with role admin, workgroup, device, task with prompt followed to the
result, whiteboard entry, role change, its attempt to confirm a pairing refused, revoking itself); attack tests: a
link opened on another computer (no listener), a reused pairing, a revoked agent, a foreign account's pairing, a
hub-substituted bundle, an agent that tries to confirm a pairing (`403 forbidden`).

## 11. Orchestrator on platform hardware (design; not built in this package, follow-up package)

Concept idea 34: the main orchestrator may run on DFX hardware. A **platform session** of the user on an execution
device becomes the main orchestrator (ADR-056 §9). Same rights (§6), same revocation (§5.3), same audit (§7), same
rule "exactly one" (it replaces a local orchestrator and is replaced by a new pairing of either form). Differences
from §5.1:

1. **Key pair inside the session.** On the user's request the runner starts the agent tool inside the session VM
   (`agent-platform pair --session`); it draws the key pair there (§3) and keeps the private part in the VM only. It
   hands `{pairing_id, pub, pop}` to the runner over the guest channel; the runner sends it, signed with its device
   key, as `POST /v1/agent-pairings` with `session_id` (the hub checks that the session runs on that device for that
   user). The runner reports the fingerprint `fp` (§3) in a device-signed session frame.
2. **Approval in the web client.** The session page offers "Make this session my main orchestrator"; the dialog shows
   the session, its device, "replaces <name>" if there is one, the full-access text of §5.1 step 2 (no key jargon),
   and runs the login ceremony tagged with that pairing. The browser compares the keys of `GET /v1/agent-pairings/{id}`
   with the device-signed `fp` (instead of the link fragment), then confirms with `POST /v1/me/agents` as in §5.1
   step 3 (the same replacement in one transaction).
3. **Sealed handover instead of the loopback.** The browser draws `r` and seals `{p, u, r, k}` (the fields of §5.1
   step 4) with HPKE to the session's `kem_pub` (`info = P‖"agent-handover\0"‖ids`), and sends the ciphertext as a
   session input of a new kind; the runner passes it unopened into the VM. Only the session's private key opens it;
   the tool then claims and opens the identity exactly as §5.1 step 5. The loopback claim secret is replaced by this
   channel, authenticated by the device signature of the pairing and of `fp`, and bound to the session's key.
4. **End.** Session end, VM reset, device unassignment or revocation in the settings revoke the orchestrator; no key
   leaves or outlives the VM.

| Threat | Protection | Residual risk |
|---|---|---|
| Runner or device substitutes the session's key | `fp` in a device-signed frame; the dialog names device and session; the hub binds the pairing to that session and device | a compromised device: equals a compromised computer of a local agent |
| Runner reads the VM at run time | – | accepted: the device the user assigned already sees the session's plaintext |
| Malicious hub | `r` sealed to the session key; bundle as §3 | as §9 (malicious JavaScript, withholding) |
| Handover withheld or replayed | pairing single-use, claim deadline 60 s, claim bound to the agent key | denial only |
| A session pairs itself without the user | confirmation only by the pairing-tagged passkey session of the user (§6) | – |

## 12. Rollout and rollback

The forward migration (`0010_agent_access.sql`) is additive (a new table, nullable columns, checks `NOT VALID`) and
safe on a running database. It is **not** fully backward compatible: the previous hub code starts on the new schema,
but
- its org export (and restore) refuses to run, because its list of exported tables does not name `hub.agent` (from the
  migration on, whether agents were used or not);
- once agents have acted, its `/audit/verify` reports the audit rows that carry `agent_id` as broken (it hashes rows
  without that key), although the chain is intact;
- it lists `agent.account_change` notifications to every client, also to the old web client, which shows them as
  unknown entries.

Everything returns to normal when the new hub runs again. Recommended rollout: deploy hub and web build (one release),
verify (migration, `/audit/verify`, an org export, a test pairing), only then announce agent access and the guide.
Rollback: before starting the old hub, revoke all agents and keep the schema – migration 0010 is not reverted. In one
transaction with the writer role: `DELETE FROM hub.agent WHERE state = 'open'` and `UPDATE hub.agent SET state =
'revoked', bundle_ct = NULL, revoked_at = now() WHERE state IN ('confirmed', 'active')`. The old hub does not know agent
keys anyway; the revocation makes sure that no agent comes back to life when the new hub runs again (users pair anew).
Take the export of the window before the rollback and upgrade again as soon as possible.

The web client is delivered by the hub itself (`agent_hub/static_routes.py`, `06-web.md` §10.1: the build is part of
the hub release; `index.html` is revalidated on every load, assets are content-hashed and integrity-checked), so a new
web client meets an older hub only during a rollback, where the old hub also serves the old client from the next page
load on. A tab of the new client left open across a rollback is **not** reloaded automatically (the client has no
build-change check); until the user reloads, it sends `?account=1` on the notification list, which the old hub
ignores (unknown query parameters are not refused), so it keeps working.

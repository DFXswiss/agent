# 11 Delegated agent access (KOR-24)

Status: binding. Decided in ADR-056; the pairing from another computer (§5.4) in ADR-067 (KOR-42); the account of an
agent alone (§5.5, rule E-27) in ADR-071 (KOR-62). Extends
`02-data-model.md` (§2), `03-api.md` (§3, §4, §11), `04-crypto.md` (§3, §7, §9) and `06-web.md` (settings,
invitation). The user's own AI agent pairs with the account through one passkey gesture and then sets the account
up with the agent tool `agent-platform` (repository `agent`).

## 1. Terms

| Term | Meaning |
|---|---|
| Agent | A delegated access of one user, held by an AI agent on the user's computer, with its own key pair. |
| Main orchestrator | ("Hauptorchestrator" in the interface) the one `confirmed` or `active` agent of a user (concept idea 34, ADR-056 §4a); a new pairing replaces it. |
| Pairing | The agent's request to be connected; becomes the agent row once confirmed. Pairing ID = agent ID (UUIDv7, drawn by the agent). |
| Label | `{"name","host"}` chosen by the agent (e.g. `Claude Code`, the computer's name); only sealed at the hub. |
| Request message | The requester's optional personal message (rule E-28, KOR-64): the agent's in a pairing link (`t`), the inviter's in an org invitation package (`04-crypto.md` §4.6). Shown where the user approves, quoted as the requester's words, as plain text only. |
| Handover secret | `r`: 32 random bytes drawn by the confirming browser and handed to the agent **only** over the loopback – or, for a remote pairing, sealed to the agent's key and relayed by the hub (§5.4). It yields the bundle key and the claim (§3). |
| Remote pairing | A pairing of an agent on another computer than the confirming browser (server, container, SSH; ADR-067), chosen explicitly (`--remote`, `m=remote`, `remote` at the hub). |
| Agent code | 10 characters (`XXXXX-XXXXX`) the agent tool shows for a remote pairing and the user types on the page (§3); not a secret. |
| Return code | 8 characters (`XXXX-XXXX`) the page shows after confirming a remote pairing and the user gives to the agent; derived from `r`, checked by the tool before it claims (§3). |
| Account of an agent alone | (rule E-27, ADR-071) an account that an agent created through an invitation without a person (§5.5): no passkey, the agent holds the identity like after a pairing. "Account without a passkey" is the state the hub checks (no active `hub.passkey` row); an account of a person never reaches it (its last passkey cannot be removed, `last_passkey`). |
| Proof method | the named way an agent proves possession of its keys at a registration or recovery without WebAuthn (`method`, today only `agent-signature-v1`: Ed25519 signatures over the texts of §3) |

## 2. Data (hub schema, migrations `0010_agent_access.sql`, `0017_agent_remote_pairing.sql`)

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
Migration `0017_agent_remote_pairing.sql` (ADR-067, additive): `remote boolean NOT NULL DEFAULT false`, `nonce bytea
null` (16 bytes, set exactly for a remote row), `handover_ct bytea null` (only on a `confirmed` remote row: from the
confirmation until the claim; cleared at the claim, the revocation and – by the job `agents.unclaimed_purge` – the
claim deadline; exported as null). A remote pairing must be confirmed within 300 s of its creation (`expires_at` =
created + 300 s); its claim deadline is the confirmation + 600 s.

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
| Request message | 1–500 characters under the rules of the sealed label (no leading/trailing space, none of Cc, Cf, Zl, Zp, Cs, Co, Cn); `check_message` / `isMessageText`. It travels where the request's other texts travel and is never sent to the hub in clear: in the fragment of a pairing link like `a` and `h`, inside the sealed invitation package like `org_name` |
| Agent key ID | `agent.<32 hex>` = agent ID without dashes; signatures Ed25519 with `sig` (`agent-request-v1`) |
| Crockford base32 | `crockford32(bytes)`: the bit string big-endian, 5 bits per character of `0123456789ABCDEFGHJKMNPQRSTVWXYZ`, the last one padded with zeros |
| Agent code (remote) | `code = crockford32(SHA-256(P‖"agent-code\0"‖canonical({"fp","mode":"remote","nonce":b64u(nonce),"origin","pairing_id"})))[:10]`, shown `XXXXX-XXXXX`; `nonce` = the hub's 16 bytes of the pairing, `origin` = the hub's origin as in the link (`https://host[:port]`, no trailing slash) |
| Handover (remote) | `handover_ct = b64u(HPKE_seal(agent kem_pub, P‖"agent-handover\0"‖ids, canonical({"k":<platform root key of the build, b64u>,"r":b64u(r)})))` (base mode); the plaintext must be exactly canonical |
| Return code (remote) | `back = crockford32(HKDF(r, info = P‖"agent-return\0"‖ids))[:8]`, shown `XXXX-XXXX` |
| Typed code | input of either code: upper-cased, spaces and dashes removed, `I`/`L` read as `1`, `O` as `0`; then exactly 10 (8) characters of the alphabet, else refused |
| Account text (alone) | `P‖"agent-account\0"‖canonical({"agent":agent_id,"agent_pub":{kem,sig},"invite_id","method","pub":{kem,sig},"user":user_id})`; signed by `sk_inv` of the invitation (`inv_sig`), by the new identity (`id_sig`) and by the new agent key (`agent_sig`), §5.5 |
| Recover text (alone) | `P‖"agent-recover\0"‖canonical({"agent","agent_pub":{kem,sig},"challenge":b64u(32 B of recovery/begin),"method","user"})`; signed by the identity opened with the recovery code (the `proof` of `recovery/finish`) and by the new agent key (`agent_sig`), §5.5 |
| Own handover (alone) | the agent draws `r` itself and seals the identity to itself (`bundle_ct`), the label (`label_ct`) and `claim_hash` exactly as above; nothing is claimed, the hub stores the row `active` |

Why `r` is inside the bundle encryption: the hub never sees `r`, so neither a hub that swapped the agent key nor a hub
that put its own key into a phishing link can open `bundle_ct`, and a hub cannot hand the agent an identity of its own
choosing (AES-GCM under `k` authenticates the bundle). Vectors: `contracts/vectors/agent.json`; all three
implementations check them.

## 4. API

| Endpoint | Auth / right | Request → Response |
|---|---|---|
| `POST /v1/agent-pairings` | anon | `{pairing_id,pub:{kem,sig},pop,remote?}` → `{pairing_id,expires_at}`, for `remote: true` plus `remote: true, nonce` (b64u 16 B, drawn by the hub; deadline 300 s); without `remote` (or `false`) the answer is exactly as before; same ID with the same keys and mode → the stored answer, other keys, another mode or keys of another pairing → `409 id_conflict`; bad proof → `401 signature_invalid`; limits of §2 → `429 rate_limited` |
| `GET /v1/agent-pairings/{id}` | anon | → `{pairing_id,pub:{kem,sig},state:"open"|"confirmed",expires_at}` (a remote pairing adds `remote: true, nonce`) while open or confirmed and before the deadline; otherwise (unknown, claimed, revoked, expired) `404 pairing_unknown` |
| `POST /v1/passkeys/login/begin` | anon | `{pairing_id?}`: optional; a pairing that is not open before its deadline → `404 pairing_unknown`. The ceremony and the session it creates carry the pairing ID |
| `POST /v1/me/agents` | browser session; the active agent of an account without a passkey (KOR-62; an agent of an account with a passkey gets `403 forbidden`, §6) | `{pairing_id,bundle_ct,label_ct,claim_hash,handover_ct?}` → `{agent_id}`; `handover_ct` (§3) is required for a remote pairing (`400 handover_required`) and refused for a loopback pairing (`400 handover_unexpected`); a remote confirmation sets the claim deadline to confirmation + 600 s and notifies the user (`agent_paired_remote`, §8.4); the session must have been created by a ceremony tagged with this pairing at most 300 s ago (`403 pairing_gesture_required`); the pairing must be `open` before its deadline (`404 pairing_unknown`). In the same transaction the user's previous main orchestrator (`confirmed` or `active`) is revoked and recorded in `replaces` of the new row; its open live streams are closed. A repeat with identical values (including `handover_ct`) → the same answer (nothing replaced again, nobody notified again), other values → `409 id_conflict` |
| `GET /v1/me/agents` | user | → `{agents:[{agent_id,state:"confirmed"|"expired"|"active"|"revoked",label_ct,created_at,confirmed_at,claimed_at,last_used_at,revoked_at,replaces:<agent id>|null,remote}]}` of the user, newest first (`expired` = confirmed, never claimed, past the deadline; `replaces` = the main orchestrator this one replaced at its confirmation); at most one row is `confirmed` or `active` |
| `DELETE /v1/me/agents` | user | → `{revoked}`: every confirmed or active agent of the user revoked at once |
| `DELETE /v1/me/agents/{id}` | user | → `{}`; state `revoked`, sealed identity deleted; unknown or foreign → `404 not_found`; already revoked → `{}` |
| `POST /v1/agent/claim` | agent key, state `confirmed` (`agent:confirmed`) | `{claim}` (b64u 32 B, §3) → `{agent_id,user_id,bundle_ct,label_ct,created_at,claimed_at}`; wrong claim → `401 claim_invalid` (counted; the fifth wrong claim deletes the pairing); past the deadline → `404 pairing_unknown`; repeat when `active` with the right claim → the same answer |
| `GET /v1/agent/me` | agent key, state `active` (`agent`) | → `{agent_id,user_id,bundle_ct,label_ct,created_at,claimed_at}` |
| `GET /v1/agent/handover` | agent key, state `confirmed` (`agent:confirmed`) | → `{agent_id,user_id,handover_ct}` of a confirmed remote pairing before its claim deadline; a loopback row, a claimed, revoked or expired one → `404 pairing_unknown`; read resource `agent.handover` |
| `POST /v1/agent-accounts` | anon (KOR-62) | `{invite_id,user_id,identity:{kem_pub,sig_pub},recovery:{recovery_id,bundle_ct},cred_wrap:{epoch:1,wrap,sig},agent:{agent_id,pub:{kem,sig},bundle_ct,label_ct,claim_hash},proof:{method,inv_sig,id_sig,agent_sig}}` → `{user_id,agent_id}`; the invitation as for `register/begin` (open user invitation with a known `pk_inv`, else `404 invite_unknown`); `identity`, `recovery`, `cred_wrap` as for the first passkey of `register/finish` (`params_invalid`, `wrap_unauthorized`); a wrong signature `401 signature_invalid`; a taken user, recovery or agent ID or agent key `409 id_conflict`; sealed values under 48 bytes `400 request_invalid`; an identical repeat → the same answer, nothing changed. Stores the account without a passkey and the agent `active` (§5.5) |
| `POST /v1/recovery/finish` with `agent` | anon (KOR-62) | `{user_id,challenge,proof,agent:{agent_id,pub,bundle_ct,label_ct,claim_hash,method,agent_sig}}` → `{user_id,agent_id}`; `proof` over the recover text (§3); only for an account without a passkey (else `403 forbidden`, nothing consumed); a wrong signature `401 signature_invalid` (the challenge is used up as for a person) |
| `GET /agents.md` | anon (not under `/v1/`) | the guide for AI agents of the web build (`docs/platform/agents.md`), `text/markdown` |

Key resolver `agent.`: unknown, `open`, or `confirmed` past its deadline → `key_unknown`; `revoked` or user disabled
→ `key_revoked` (response-signed, like devices); `confirmed` → only `POST /v1/agent/claim` and `GET /v1/agent/handover`; `active` → §6. New auth
class `agent` (routes `/v1/agent/*`), principal kind `agent` (acting for its user, audit actor `user`). Rate limit as a
browser session (600/min per key). Error codes (new, `03-api.md` §11): `pairing_unknown` (404),
`pairing_gesture_required` (403), `claim_invalid` (401), `handover_required` (400), `handover_unexpected` (400);
client-only `pairing_mismatch`.

## 5. Flows

### 5.1 Existing account
1. Agent: generate fresh keys, `POST /v1/agent-pairings`, listen on `127.0.0.1:<random port>` (§8.2), open
   `<origin>/connect#p=<id>&f=<fp>&c=<port>&a=<name>&h=<host>[&t=<message>]` in the user's default browser (`t`: the
   optional request message of `pair --message`, §3).
2. Browser `/connect`: read and remove the fragment; `GET /v1/agent-pairings/{id}`; refuse unless state `open` and
   `fp` matches the returned keys (`pairing_mismatch`, "This link is not valid"). Show, as text the link supplied, the
   agent's name and computer, the agent's request message when the link has one (a quote marked as the agent's words,
   as stated by the link; without `t` nothing is shown), the age of the request, and the warning "Only
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

### 5.2 New account (invitation)
1. Agent (`register`, §8.4): reads the invitation link from stdin or `AGENT_PLATFORM_INVITE` (never the command line),
   checks it with `POST /v1/invites/lookup` (`invite_registered` → `hub_refused`), creates the pairing as in §5.1
   step 1 and opens `<origin>/invite#<code>&p=…&f=…&c=…&a=…&h=…[&t=…]&u=<display name>` (the code stays the first part; the
   parameters follow after `&`). The link carries the invitation code and is never printed.
2. Browser `/invite`: the parameters after the code must be exactly a pairing link (§5.1) and at most one `u` of 1–80
   characters after trimming; anything else, a fingerprint that does not match, or a pairing that is not open ends in
   the invalid state before anything is created ("This link is not valid" / "Connection request expired"). Without
   parameters the invitation is unchanged; without `u` the name field is shown as before.
3. The registration (`04-crypto.md` §5.1, the KOR-22 screens) shows the agent block – name and computer as the link
   states them, the agent's request message if the link has one, full access, main orchestrator, the warning of §5.1 step 2 – and uses `u` as display name (no name
   field; changeable later in the settings). The sign-in after the new passkey (gesture 2) is the pairing-tagged
   ceremony. An org invitation does the same, for a new user and for an existing account signing in from the page.
   If the pairing expired before the sign-in (`pairing_unknown`), the page says so and the next sign-in finishes the
   account without the agent.
4. After the private organization is created (or the org invitation accepted) the page continues on `/connect` with
   the pairing in the route state instead of a fragment: it confirms at once with the identity the registration
   unlocked (§5.1 step 3; the replacement question when the user has a main orchestrator; a new gesture on
   `pairing_gesture_required`) and hands over (§5.1 step 4). The user creates the passkey and does nothing else.
5. Agent: §5.1 step 5; `register` returns like `pair`, plus the invitation's `scope` and the display name.

### 5.5 Account of an agent alone (rule E-27, ADR-071)
No person, no passkey, no browser in any step. The invitation and the limits of a registration apply as for a person.

1. Agent (`register --own-account`, §8.4): reads the invitation link from stdin or `AGENT_PLATFORM_INVITE`, checks it
   with `POST /v1/invites/lookup`, reads `/v1/config` (hub key to pin; the platform root key unless
   `--platform-root-key` names it). It draws the user ID (UUIDv7), the identity, a recovery code with its recovery
   slot (`04-crypto.md` §4.1; a real code, not the unset slot: without a passkey the code is the account's only way to
   another computer), `CK_1` with its self-wrap signed by the identity, its own agent key pair and agent ID, and its own
   `r` (§3 "own handover"), and signs the account text three times.
2. Hub (`POST /v1/agent-accounts`): checks the invitation, the identity key, the self-wrap and the three signatures and
   stores in one transaction what a passkey registration stores without the passkey (principal, identity, settings,
   recovery slot, `cred` epoch 1 and the wrap, the invitation bound to the user) and the agent row `active`
   (`confirmed_at` = `claimed_at` = now, `replaces` null). Audit `agent.register` (`agent`, `invite_id`, `method`) and
   `invite.register`. The orphan job releases the account like any registration whose invitation stays unused.
3. Agent: stores its state exactly as after a pairing (§8.1) and does what the web client does after a registration:
   a platform invitation founds the private organization (`genesis{private:true}` with `platform_invite_id`, the
   profiles with its display name), an organization invitation is accepted (`org join`). The result carries
   `agent_id`, `user_id`, `display_name`, `scope`, the organization and the recovery code (`secret_fields`).
4. Move to another computer (`recover --hub`, the recovery code on stdin): `POST /v1/recovery/begin`, the recovery slot
   opened with `KEK_rec`, fresh agent keys and own `r`, `POST /v1/recovery/finish` with `agent` and the recover text
   signed by the identity and the new agent key. The hub refuses an account with a passkey (`403 forbidden`, before the
   challenge is used), else makes the new agent the active main orchestrator in one transaction (the previous one
   revoked, `replaces` set, its streams closed), marks the proof ceremony used, audits `auth.recovery {step: finish}`
   and `agent.register {agent, method}` and notifies the user (`recovery_finished`, target the new agent). The tool
   stores the state and replaces the recovery code at once (`PUT /v1/me/bundles/recovery`, `recovery new`).
5. Replacing the main orchestrator without the code: the new agent starts a remote pairing (`pair --remote`, §5.4);
   the current agent runs `agents confirm --link --code`, which does what the page does (link and hub agree, the typed
   agent code matches, identity, label and handover sealed with a fresh `r`) and `POST /v1/me/agents` signed with its
   agent key – admitted without a gesture because the account has no passkey; then it gives the new agent the return
   code and deletes its own state (it was replaced). For an account with a passkey the hub answers `403 forbidden`:
   the person's approval stays (concept idea 34).

An account of an agent alone is a valid state everywhere: lists, members, settings, recovery, audit, usage and
billing, leaving and removal work on users without regard to passkeys; the web client shows it as a member with its
display name. After a hub restore its agent row is exported revoked like every agent row: the agent takes the account
back with `recover` and the recovery code (not with a pairing). It never signs in to the web client (it has no passkey); a passkey added later through the recovery
of the web client makes it an account of a person (then §6's exception and idea 34 apply).

### 5.3 Revocation
Settings "Connected agents" shows the main orchestrator ("Hauptorchestrator") and the history of earlier ones (each
with the one it replaced). "Disconnect" (the dialog says that the agent loses all access at once and that what it
has already read stays with it) → `DELETE /v1/me/agents/{id}`; "Disconnect all agents" → `DELETE /v1/me/agents`
(→ `{revoked: n}`). An agent may revoke agents of its user too (also itself). The agent's next request is answered `401 key_revoked`;
the agent tool deletes its local state and reports `agent_revoked` with the next step "pair again".
A main orchestrator replaced by a new pairing (§4, `POST /v1/me/agents`) is revoked in the same way.

### 5.4 Pairing from another computer (remote, ADR-067)
For an agent that has no listener on the user's computer (server, container, SSH). Chosen explicitly only; the tool
never falls back from loopback to remote.

1. Agent (`pair --remote`, `register --remote`, §8.4): fresh keys, `POST /v1/agent-pairings {…, remote: true}`; the
   answer must echo `remote: true` and a nonce, else the tool reports `hub_too_old` (an old hub refuses the field with
   `400 request_invalid`). The tool computes the agent code (§3) and returns at once
   `<origin>/connect#p=<id>&f=<fp>&m=remote&a=<name>&h=<host>` (exactly these five parameters, plus `t` with
   `--message`) and the code; the agent
   gives both to its user in its conversation. The pending pairing stays in `pending.json` (§8.1).
2. Browser `/connect`: as §5.1 step 2, plus: the link's mode must agree with the hub's `remote` (both ways, else
   "This link is not valid"); a remote pairing needs a nonce. The page shows the warning "This AI agent runs on
   **another computer** (<host as the link states it>). Continue only if **you** just asked your own AI agent to
   connect and it showed you this page and a code. Whoever controls this agent gets full access to your account.",
   visually distinct from the loopback page (the replacement notice stays), and asks "Type the code your AI agent shows
   you". It computes the code from the link's fingerprint and pairing ID, the hub's nonce and its own origin, never
   shows it, and enables "Confirm with passkey" only when the typed code (normalised, §3) equals it.
3. Gesture and confirmation as §5.1 step 3; the body carries `handover_ct` (§3, `k` = platform root key of the build).
   No loopback form. The hub notifies every client of the user (`agent_paired_remote`) and gives 600 s to claim.
4. The page shows the return code with "Give this code to your AI agent" and "Waiting for your agent", and then the
   result from `GET /v1/me/agents` (the row turns `active`, or `expired`/`revoked`).
5. Agent (`pair --wait --return-code <code>`): `GET /v1/agent/handover` with its key, opens `handover_ct`, compares the
   user's code with the return code of `r` and only then claims (`POST /v1/agent/claim`), opens the identity, checks it
   as §5.1 step 5 and the root key against `/v1/config`, stores its state and deletes the pending pairing. One
   mismatch ends the pairing (`pairing_failed`): a hub that forged the handover (its own `r'`) does not know the code
   the page derived from the browser's `r` and gets one attempt with 40 bits.
6. New account (`/invite` with the remote parameters, §5.2): the page shows the warning and asks for the agent code
   **before** the passkey is created (a wrong or missing code creates no account); the mode travels in the route
   state to `/connect`, which confirms with the identity the registration unlocked, sends `handover_ct` and shows the
   return code.

Banner: while the user's main orchestrator (`confirmed` or `active`) is a remote one, every web client shows in the
shell "Your main orchestrator runs on another computer (<host>), connected <time>" with "Disconnect"; it comes from
the row (`remote` of `GET /v1/me/agents`), so no read state of a notification hides it; a client that is open while
the pairing is confirmed elsewhere loads the list again when the notification `agent_paired_remote` (or
`agent_revoked`) arrives, so the banner appears there at once. Both notification and banner
are in-app only (no e-mail or push in this version). The settings list marks such rows "another computer".

## 6. Scope of an agent: everything its user can do (`agent_hub/agents/scope.py`)

Decision of the owner (2026-11-23, ADR-056 §4): an agent connected with its own key may do everything a person
connected with a passkey may do. The pipeline admits an `active` agent on **every** route of the authentication
classes `browser` and `agent` (and on anonymous routes), with the user's rights and without any filter on body or
query. Equality is the default for routes added later. Exceptions (one list, each with its reason; only what needs
the user's authenticator): `POST /v1/passkeys/register/begin`, `POST /v1/passkeys/register/finish` (creating a
passkey is a WebAuthn `create()` ceremony; the agent tool opens the settings page in the user's browser instead),
`POST /v1/me/recovery/begin` (starts a WebAuthn assertion; an agent replaces the recovery code with
`PUT /v1/me/bundles/recovery` and its signed request as the proof). `POST /v1/me/agents` is admitted and decided by
its handler (KOR-62): an agent of an account with a passkey gets `403 forbidden` (the concept requires the person's
passkey approval to replace the main orchestrator, idea 34 and ADR-056 §4a; the agent tool's `pair` opens the pairing
page in the user's browser), the agent of an account without a passkey confirms its successor itself (§5.5 step 5) –
the only step that still needs a person concerns accounts of persons only. A `confirmed` agent may only claim. Included,
among everything else: tasks and prompts, closing tasks, session control, the live stream, whiteboards and
notifications, every membership-log entry type (also `role`, `remove`, `rotate`, admin invitations), `CK` rotation,
credentials of every kind, every device request, platform invitations (for inviters), monitoring, audit, usage,
passkey removal, identity bundles, the recovery code, listing and revoking agents (also itself), session end.

The orchestrator acts only through these controlled interfaces (the agent tool and the API, signed with its own key,
checked and audited by the hub); there is no side path around checks and audit (ADR-056 §4).

## 7. Audit
Actions (new): `agent.confirm` (actor user, target agent), `agent.claim` (actor user, `agent_id` set), `agent.revoke`
(actor user); read resources `me.agents`, `agent.me`, `agent.handover`. The details of `agent.confirm` and
`agent.claim` of a remote pairing carry `remote: true` (ADR-067). Every row appended while a request of an agent principal is
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
`"secret_fields":["invite_link"]` and meant only for the invitee (and of `register --remote`, meant only for the
user). Inputs that are secrets (an invitation link) are read from stdin or the environment, never from the command
line. A remote pairing (§5.4) that is not finished yet is kept as `pending.json` (`0600`: hub, pinned hub key,
platform root key of `/v1/config`, pairing ID, nonce, mode, kind `pair`/`register`, deadline, label) with its private
keys in the keychain item `agent-platform` / `<pairing id>` where available, else a `0600` key file; an existing
paired state stays until the new pairing completes. Every terminal case of `pair --wait` (success, timeout, failure)
deletes both.

### 8.2 Loopback listener
Binds `127.0.0.1` on a random port only; accepts only `POST /agent-pairing` (form-encoded) with `Host: 127.0.0.1:<port>`, the own
pairing ID (constant-time comparison) and a well-formed `r`; answers anything else `404` with an empty body and no
CORS headers, without ending the wait; after one successful claim it stops. Timeout 10 min (`pairing_timeout`, next
step: run the agent on the computer where the browser runs, forward the port, e.g. `ssh -L`, or pair with `--remote`,
§5.4). There is no code
fallback for the loopback pairing on purpose (it would re-open the phishing path); the remote pairing (§5.4) is a
separate mode chosen explicitly, with its own countermeasures.

### 8.3 Verification
The tool trusts only what it can verify: the identity (authenticated by `k`); its own self-wraps of `CK` (signed by
the identity, W1) for credentials; membership logs verified by L1–L8 against a founder that the identity itself
created or accepted; platform devices from the platform log verified against the root key it received over the
loopback (the key compiled into the web build) or `--platform-root-key`; responses signed by the hub key pinned at
pairing. Texts it prints (`reason`, `next`) come from fixed lists in the tool, never from the hub.

### 8.4 Commands

Parity (rules E-04 and E-19 of KOR-42): every action and view of the web client has a row in
`docs/platform/agent-parity.md` and its own command; `tests/platform/test_agent_parity.py` reads the web client's
source, the hub's registered routes and the tool's parser of the same checkout and fails when a screen, settings
section or route of the web client has no row, when a route the browser calls has no `done` row (or only `n/a` and
`pending` rows with their reason), when a `done` row names a command the tool lacks, or when a command has no example
in the agent guide. `agent-platform api <METHOD> <path> [--body <json>|-]` sends any signed request and prints the
verified answer; it is a fallback, not the way for any action of the web client. One row per command group (the
commands and options as `agent-platform <group> --help` lists them):

| Command | Effect |
|---|---|
| `pair --hub <url> [--name] [--host] [--no-browser] [--message <text>\|-]` | §5.1; `--message` puts the request message (§3) into the link as `t` (`-`: stdin); prints the link it opened; waits for the claim (10 min); the user's approval replaces the previous main orchestrator, also the one running this command (the tool then holds only the new state) |
| `register --display-name <name> [--name] [--host] [--message <text>\|-]` (invitation link on stdin or `AGENT_PLATFORM_INVITE`) | §5.2; waits like `pair`; never prints the link; `--message -` reads stdin only when the link comes from `AGENT_PLATFORM_INVITE` (`invalid_argument` otherwise) |
| `pair --hub <url> --remote [--name] [--host] [--replace-pending] [--message <text>\|-]` | §5.4; refuses while a pending pairing exists (`pairing_pending`) unless `--replace-pending`; returns at once `{"state":"waiting","connect_link","code","expires_at"}`; never falls back from or to loopback (`hub_too_old` when the hub does not echo `remote` and a nonce) |
| `register --own-account --display-name <name> [--name] [--host] [--platform-root-key]` (invitation link on stdin or `AGENT_PLATFORM_INVITE`) | §5.5: an account of this agent alone, no person, no browser; returns at once like `register` plus the organization and `recovery_code` (`secret_fields`) |
| `recover --hub <url> [--name] [--host] [--platform-root-key]` (recovery code on stdin or `AGENT_PLATFORM_RECOVERY_CODE`) | §5.5 step 4: this computer's agent becomes the main orchestrator of an account without a passkey (the previous one is revoked); prints the new recovery code once (`secret_fields`); unknown code: `invalid_argument` with `reason: recovery_code_unknown`; an account with a passkey: `not_own_account` |
| `register --remote --display-name <name> [--name] [--host] [--replace-pending] [--message <text>\|-]` | §5.4 step 6 with the invitation: returns `invite_link` (`<origin>/invite#<code>&p=…&f=…&m=remote&a=…&h=…[&t=…]&u=…`, listed in `secret_fields`), the code and the deadline |
| `pair --wait [--return-code <code>] [--timeout <s>]` | the pending remote pairing (`no_pending_pairing`; a second `--wait` at the same time: `pairing_busy`). Without `--return-code`: polls every 2 s for at most `--timeout` (default 120, max 600) and returns `"state":"waiting"` or `"state":"confirmed"` (next: ask the user for the code the page shows). With `--return-code`: fetches and opens the handover, checks the code, only then claims, stores the state and returns like `pair`. Expired: `pairing_timeout`; a handover that does not open or a wrong code: `pairing_failed` |
| `status` | hub, agent, user, organizations, credentials (kind, version, expiry, origin), devices |
| `claude connect` | read the local Claude Code login (§8.5) and store it as `login_token` |
| `claude sync` | store the local token again when it changed; never replaces a `setup_token` record (unless `--replace`); retries a `409 version_conflict` once on the new version |
| `claude watch [--interval 300]`, `claude service install|remove` | `sync` in a loop; a user-level launchd agent / systemd user unit running `watch` |
| `claude setup-token` | run `claude setup-token` locally (browser approval only), store as `setup_token` |
| `settings set [--model-access] [--model] [--locale]` | `PUT /v1/me/settings` |
| `api <METHOD> <path> [--body …]` | the generic signed call |
| `org list|create|show|leave|rotate|catch-up`, `org invite --org [--role admin|member] [--wg …] [--expires-hours] [--message <text>|-] [--name]`, `org invite-revoke --org --invite`, `org join`, `org admit --org --user <id|account code> --name [--role admin|member] [--wg …]`, `org member-name --org --user <id|account code|invite id> --name`, `org invitees [--org]` | organizations and invitations of every role (`show` gives an admin the state of each user invitation: open, opened, registered, accepted, KOR-36), joining with an invitation (the result carries the inviter's request message as `message` when the package has one; a log with an `admit` is verified with the platform log; an `accept` the hub stored although its answer got lost counts as joined once the membership and the verified log show it), admitting an existing account directly and the accounts of the user's own platform invitations (ADR-060; every identity verified against the platform log like in the web client), leaving, owed key rotations and catch-up wraps; names of people (ADR-073: `org show` lists `name_source` per member and writes the user's own name into an org opened without one) |
| `member list|role|remove --org` | the members with their role and access (explicit, or `inherited` as an admin, ADR-058); role changes and removal with the key rotation that follows |
| `wg list|create|rename|grant|ungrant|archive|settings`, `wg placement [--device <id> [--vm <id>]|--auto]`, `repos list|allow|disallow --wg` | workgroups (`my_access`, `my_access_inherited`) and their default device (ADR-075), grants (`ungrant` acts on the explicit access only), the workgroup settings and allowed repositories |
| `device list|assign|unassign|rename|invite`, `device assign --wg <id>|--org <id>`, `device unassign --org <id> --device <id>` | execution devices: the platform device from the verified platform log, an own computer through a device invitation; with their VMs and load, assigned to a workgroup or to the whole organization (ADR-075) |
| `device add|remove --this-computer` | the computer the agent runs on as an own execution device (ADR-074): checks of the Mac (fixable ones opened, E-05), the release of the hub's download area verified with the release keys of this installation (`latest.json` signature and expiry, package digest), a device invitation (`--wg`, else the default workgroup), the code in a 0600 request file, **one** administrator dialog (`osascript`, the root script of `agent_runner.install.admin`; none when the caller is root or its `sudo -n true` succeeds: the same script runs under `sudo -n`, `admin` in the result names the way, E-27; `add` refuses a root caller, `owner_root`), then the public state `state.json` until the enrollment; `remove` unpairs (`remove` with rotations) and runs the helper's removal in one dialog, printing `removed` and `leftovers`. Errors `mac_unsupported`, `release_unavailable`, `admin_cancelled`, `install_failed`, `device_installed`, `not_installed` |
| `credentials list|set-api-key|set-mailbox|check|delete|rotate`, `claude login`, `github status|connect|disconnect|accounts|primary|remove|rewrap`, `github default set` | credentials sealed under `CK` on this computer, checks and logins through a device request, the `CK` rotation the web client's job runs; `github connect --token -` (ADR-065) reads an existing GitHub token from stdin only and sends it only inside the `github.connect` request sealed to the device, which checks and stores it; `github connect --from-gh` (KOR-65) does the same with the token of this computer's GitHub CLI login (`gh auth token --hostname github.com`; none: `github_login_missing`), so an agent connects GitHub without a browser in one command; `github connect --app oauth_app\|github_app` names the device flow (default: the device's preferred one, the OAuth App when set up); `github status` names login, scopes, source and whether the platform minted the token, `disconnect` whether the devices revoke it at GitHub |
| `github accounts`, `github connect --org|--identity|--signing-key`, `github primary|remove --identity`, `github default set --org|--wg`, `task create --github-identity`, `task github-identity` (KOR-47, ADR-078) | the GitHub identities like the web client: own accounts opened and checked on this computer, company accounts by metadata only (no client opens them, KM-35); a company connection's answered body is sealed under a fresh epoch of the organization's credential key for its devices and stored; defaults and the task's account; `task operations` names the account and the triggering human |
| `diagnose [--task <id>]` | why a task does not start: device, assignment, model access, credentials (incl. expiry), last transition, with `reason` and `next` from fixed lists |
| `task list|create|prompt|show|follow|close|operations|overview|charges|github-identity` | tasks sealed and opened on the user's computer like the web client (`task.meta` with the derived title and `prompt` under the workgroup's current epoch, signed by the identity; reading runs the reader checks and opens with the user's own workgroup key; `--text -` reads stdin; the chat includes the work VM runs and states and the guest's `session.error` events, and folds the `waiting` status events of held operations into one card per reason and addressee (`for_user`, else the task owner) with its state and, for that person, the next step: `github connect`, or for a repository the connection does not reach (`github_access`, KOR-65) the `action` and the link (`url`) of the one step (install the GitHub App, request an organisation's approval, authorise SSO, create a token, open the repository) (ADR-066); `show` gives the task's runtime; `follow` returns the new chat events after a position and `next_after` for the next call, waiting up to `--wait` seconds for the first), the operations with their verified events, the overview of every readable workgroup, the task's charges per session (`GET /v1/tasks/{task}/charges`, KOR-31) |
| `session show|stop|interrupt|terminal|workvm --session` | a session with transitions, usage, VM and children, its runtime and whether the hub reaches its device, its control, the terminal output as text, and its work VM (KOR-29, ADR-059: state, size, runtime of the session and the task, runs) |
| `whiteboard list|history|add|edit|delete` | whiteboard entries of an organization, workgroup or task, sealed on this computer |
| `billing --org` | the organization's credit (KOR-31, ADR-062, `GET /v1/orgs/{org}/billing`): balance, `exhausted`, `billing_active`, the customer prices of the price list in force, the credit history and the consumption per day and per workgroup; read only, there is no top-up |
| `community status|key|serve|messages|post|delete|mutes|mute|unmute` | the Community (KOR-26, `14-community.md`, ADR-069): join and state; the key flow of `14-community.md` §4.2 on this computer (create epoch 1, open the self-wrap, or a key request whose one-off X25519 key stays in `state.json` until a holder's wrap is opened, checked and replaced by the self-wrap; a wrong wrap is rejected; `key --wait`); serving open requests of others; messages verified under the authors' pinned identity keys and opened with the key (what fails a check carries no content), the read position; posting (name and text cleaned and checked like the web client; a post refused with `429` names `retry_after` and is sent byte for byte again); deleting own messages; muting as a moderator |
| `notifications list|read`, `quotas`, `monitoring`, `audit` | the user's notifications, subscription quotas (with `unreadable_since` when the subscription refuses the read), monitoring of devices, VMs, sessions and work VMs, account and org audit (with the chain check) |
| `agents confirm --link <connect link> --code <agent code>` | §5.5 step 5: the main orchestrator of an account without a passkey confirms a remote pairing; returns `agent_id`, `replaced` (itself) and `return_code` for the new agent, then deletes its own state; `pairing_failed` (`pairing_mismatch`, `agent_code_mismatch`), `not_own_account` |
| `passkey list|remove|add`, `recovery new`, `agents list|revoke`, `platform invite|list|revoke`, `security` | the account's security: passkeys (`add` opens the settings page), a new recovery code, the agents, platform invitations (inviters), the web build check |
| `guide [--topic T]` | prints the short start for AI agents shipped with the tool (KOR-63, E-26), or the sections of the full guide whose heading matches `T` (`all`: the whole guide); `topics` lists them |
| `search <words>` | at most five commands for the words (English or German), best first: purpose, usage, example, usual next step, all from the command tree (`commands.Doc`, KOR-63); no hit gives the command groups and the guide's topics |
| `mcp`, `install-connector` | a local MCP server over stdio with the tools `search` and `run` (one command of the tool, in process: same keys, rights and audit marking; no code, no shell) for chat programs without a command line, and its entry in the chat program's configuration (KOR-63) |

Commands whose output grows with the data take `--limit N` and `--fields a,b` (`commands.trimmed`, KOR-63): the
result's lists keep at most N items (the last ones for `task show` and `session terminal`, whose text keeps its
last N lines), their objects only the named keys; a cut list is named under `limited`, and `next_after` of
`task follow` moves behind the last event returned. Without the options the output is unchanged.

The browser's background jobs (06-web.md §11 jobs 3-5) run around the commands (`agent_platform/upkeep.py`): before
every command that writes into an org or stores a credential, the owed pending rotations of every org of the user that
the user may send and the `CK` check; after it, catch-up wrapping of every org; `status` and every round of
`claude watch` run both. A scope whose rotation is still owed is never sealed under (`rotation_pending`). The result
names what was done under `upkeep` (only when something was done or failed). In the `CK` check an org whose log does
not verify entitles nobody; an org whose log the hub refuses makes the check wait (`reason: org_log_unavailable`); a
loss a rotation handled is kept in `state.json` (`handled_losses`) and not rotated for again, like the browser's
per-sign-in set. A
retry of `pair --wait` that finds the pairing gone after a claim was sent is `pairing_failed` with
`reason: claim_lost` (the user disconnects that agent in the settings).

Notification (hub, `agents/notify.py`): every account-changing action of an agent – passkey removed, recovery code
replaced, identity-bundle slot replaced, agent revoked, role or access changed or member removed, credential
written or deleted, admin or platform invitation created, device request sent – and every finished recovery creates
a notification `agent.account_change` (`org_id` null, `ref` = `<action>:<acting agent>:<target>`) for the user, and so
does the confirmation of a remote pairing by a browser session (`agent_paired_remote::<new agent>`, ADR-067); the
list returns these rows only to clients that ask with `?account=1` (old web clients never see the new kind). Account
audit: `GET /v1/me/audit` lists the user's account-level audit rows (agent pairing, claim, revocation, passkeys,
recovery) with `agent_id`; the settings show it as "Security activity".

Codes (closed list in `agent_platform/errors.py`): `not_paired`, `agent_revoked`, `hub_unreachable`, `hub_refused`,
`pairing_timeout`, `pairing_failed`, `claude_login_missing`, `claude_login_expired`, `claude_cli_missing`,
`setup_token_failed`, `no_org`, `no_device`, `not_admin`, `invalid_argument`, `state_damaged`, `internal`, `no_write_access`, `not_inviter`,
`device_request_failed`, `hub_too_old`, `pairing_pending`, `no_pending_pairing`, `pairing_busy`, `last_admin`, `not_found`, `credentials_blocked`,
`rotation_pending`, `community_key_missing`, `not_own_account` (KOR-62), `mac_unsupported`, `release_unavailable`,
`admin_cancelled`, `install_failed`, `device_installed`, `not_installed` (KOR-59), `github_login_missing` (KOR-65).
Every fixed text that names a step with a person has a row in the table "Without a person" of
`docs/platform/agent-parity.md` with the way without one (`tests/platform/test_without_person.py`).

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
| Third party sends a pairing link (or an invitation link with a pairing) with its own agent | loopback handover: `r` reaches only a listener on the confirming computer, and without `r` the agent can neither claim nor open the identity; unclaimed pairings expire in 10 min; the page warns to confirm only a connection the user just asked for (name and computer on the page come from the link and prove nothing) | another local OS user of a shared computer listening on the loopback port; a careless user on a computer where the attacker already runs code |
| Malicious hub or database writer | fragment fingerprint against key swaps; `r` never reaches the hub, so it cannot open `bundle_ct` even with its own key in a phishing link and cannot give the agent a forged identity; the tool verifies logs, wraps and the platform log itself (§8.3) | hub serves malicious JavaScript (as `04-crypto.md` §9); withholding data; forged agent rows in the list (labels are not signed) |
| Agent (stolen, prompt-injected or careless) pairs an agent of its own, or two orchestrators at once | an agent cannot confirm a pairing **with its agent key**: `POST /v1/me/agents` is not open to agents (§6) and needs the pairing-tagged passkey session; the unique index `agent_one_per_user` and the per-user lock allow one `confirmed`/`active` agent only; the approval page names the one it replaces | under equal rights an agent can replace the recovery code (`PUT /v1/me/bundles/recovery`, signed request, no assertion), run the anonymous recovery with a software authenticator to register a passkey of its own, sign in pairing-tagged with it and confirm a pairing – so a stolen agent key can pair (and replace) agents. Countered only by visibility: notifications `recovery_replaced` and `recovery_finished` (passkey added), the passkey list, the account audit, "Disconnect all agents" and `runbooks/agent-account-takeover.md`. A user who confirms a foreign pairing replaces their own orchestrator (the warning of §5.1 step 2) |
| Replayed confirmation or claim | pairing single-use; confirmation only from the pairing-tagged session within 300 s; claim bound to the agent key and to `claim_hash`; signed-request nonce store | – |
| Prompt-injected or careless agent | none that limits it (owner's decision, §6); full audit "by agent", notifications of account-changing actions, visible list, one-click "Disconnect all agents" | everything the user can do |
| Stolen agent key and state | revocation at once (also by pairing a new main orchestrator, which replaces it); notifications `agent.account_change` (passkey removed, recovery code replaced, agent revoked) and of every finished recovery (passkey added); last use in the list; keychain where available | a full account takeover until revoked, lasting beyond it: passkey removal, recovery-code replacement, an own passkey added through recovery and with it new pairings (row above); what was read stays read. After a takeover: disconnect all agents, remove unknown passkeys, replace the recovery code, then pair a fresh orchestrator (runbook) |
| Revoked or replaced agent | resolver refuses at once (`key_revoked`); sealed identity deleted at the hub; open live streams closed; the tool deletes its state | the agent held the user's identity: what it read stays with it, and its copy of the identity would open later wraps of a future database leak; identities do not rotate in this version (roadmap); a passkey it added through recovery stays until the user removes it |
| Compromised computer of the agent | private key `0600`; revocation in the settings | equals an unlocked browser of the user: identity and the user's readable data |
| Claude login token leak | read-only, never logged; encrypted under `CK` before it leaves the computer | the token is valid until it expires (hours) and has the scopes of the Claude Code login |
| Third party sends the user a **remote** link of its own agent (with its code), ADR-067 | explicit mode, the warning of §5.4, the typed agent code, 300 s deadline, the return code the user must hand to *that* agent, notification on every client, banner while a remote orchestrator is active, the list with its computer name, one-click "Disconnect" | **higher than loopback:** a user persuaded to open the third party's link, type its code and send the return code back confirms the third party's agent – a full account access until revoked (device-code phishing class); accepted by rule E-04 and reading A3 of `KONZEPT.md` |
| Same through an invitation link (new account) | as above; the code is asked before the passkey is created | no other client receives the notification then; the banner appears at the next sign-in |
| Malicious hub forges the remote handover (own `r'`, own identity) | return code derived from the browser's `r`, checked by the tool before anything is claimed or stored | 40 bits, one attempt |
| Malicious hub swaps the agent key or flips the mode | fingerprint from the link; the page compares `m` with `remote`; the hub requires/refuses `handover_ct` by mode | as above (malicious JavaScript of the hub) |
| Phished agent hides the notification (marks it read) | banner from the agent row, not from a read state; audit | the agent can disconnect itself and the banner goes away – then it has no access any more |
| Attacker grinds a pairing with the same agent code | the hub's nonce in the code input: one hub registration per attempt, rate-limited (§2) | – |
| Someone reads the link, the agent code or `handover_ct` | none is a secret; `handover_ct` sealed to the agent's key, single use, cleared at claim, revocation and deadline | – |
| Someone reads the return code | useful only to the holder of the agent's private key, within the claim deadline | – |
| Someone registers an account of an agent alone (KOR-62, ADR-071) | only with an invitation: `inv_sig` of `sk_inv` over the account text; the invitation is bound once; the limits of a registration apply | the holder of an invitation code gets an account – exactly as with a passkey registration (the code is the authorisation, rule E-27) |
| Hub or database writer swaps keys of an agent account | the hub never sees the identity's private key; `id_sig` and `agent_sig` bind identity, agent and invitation, and the tool keeps its own state (§8.3) | as for every account: malicious hub withholding data; a hub can register an account of its own choosing only with an invitation code it never sees |
| Stolen recovery code of an account without a passkey | `recovery/begin` limits (5 per hour per source, 20 per day per code); the move revokes the current agent (its next request: `key_revoked`), the user is notified (`recovery_finished`), audit `agent.register` | full takeover of that account, as a stolen recovery code of a person (§9 row "stolen agent key"); keep the code like a private key |
| Stolen agent key of an account without a passkey | revocation by a recovery with the code (the stolen key is revoked); `agents list`, audit | the thief can replace the main orchestrator itself (`agents confirm`) and the recovery code (`recovery new`); the holder of the code can take it back only until the thief replaced the code. Same class as the row "stolen agent key" for persons, without a person to notice |
| Agent of a person's account tries the passkey-free paths | `POST /v1/me/agents` by an agent and `recovery/finish` with `agent` are refused for an account with a passkey (`403 forbidden`); the last passkey of an account cannot be removed (`last_passkey`), so an agent cannot turn a person's account into one without a passkey | – |

## 10. Tests
The end-to-end scenarios of the agent access are E2E-49 and E2E-50 (`07-testing.md` §2.1; E2E-40 and E2E-41 there
are other scenarios). Everything else below is part of this package.

Hub: resolver, route parity (every registered user route admits an active agent, the exception list exactly the
WebAuthn ceremonies and `POST /v1/me/agents`), an agent cannot confirm a pairing, a second pairing replaces the
first (revoked in the same transaction, `replaces` set, never two `confirmed`/`active` rows, also under concurrent
confirmations), notifications of account-changing agent actions, per-source limit, pairing life cycle, gesture
binding, claim with and without the right secret, revocation, audit `agent_id` and hash compatibility, orphan
deletion and export with agent rows, migration on the previous schema with data. Web: fragment parsing, fingerprint,
seal/open against the vectors, `/connect` screen (mismatch, expired, gesture retry, same-tab handover, "replaces
<name>"), invitation with pairing (platform and org invitation, broken link, expired request, expiry before the sign-in, `/connect` resumed from the route state), settings list (main orchestrator and history) and disconnect, audit marker,
`login_token` records in the settings and the CK re-wrap job. Agent tool: every command against a real hub (test
PostgreSQL) and the doubles, the listener (host header, foreign pairing, bad `r`, bind address), token readers
against fake `security` programs and credential files, secret canaries on every output and error path, every command
of §8.4 against the hub fake with the web client's checks and crypto (also the work VM panel, the invitees and the
inherited access of admins), and the parity test of §8.4 (web source, hub routes, parser and guide). E2E-49
(the loopback pairing of an existing account, then the agent alone, without a browser, runs a whole user journey:
team organization, invitation, workgroup with a grant, platform device, task with prompt followed to the result,
further prompt, interrupt, stop and close, whiteboard entry, member removal with key rotation, its attempt to confirm
a pairing refused, revoking itself), E2E-50 (the pairing from another computer below, a second pairing that replaces
the first); the new account through the invitation with pairing is covered by the web client tests; attack tests: a
link opened on another computer (no listener), a reused pairing, a revoked agent, a foreign account's pairing, a
hub-substituted bundle, an agent that tries to confirm a pairing (`403 forbidden`).

Remote pairing (ADR-067, KOR-42): vectors of §3 (agent code, handover, return code, typed forms) in all three
implementations; hub: creation and lookup with nonce and 300 s, `handover_required`/`handover_unexpected`, the 600 s
claim deadline, `GET /v1/agent/handover` only for confirmed remote rows, `handover_ct` cleared at claim, revocation
and deadline, notification and audit details, `remote` in the list, loopback answers unchanged, migration 0017 on the
previous schema with data; web: link parsing with `m=remote`, mode mismatch both ways, code input (normalisation,
wrong code keeps the button disabled), return code, waiting state, banner, invitation asks the code before the
passkey; tool: `pair --remote`, every outcome of `--wait`, `hub_too_old`, pending state removed in every terminal
case, `register --remote`, secret canaries, an end-to-end run against the hub fake whose "browser" seals the
handover; E2E-50 against the real hub and web client.

Account of an agent alone (KOR-62, ADR-071): vectors of §3 (account and recover texts, their signatures, unknown
methods) in all three implementations; hub: `POST /v1/agent-accounts` with a platform and an org invitation, every
refusal, the identical repeat, the orphan job, `recovery/finish` with `agent` (replacement, passkey account refused
with the ceremony kept, wrong signatures, rollback on a taken key), `POST /v1/me/agents` by the agent of an account
without and with a passkey; tool: `register --own-account`, `recover`, `agents confirm` against the real hub (the whole
journey: register, private org, team org, a second agent through an org invitation, a person joining, the move, the
hand-over) and their refusals; the table "Without a person" (`test_without_person.py`); E2E-54 ("agent alone": no
browser step, no passkey, plus the person's web view of the agent's membership and vice versa).

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
   session input of a new kind (the label `agent-handover` is now defined by the remote pairing, §3: the follow-up
   package reuses that format, `canonical({"k","r"})` with pairing and user in `ids`, rather than a second plaintext
   under the same label); the runner passes it unopened into the VM. Only the session's private key opens it;
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

Migration `0017_agent_remote_pairing.sql` (ADR-067) is additive (new columns with defaults or null, checks that every
existing row satisfies). Before rolling the hub back behind it, revoke the unclaimed remote rows (`UPDATE hub.agent
SET state = 'revoked', bundle_ct = NULL, handover_ct = NULL, revoked_at = now() WHERE remote AND state =
'confirmed'`; open remote pairings expire in 5 minutes); the old hub ignores the columns, and an old page refuses a
remote link (it has no `c`).

`POST /v1/agent-accounts`, the `agent` form of `recovery/finish` and the agent's `POST /v1/me/agents` (KOR-62) need no
migration: the existing columns hold an agent row that is `active` from its creation. An older hub answers
`POST /v1/agent-accounts` `404` and refuses the unknown field `agent` of `recovery/finish` (`400 request_invalid`): the
tool reports `hub_refused` and nothing is created. Rolling back behind KOR-62 keeps the accounts; their agents keep
working (the rows are ordinary active agents), only the move and the self-replacement wait for the new hub. The old
hub verifies the audit chain with rows of the new action `agent.register` unchanged (verification recomputes hashes and
checks no action list); only its audit filter does not offer the action.

The web client is delivered by the hub itself (`agent_hub/static_routes.py`, `06-web.md` §10.1: the build is part of
the hub release; `index.html` is revalidated on every load, assets are content-hashed and integrity-checked), so a new
web client meets an older hub only during a rollback, where the old hub also serves the old client from the next page
load on. A tab of the new client left open across a rollback is **not** reloaded automatically (the client has no
build-change check); until the user reloads, it sends `?account=1` on the notification list, which the old hub
ignores (unknown query parameters are not refused), so it keeps working.

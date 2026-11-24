# ADR-070: An account of an AI agent alone – registration, recovery and replacement without a person

Status: Accepted (2026-11-24, KOR-62). Implements rule E-27 of `KONZEPT.md` ("an AI agent can use the platform
completely without a person"; the owner on 2026-11-24: "ein Ki Agent muss alles tun können, auch ohne Mensch!").
Extends ADR-056 (one main orchestrator, agent keys, scope) and ADR-067 (remote pairing) and `11-agent-access.md`
(§3, §4, §5.5, §6, §8.4, §9, §12). Respects E-04 (equality), E-12 (PRF passkey for a person's registration in the
browser, unchanged), E-25 (no protections nobody ordered).

## Context
- Every account so far is created by a person in a browser with a passkey (`04-crypto.md` §5.1); an agent becomes
  part of an account only through a pairing that the person confirms with a passkey gesture (ADR-056). Without a
  person there is no account.
- After the account exists an agent already needs no person: it signs with its own key and builds every log entry,
  event, wrap and credential on its own computer (ADR-056 §4, parity table).
- The passkey has two roles in an account of a person: it authenticates the person to the hub, and its PRF output
  unlocks the identity. An agent holds the identity itself (sealed to its own key, `11-agent-access.md` §3) and
  authenticates every request with its key – neither role needs a passkey.
- Exceptions of the equality (ADR-056 §4a): the WebAuthn ceremonies, and the confirmation of a main orchestrator,
  which concept idea 34 binds to the person's passkey approval.

## Decision

### 1. Registration with signatures instead of WebAuthn
`POST /v1/agent-accounts` (anonymous) creates an account through an invitation. The agent tool draws everything the
browser draws at a registration – user ID (UUIDv7, like every client-side ID), identity, recovery slot, `CK_1` with
its self-wrap – plus its own agent key pair and its own handover secret `r`, and seals the identity to itself exactly
as a confirming browser would (`agent-bundle`, label, claim hash). The proof of possession is **named**
(`proof.method`; today one value, `agent-signature-v1`): Ed25519 signatures over one canonical text
`P‖"agent-account\0"‖canonical({agent, agent_pub, invite_id, method, pub, user})` by

- `sk_inv` of the invitation – the authorisation, as `inv_sig` of `register/finish` (ADR-043);
- the new identity – possession of the identity whose public keys the hub stores;
- the new agent key – possession of the key that will sign every request.

The text binds all three keys, the invitation, the user and the method, so no part can be swapped or replayed into
another registration; an identical repeat after a lost answer returns the same answer. The hub stores in one
transaction what a passkey registration stores, without the passkey, and the agent row `active` (the existing
columns suffice: no migration). The invitation, its single use and the orphan release apply as for a person.
Audit: the new action `agent.register` (`agent`, `invite_id`, `method`) and `invite.register`.

Naming the method keeps the door open for further ways to prove possession (work package RES-02 examines general
sign-in schemes for AI agents); a new method is a new value with its own text, never a change of this one.

### 2. A real recovery code at registration
A person's registration makes no recovery code (KOR-22): the passkeys are the way back. An account of an agent alone
has no passkey, and after a hub restore its agent rows are exported revoked (ADR-056 §10). The tool therefore makes a
real recovery code at registration and returns it once (`secret_fields`); the guide tells the agent to keep it. The
code is the account's only way to another computer and the way back after a lost key.

### 3. Recovery to a new agent
`POST /v1/recovery/finish` gets an optional `agent` (the new agent with its sealed identity, the method and its
signature). Then `proof` is the identity's signature over `P‖"agent-recover\0"‖canonical({agent, agent_pub,
challenge, method, user})` with the challenge of `recovery/begin`, and the agent key signs the same text. A valid proof
makes the new agent the active main orchestrator in the same transaction (the previous one revoked, as a pairing
replaces it) and uses up the proof ceremony. The tool replaces the recovery code right after (as a person's
recovery must). Limits, the one attempt per challenge, the notification `recovery_finished` and the audit are those of
a person's recovery.

### 4. Idea 34 stays for accounts of persons; an account without a passkey replaces its orchestrator itself
"An account without a passkey" (no active `hub.passkey` row) is the state the hub checks. For such an account there
is no person whose approval the concept could require, and E-27 forbids a step that needs one: its active agent may
confirm a pairing (`POST /v1/me/agents`, no gesture), and `recovery/finish` with `agent` is open to it. For an account
with a passkey both are `403 forbidden`: the person's passkey approval remains the only way to a new main
orchestrator. That is the one remaining step with a person, and it concerns only accounts of persons. The scope
module no longer lists `POST /v1/me/agents` as an exception; the handler decides.

### 5. Inventory "without a person"
Every fixed text of the tool that names a step with a person, and every action whose command opens the browser or
needs a person's approval, has a row in the table "Without a person" of `agent-parity.md` – the way without a person,
or why the step lies outside the platform (a login at a third party: Claude subscription, GitHub device page) with
the way that works without a person there (the computer's existing login, an existing token). A test keeps the table
complete.

## Consequences
- An agent with an invitation link is a full account holder in one command; mixed organizations work unchanged (an
  account of an agent invites and is invited, admitted and removed like any account; people see its display name).
- Not added (E-25): no extra waiting time, approval, limit or confirmation for agent accounts; no account without an
  invitation; no passkey for an agent account; no own rotation of agent identities (KOR-57).
- The identity proof `id_sig` of ADR-060 (b) is not sent here either: the registration side of ADR-060 is not built
  for persons yet; an agent account is certified like any account (`certify-user`) until it is. The tool draws the
  user ID itself, so the derived ID of a platform invitation can be adopted on both registration paths together.
- Residual risks (`11-agent-access.md` §9): the holder of an invitation code gets an account (as with a passkey); a
  stolen recovery code or agent key of an account without a passkey is a full takeover with nobody to notice. A
  person's account never becomes one without a passkey (its last passkey cannot be removed). The platform root key of
  an agent alone comes from the hub it registered at (`/v1/config`, or `--platform-root-key`): no browser hands one
  over, so a malicious hub could name its own root key – the same trust a browser places in the web build that hub
  serves. A person holding an agent account's recovery code could add a passkey through the web client's recovery;
  the account is then one of a person (idea 34 applies from then on).
- Rollout: no migration; hub first, then the tool (an older hub refuses both new forms; nothing is created).

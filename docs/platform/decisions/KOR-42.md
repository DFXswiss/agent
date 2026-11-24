# KOR-42 – decisions

## Remote pairing (ADR-062): design review with fresh context, before building

The draft of ADR-062 was reviewed by a reviewer without the author's context. Findings and their handling:

1. **High – a malicious hub could forge the remote handover** (HPKE base mode: anyone who knows the agent's public key
   can seal to it; the hub could seal its own `r'` around an identity it controls). Fixed: return code
   `HKDF(r, "agent-return")` shown by the page after the confirmation; the tool checks it before it claims or stores
   anything (ADR-062 §3). This adds one step for the user in the remote mode (passing a code back to the agent); it
   is the price of E-04 without letting the hub impersonate the user.
2. **High – a phished agent could mark the notification as read.** Fixed: `remote` per row in `GET /v1/me/agents`
   and a banner in the web shell while the main orchestrator is a remote one (no read state hides it) (§4.4).
3. **Medium – 60 s claim deadline too short** for a conversational agent. Fixed: 600 s for remote pairings; the page
   shows the waiting state (§3).
4. **Medium – agent code could be ground offline** (the attacker picks the pairing ID). Fixed: hub nonce, origin and
   mode in the code input (§2).
5. **Medium – invitation path under-specified.** Fixed: the code is asked before the passkey is created; the missing
   notification recipient for a new account is a stated residual risk (§6, §7).
6. **Medium – tool state not defined.** Fixed: `pending.json`, `--replace-pending`, lock, `no_pending_pairing`,
   deletion in every terminal case (§5).
7. **Low** – `hub_too_old`, explicit error codes `handover_required`/`handover_unexpected`, clearing `handover_ct` at
   the deadline, identical-repeat check including it, audit details on claim, Crockford base32, notification wording,
   missing threat rows, rollback step: all taken over into the ADR.

## Command names and table states

- The parity table (`agent-parity.md`) knows three states only: `done` (a command of its own exists and is tested),
  `pending` (a parallel package delivers the action; the row names it) and `n/a` (nothing an agent does, with the
  reason). The former `api`/`stage b`/`stage c` states are gone; `api` stays as a fallback command only.

## Independent review of the agent tool (fresh context) – findings and handling

1. **High – owed rotations and the credential-key re-wrap ran only after the tool's own changes.** A browser runs
   catch-up, pending rotation and the CK re-wrap on every sync; an account with only an agent must get the same
   protection (E-04). Handling: see the fix entry below.
2. **Medium – a refused (not: unverifiable) org log counted as "entitles nobody"** in the CK check, so a flaky or
   malicious hub could cause a rotation away from entitled devices and the deletion of their records. Handling: see
   below.
3. **Medium – the hub fake was only checked against itself.** Handling: see below.
4. **Medium – `wg ungrant` used the effective access (incl. an admin's inherited read, KOR-34)** instead of the
   explicit access; error codes differed from the web. Handling: see below.
5. **Low** – two event readers, two copies of the credential body checks, unchecked event ID in the whiteboard send,
   an over-strong docstring claim about the CK epoch start, a lost claim answer reported as timeout, the guide's
   "no command prints a code" sentence, the outdated §8.4 of the spec. Handling: see below.

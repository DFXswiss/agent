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

# KOR-57 – decisions

Architecture in ADR-082; small decisions here.

1. **KOR-55 is not built** (owner, E-25): the runner keeps trusting the hub for what KOR-55 would have anchored
   (the binding of a session to its task owner and device, the founder of a private organization, the hub key on first
   use). The sub-principal itself is verified by every reader (certificate under the user's key), independently of it.
2. **The user's identity is not rotated** (`rekey`, ADR-082 §5): evaluated and not small (every passkey slot needs
   that passkey's PRF output, platform anchors and community pins change); not built (E-25). Agents paired before this
   package are converted, but a copy of the identity they made earlier keeps opening wraps to the user; the settings
   and the revocation dialog say so.
3. **Older slots offered by catch-up.** The browser confirms a catch-up slot for an agent only when the log shows that
   its user held that epoch; history a user received through a grant with history is not visible in the log, so such
   slots reach the agent only through the confirmation batches (the browser wraps every epoch it holds) or the
   user's own client, not through another holder's catch-up offer.
4. **Rotation owed by the user (`agent_owed`).** A user without the admin or write right may rotate exactly the scopes
   that its removal or replacement of its own agent put into R (found by the TypeScript twin; the general rule would
   otherwise have blocked every non-admin from revoking its agent).
5. **Global revocation only by the owner.** An agent counts as revoked in every log only when a higher generation of
   its user exists somewhere or when its user, an agent of its user or the agent itself removed it; an admin of one
   organization who removes a member's agent ends only that membership (independent review: otherwise a foreign admin
   could revoke a user's agent everywhere without the user noticing).
6. **Accounts of an agent alone keep the identity handover.** `agents confirm` in an account without a passkey still
   hands the account's identity to the successor (ADR-082 §7): there is no person whose identity could be protected
   from it, and without the identity the successor could no longer confirm further agents or make a recovery code
   (E-27). Such an account converts like every other once it gets a passkey.
7. **Self-add.** An agent with its own identity adds itself (`agent_add` by itself, its certificate and generation) to
   every organization of its user that has no active agent of the user, e.g. after its user was admitted by someone
   else; the keys follow by catch-up. The browser job adds an agent from the hub's list only when this browser
   confirmed that pairing with exactly that certificate, or a verified log already names it.
8. **Agents that never run again** keep their sealed identity at the hub until their next command (the conversion
   runs there); a person who wants them gone disconnects them, which deletes it at once. No deadline job (E-25).
9. **Agent labels** are sealed to the user's identity as before, so an agent with its own key shows `label: null` in
   its own `agents list`; the user's clients show the names.
10. **First-load guard raised to 220 KiB.** With the agent rules every client runs at sign-in (log verification,
    catch-up) the first load grew by about 1 KiB gzip; the confirmation, the revocation, the job `agents` and the
    community check of agent authors were moved to lazily loaded chunks first. 06-web.md §12.2 allows raising the
    guard with a reason; the ceiling of 300 KiB stays.

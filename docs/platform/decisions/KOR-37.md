# KOR-37 – decisions (runner and agent tool)

Direct admission of existing accounts (ADR-060; the hub, web and contract side are in agent-core).

1. **Verifier by copy.** `agent_runner.crypto` (incl. the new `identity.py`) and its tests are the copies of agent-core
   made with `copy_tool.py`; the runner understands `admit`, `user_certify`, `user_decertify` and the `consented` rule
   of the credential key exactly like hub and web.
2. **Capability header.** The runner announces `Agent-Features: log.admit` on every live-channel upgrade
   (`sync/ws.py`). A header because an older hub refuses unknown query parameters; the runner can be updated before the
   hub. The hub refuses an `admit` into an org while one of its devices has not announced it.
3. **`agent-platform org admit --org --user <id|account code> [--role] [--wg …]`** verifies both identities (the
   admitted account and the admin itself) against the platform log it verified with the configured root key, and a
   typed account code against the proven keys – the same rule as the web client. Workgroup keys the admin does not hold
   are left to catch-up (`build_batch(by_catch_up=…)`).
4. **Trust rule of the tool**: an org is trusted if the user created it, accepted an invitation into it or was
   admitted with its own keys (the admitter's identity is proven by rule L5 of `admit`).
5. **`status` prints the account code** (computed from the unlocked identity), so an agent can pass it to an admin.

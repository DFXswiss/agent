# KOR-37 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-37 – decisions (runner and agent tool)

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

## KOR-37 – decisions

Direct admission of existing accounts (ADR-060; 04-crypto.md §3, §4.7, §4.8, §5.13, §6.3).

1. **Anchors in the platform log, not in the hub's records.** Existing accounts had no proof binding their identity
   keys to anything the hub cannot forge; the root key is the only such anchor the clients already verify. Existing
   accounts are therefore certified once by the operator (`certify-user`); future registrations get an identity proof
   from their invitation code (verifier side built now, registration side a follow-up).
2. **No `--all` for `certify-user`.** Certifying from the hub's records trusts the hub at that moment; the operator
   names every account and can confirm it with the account code the person reads from its own settings (`--code`).
3. **User ID derived from the platform invitation** for identity proofs (review finding H2): otherwise the holder of any
   invitation code could sign an identity for another account. The derivation keeps the UUIDv7 format, so nothing
   else changes.
4. **The admitter's anchor is inside the `admit` entry** (`by_platform_seq`, `by_id_sig`): readers (second browser,
   agent tool, runner) check it offline; there is no lookup at verification time. Consequence: an admin must itself be
   anchored before admitting.
5. **Proofs inside the body, `""` when unused**: the entry key set of every type stays fixed, so the format check of
   the three implementations does not depend on the platform state.
6. **No credential key for orgs without consent** (review finding H3): an admission is not the admitted person's
   choice; its GitHub/Claude credentials never become available to devices of that org. The rule lives in the shared
   wrap rules (hub, runner, web) and in the hub's eligibility SQL (`org_member.consented`). A user regains it by joining
   by leaving and joining again through an invitation (an own `accept`). Device requests of the admitted user to devices of that org are not offered either.
7. **Capability by header, not query.** The hub refuses unknown query parameters (`400 request_invalid`), which would
   cut an updated runner off an older hub; unknown headers are ignored. The header is not signed: a device lying about
   it only blocks or harms itself.
8. **`device_assign` gate**: a device without `log.admit` cannot be assigned into an org whose log already holds an
   `admit` (it would refuse that log). Org device enrolment is not gated: a new runner verifies the log itself before
   it sends its `accept`, an old one fails on its own side.
9. **Single 404 for unknown, disabled and unanchored accounts** (`identity_unproven`): the hub discloses no more than
   "not admissible"; the UI then offers the invitation link.
10. **The admitted person sees the org at its next sign-in.** A live adoption of a new org into an open tab was not
    built (the requirement says "beim nächsten Öffnen"); the notification `org.admitted` is listed then.
11. **`member.profile` is not sent automatically for an admitted org**: the admitter learns the display name only when
    the admitted person opens and uses the org.
12. **Rollback**: a hub rollback after the first `admit` is not supported (the older hub refuses its own log). Before
    that, every step is reversible; migration 0013 is additive.

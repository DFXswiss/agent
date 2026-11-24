# KOR-62 decisions – an account of an AI agent alone (rule E-27)

Architecture in ADR-068; small decisions here.

1. **One endpoint, one transaction.** `POST /v1/agent-accounts` takes everything at once (no begin/finish pair): the
   three signatures over one canonical text bind all parts, so a challenge adds nothing; an identical repeat returns
   the stored answer. The user ID is drawn by the tool (UUIDv7, client-side IDs as everywhere, `04-crypto.md` §1);
   a taken ID is `409 id_conflict`.
2. **Same rows as a person, minus the passkey.** `auth/register.py` now has `check_identity`, `locked_invitation_key`
   and `store_account`, shared by the passkey registration and the agent account, so both stay identical by
   construction (E-19: the class, not the case). The principal insert reports a taken ID as `id_conflict` instead of
   a database error.
3. **Agent row active from the start.** The tool draws its own `r` and seals the identity, the label and the claim
   hash exactly as a confirming browser; the hub stores the row `active` with `confirmed_at` = `claimed_at` = now and
   `expires_at` = now + 600 s (the column needs a value after `created_at`; it is only read for unclaimed rows). The
   tool's state is then identical to a paired agent's, so every command works unchanged. No migration.
4. **A real recovery code at registration** (ADR-068 §2), printed once in `secret_fields` of `register --own-account`.
   The unset slot of KOR-22 would leave an account of an agent alone without any way back after a lost key or a hub
   restore (agent rows are exported revoked).
5. **"Account without a passkey"** is the hub's test: no `hub.passkey` row with `removed_at IS NULL`
   (`agents.store.has_passkey`). It decides `POST /v1/me/agents` by an agent and `recovery/finish` with `agent`
   (`403 forbidden` otherwise, checked before the recovery challenge is used, so a person's recovery still works with
   it). `POST /v1/me/agents` left the scope exceptions; the handler decides.
6. **Recovery replaces the code in a second step.** `recovery/finish` with `agent` does not take a new recovery slot;
   the tool calls `PUT /v1/me/bundles/recovery` right after with its signed request (the existing proof for agents).
   A crash in between leaves the old code working, which is the safe side.
7. **Platform root key without a browser.** No web build hands the tool a root key, so `register --own-account` and
   `recover` take `/v1/config`'s value unless `--platform-root-key` names it. The residual risk equals the web
   build's: both come from the hub the agent chose (§8.3 of the spec still holds for everything verified with it).
8. **`agents confirm` only for remote pairings.** The confirming agent is never on the new agent's computer, so the
   loopback form makes no sense; it does exactly what the page does (link/hub agreement, typed agent code, fresh `r`,
   `handover_ct`) and deletes its own state after the hub accepted (it was replaced).
9. **New tool code `not_own_account`** for both refusals of a passkey account; `invalid_argument` with
   `reason: recovery_code_unknown` for an unknown code (its own next step).
10. **Inventory test.** `tests/platform/test_without_person.py` scans every fixed next-step text of the tool and the
    action rows of the parity table for steps that need a person (user, person, browser, passkey, `setup-token`,
    `claude login`, `github connect` without `--token`) and requires a row in "Without a person". Third-party logins
    are kind `outside` with the way that works without a person; the single kind `person` row is idea 34 for accounts
    of persons.
11. **ADR-060 identity proof not sent.** The registration side of ADR-060 (b) is not built for persons either; the
    agent account is certified like any account until both get it (the tool already draws the user ID, so the
    derived ID can be adopted later).
12. **Audit.** A new action `agent.register` instead of widening `auth.register` (whose detail requires a passkey ID
    and whose meaning is a passkey registration); the recovery keeps `auth.recovery {step: finish}` as for a person.
13. **Research note RES-02 (immediate points).** Adopted as built: the proof stays in the body with a named method,
    checked strictly (an unknown value is `400 request_invalid` with the pointer `/proof/method`, never a fallback);
    the agent key stays raw Ed25519 (a JWK thumbprint for RFC 9421 or DPoP can be computed from it later); the
    invitation is one string on stdin or in the environment; account identity and agent key stay separate. Binding to
    the hub: the signed texts carry the `invite_id` (registration) or the challenge of `recovery/begin` (recovery),
    both random and known to exactly one hub, so a proof cannot be replayed at another hub; the origin is not added
    to the text. A dedicated error code with the list of accepted methods comes with the second method.

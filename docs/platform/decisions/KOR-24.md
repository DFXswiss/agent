# KOR-24 – decisions

Small decisions of the hub part of the delegated agent access (ADR-056, `11-agent-access.md`), taken while writing the
hub test suite and while fixing the findings of the second security review.

## Decisions

1. **Equality covers every class a browser passes.** `agents/scope.py` admits an active agent on the authentication
   classes `browser`, `anon_or_browser`, `anon`, `device_enroll` (signed by the new device's key, which the route
   checks itself) and `agent` (`BROWSER_ADMITTED`), and on a request that matches no route (it gets the same
   `404 not_found` as a browser instead of `403 forbidden`). `device` and `root` are not browser classes. The route-parity test checks every registered route against this rule and checks
   that `EXCEPTIONS` names exactly the WebAuthn ceremonies.
2. **Notifications only for a real change.** A repeated confirmation with identical values and a repeated revocation
   of an already revoked agent change nothing and write neither an audit row nor a notification.
3. **Too short sealed values are `400 request_invalid`.** The contract's `bundle_ct` form allows 28 bytes, the table
   refuses sealed identities and labels under 48 bytes (HPKE `enc` and tag alone are 48); the route refuses them with
   a pointer instead of failing on the table check.
4. **Org exports carry the account notifications of the org's users.** The `notification` rule exports the org's rows
   and the rows without org (`agent.account_change`) whose recipient is a user of the org; several exports may carry
   the same row (newest export wins, like principal rows).
5. **`POST /v1/session/end` by an agent ends the agent's access** (review L1): it is a self-revocation
   (`agent.revoke`, notification `agent_revoked`), answered `{}`. Before, the audit detail of `auth.session_end`
   (a session key ID) refused the agent key ID with a `500`. Ending a browser session also ends the session's open
   streams.
6. **`last_used_at` only after a verified signature** (review L2): the resolver reads only; the pipeline calls
   `agents.keys.touch` after the gate verified the signature and the route admitted the agent. An unsigned or forged
   request with a known agent key ID writes nothing.
7. **Pairing limits per network, serialised** (review M6): at most 3 unexpired open pairings per IPv4 address or IPv6
   /64, 10 per IPv4 /24, 10 per IPv6 /56, 30 per IPv6 /48 and 10 000 in all. IPv4-mapped IPv6 addresses count as
   IPv4. The new column `source_net` (the address as text, compared as `inet` with a GiST expression index - text keeps
   the export's column forms; cleared at the confirmation like `source`)
   carries the network counts. Creations run under the transaction lock `PAIRING_LOCK`, so the counts hold until the
   insert commits; a unique violation on the keys of an agent that is not revoked is `409 id_conflict` (no `500`).
8. **Claim deadline after the confirmation** (review M1): the confirmation sets `expires_at` to
   `min(expires_at, confirmed_at + 60 s)`; the agent listening on the loopback claims at once.
9. **`GET /v1/me/audit`** (review M2): the user's account audit - rows with `actor_id` = the user and no org (sign-in,
   passkeys, recovery, agents), and every row written by a request of one of the user's agents (also in orgs, so the
   user sees everything the agents did, also rows of orgs the user later left) - newest first, paged like the org
   audit; read resource `me.audit`. The query uses the indexes `audit_actor` and `audit_agent`.
10. **Account notification `ref`** (review M3): `<action>:<acting agent ID>:<target>`; targets: the paired or revoked
    agent, the passkey reference (`credential_reference`), the org (`role_changed`, `invite_created` of an org admin
    invitation), `platform` (platform invitation), the credential kind (`credential_changed`, put and delete), the
    passkey of an overwritten bundle slot (`bundle_replaced`, only when the slot existed), the device of a device
    request (`device_request`, not for a replay). One notification per revoked agent. `role_changed` and the
    invitation notification are published after the commit with the append (`AppendResult.notices`).
11. **Finished recovery is notified** (review M3): a recovery is finished when `register/finish` adds the passkey of
    the recovery ceremony (`auth/register.py`, not `auth/recovery.py`, which only checks the proof); the notification
    `recovery_finished::<passkey>` has an empty agent field and is written for every recovery (a stolen recovery code
    is as dangerous as a stolen agent key).
12. **Marking notifications read by an agent is audited** (review M3): action `notification.read`
    `{marked, scope: ids|all|all_account}`; nothing for a browser session.
13. **Old web clients never see account notifications** (review M4a): `GET /v1/notifications` lists them only with
    `account=1`; `POST /v1/notifications/read {all:true}` marks them only with `account:true` (IDs always work);
    `GET /v1/me` `unread_notifications` does not count them; the stream frame stays `notification` (no new frame type
    in 03-api.md section 5), and a stream delivers a notification without org only when it was opened with
    `account=1` (before, such a frame reached no stream at all, since streams are per org). The new web client sends
    `account=1` on the list; it does not yet send the flag on `read {all:true}` or on the stream, so "mark all read"
    leaves account rows unread and new ones appear at the next list load (follow-up).
14. **Migration 0010 and locks** (review M4b): the two notification checks are added `NOT VALID` (every existing row
    satisfies them; new and changed rows are checked; no scan under the migration's `ACCESS EXCLUSIVE` lock). A
    `VALIDATE CONSTRAINT` in the same transaction would scan anyway and is left out. `CREATE INDEX audit_agent` cannot
    be `CONCURRENTLY` inside the migration transaction: it holds a `SHARE` lock on `hub.audit` while it builds, which
    blocks audit writes (and therefore most writing requests) for the build time; the index is partial
    (`agent_id IS NOT NULL`) and empty on existing data, so the build is a scan of the table without sorting -
    seconds for millions of rows. Run the migration in the release window as before.
15. **Revocation closes open streams** (review M5): a stream remembers the key that opened it; after a revocation (one,
    all, or the agent's own session end) and after a browser session end, the internal frame `close_keys`
    (`stream.publish.CLOSE_KEYS`, never sent to a browser) reaches every hub process over the bus and ends the user's
    streams of those keys. A removed passkey ends its browser sessions at the resolver; their open streams end at the
    next reconnect (not closed actively).
16. **Exports never carry a usable agent** (review L3): an exported agent row is `revoked`, without `bundle_ct`, with
    `revoked_at` = its revocation time or the export time. A restored hub has no working agent; the user pairs again.

## Third security review (accepted risks and follow-ups)

17. **No Origin check at the loopback listener** (finding 1, code unchanged). The hub sends `Referrer-Policy:
    no-referrer`, so the legitimate form POST of `/connect` to `http://127.0.0.1:<port>/agent-pairing` carries
    `Origin: null`; an Origin check would break the handover, and an attacker's page can send `Origin: null` too. An
    attack needs the live link (pairing ID and port, which exist only in the agent's local process and terminal) within
    the 10-minute window, plus a confirmation of that pairing into the attacker's account before the user's. Residual
    risk accepted. Mitigation: `pair` reports the account it joined (`user_id`, `display_name`), and the guide tells
    the agent to tell the user which account it joined. Follow-up: a per-page `Referrer-Policy: origin` on `/connect`
    plus an Origin check in the listener, after browser tests on Safari, Firefox and Chrome.
18. **"Replaces X" only after the gesture** (finding 8). A fresh signed-out tab learns the user's current main
    orchestrator only after the passkey login (the hub tells a signed-out page nothing about the account), so the
    page shows "replaces <name>" after the gesture and asks for an explicit "replace and connect". The confirmation
    that replaces is sent within 300 s of that gesture (`pairing_gesture_required` otherwise), so the passkey approval
    always precedes the replacement.
19. **An agent can obtain a passkey through recovery** (finding 2). Under equal rights an agent replaces the recovery
    code with a signed request and can then run the anonymous recovery with a software authenticator, sign in
    pairing-tagged and confirm a pairing. Stated in ADR-056 §4a, `11-agent-access.md` §9 and the takeover runbook;
    the countermeasures are visibility (notifications `recovery_replaced`, `recovery_finished`), "Disconnect all
    agents" and the runbook (remove unknown passkeys, replace the recovery code, then pair anew).
20. **Rollback** (findings 3, 4): documented in ADR-056 (Consequences) and `11-agent-access.md` §12 – the previous
    hub degrades org export and audit verification on the new schema; the rollback revokes all agents and keeps the
    schema.
21. **Known, accepted or follow-up** (findings 9-13):
    - CSP `form-action 'self' http://127.0.0.1:*` is app-wide, not only on `/connect`. With `script-src 'self'` an
      injection-based abuse is unlikely. Follow-up: a per-route CSP for `/connect`.
    - A planted link that the user confirms revokes the current orchestrator before any claim (the attacker's agent
      never gets `r`, but the replacement is done). Documented and shown on the page ("replaces <name>").
    - `claude watch` creates a `credential_changed` notification for every token refresh. Follow-up: group them.
    - The global pairing creation lock and the overall cap (10 000 open pairings) are a minor denial-of-service
      surface; the limits per address, /24, /64, /56 and /48 (decision 7) bound a single source.

## Stage b: invitation with pairing

22. **Fail closed before the registration.** The parameters after the invitation code must be exactly a pairing link
    and at most one display name (1–80 characters, as the name field); the invitation screen looks the pairing up
    with the invitation. A broken link, a fingerprint that does not match or a pairing that is not open ends in the
    invalid state (`pairing_mismatch` / `pairing_unknown`) before a passkey or an account exists: a half-made account
    whose agent never connects would leave the user with a step to repeat.
23. **A request that expires during the registration does not strand the account.** When the pairing-tagged sign-in
    after the new passkey is refused with `pairing_unknown` (the agent's ten minutes ran out), the page says so and the
    next sign-in finishes the account without the agent; the agent's `register` ends with `pairing_timeout` and can
    pair the now existing account with `pair`.
24. **`/connect` finishes the pairing.** After the account is ready the invitation screen navigates to `/connect` with
    the pairing in the route state (no fragment, no secret: `r` is drawn there). `/connect` confirms at once with the
    identity the registration unlocked and keeps its replacement question, its repeated gesture on
    `pairing_gesture_required` and its handover. One confirmation path for both entries instead of a second copy in the
    invitation screens. A route state that is not a valid pairing is ignored (the page then reads the fragment).
25. **Org invitations too.** The org path of the invitation screen takes the same pairing and name: a new user (sign-in
    after the passkey) and an existing account signing in from the page are pairing-tagged. An existing account that
    was already signed in joins without a ceremony; `/connect` then asks for the gesture (`pairing_gesture_required`).
26. **E2E-40/41 as scenario specs.** `e2e/platform/traceability.json` may only be changed by acceptance packages
    (ADR-027), and a spec that names an unknown test ID fails the traceability check. The two entries are therefore
    covered here by the web suite against the real hub (`web/e2e/connect.spec.ts`, `web/e2e/invite-agent.spec.ts`) and
    by the tool's tests against the hub double; the orchestrator route `agent-tool` is ready for the scenario specs,
    which the next acceptance package registers (`board/reports/KOR-24.md`).

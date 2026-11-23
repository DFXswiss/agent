# HUB-11 – Org invitations, expiry and revocation, recovery, adding and removing passkeys, joining

Decisions taken where `03-api.md` sections 4.1–4.3, 11, 12, `04-crypto.md` sections 4.6, 4.7, 5.2, 5.2a, 5.5–5.7,
`02-data-model.md` section 2.1 and ADR-004/005/026 were silent or ambiguous.

## Org invitations (`invites/orgs.py`, `invites/routes.py`)

1. **One transaction.** `POST /v1/orgs/{org}/invites` calls `orgs.append.append_batch` (HUB-05) with an `extra` step
   that stores the invitation row in the same transaction; L1–L8 are not duplicated. The row takes `kind` and
   `expires_at` from the signed entry, `log_seq` = the entry's `seq`, `created_by` = the admin, `created_at` = receipt.
2. **Expiry bounds.** The hub stores what the signed entry says; the defaults 72 h (user) and 24 h (device) are the
   browser's choice (WEB-06). The hub refuses an expiry that is not in the future or more than 30 days ahead with
   `400 request_invalid`, pointer `/entry/body/expires` (same code as HUB-04 for platform invitations; no dedicated
   code exists in section 11).
3. **Entry shape.** The entry of a create must be an `invite`, of a revoke an `invite_revoke` naming the invitation of
   the path, each with an integer `seq`; otherwise `400 request_invalid` (pointer `/entry`, resp.
   `/entry/body/invite_id`). `expected_seq` of the append is the entry's `seq` (`seq_conflict` as in HUB-05).
4. **Rights.** `org:admin` is checked first through `orgs.rights.require(..., write=True)`: a member or an outsider
   gets `403 forbidden` on a known org, an unknown org `404 not_found`; the listing (a read) answers `404 not_found`
   to every non-admin. The log rule (L5) is checked again by the append.
5. **Idempotency.** The response schemas have no `replayed`. An identical repeat of a create (same entry; same package
   while it is still stored) answers the original `{invite_id, seq}` without appending or publishing; any other use of
   an existing invitation ID – also of a platform invitation or one of another org, and an ID that appears between the
   check and the insert – is `409 id_conflict`. An identical repeat of a stored revocation answers its `seq`; another
   revocation of a revoked invitation is refused by the log rules (`invite_revoked`), of a used one
   (`invite_consumed`).
6. **Revoke target.** An invitation that is not an invitation of this org is `404 not_found` (as the platform revoke of
   HUB-04).
7. **Listing.** All invitations of the org, newest first, at most 1000 (the contract has no paging for this list);
   state precedence `used` > `revoked` > `expired` > `open` (reuses `platform.invites.state_of`).
8. **Audit.** `invite.create` and `invite.revoke` (detail `invite_id`, `kind`, `org_id` set) next to the `log.invite`
   / `log.invite_revoke` rows of the log service. The accept is audited by HUB-05 as `log.accept`. `invite.use` is not
   written: the use happens in `orgs.append._accepts` (HUB-05), the only place where it could be audited in the same
   transaction; writing it from the signal subscriber after the commit could lose or double it. Left as an open item
   for the owner of `orgs/append.py` (report).
9. **Lock order.** Create and revoke lock the org row before the invitation row – the order of every log append
   (`accept` locks the org, then the invitation) – so a revoke racing an accept ends in one success and one
   `seq_conflict` instead of a database deadlock (review finding).
9a. **Live frames.** After a committed create or revoke the route reuses `orgs.routes.publish_append` (`log` frames
   and hints, signal `log.appended`); replays publish nothing.

## Expiry job (`invites/jobs.py`)

10. `invites.expire_packages` runs every 60 s (first run at process start), deletes the packages of org invitations
    that expired unused and unrevoked (at most 1000 per run, `FOR UPDATE SKIP LOCKED`) and writes `invite.expire`
    (actor `hub`) once per invitation – the deleted package marks it done. Platform invitations have no package and
    are left alone. Refusal of expired invitations never depends on the job (lookup, registration and the `accept`
    log rule compare with the hub clock).

## Signal `invite.accepted` (`invites/signals.py`)

11. The `accept` entries are appended by areas this package does not own (users: `POST /v1/orgs/{org}/log`, HUB-05;
    devices: the enrollment, HUB-08). The invites area therefore subscribes to `log.appended` and emits
    `invite.accepted(org_id, invite_id, subject_id)` for every `accept` of the committed batch – the entries sharing
    the receive time of the head entry, the rule of `device/signals.py` (HUB-08). Only if two appends to one org got
    the very same microsecond would an accept be announced twice; HUB-15 may deduplicate on `invite_id`. No new
    column was added (the schema test of HUB-01 fixes the columns of section 2.1).
12. As in the device area, the subscriber reads with the hub context bound by its job (no app context reaches a
    subscriber) and queues the work on one background thread, so the emitter's event loop never blocks; failures are
    logged. Finished futures are dropped on every new signal, so the list holds only running work (review finding; the
    same pattern in `device/signals.py` keeps growing until `wait()` and is left to its owner). Without a bound context
    (a process that runs no jobs) nothing is announced.

## Recovery (`auth/recovery.py`)

13. **Proof ceremony.** `recovery/begin` stores the challenge as a ceremony of kind `recovery` whose `hub_value` is the
    challenge too (the mark that distinguishes it from the registration ceremony of the same kind that
    `recovery/finish` opens; no new ceremony kind, the schema is fixed). `recovery/finish` finds it by `user_id` and
    challenge; it gets one attempt: afterwards its challenge is replaced by random bytes. It is never marked used, so a
    refused proof can never satisfy the "used recovery ceremony" condition that allows the replacement of the recovery
    slot (HUB-03 decision 19); only the registration ceremony, used by `register/finish`, does.
14. **Codes.** Unknown recovery ID or disabled user: `404 not_found`. Unknown, used or expired proof ceremony (also a
    wrong `user_id`): `400 ceremony_invalid`. Wrong proof: `401 signature_invalid` (status of section 11), audited
    `auth.recovery {step: "finish"}` with result `denied`. A successful begin is audited `auth.recovery {step:
    "begin"}` (actor anonymous, target the user); the successful registration is audited by HUB-03 with
    `{step: "finish"}`.
15. **Rate limits.** 5 begins per hour and source address: a sliding window per hub process (like every limit of
    `http/limits.py`), counting every attempt including unknown IDs, kept in the app state (`recovery_limit`).
    20 per day and recovery ID: counted in the database from the proof ceremonies of the user created in the last
    24 h (ceremonies live one day, HUB-03), so it holds across processes; per user equals per recovery ID because a
    recovery ID belongs to exactly one user. Refusal `429 rate_limited` with `Retry-After` (seconds until the oldest
    counted attempt leaves the window). Begins refused by a limit are not audited (no audit flooding).
16. **Options.** The creation options of `recovery/finish` exclude the user's active passkeys (`excludeCredentials`).

17a. **Review findings kept (low).** A proof ceremony could in theory be finished by `register/finish` if its ID were
    known (HUB-03's `take_ceremony` does not look at `hub_value`); the ID is a UUIDv7 that is never sent, and closing it
    needs a change in `auth/register.py` (HUB-03), listed in the report. The per-source limit is per process and per
    exact address (IPv6 /64 grouping not done); the per-recovery-ID limit holds across processes. Limit refusals are
    not audited (a refusal rolls back the begin transaction; bounded by the limits themselves).

## Passkeys (`auth/passkeys_manage.py`)

17. **Add.** `begin_add` is the hook of `register.begin` (HUB-03 decision 6). The options exclude the active passkeys.
    The hub cannot see whether the identity is unlocked (04-crypto.md 5.6 "with unlocked identity" is a browser rule).
18. **Remove.** Only an active passkey of the caller (`404 not_found` for foreign, unknown, removed or non-canonical
    IDs – no oracle). The user's active passkeys are locked `FOR UPDATE` first, so parallel removals keep one
    (`403 last_passkey`). The removal sets `removed_at`, deletes the `passkey:<id>` bundle slot and ends the browser
    sessions certified by that passkey (`ended_at`; the resolver of HUB-03 already refuses them with `key_revoked`).
    Removing the passkey of the current session is allowed; that session ends with it.
19. **Audit detail.** `auth.passkey_remove` names the passkey in `target_kind`/`target_id` with an empty detail,
    following HUB-03 decision 10 (the plaintext word list refuses `credential_id` as a detail key).

## Shared files

20. **`auth/routes.py`** (HUB-03) gained the routes `POST /v1/recovery/begin|finish` and
    `DELETE /v1/passkeys/{credential_id}` ("route files of these areas" in the package; route discovery only finds
    `<area>/routes.py`). The handlers only delegate to `auth/recovery.py` and `auth/passkeys_manage.py`.
21. **`tests/platform/hub/auth/test_register.py`** (HUB-03): the test "refused until passkey management exists" now
    points `register.ADD_MODULE` at a missing module, because this package creates the module; the refusal branch
    stays covered.
22. **E2E.** E2E-02, E2E-03, E2E-30 and E2E-39 have no spec yet (`e2e/platform/specs/` holds only the smoke spec);
    they are written by the packages that own them (WEB-06/08/14, SCN-03).

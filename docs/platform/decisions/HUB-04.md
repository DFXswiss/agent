# HUB-04 – Platform log, operator CLI, platform invitations, invitation lookup, platform access

Decisions taken where `03-api.md` sections 1, 4.1–4.3, 11, `04-crypto.md` sections 3, 4.8, 5.3, 5.9, 6.2,
`02-data-model.md` sections 2.1/2.2 and ADR-005, ADR-023, ADR-026 were silent or ambiguous.

## Platform log at the hub (`platform/log.py`)

1. **The hub only verifies.** Every appended entry passes P1–P4 and the type rules of `agent_hub.log.platform` against
   `AGENT_HUB_PLATFORM_ROOT_KEY`; the hub holds no key that could sign an entry. The request itself must also be
   root-signed (`auth="root"`), so a stolen browser or device session cannot even submit a batch.
2. **Serialisation.** Appends take the advisory lock `742180102` (next to migration `742180100` and audit
   `742180101`); `hub.platform_entry` is append-only, so a table lock would need privileges the writer lacks.
3. **`expected_seq`.** Checked against the stored head before the entries: `409 seq_conflict` with
   `detail.expected_seq`. A wrong `seq` of the first entry is `seq_conflict` too, a wrong `seq` later in the batch
   `log_chain` (as `append_platform` of CRY-02).
4. **Error pointers.** A refused entry answers `detail.pointers = ["/entries/<i>"]` (or a field below it) so the
   operator sees which entry failed. The client-only code `platform_root_mismatch` (`03-api.md` section 11) is
   answered as `log_genesis` (the `root` entry names a key the hub is not configured with). An empty batch is
   `log_empty`.
5. **Hub rules on top of the log rules.** `device_invite`/`user_invite`: expiry in the future and at most 30 days
   ahead (ADR-005, `request_invalid` with the pointer of `expires`), invitation ID unused across platform and org
   invitations (`id_conflict`). `device_certify`: the device ID must not be a user or an org device (`id_conflict`).
   `org_platform_access`: the org must exist (`not_found`) – a grant for a typo would otherwise be silently lost.
   These are refusals only; they never make the hub accept something the root did not sign.
6. **Stored log and cache.** Stored entries were verified at append; the verified state is rebuilt from them on demand
   and cached per process keyed by the head (a hash chain names the whole log). A stored log that does not verify
   against the configured key stops every platform request with `503 unavailable` (fail closed).
7. **`subject_id`** of `platform_entry` = `device`, else `subject`, else `org` of the body (invitations: NULL).

## Projections and effects (`platform/projection.py`, `platform/projections.py`)

8. **Invitation rows.** `device_invite` and `user_invite` create `invite` rows (scope `platform`, `created_by` NULL =
   operator, `created_at` = receipt), so lookup, registration (HUB-03), genesis (HUB-05) and enrollment (HUB-08) use one
   table for every invitation.
9. **Certification.** `device_certify` upserts the `device` row (kind `platform`, `active`, `certified_seq`, certified
   keys) – a row that HUB-08 created at enrollment keeps the version/OS/arch/capacity its heartbeats report; without a
   row the entry's values are used (rebuild needs no enrollment row). It also sets `device_enrollment.certified_at` and
   the device invitation's `used_at`/`used_by`.
10. **Decertification.** Device state `decertified` (the device resolver of HUB-08 refuses it with `key_revoked`, its
    package text). Every org where the device is assigned to a workgroup (`wg_device` not unassigned) or is a device
    member gets `rotation_pending` entries `{scope:"wg",scope_id}` per assigned workgroup plus `{scope:"org"}` – exactly
    the rotate set R the admin's `device_unassign` batch will have (04 §4.7); entries are merged, de-duplicated and
    sorted like `rotation.as_json`. After commit: hint `decertified {}` to the device through `stream.publish`
    (in-process fake until HUB-07) and the signal `device.decertified` (for HUB-07 to close the WebSocket).
11. **Rebuild** (`REBUILDERS["platform"]`) replays the stored log with the stored receive times: `platform_role` from
    scratch, `org.platform_access = private OR latest grant`, device state/`certified_seq`/keys, invitation rows (insert
    if missing), certification marks. `rotation_pending` is an effect, not a projection, and is left alone (HUB-05
    clears it when the rotation arrives). `updated_at` only moves forward.
12. **Private orgs** keep `platform_access = true` even after `org_platform_access{granted:false}` (ADR-026 and
    `PlatformState.platform_access` of CRY-02: private ⇒ access).

## Reads

13. **`GET /v1/platform/log`** before `platform init` answers `404 not_found` (the response requires a `head`).
    `next_after` is the last returned `seq` while entries follow, else null.
14. **`GET /v1/platform/enrollments`** lists only enrollments that verify against the platform log (open
    `device_invite`, uncertified device, `enroll_sig` and `inv_sig` valid over the rebuilt enrollment text); everything
    else is left out silently. Oldest first, at most 1000.
15. **Contract change (`contracts/changes/HUB-04.md`).** The enrollments response also carries the whole platform log:
    the operator CLI has only root-signed requests, `GET /v1/platform/log` needs a browser session, and the CLI must
    verify the log against its own key and know `pk_inv` of each invitation instead of trusting the hub.

## Platform invitations (`platform/invites.py`)

16. **Right** `platform:inviter` = a `platform_role` row without `revoked_seq`; checked in the transaction of each
    request (`403 forbidden` otherwise).
17. **Listing** shows all platform user invitations (also the operator's, `created_by` null), newest first, at most
    1000: inviters are DFX staff working as one team. States `used` > `revoked` > `expired` > `open`.
18. **Idempotency.** An identical repeat of a create answers `200 {}` again (the response schema has no `replayed`);
    other content with the same ID, or an org invitation with that ID, is `409 id_conflict`.
19. **Revoke** of an unknown or non-platform invitation is `404 not_found`, of a used one `invite_consumed` (400),
    a repeat is a no-op; revocation clears `package_ct` (none for platform invitations, kept for uniformity).

## Lookup (`invites/lookup.py`)

20. **Route location.** Discovery only finds `<area>/routes.py`, and `invites/routes.py` belongs to HUB-11, so the
    route is declared in `platform/routes.py` and calls `invites/lookup.py`.
21. **Org form.** `log_head` is the org's current head (`org.log_head_*`) and `log` every entry from 0 to it; a missing
    org, a gap in the stored log or a deleted package also answer `invite_unknown` (one code).
22. **Audit.** Only answered lookups write `invite.lookup` (actor `anonymous`, detail `invite_id`, `kind`); refused
    lookups are not audited, so a flood of guesses cannot grow the audit chain (the pipeline's 30/min/source limit
    applies).

## Operator CLI (`platform/cli.py`)

23. **Key file.** JSON `{"v":1,"root":"<b64u seed>"}`, created with `O_EXCL` and mode 0600; loading refuses group or
    other permissions (like SSH). `init` never overwrites; with `--hub` it uses an existing file and appends `root`.
24. **Every command appends `root` first while the log is empty**, so the orchestrator needs only
    `init --key-file` (offline, before the hub starts with the printed key) and then any command.
25. **Checks before signing.** `/v1/config.platform_root_key` must equal the operator key (`platform_root_mismatch`);
    every response must carry a valid `agent-response-v1` signature by the hub key from `/v1/config`; the log from the
    hub is verified with P1–P4; the new entries are checked locally with the same rules before they are sent.
26. **Retries.** `seq_conflict` (a concurrent operator) is retried up to three times on a fresh snapshot.
27. **Arguments.** `--hub` and `--key-file` are flags, not environment variables (the hub refuses unknown
    `AGENT_HUB_*` variables). `grant-role`/`revoke-role` take the role as positional argument (`inviter`).
    `--expires-hours` defaults to 72 (user) / 24 (device), 1–720. Exit codes 0/1 (refused, unreachable)/2 (usage).

## Shared files

28. **`tests/platform/hub/core/test_registry.py`** (HUB-02) asserted that `db` is the only CLI area; every package that
    adds a CLI (`<area>/cli.py:register`, ADR-027 §4) breaks it. Changed to a subset check (`db` and `platform`
    present) – the only edit outside the package's paths, needed by the registration rule itself.

## Review outcome

29. **Operator record (review, medium).** The CLI keeps `<key file>.state` (0600, public data only): per hub URL the
    hub key pinned on first use (`hub_key_changed` on a change) and the last verified head (a log that does not extend
    it is refused as `log_rollback`, rule L9 applied to the platform log). A hub or a network attacker can therefore
    neither make the root sign a fork of a shortened log nor swap the response-signing key. `--hub` must be https
    (http only for localhost). Malformed hub answers end with exit code 1 instead of a traceback.
30. **Fan-out after commit (review, low).** A failing `publish` of the `decertified` hint is logged; the stored append
    still answers 200 (otherwise the CLI would retry and append a second invitation).
31. **Exposed bootstrap invitations (review, high – specification gap, not fixed here).** A `user_invite` entry
    carries `invite_id` in clear text and every signed-in user may read the platform log (03 §4.3), while a platform
    user invitation has no `pk_inv`: a member who reads the log could register with an open operator invitation before
    the invitee (HUB-03 binds registration to `invite_id` only). The entry format and the read right are fixed by the
    contract and 04 §4.8, so the fix belongs to the architecture (e.g. `pk_inv` in `user_invite` plus an `inv_sig`
    proof at `register/begin`, or the hash of the `invite_id` in the log). Until then operators should prefer
    `POST /v1/platform/invites` by an inviter (not in the log) and use `invite-user` only for bootstrap with short
    lifetimes (`--expires-hours`). Recorded as open risk in the report.
32. **Kept as is (review, low).** Certification after the expiry of the device invitation is allowed: the invitation
    is checked at enrollment (HUB-08), certification only names it (CRY-02 decision). `certify-pending` is
    all-or-nothing per run; `--device` selects. The invitation list keeps the contract's unpaged form (≤ 1000, no
    `before` parameter in the schema). `key_revoked` for decertified devices is enforced by the device resolver of
    HUB-08; this package sets `device.state = decertified` and tests that. The enrollments response carries the whole
    log; with a growing platform log a root-readable `GET /v1/platform/log?after=` would be the better contract (noted
    for a later contract change; the property is required, which is safe because the hub is its only producer).

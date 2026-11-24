# HUB-13 – Credentials and device requests (browser and device side)

Decisions taken where `03-api.md` sections 4.1, 4.8, 4.10, 6.1, 6.2, 11 and 12, `02-data-model.md` section 2.7,
`04-crypto.md` sections 4.4, 4.5 and 6.3, ADR-003, ADR-011 and ADR-015 were silent or ambiguous. Architecture-relevant
parts (notices, eligibility, request chains) are ADR-035.

## Credential records (`credentials/records.py`)

1. **Check order**: contract size limit (`size_exceeded`), record form and the expected author's signature (`header_invalid`, `non_ascii`, `size_exceeded`,
   `401 signature_invalid` as for events), header names owner/kind/author of the request (`header_invalid`),
   `version = expected_version + 1` (`request_invalid` `/record/header/version`), epoch = the owner's current CK epoch
   (`epoch_stale`; also for a user without any CK epoch), stored version (`409 version_conflict` with
   `detail.expected` = stored version, 0 when none).
2. **Author**: browser writes and rotations must be authored by the owner; device writes by the writing device.
3. **Record ID** stays the same across versions and rotations (`request_invalid` `/record/header/id` resp.
   `/records`); a new record with an ID used by any other record is `409 id_conflict`.
4. **Idempotent retry**: a `PUT` of the exact stored bytes returns the stored version without a second audit row
   (the response schema has no `replayed` flag).
5. **Delete** of a missing record is `404 not_found` (clients treat it as done); the delete leaves no row and no bytes
   of the record (tested on a full table dump); the audit keeps `kind` and `version` (metadata of 04-crypto.md §7).
6. **Rotation**: `epoch` must be current + 1 (`409 version_conflict`, `detail.expected`); every wrap must be of scope
   `cred`, the caller and the new epoch (`request_invalid` `/wraps/<i>`), the caller's self-wrap is required
   (`request_invalid` `/wraps`), all wraps pass W3 (`wrap_unauthorized`); the records must be exactly the stored kinds,
   each with `version` + 1, the new epoch and the owner as author (`409 version_conflict` for a missing, extra or stale
   record). Wraps for the eligible devices are not required (catch-up wrapping can add them). Writes of one owner are
   serialised by a row lock on the principal.
7. **Audit**: `credential.put` (target `credential`/record ID, detail kind+version), `credential.delete` (same),
   `credential.rotate` (target `credential_key`/owner, detail epoch+record count); refusals are not audited (as the
   other write routes).

## Device access

8. `GET /v1/device/credentials/{owner}/{kind}`: an ineligible device, unknown owner or kind is `404 not_found`; the read
   clears the device's notice of the owner (ADR-035).
9. `PUT /v1/device/credentials/{owner}/{kind}`: `403 forbidden` unless the device is eligible and `request_id` names an
   active request of the owner addressed to the device whose kind produces the credential (`github.connect` → `github`,
   `claude.login` → `claude_subscription`; a follow-up of the chain counts). The notice and hint go to the other
   eligible devices only.

## Device requests (`requests/store.py`)

10. **Creation**: the envelope signature is checked with the caller's identity key; `header.user` must be the caller
    (`403 forbidden`); the device must be eligible (`403 device_not_assigned`); an existing ID with the identical
    envelope is a replay (`replayed:true`, no new hint), otherwise `409 id_conflict`. No check of `header.ts` (the ID
    makes requests idempotent; the expiry runs from the hub's receipt time).
11. **Replacement** of an active root of the same user/device/kind (ADR-035) instead of refusing, with a
    `reconcile {}` hint to the device; follow-ups have no own limit and expire at the latest with their root.
    Request kinds outside the closed list are `header_invalid`.
12. **Delivery**: the device's first fetch of an active request sets `delivered`; a response to a still `pending`
    request also sets `delivered` first.
13. **Responses**: `in_reply_to`, `user`, `device`, `kind` must match the request (`header_invalid`); a stored identical
    response is accepted again without a new frame (also after the request closed); otherwise a closed or expired
    request refuses new responses with `403 forbidden`; `seq` must be the next one (`409 seq_conflict`,
    `detail.expected_seq`). The response envelope is stored as canonical JSON.
14. **Expiry**: enforced lazily in every write (a request past `expires_at` is treated as `expired`, `GET` shows
    `expired`) and persisted by the job `requests.expire` every 10 s (actor `hub`); a new request first closes the
    user's due requests. The same job fails active requests of devices that lost eligibility.
15. A device that is no longer eligible for the request's user gets `404 not_found` on fetch and response.
16. **Audit** of every transition `device_request.<state>` with target `device_request`/ID, `device_id` = the
    addressed device, detail `{kind, error_code}` (`error_code` only `device_not_assigned` for the eligibility failure).

## Limits (from the contracts)

17a. Credential `ct` at most 87382 characters (64 KiB), request and response `ct` at most 349526 characters
     (256 KiB), at most 100 responses per request: `400 size_exceeded` (the crypto modules allow 1 MiB; the hub
     enforces the contract so that no read returns a body outside its schema).

## Review (independent subagent)

Fixed: follow-ups outliving their root (M1), request kinds outside the closed list answered with 500 (M2), contract
size/count limits (M3), per-owner notices hiding a deletion of another kind (M4, now per kind), no device hint on
replacement (M5, `reconcile`), lock order root before follow-up (L1), follow-up expiry shown wrongly (L2, via M1).
Not changed: L3 (a device that regains eligibility is not told about changes made meanwhile - it receives the
current `CK` wraps again and re-reads records on use; documented for RUN-09), L4 (`id_conflict` for a foreign UUIDv7
reveals only that a random ID exists), L5 (the presence query also needs `last_seen_at`, which `stream.presence`
does not offer), L6 (follow-up lookup on `envelope` JSON; device requests are few and short-lived), L7 (credential
and request audit rows have no org: credentials belong to a user, not to an org).

## `GET /v1/me/devices`

17. One item per device and org (`org_id` is single in the contract), `wg_ids` = the workgroups of that org in which
    the device is assigned and the user has access; newest enrolment first, then device and org ID descending;
    `online` = heartbeat within `AGENT_HUB_DEVICE_ONLINE_SECONDS`, `last_seen_at` = last heartbeat of the current
    connection or null.

## Ownership

18. The schema test `tests/platform/hub/test_schema.py` (HUB-01) gains the table of migration 0003 under ADR-035, as
    HUB-10 did for migration 0002; the ADR index gains the line of ADR-035.

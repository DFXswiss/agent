# HUB-05 – Organizations, membership-log appends, projections, wraps, catch-up

Decisions taken where `03-api.md` section 4.3, `04-crypto.md` sections 4.7, 5.3, 5.10, 6.1, 6.3, `02-data-model.md`
sections 1.3, 2.2, ADR-007, ADR-024 and ADR-026 were silent or ambiguous.

## Verification at the hub

1. **The hub re-verifies its own stored log.** Every decision that needs the membership log (append, wraps,
   catch-up) replays the stored entries with `agent_hub.log.verify` against the pinned founder key and the platform
   log against `AGENT_HUB_PLATFORM_ROOT_KEY` (`orgs/logs.py`). The founder key is the registered identity of
   `org.created_by` (L3, "pinned at org creation"). Verified logs are cached per process under
   `(org, head seq, head hash)` (the hash chain fixes every entry; a log cached during a rolled-back append can only
   match a stored log with identical entries). A stored log that does not verify, a head that does not match, or a
   founder without identity is never acted on (`503 unavailable`).
2. **Entries are authored by the caller.** Every entry of a batch must have `by` = the appending principal
   (`403 forbidden`, before the log rules), and every event of a batch `header.author` = the appending principal.
   A client never submits entries or events signed by somebody else, even if their signatures are valid.
3. **`expected_seq`.** Must equal the current head + 1, else `409 seq_conflict`; `detail.expected_seq` only for an
   active member (a non-member learns no log length). Authorship is checked first. A first entry whose own `seq`
   differs is `seq_conflict` as well (library append mode). One batch has one appender: it authors every entry and
   every wrap (two library batch vectors with a different wrapper are `forbidden` at the hub).
4. **Platform access** for `device_assign` with `platform_seq` is read from the verified platform log
   (`PlatformState.platform_access`: private orgs always, others by the latest `org_platform_access`), not from the
   `org.platform_access` column, and refused with `403 platform_access_denied` before the log rules run.
   `org.platform_access` is maintained as a projection (genesis, every append, the rebuilder) with the same rule.
5. **User `accept` at the hub** (04-crypto.md 4.7, 5.2a): `pub` must equal the appender's registered identity
   (`log_accept`); an invitation row bound to another registered user is `409 invite_registered`; the invitation row
   (if any) is marked used and its package deleted in the same transaction. A device `accept` (HUB-08) gets no
   identity check here: the enrollment endpoint compares `pub` with the enrolled keys.
6. **`invite`/`invite_revoke` and device `accept` are refused on `POST /v1/orgs/{org}/log`** (`400 request_invalid`,
   pointer to the entry). A device `accept` exists only through the enrollment of the device (HUB-08), otherwise a
   browser user holding a device code would become a device principal and receive workgroup keys by catch-up.
   They exist only together with their invitation row and package (`POST /v1/orgs/{org}/invites`, HUB-11), which
   calls `orgs.append.append_batch` with an `extra` step that runs in the same transaction. The service itself accepts
   every entry type (HUB-08 uses it for the device `accept` of an enrollment). Marking an org invitation row used at
   `accept` (direct SQL on `hub.invite`, whose rows HUB-11 creates) belongs to the atomic accept and stays here.
7. **Codes.** Log and crypto codes are passed on unchanged (03-api.md section 11). Reader-only codes become their hub
   form: `log_fork` (an event `lh` beyond the head) → `header_invalid`; anything outside the closed list →
   `request_invalid`. A wrap or event with a bad signature answers `signature_invalid`, which section 11 lists with
   status 401 only.
8. **Body limit.** `POST /v1/orgs`, `POST /v1/orgs/{org}/log` and `POST /v1/wraps` accept 4 MiB (a rotation batch can
   carry thousands of wraps); other bodies keep 1 MiB.

## Genesis (`POST /v1/orgs`)

9. **Idempotency.** The genesis `org` is the client-generated ID: the same caller with the byte-identical genesis gets
   `{org_id, default_wg, replayed:true}` (the genesis entry is the identity of the request; differing wraps or events
   of a repeated request are not compared); any other use of an existing org ID, or a `default_wg` that is already an
   org or workgroup ID, is `409 id_conflict`. Two *concurrent* creations of the same new ID end in one success and one
   `503` from the unique constraint (UUIDv7 client IDs make this practically impossible; no extra handling).
10. **Founder identity.** `pub` must equal the caller's registered identity in both keys (`kem` and `sig`), not only
    `sig`: the hub stores the founder's wraps under that `kem`.
11. **Platform invitation.** Valid = a row with `scope='platform'`, `kind='user'`, `registered_user` = caller, not used,
    not revoked. Its expiry is not checked again at genesis: it governs `register/begin`; the genesis follows the
    registration (orphan registrations are removed after 24 h by HUB-03). `platform_invite_id` on a non-private genesis
    is `400 request_invalid` (it would otherwise be silently ignored).
12. **Profile events.** The events of a genesis must be exactly `org.profile`, `wg.profile` and `member.profile`, each
    at least once (`kind_not_allowed` for any other kind, `request_invalid` for a missing one); they are checked after
    the genesis rules.

## Projections and rebuild

13. **Diff projection.** `orgs/projection.py` writes the difference of the rights state before and after each entry;
    the incremental path and the rebuild fold the same function over the entries, so they agree by construction (and
    by test against the vector log). `wg_access` rows are the *effective* access, including `write` on the default
    workgroup for every active user (ADR-024 addendum: not reducible); a role or access change sets `granted_seq`
    anew. Platform devices join `org_member` at their first `device_assign`.
14. **Self-leave.** A member that leaves by itself cannot rotate (L8); its R becomes pending (CRY-02) and the hub does
    not force later batches to include it (exactly R is required per batch); admin browsers perform the pending
    rotation as their own batch (`rotation.required` notification, HUB-15).
14a. **`rotation_pending`** is derived: the scopes a self-removed member could read (`OrgState.pending`, until rotated)
    plus, for each platform device whose certification ended and that is still assigned, R of unassigning it
    everywhere. Every append and the rebuilder recompute it; HUB-04 refreshes it at decertification with
    `orgs.projection.update_org(connection, org, log, platform, access)` (or by running the `orgs` rebuilder for the
    affected orgs).
15. **`REBUILDERS`** = `{"orgs": rebuild(connection, config) -> int}`; it runs in the caller's transaction (DR-01's CLI)
    and rebuilds `org_member`, `wg`, `wg_access`, `wg_device`, the `org`/`wg` rows of `scope_epoch` and the derived
    `org` columns from `membership_entry` and the platform log. `key_wrap` is data, not a projection.
16. **Wraps of removed principals.** `remove` deletes the removed principal's wraps of this org's `org`/`wg` scopes.
    `device_unassign` keeps the old-epoch wraps (the device held those keys anyway); `cred` wraps are left to the
    owner's CK rotation (04-crypto.md section 5.10).

## Events of a batch (interface to HUB-06)

17. `orgs/events.py` defines `store_events(connection, events, *, log, author, now, excluded) -> list[StoredEvent]`
    and a minimal implementation (E1–E9 via `agent_hub.log.events.check_event_hub`, gapless `scope_seq` through
    `scope_head`, audit `event.append`, E6 grace from `scope_epoch.created_at`). When the events area provides
    `agent_hub/events/batch.py:store_events` with this signature, `event_store()` uses it instead. Events in a batch
    are checked against the log *after* the batch (a `wg.profile` may name the workgroup the batch creates) and after
    the batch's entries are stored, so E6 sees the receipt time of a rotation in the same batch.

## Wraps and catch-up

18. **`POST /v1/wraps` is all or nothing** and holds `FOR SHARE` on the org rows it checks against, so a concurrent
    `remove` (which holds `FOR UPDATE`) cannot slip between the W3 check and the insert. One wrap outside W1/W2 refuses the request (`400 wrap_unauthorized`); a wrap
    for a slot that already holds one is ignored and counted in `ignored`. For the `cred` scope the hub uses the
    owner's current epoch (`scope_epoch`, `cred`) and the logs of the orgs in which the owner or the wrapper is an
    active member.
19. **Catch-up** lists the current-epoch `org`/`wg` slots of the org and the caller's own `cred` slots for recipients
    that are principals of this org (5.2a); other users' `cred` scopes are never listed. Decertified platform devices
    are no recipients. The list is sorted by slot (a work list, not a UI list); `GET /v1/wraps` is newest first.
20. **Lists of `GET /v1/orgs/{org}`** are newest first (repository rule): workgroups by `created_seq`, members by
    `joined_seq`, devices of a workgroup by `assigned_seq`, each with the ID as tie-breaker. Only active members and
    assignments are listed.

## Live frames and signals

21. After the commit the routes publish (only through `agent_hub.stream.publish`): `log {org_id, head}` to the org's
    browsers, the device hint `log {org_id, seq}` to every active device of the org, `wraps {}` to every recipient of a
    new wrap (browser frame for users, hint for devices) and `event {scope_id, scope_seq, event}` to the org for every
    newly stored event, and emit the signal `log.appended` (`org_id`, `seq`). The `event` frame goes to the org
    audience (`Audience` carries no scope): the stream (HUB-07) must deliver workgroup events only to browsers with
    `wg:read`. `event.appended` is left to the events area (ADR-027 §4).

## Not audited

22. Refused appends are not written to the audit log (only stored entries get `log.<type>` rows with `detail.seq`);
    refusals of operations are audited by HUB-12, refused authentications by HUB-03.

# HUB-08 – Device API: enrollment, device state, logs, wraps, pull, task reads

Decisions taken where `03-api.md` sections 1, 3, 6.1 and 6.2, `04-crypto.md` sections 3, 5.8 and 5.9,
`02-data-model.md` sections 2.1, 2.2 and 2.4, ADR-013, ADR-023 and ADR-027 were silent or ambiguous.

## Gate and rights (`device/keys.py`)

1. **Resolver.** `RESOLVERS["device."]` reads the `device` row of `device.<32 hex>`. Unknown → `401 key_unknown`
   (gate); state `decertified` or `removed`, a disabled principal, and an org device that is no longer an active
   member of its `owner_org` → `401 key_revoked`. Nobody writes the state `removed` yet; an org device removed by a
   `remove` entry is recognised from `org_member` (no projection of another area is changed).
2. **`device:any` vs `device`.** A resolver does not know the route, so the gate admits a pending device and the
   routes ask for the right with the dependencies `AnyDevice` (only `GET /v1/device/state`) and `ActiveDevice`
   (every other device route): a pending device gets `403 forbidden`. **Later device routes (HUB-07 WebSocket,
   HUB-10 outbox, HUB-12 operations, HUB-13 credentials/requests) must use `ActiveDevice`**; the test
   `test_every_device_route_requires_an_active_device` walks every discovered `auth="device"` route and fails when one
   does not, so the rule is enforced without changing the pipeline of HUB-02.
3. **Re-certified platform devices.** Rights come from the hub's verified logs (`device/access.py`): a platform device
   whose org keys come from an ended certification (`PlatformState.excluded`) reads nothing of that org, even after a
   re-certification with new keys, consistent with W2/E5.

## Enrollment (`device/enroll.py`)

4. **Proof of possession first.** The request signature is checked with `pub.sig` of the body before any database
   work (`auth.gate.verify_enrollment`); an `Agent-Key` other than `device.<hex of device_id>` is `401
   signature_missing`, a signature by another key `401 signature_invalid`.
5. **One code for all unusable invitations.** Unknown, used, revoked, expired, of the other scope or of kind `user`,
   a platform invitation that is missing from the verified platform log, already certified or already has an
   enrollment: `404 invite_unknown`. Invalid `enroll_sig`/`inv_sig` of a platform enrollment: `401
   signature_invalid` (the code is listed only with 401); an invalid `inv_sig` of an org `accept`: `400 log_accept`
   (log rule L5).
6. **Idempotency.** `device_id` is the client ID of the request (03 §1): the identical request again answers the
   original result (platform: same stored request; org: same device keys and the same `accept` stored in the org's
   log, while the device is still an active member), so a runner may retry after a lost answer. A concurrent
   enrollment of the same device ID that loses the race is `409 id_conflict` too (not a database error). Any other request for a known device ID (also a user ID) →
   `409 id_conflict`. The contract response has no `replayed` field, so none is sent.
7. **Org form checks.** The `accept` must name this enrollment (`type`, `by` = `subject` = `device_id`, `kind`
   `device`, `invite_id`, `pub`, `org` of the invitation, integer `seq`); a mismatch is `400 request_invalid` with the
   pointer below `/entry/body`. `expected_seq` of the append is the entry's `seq` (`409 seq_conflict` without detail
   for the not yet member device). The batch has no wraps and no events: the device keeps OK/WK from the package.
8. **Rows.** Org device: `device` kind `org`, `owner_org` = the invitation's org, `active`; platform device:
   kind `platform`, `pending_certification`, `device_enrollment.request` = the body as sent (what
   `certify-pending` verifies). Audit `device.enroll` (actor and device = the device, org, target = invitation,
   detail `{kind, state}`); the org append adds its `log.accept` row.

## Device state (`device/state.py`)

9. **Section interface.** `<area>/device_state.py:SECTION = Section(fields, provide)`: one object per area (HUB-09
   provides three fields, HUB-14 two); `provide(StateRequest)` returns exactly its fields and runs in the same
   repeatable-read transaction (`connection`, `device_id`, the verified `view`, `now`, `config`). A field of two areas,
   an unknown field or a wrong type stops the discovery (`RegistryError`); a provider returning other fields fails the
   request (`503`). Defaults: `[]`, `{}` for `delivered_prompt_event_ids`, `null` for `platform_budget`.
10. **`rotation_pending`** (contract: list of UUIDs) = the scope IDs (org or workgroup) of `org.rotation_pending` of
    the device's orgs that the device reads. Devices never rotate; the list tells the runner that the admin still
    owes a rotation.
11. **`orgs`** lists every org in which the device is an active member (also without assignment, so the runner keeps
    verifying its log); `assigned` the assigned workgroups. `last_origin_seq` is 0 before the first outbox item.

## Logs, wraps, pull, tasks

12. **Logs.** `GET /v1/device/logs` needs active membership in `org` (`404 not_found` otherwise, also for unknown
    orgs); `org` is required (`400` pointer `/org`). `GET /v1/device/platform-log` answers `404 not_found` while the
    platform log is empty (as `GET /v1/platform/log`).
13. **Wraps.** The device route reuses the W3 store of HUB-05 with the device as `wrapped_by`; the list is newest
    first like `GET /v1/wraps`.
14. **Pull.** Without `cursor` the pull covers every readable scope from `seq 0`, sorted by scope ID. A cursor for a
    scope the device does not read (or an unknown one) is `404 not_found`; a repeated scope or an unknown kind is
    `400 request_invalid`. Org scope kinds: `org.profile`, `wg.profile`, `member.profile`, `device.profile`,
    `whiteboard.*`; workgroup scopes: every kind but `session.terminal`. The answer fills `limit` across the scopes in
    cursor order; `more` is true when any matching event remains.
15. **Task reads** come straight from the tasks area's tables (HUB-09 owns them; nothing is written).
    `main_session_id` = the earliest session of depth 0; `title_event_id` = the newest `task.meta` event of the task.
    The list cursor is b64u of canonical `{"at": <microseconds since the epoch>, "id"}` (microseconds so that rows
    within one millisecond are not skipped); HUB-09 may adopt the same form for `GET /v1/workgroups/{wg}/tasks`.

## Hints (`device/signals.py`)

16. **`reconcile` on log changes.** Subscriber of `log.appended`: every active device member of the org plus every
    device named by the batch (`accept`, `device_assign`, `device_unassign`, `remove`; the batch = the entries with the
    head's receive time) gets `reconcile {}`. Signals carry no app context, so the subscriber uses the pool of the hub
    context bound by the job `device.bind_context` (runs at process start) and by the device resolver. The emitter
    calls subscribers on the event loop, so the subscriber only queues the query and the hints on one background
    thread (failures logged). A proper context for signal subscribers would be an ADR-027 extension (proposal for
    HUB-07/HUB-09).
17. **Changed test of HUB-02.** `tests/platform/hub/core/test_jobs_signals.py` asserted that no area subscribes to a
    signal; it now compares with the discovered subscribers (first real subscriber).

## Review

18a. **Kept after review:** expired invitations answer `invite_unknown` (not `410 invite_expired`), like
     `POST /v1/invites/lookup` (03 §4.1, one code without oracle). The `title_event_id` subquery uses the index
     `(task_id, scope_seq)` of `hub.event`. `POST /v1/device/wraps` now has the default 1 MiB body limit (03 §1).

## E2E

18. E2E-04, E2E-05 and E2E-14 do not exist yet (SCN packages); they are not run here.

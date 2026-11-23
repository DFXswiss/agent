# ADR-035: Credential-change notices and device-request chains

Status: Accepted (2026-11-22)

## Context
- 03-api.md §6.1 lists `credentials_changed:[owner_id]` in `GET /v1/device/state`, the reconcile form of the
  content-free hint `credentials {owner_id}` (§6.2). The hub never queues hints for offline devices, and
  02-data-model.md §2.7 hard-deletes credential records, so after a deletion nothing in the schema tells a device that
  reconnects later that its cached record is gone. Without a durable trace `credentials_changed` would be empty or a
  guess (a time window).
- 03-api.md §4.10 and 04-crypto.md §4.5 define follow-up requests (`follows`), "≤ 1 active request of each kind per
  user per device" (05-runner.md §11.1) and the states of 02-data-model.md §2.7, but not what happens to a stuck
  request when the user starts the flow again, which response ends a flow with follow-ups, or when `failed` is used.

## Decision
1. **Notices.** A new table `hub.credential_notice (device_id, owner_id, kind, changed_at)` (migration 0003) holds one
   row per device eligible for the owner's `CK` (except the device that made the change) and changed kind after every
   committed `PUT`, `DELETE` and CK rotation (every rotated kind). `credentials_changed` lists the owners of a device's
   notices while the device is still eligible. A device read of the owner's record of a kind
   (`GET /v1/device/credentials/{owner}/{kind}`, also a `404`) deletes the device's notice of that kind if it is not
   newer than the read; the owner stays listed until every changed kind was re-read, so re-reading one kind never
   hides the deletion of another. The runner therefore re-reads every kind of a listed owner (at most four reads). A
   job prunes notices of devices that lost eligibility. The row holds identifiers, the kind and a time (credential
   metadata of 04-crypto.md §7), never a version or content; the record itself remains hard-deleted.
2. **Eligibility** for `CK`, `GET /v1/me/devices` and device requests is one rule: an active, enabled device that is an
   active member of an org and assigned to a workgroup of that org in which the user (an active member) has access -
   the W2 `cred` reader rule, answered from the membership projections.
3. **Chains.** A root request (`follows = ""`) and its follow-ups form a chain. A new root for the same user, device and
   kind **replaces** an active root: the old chain becomes `failed`, the device gets a `reconcile {}` hint before the
   `request` hint of the new request and drops the replaced flow (it is no longer in its desired `requests` list), so
   a user who restarts a flow is never blocked for 15 minutes. A follow-up needs an active root of the same
   user, device and kind that is no follow-up itself (`400 params_invalid`, one answer for every refusal). A final
   response to any request of a chain answers the whole chain. A follow-up's `expires_at` is at most its root's, so a
   chain expires together and nothing of it stays usable after the root's 15 minutes. Active requests whose device is no longer eligible become `failed` (`error_code` `device_not_assigned` in the
   audit detail).
4. **Device credential writes** are tied to the request kind: `github.connect` may write `github`, `claude.login` may
   write `claude_subscription`; checks (`credential.check`, `mailbox.check`) write nothing.

## Consequences
- An offline device learns about every credential change on its next reconcile, also after a deletion.
- One extra table and one partial deviation from the column list of 02-data-model.md §2 (documented here; the schema
  test lists the table under this ADR).
- Runner side (RUN-09, refines 05-runner.md §11.1): "at most one active request of each kind per user per device"
  holds for roots; a newer root replaces the older one, so the runner treats requests that disappear from `requests`
  as cancelled (stops their flow, never answers them: `403 forbidden`) and re-reads every kind of an owner listed in
  `credentials_changed`.

## Alternatives considered
Time-window `credentials_changed` from audit rows (misses changes of long-offline devices); tombstone rows in
`credential` (contradicts the hard delete); refusing a second active request with a conflict code (blocks the user for
up to 15 minutes and needs a new error code in the closed list).

## Deviation from DESIGN.md
None.

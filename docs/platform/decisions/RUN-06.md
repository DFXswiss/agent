# RUN-06 decisions (org device enrollment, catch-up wrapping, assignment changes, status and doctor)

Small decisions of the package (05-runner.md §1.2, §2.1, §2.2 steps 4, 7, 8, §2.5, §3.5; 04-crypto.md §4.6, §5.8,
§5.10, §5.11, §6.1, §6.3; 03-api.md §4.1 `invites/lookup`, §6.1 `enroll`, `wraps`, `state.rotation_pending`, §6.2
`decertified`; ADR-005, ADR-007, ADR-013).

## Modules
1. `enroll/org.py` (enrollment), `catchup/plan.py` + `catchup/reconcile.py` (step 80), `assignments/teardown.py` +
   `assignments/reconcile.py` (step 40) + `assignments/tasks.py` (task `revocation`), `doctor/checks.py` +
   `doctor/cli.py` (`status`, `doctor`). Registration only through the discovered extension points (ADR-027 §4).

## Org device enrollment (05 §2.1, 04 §5.8)
2. The lookup is anonymous and not response-signed (03 §1): read without redirects or proxy, bounded size. Unknown,
   used, revoked and expired codes are one answer (`invite_unknown`, ADR-005); a user or platform invitation is
   `invite_unknown` too, named `user`/`platform` (the operator should use `--platform` for the latter).
3. The lookup log is verified like the web client's `org_verify` (`web/src/core/account/join.ts`): from `seq` 0
   against the package's founder (L1-L8), it must contain the package head (`log_rollback`), end at the served head
   (`log_chain`), name the package's org (`log_genesis`) and hold this device invitation (`invite_not_in_log`,
   `invite_revoked`, `invite_consumed`). A log with platform devices cannot be verified before the device is active
   (the platform log needs a signed device request): it is anchored without signatures first (hash chain through the
   package head, genesis of the package's founder and org, the invitation at or before the package head) and verified
   completely right after `POST /v1/device/enroll`, before anything is stored. A later `invite_revoke` or `accept` of
   the invitation is refused structurally already in the anchored mode. Accepted risk: a hub can force the anchored
   mode (an unsigned `device_assign` with `platform_seq` after the package head); the device then signs and sends its
   `accept` but stores nothing, because the full verification fails (the admin removes the stray device).
4. The hub key is pinned **from the package** (05 §2.1, no TOFU): the enrollment answer and every later answer are
   verified with it. The answer must be exactly `{state:"active", org_id:<package org>}`.
5. After the enrollment the device reads the platform log (P1-P4 against `AGENT_RUNNER_PLATFORM_ROOT_KEY`) and stores,
   in one transaction of the runner store: the platform log, the org log with its own `accept` appended (verified
   again), the head and the package keys it is entitled to after the `accept` (read right, existing epoch;
   others in the package are ignored) as `scope_key` rows with source `package`. The store is started for the command
   when it does not run (`vm.cli.store`), and a store that already holds logs, keys, sync state, outbox items,
   sessions or workgroup VMs of an earlier identity is refused (`store_not_empty`; `agent-runner uninstall` cleans it).
6. Order: code parsed and lookup verified before any key exists; `keys/device.key`, enrollment, platform log, store
   transaction, `device.json` last. Any failure after the key file removes both identity files and rolls the store
   transaction back. A failure after the hub accepted leaves a device at the hub that an admin removes (`remove`).
7. `seq_conflict` (another entry was appended between lookup and enrollment) is reported, not retried: the operator
   runs `enroll` again with the same code (the invitation is still open).

## Org devices in the verified book (RUN-03 files `orgs/book.py`, `reconcile/logs.py`)
8. `OrgBook` takes the device kind. An org device has no certification: an org is acting while its copy is verified,
   neither it nor the platform copy is stopped and the device is an active member with exactly its own keys. The
   founder is pinned by the stored copy that the enrollment verified against the package; a copy of an org the device
   never enrolled in is never started from a hub answer (`device_not_assigned`). Step 2 keeps fetching the platform
   log for org devices (to verify platform devices in their org) but does not halt them for missing certification.
   Book status of an org device: `enrolled` (or `stopped`). These are the only edits outside the package's areas.

## Catch-up (05 §2.5, 04 §5.11, W2)
9. The plan is the hub's own function (`crypto.log.catchup`, same vectors): current epochs only, restricted to slots
   whose key the device holds. `cred`: the device cannot read a user's current CK epoch from the logs; it plans for
   the newest epoch it holds, for users of its acting orgs only.
10. The device does not see the wraps the hub already holds for others: an uploaded slot (stored or ignored as
    existing) is remembered per daemon; after a restart each slot is sent once more and ignored by the hub. At most
    200 wraps per run, sorted by slot.
11. The hub refuses a whole request for one rule violation: on `wrap_unauthorized`, `header_invalid` or
    `signature_invalid` the device halves the request until the refused wraps are isolated (bisection keeps the request
    count logarithmic for few refusals, within the device rate budget) and remembers refused slots until a verified head
    changes. Other refusals fail the step (logged by the engine, retried next run).

## Assignment changes (05 §3.5, step 4)
12. Only verified evidence ends anything: `device_unassign` (reason `unassigned`), `wg_archive` (`archived`), `remove`
    of this device (`removed`). A stopped org (rollback/fork) keeps VM and keys; an org the hub stops listing is not
    ended (step 5 stops its sessions because the hub no longer lists them; the VM idles out).
13. Sessions of an ended workgroup (all depths) get `stopping` with reason `unassigned` through the session service
    (a running start is awaited first), then the VM is deleted (`VmManager.delete_workgroup`).
14. Keys: deleted with all epochs. After `remove` at once (org and its workgroups). Otherwise when the device reads the
    scope no more **and** the log shows an epoch newer than every one it holds (rotation observed). Archived
    workgroups stay readable (CRY-02), so their keys stay. `cred` keys are deleted only with a revocation (the CK
    rotation is invisible in the logs).
15. Revocation: a removed org device and a decertified device are refused with `401 key_revoked` on every request, so
    their reconcile runs fail at step 1 and never reach step 4 (and a platform device that sees its
    `device_decertify` halts at step 2). Without changing the engine, the task `revocation` watches the engine's last
    run: halted `decertified` → revoke; failed → one signed probe `GET /v1/device/state`; `key_revoked` revokes only
    when confirmed by probes of failed runs over at least 60 s without a successful run in between (one wrong hub
    answer ends nothing). The teardown holds the engine's lock and the gate step `revoked` (order 5) halts every
    later run, so nothing is rebuilt afterwards and no run interleaves with the teardown. Revoke = every session stopped, every workgroup VM deleted, every scope key deleted, persisted first as
    `sync_state.device_revoked` (repeated at the next start; pool VMs hold no workgroup data and stay for
    `uninstall`). The hub's authenticated refusal of the device key is trusted for this: it can only reduce what the
    device holds (availability, never confidentiality), and a revoked identity is unusable anyway.
16. Devices never rotate; `state.rotation_pending` needs no device action (the next admin browser rotates, step 3 then
    brings the new wraps).

## status and doctor (05 §1.2)
17. `status` makes no hub request; it reports identity, last WebSocket connect, sync markers, outbox numbers, verified
    orgs and key count, VMs, sessions, pool and capacity from the store (started for the command when stopped).
18. `doctor` checks in a fixed order: `os` (only `darwin`; on another OS the configuration refuses anything else, so the
    command prints the failed `os` check only), `lima` (>= 2.3.0), `vz` (`limactl info` `vmTypes`), `postgresql`
    (warn `store_missing` before the first run), `golden`, `landlock` (from the golden's host build record), `hub`
    (signed state request verified with the pinned key; not enrolled: anonymous `/v1/config`), `clock` (skew < 5 s
    against `hub_time`), `textfile`, `test_mode` (warn when on), `sync` (one failure per operator marker). Exit 1 when a
    check failed.
19. `doctor --clear NAME` removes `sync_fatal`, `platform_stopped` or `org_stopped_<hex>` (decisions/RUN-03.md item 11
    left the clearing to this package); `device_revoked` is not clearable (a revoked device enrolls again). It takes
    effect at the next daemon start.

## Review follow-ups
21. Independent review: three medium findings fixed (revocation confirmed over 60 s and gated, item 15; teardown under
    the engine lock, in-flight starts awaited first; no rebuild after revocation). Low fixed: anchored mode checks
    revocation/acceptance (item 3), `store_not_empty` covers every table of an earlier identity (item 5), the key file
    is written inside the rollback block, the clock check compares the unrounded skew and reports an answer outside
    the signature window as `clock fail >120`, catch-up bisects refused batches (item 11), a workgroup is not ended
    without a session service. Low accepted: forced anchored mode (item 3); the edits of RUN-03 files (item 8) are
    required for org devices and documented here.

22. Other packages' files: `enroll/cli.py` (RUN-03) loses its dead fallback `unsupported enroll_org` (the org module
    exists now) and its test the matching branch; `tests/platform/guest/gateway/conftest.py` (GST) derives scenario
    step IDs from SHA-256 instead of `hash() % 1000`, which collided at random and failed the merge gates.

## Tests and E2E
20. The hub is mocked with contract checks and signed answers: the lookup/enrollment hub of
    `tests/platform/runner/assignments/orgkit.py` extends the RUN-03 rig; the catch-up plan is checked against
    `contracts/vectors/log-wraps.json`; assignment changes run the real VM manager on a fake `limactl` and the real
    session service. E2E-04, E2E-05, E2E-31 and E2E-38 do not exist yet in `agent-core` (`e2e/platform/specs/`); their
    scenario packages run them.

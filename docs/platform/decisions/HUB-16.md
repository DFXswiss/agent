# HUB-16 – Monitoring ingestion, rollups, monitoring and audit API, checkpoints, read log

Decisions taken where `08-monitoring.md` sections 4, 6 and 7, `03-api.md` sections 1, 4.11, 5 and 6.1,
`02-data-model.md` sections 2.8 and 2.9, `06-web.md` section 9 and ADR-018 were silent or ambiguous.

## Rollups and retention (`monitoring/rollup.py`, `monitoring/jobs.py`)

1. **Received time and cursor (migration 0005).** Samples arrive late (the runner's outbox queues them up to 48 h), so
   the job cannot only roll up the newest buckets. `metric_sample.received_at` (column default `now()`, so the outbox
   handler of HUB-10 stays unchanged) and the one-row table `metric_rollup_cursor` let a pass recompute exactly the
   buckets touched by samples received since the previous pass. The overlap of 300 s covers outbox transactions that
   started before a pass read the table but committed after it. Recomputing is idempotent; a pass after an outage
   catches up with everything received meanwhile.
2. **One pass at a time.** The pass locks the cursor row (`FOR UPDATE`), so passes of several hub processes are
   serialised; the cursor moves to the start of the pass's statement, never to a time it has not seen.
3. **Aggregates.** Gauges (`cpu_ratio`, memory, disk, `free_vm_slots`, `free_mem_bytes`): average and maximum of the
   non-null values; integer averages are rounded (PostgreSQL `round`, half away from zero). Counters
   (`net_rx_bytes`, `net_tx_bytes`) and `uptime_seconds`: newest non-null value of the bucket. `reachable`, `state`,
   `device_id`: the newest sample's value. A bucket is `[300·k, 300·(k+1))` seconds since the epoch.
4. **Old buckets are not replaced by partial data.** An existing rollup is only replaced while the bucket start is
   within the raw retention (all its samples are still kept). A late sample of an older bucket would otherwise replace
   the complete rollup by one built from that sample alone; such a sample is kept until the raw retention drops it,
   and only creates a rollup when its bucket had none.
5. **Retention on the hub clock.** Raw samples older than 48 h and rollups older than 90 days are deleted in the same
   pass, after the rollup (the oldest raw samples are rolled up before they are dropped).

## Monitoring API (`monitoring/read.py`, `monitoring/routes.py`)

6. **Visible devices** are the active device members of the org (`workgroups.read.org_devices`): the org's devices and
   the platform devices assigned to its workgroups. For a shared platform device only the device sample is shown; VMs
   and sessions only for workgroups the member reads. Pool VMs (no workgroup) and `deleted` VMs are not shown.
7. **Sessions in the overview**: those that have not ended or ended less than 15 minutes ago (the lifetime of an ended
   session's Prometheus series, 08 section 3), newest first. Older sessions remain reachable through the series API.
8. **`stale`** is an additive contract field (`contracts/changes/HUB-16.md`): true when `latest` is null or older than
   60 s by the hub clock. `latest` is the newest raw sample (null after 48 h without one).
9. **Series rights without an org parameter**: a device is visible when it is an active device member of an org the
   caller is an active user member of; a VM or session when the caller reads its workgroup. Everything else (unknown,
   foreign, pool VM) is `404 not_found`.
10. **Series source and steps.** A range that starts within the raw retention is read from raw samples (`step` >= 15 s),
    an older one from rollups (`step` >= 300 s). At most 20000 points (the response contract's limit); `to` must be
    after `from`. Buckets start at `from`; a bucket without data is null. Gauges are averaged (rollups weighted by
    their sample count), counters, `uptime_seconds` and `reachable` (1/0) take the newest value.
11. **Errors**: a malformed or missing series parameter is `400 request_invalid` with the pointer of the first bad
    parameter (order: kind, id, metric, from, to, step); a missing `org` of the overview likewise; a malformed org ID is
    `404 not_found` (like every read of a foreign object).

## Live frames (`monitoring/stream.py`)

12. **Published every 15 s by a job** for every entity sampled within the last 30 s (two sampling intervals): device
    samples to every org the device is a member of without workgroup restriction, VM and session samples to the org of
    their workgroup with `wg_id=` that workgroup (the stream of HUB-07 drops frames for streams without
    `monitoring=1` and filters by workgroup). Frames are split to at most 7000 bytes of JSON so that they travel the
    bus in one notification (< 7900 bytes with the envelope).
13. **Several hub processes** each run the job and publish through the bus; a stream can then receive a sample more
    than once per interval. Accepted: the client keeps the newest sample per entity; the hub normally runs as one
    process. Frames are published for orgs with fresh samples, not only for orgs with an open monitoring stream (the
    stream registry is internal to the stream area and per process).

## Audit (`audit/query.py`, `audit/routes.py`, `audit/jobs.py`)

14. **Member rows**: rows of the org whose `wg_id` the member reads, plus the member's own actions (`actor_id`). Rows of
    the org without workgroup by other actors (log appends, task closes by the admin) are admin-only.
15. **Filters**: `action` matches the action or a dotted group (`operation` → `operation.*`); `actor`, `task`,
    `session`, `wg` are exact. The `time` filter of `06-web.md` section 9.2 has no API parameter (`03-api.md` section
    4.11); the web client pages with `before` (an opaque cursor over `seq`).
16. **Verification** recomputes the whole (global) chain and checks the newest checkpoint of the hub key: signature
    under the hub key and equality with the stored hash at its `seq`. `ok` requires both; a rehashed chain is thus caught
    by the checkpoint. The empty audit has the head `{seq: 0, hash: "0"*64}` (the contract requires a hash). The head is
    global (the same for every org), which reveals the number of audit rows of the hub to an org admin - accepted.
17. **Checkpoint job**: checks every 60 s and writes when the newest checkpoint of the key is at least one hour old (or
    none exists) and the head advanced; serialised across processes by the advisory lock 742180103. Checking often
    keeps the hourly rhythm across restarts.

## Read log (`tests/platform/hub/test_read_log_complete.py`)

18. **Completeness test** enumerates the routes the app discovers (so every later package's routes are included), checks
    the `read_resource` against `contracts/api/INDEX.json`, calls every authenticated non-streaming `GET` with a client
    of its auth class and random IDs, and requires one `read.<resource>` row per call with the caller as actor. The
    only streaming `GET` (`/v1/stream`) is pinned as the exception (decision HUB-07).

## Review (independent subagent: no high, two medium, six low findings)

20. Fixed: the verification reads the head first and verifies exactly up to it (the answer never names an unverified
    head); verifications run one at a time per process (`audit/routes.py`), which bounds the load of repeated requests
    (finding M2). Verifying only from the newest checkpoint was rejected: a tampered row before the checkpoint whose
    successors were not rehashed would go unnoticed. Tests added: a revoked grant hides the workgroup's rows; the
    other org's session sample on the shared device.
21. Accepted and documented (M1, see 13): duplicated `metrics` frames with several hub processes.
22. Accepted (low): the series of a VM is shown to the readers of its current workgroup, including samples from an
    earlier assignment (VMs are reassigned only through the pool, whose samples carry no workload data); rollup
    averages are weighted by the bucket's sample count, also where the metric was null in some samples; ranges read
    from rollups ignore a bucket that starts before `from`, and a `step` that is not a multiple of 300 assigns whole
    rollup buckets; freshness and staleness use the sample time (device clock), as 08 section 6 states the age of the
    data; a sample transaction longer than the 300 s overlap would be missed by the rollup until its bucket is touched
    again.

## Out of the owned paths

19. `tests/platform/hub/test_schema.py` (HUB-01) lists the new column and table of migration 0005, as HUB-10 and HUB-13
    did for theirs.

# KOR-06 – Prompt routing of concurrent follow-ups, rate limit behind a proxy, budget hook, signal futures

Decisions taken where `03-api.md` sections 1, 2, 4.6.3 and 6.1, `02-data-model.md` sections 2.3 and 2.4, the
decisions of HUB-02, HUB-08, HUB-09 and HUB-14 and ADR-027 were silent or had to be corrected (audit items A-09, A-11,
A-12, A-13, A-15).

## A-09: concurrent follow-up prompts

1. **Cause.** `add_prompt` read the hub time before it waited for any lock. Of two follow-ups on an ended session the
   one that read the time first could be stored second: the other one had created the new main session with a later
   `created_at`, so routing (newest main session with `created_at ≤ received_at`) gave the earlier prompt to the ended
   session, where it was shown but never delivered. This lifts the "known limit" of HUB-09 decision 11.
2. **Per-task prompt lock instead of `FOR UPDATE` on the task first.** Locking the task row before `append_events`
   would invert the lock order of the tasks area (events, then task row; `sessions/taskstate.py`) and could deadlock
   with device pushes. `add_prompt` therefore takes a transaction-level advisory lock per task
   (`pg_advisory_xact_lock(742180201, hashtext(task_id))`) before any other lock; only `add_prompt` takes it. The
   two-key form keeps it out of the key space of the one-key locks (migration, audit, platform log, checkpoint); a hash
   collision only serialises the follow-ups of two tasks.
3. **Receipt time after the lock, later than the newest main session.** The hub time is read under the lock, and a
   follow-up's `received_at` is at least one microsecond (PostgreSQL's resolution) after the newest main session of the
   task. With a clock behind that session (another hub process, a clock step) the stored receipt time is therefore
   slightly later than the hub's clock; routing stays consistent, and the order of two main sessions never depends on
   their IDs. Main sessions are created only by `create_task` (with the task) and `add_prompt`, so no other writer
   needs the lock.
4. **Not covered (unchanged design).** A follow-up routed to a live session that the device ends in the same instant
   (the runner reports `stopping`/`stopped` after the hub sent the `prompt` hint) is not carried over to a new session
   (HUB-09 decision 11: no carry-over, so "Retry" never delivers twice). Superseded by KOR-11 (decisions 1–5).

## A-11: anonymous rate limit behind a reverse proxy

5. **Two settings, off by default.** `AGENT_HUB_TRUSTED_PROXIES` (comma-separated IP addresses or networks
   `address/prefix`; empty or unset = no proxy is trusted, the peer address is the source as before) and
   `AGENT_HUB_FORWARDED_HEADER` (`x-forwarded-for`, the default, or `forwarded`; refused without trusted proxies, so
   a misspelt deployment fails at start instead of silently ignoring the header). Networks with host bits set and
   prefix length 0 (trusting every peer) are refused. Both are additions to `03-api.md` section 2 (documentation need
   for DOC-01/DOC-02).
6. **Only one header is read.** A proxy that writes `X-Forwarded-For` usually passes a client's `Forwarded` through
   unchanged (and vice versa); reading both would let a client choose its source.
7. **Rightmost untrusted address.** The header is only read when the peer is a trusted proxy, then from the right:
   trusted hops are skipped, the first other address is the source; a malformed or obfuscated entry (`unknown`,
   `_hidden`, an element without `for=`) ends the walk at the last trusted hop. IPv4-mapped IPv6 addresses count as
   IPv4; ports and brackets are removed.
8. **Resolved once in the pipeline.** `HubPipeline` resolves the source at the start of every HTTP request and
   WebSocket upgrade (`scope["agent_hub.source"]`); `source_address(scope)` returns it, so the anonymous limit, the
   failed-signature limit and the per-source limit of `recovery/begin` (auth area, unchanged) all use the same
   source.
9. **IPv4-mapped networks refused.** Peers and entries in `::ffff:0:0/96` are compared as IPv4, so a trusted network
   inside that range could never match; the start fails instead of silently trusting nothing.
10. **Accepted limits (review).** Parsing is lenient where only a trusted proxy writes the entry (a bad port is
    dropped, an IPv6 zone ID is kept, quoted commas in `Forwarded` are not special): entries left of the first
    untrusted address are never used. IPv6 sources are counted per address as before ("per source address",
    `03-api.md` section 1); grouping by /64 would be a spec change.

## A-12: budget hook before the configuration binding

11. **Bind before serving, and fail closed.** The budget hook gets no configuration (ADR-031) and read the limit
    bound by the job `usage.bind_config`, whose first run raced with the first requests (the job loop starts with the
    app; with the periodic jobs off it never ran), so the hook answered "unlimited". Root fix: a `Job` may be marked
    `at_start`; `JobRunner.prepare` runs those once and the app lifespan awaits it before it serves the first request
    (also when the periodic jobs are off); a failed at-start job stops the start (an own pool is closed). The usage job
    is `at_start` (and then hourly). Defense in depth: should the hook ever run unbound it raises `503 unavailable`
    (retryable) instead of answering "unlimited" or a final `budget_exceeded`.
12. **Outside the listed paths.** The generic flag touches `jobs/runner.py` and the lifespan in `app.py` (HUB-02's
    files): the package names "bind in the lifespan before requests" as the fix, and a usage-specific call in the
    lifespan would break the discovery rule of ADR-027 section 4. Documentation need for DOC-01: ADR-027's job
    extension point gains the optional `at_start` flag.
13. **Review: "fail closed only" rejected as the sole fix.** It would stay inside the listed paths, but every hub
    start would refuse subagent requests with `503` until the first job run or device-state request (with the periodic
    jobs off: until a runner fetches its state). The runner retries a 5xx with the same operation ID (verified in the
    runner's `operations/register.py`), so the fail-closed path stays as defense in depth, and its retry is tested. An
    at-start job runs without a timeout: it must be quick and local (binding memory state); a cancelled start also
    closes the own pool.

## A-13, A-15

14. **A-13.** `device/signals.py` drops finished reconcile futures before it queues the next one (as
    `invites/signals.py` does); `wait()` still drains the queued and running ones.
15. **A-15.** `main_session_id` of the device task rows is the newest main session (`created_at DESC, id DESC`), the
    same rule as the browser task rows (HUB-09 decision 9). The runner does not read the field today, so nothing
    changes for it.
16. **One merge for A-13 and A-15.** Both are low-severity one-line fixes in separate commits; they were reviewed and
    merged together after the medium and high items had landed one by one.

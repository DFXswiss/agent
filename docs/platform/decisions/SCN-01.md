# SCN-01 – decisions in the agent repository (integration fixes found by the M1 full-chain scenarios)

The scenarios themselves live in agent-core (`e2e/platform/`, decisions `docs/platform/decisions/SCN-01.md` and
ADR-034 there). Three small runner changes were needed for the first runs over the whole chain:

1. **`agent-runner image adopt <cache-golden-…>`** (`vm/cli.py`, `golden.Builder.adopt`, `Lima.shared`): an E2E
   runner registers the shared golden cache (ADR-027 §7, built by the work-package tools from the same `images/` and
   guest tree) as its `ready` golden, so its pool clones the cache instead of building inside the hermetic run (a
   build fetches packages and would be egress). The instance must exist and be stopped (`golden_missing`,
   `golden_not_stopped`); older `ready` rows become `retired`; re-adopting refreshes the row. The cache is never
   copied, changed or deleted: `retire` deletes only own instances, so retiring an adopted cache drops just the row.
   Version = the cache's suffix (`<sha12>`); claude/guest versions and image digest come from the local source tree,
   whose `golden.cache_tag` (the ADR-027 §7 rule, identical to the work-package tools and the E2E orchestrator) must
   equal the suffix (`golden_stale` otherwise). The command is TEST_MODE only (`test_mode_required`), like
   `--test-model-port`: production runners build their own golden.
2. **Outbox push pacing from the end of the previous push** (`sync/push.py`): the hub admits one push per device and
   250 ms *by arrival time* (`agent_hub.outbox.pacing`) and answers an earlier one with `429` and `Retry-After: 1`.
   The runner waited 250 ms after the previous push *started*; with latency jitter a push could arrive less than
   250 ms after the previous one and cost a whole second (seen in the E2E logs as `push_refused status=429` right
   during session starts, i.e. inside the time-to-working budget). Waiting 250 ms after the previous answer guarantees
   the gap at the hub. Test: `test_the_interval_counts_from_the_end_of_the_previous_push`.
3. **`session.start` hint wakes the reconcile run** (`reconcile/hints.py`): the hub sends it for a session placed on the
   device (03-api.md section 4.6.1 step 3, section 6.2), but no area registered it (`runner.sync hint_unknown
   type=session.start`). The first task of a new org was still picked up at once because the device assignment changed
   the org log (`log` hint), while a further task in the same workgroup waited for the next periodic reconcile run –
   29.5 s in a T2 run of E2E-08. The hint now wakes the engine like `reconcile`/`log`/`wraps`; the run fetches
   `GET /v1/device/state`, whose desired sessions the session service starts.

Findings reported, not changed here (see the SCN-01 report): pool VMs filled before the device is enrolled are found
"unknown" and deleted after the restart that follows the enrollment; the hub's `session.vm_id` stays empty although the device reports its VMs.

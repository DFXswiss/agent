# KOR-03 decisions (agent repository: runner corrections from the audit AUD-01)

Items A-06, A-17, A-18, A-27, A-29, B-19, B-25 and B-30 of the audit. Each correction comes with a test that fails
without it.

## A-06 Interrupted VM start

1. **What happened.** A runner stop is a SIGTERM to the daemon's process group (the E2E orchestrator; a service
   manager behaves alike). The signal also hits a running `limactl start` or `limactl clone`: the call fails
   (`vm.start_failed … code=lima_failed`), the half-created instance is deleted and the provisioning **retried under a
   new name while the daemon is already stopping**. When the supervisor then cancels the task, `limactl clone/start`
   keeps running in its worker thread (cancelling an `asyncio.to_thread` await does not stop the thread). The VM boots
   without a row, outside the runner's control. On the hot path the old cancellation handler even ran
   `limactl delete -f` *concurrently* with the start still running in the thread.
2. **Correction (`vm/pool.py`, `vm/tasks.py`).** `VmEnv.stopping` (the daemon's `context.stopping`, set on SIGTERM
   before the shutdown hooks run): no clone is started and no failed clone is retried once it is set; `Pool.missing`
   is 0. Every `limactl clone/start` of a provisioning and every deletion (`VmEnv.destroy`, used by all cleanups,
   also the hot path's) take a per-instance lock in their worker thread: a deletion never overlaps a running call on
   the same instance and completes even when the task is cancelled again (the interpreter joins the worker threads
   before the process exits). Left over anyway: a VM of a daemon killed with SIGKILL, or of a clone whose queued work
   item had not yet taken the lock when the deletion took it (a window of microseconds); neither has a row and the
   startup reconciliation deletes it before any other VM work (unchanged rule of 05-runner.md section 3.5).
   A hot-path request that arrives while the daemon stops fails with the existing `vm_start_failed` (no new session
   failure reason; the hub re-schedules the session like any start failure).
3. **Measured on the runner device (Lima 2.3.0, golden cache of the integration branch).** A pool clone of the
   hardened golden whose `limactl start` was killed with SIGTERM or SIGKILL two seconds in boots completely (guest
   services, hardening, self-test) and, like a regularly started one, sends nothing but NTP in four minutes:
   pollinate is skipped (seeded in the golden), the apt, motd and firmware timers are disabled. `limactl delete -f`
   during a running `limactl start` (0.3 to 4 s in) kills the host agent and leaves nothing. So an orphaned pool
   clone of the golden is not what reached the mirrors in the SCN-07 run; its addresses (Canonical image and archive
   hosts on 443/80 and the CDN that serves the npm registry and the Node.js downloads, all pinned in
   `images/versions.json`) are those of a VM booted from the plain cloud image or of a golden build, whose run
   artifacts were overwritten by later runs. The hardened golden has network before and after `harden.sh` (the
   nftables policy restricts session users and the platform user, not root), but no service of the golden uses it;
   nothing to close in the golden for this item.
4. **Two Lima behaviours found on the way (measured, corrected here).** A `limactl clone` killed in its first
   ~40 ms (the stop signal above) can leave an empty instance directory. Lima lists it as `Broken` and refuses
   `limactl delete -f` on it ("it was NOT deleted, in case it is currently being cloned into"); the runner read that
   answer as "instance missing", so the startup reconciliation raised on every start and the VM area never became
   ready. `Lima.delete` now removes such a leftover itself (own prefix, `Broken`, directory named like the instance,
   directly in Lima's home, no `lima.yaml`; recognised by Lima's own refusal line), and the reconciliation logs an
   instance it cannot delete (`vm.unknown_delete_failed`) and goes on. `limactl start NAME` of a missing instance creates a new one from `template:default` (stock image, host home
   mounted, open network): `Lima.start` already refuses a missing instance (unchanged).
5. **E2E quiet period.** `capacity.restart_quietly` (agent-core, SCN-07 decision 15) waits 30 s for settled VMs
   before a runner restart. It belongs to agent-core and stays; the acceptance run of this package removes the wait in
   a local variant only (report KOR-03).

## A-17 Profile key without scope

6. Migration `0009_profile_event_scope.sql` adds `scope_id` (from the stored signed header) to `runner.profile_event`
   and to its key `(kind, org_id, scope_id, object_id)`. `profiles.name` reads only the requested scope: the session's
   workgroup for `task.meta` (now required, a missing one is a programming error) and the org for the org-scoped kinds
   (`scope_id = org`, 04-crypto.md section 5.4). The signed header check stays as a second line.

## Review (independent subagent, batch A-06/A-17)

7. Fixed: M1 (the hot path's own cleanup after a second cancellation deleted beside a running start: every deletion
   now takes the instance lock), M2 (the refusal is recognised by Lima's fatal line alone), L1 (the removed directory
   must lie directly in `LimaHome`), L2 (`delete` reports an instance gone meanwhile as missing), L3 (docstring no
   longer says "never"), L9 (unused fixture, no public host names here).
8. Accepted with reasons: L4 (a deletion whose work item never started because the loop shut down first is caught by
   the startup reconciliation, like a SIGKILL); L5 (see item 2); L6 (`task.meta` is the only workgroup-scoped kind of
   the four, checked against `contracts/events/kinds.json` by the inbox before an event reaches the store); L7 (rows of
   `profile_event` come only from verified events, whose header always has `scope_id`).

## Documentation needed (for DOC-01)

9. `docs/platform/spec-deviations.md` section 4 (runner migrations): add `0009_profile_event_scope` (`scope_id` in the
   key of `runner.profile_event`).
10. `05-runner.md` section 3.5 (startup reconciliation): an unknown instance Lima cannot delete is logged
    (`vm.unknown_delete_failed`) and does not stop the reconciliation; the leftover of an interrupted clone is removed
    by the runner itself. Section 3.3: no clone is started or retried while the daemon stops.

## A-18 Arguments of refused reads

11. A `platform.read` event is sealed only with arguments a tool checked (`subagents/controller.py`). Its body must
    match `events/platform.read.json`, whose `arguments` are the tool's input schema; arguments a tool refused as
    `params_invalid`, or of a call that failed inside the tool, have no valid body, and an empty object is no valid
    body for the tools with required arguments either. So such a call is audited (`read.session.<tool>`, result
    `failed`) and logged (`query_arguments_refused` / `query_failed`), without an event: nothing was read. Reads
    refused on rights (`forbidden`, `not_found`, `denied`) keep their event as before. This narrows RUN-08 decision 19
    ("also for refused reads") to reads whose arguments a tool accepted; the contract stays unchanged.
12. Contract test `tests/platform/runner/queries/test_read_contract.py`: per tool argument sets the input schema
    refuses (and the two `get_whiteboard` level/`task_id` combinations the schema cannot state) are refused by the
    tools, audited and never sealed; every sealed body validates.

## A-27 Checkout in memory

13. `github.checkout` streams the repacked tar from its scratch file to the guest in pieces of 1 MiB
    (`checkout.chunks`); `GuestClient.import_files` takes a stream with its `size` (the guest refuses chunked bodies,
    `routing.py`), sent as `Content-Length`. The runner no longer holds up to 512 MiB per checkout.

## A-29 Budget renewal and the observed cost

14. Confirmed: a renewal (`PUT .../credential` after the organization's spent amount changed) restarted the guest
    gateway's observed cost at 0, while the new spent amount does not yet contain this gateway's recent cost (usage
    journal -> runner -> outbox -> hub); a gateway near its limit could spend that amount again. The root is the
    guest's credential store, so the correction is there (`agent_guest/gateway_credentials.py`; the package names
    `model/gateways.py`, which only decides *when* to renew and needs no change): both `old spent + observed` and
    `new spent` are lower bounds of the true spending, the renewal keeps the larger one (it carries
    `max(0, old spent + observed - new spent)`); a smaller new amount (a new day) carries nothing (the observed cost
    belongs to the day before; carrying it would stay as a phantom amount); a request that ends after a renewal is
    charged to the current entry. Tests:
    `tests/platform/guest/gateway/test_units.py`, `test_gateway.py::test_budget_is_enforced` (red without the change).

## B-19 Retention

15. New daemon task `retention` (area `agent_runner/retention/`, discovered `tasks.py:TASKS`) every 10 minutes, in
    batches of 5000 rows per transaction:
    - `runner.outbox`: items the hub acknowledged (`origin_seq <= pushed_origin_seq`) and older than one hour
      (ADR-038: the subagent results read the event items of the last hour); never while the hub's position is
      unknown; the item with the highest `origin_seq` always stays (numbering `MAX + 1`); under the outbox lock.
    - `runner.prompt_event`: events whose delivery is final (`accepted`, `refused`, `failed`) and unchanged for 24 h;
      the delivery row stays (it alone keeps a final prompt from being delivered again); kept: prompts without
      delivery row or with an open one, and per task the earliest prompt that was not refused or failed (restart
      detection `queue.earlier_of_task`); accepted never changes again, so the kept prompt is stable across rounds.
    A routing of a pruned final prompt (the hub may route a final prompt once more) answers its final state in
    `Deliverer.route_one` instead of `missing` (no inbox refresh, no `prompt_unverified` audit); HOT-03's routing and
    delivery are otherwise unchanged.
16. A hub restored from a backup that asks again for items older than an hour (`409 origin_seq_gap` below the lowest
    stored `origin_seq`) cannot get them from this device any more: the push goes fatal (`origin_seq_gap`, operator
    action, `sync/push.py`) instead of resending for ever; `rewind` and `prune` run under the outbox lock. Recovery
    after a hub restore stays DR-01's. The new area `agent_runner/retention/` only adds files (discovered task, ADR-027
    section 4); `sync/push.py` (one condition) and `prompts/deliver.py` (`route_one`) change only as consequences of
    the retention.

## B-25 `images/` and the wheel

17. The golden build needs one tree with `images/`, `src/agent_guest/` and `contracts/` (VM-01 decision 7);
    package data cannot carry the repository's top-level directories without moving them. Decided: no package data;
    the installation profile of a runner device is the checkout (runbook runner-installation: editable install), or
    `AGENT_RUNNER_GOLDEN_TEMPLATE` naming a checkout's `images/golden.yaml`. A runner installed as a package without
    either refuses the build (`golden_source_missing`). Test `tests/platform/runner/vm/test_build_input.py` (a
    wheel-like copy of the packages: refused; with the template: the checkout's tree).

## B-30 Pool VMs before the enrollment

18. Analysed: the enrollment changes no `runner.vm` row; a pool VM gets its row only after its provisioning (with the
    self-test, 10 s and more). The restart that follows the enrollment interrupted fills in flight, whose instances had
    no row and were deleted as "unknown" at the next start - the A-06 situation. With A-06 the stop deletes them
    itself; completed pool VMs survive a restart (`tests/platform/runner/vm/test_restart_keeps_pool.py`). Not
    keeping a half-provisioned VM is intended (its self-test did not run). Closed with A-06.

## Documentation needed (for DOC-01), continued

19. `02-data-model.md` section 4 / `05-runner.md` section 2.3: retention of `runner.outbox` (acknowledged, older than
    1 h, newest item kept) and `runner.prompt_event` (final delivery 24 h ago, earliest counting prompt per task
    kept); the daemon task `retention` in section 1.1.
20. `03-api.md` section 8 / ADR-024: a call whose arguments the tool refused is audited but has no `platform.read`
    event.
21. Runbook runner-installation: the golden build input (checkout or `AGENT_RUNNER_GOLDEN_TEMPLATE`).
22. `05-runner.md` section 10.2: the gateway's observed cost across a renewal (item 14).

## Review (independent subagent, batch A-18 ... B-30)

23. Fixed: H1 (gap below the pruned range: fatal instead of an endless resend; position read under the lock), H2
    (`get_subagent_result` refuses unknown keys, so no VM text can reach a sealed `platform.read`), M3 (routing of a
    pruned final prompt), M4 (no carry-over into a new day), M6 (earliest *accepted* prompt kept), low: the checkout
    stream is closed on failure (`aclosing`), the failing-tool test asserts that no event is sealed, ownership noted
    (item 16).
24. Accepted, for DOC-01: M5 - `get_subagent_result` of a child that ended more than an hour before a daemon restart
    answers an empty `final_text` (the event items are pruned after an hour, ADR-038's bound); 03-api.md section 8.1
    should name the bound. Low: the retention task's own test uses stub pruners plus an empty store (each pruner has
    its own test against real data); the `received_at` sort of the prompt pruning has no index (5000-row batches).

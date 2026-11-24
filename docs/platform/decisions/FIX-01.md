# FIX-01 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## FIX-01 - Flaky platform tests fixed at the root (agent)

Every case below was traced to its cause and, where marked, reproduced on purpose (artificial delay, forced state)
before it was changed; nothing was deleted, skipped, marked as expected to fail or retried. The full classification of
the gate and merge logs, the series of runs and the measured durations are in the workspace report of FIX-01.

### 1. Known cases

| Test | Classification | Cause and evidence |
|---|---|---|
| `guest/gateway/test_gateway.py::test_parallel_first_requests_all_find_the_credential` | flaky, already fixed (RUN-06, `51a6008a`) | Step ids `s{hash(text) % 1000}` collided for 8 steps in about 1.5 % of processes (`hash` is salted per process): 3 of 201 `PYTHONHASHSEED` values give a duplicate id and the scenario refuses to load. Now a SHA-256 prefix. Confirmed by the series. |
| `guest/mcp/test_mcp.py::test_argument_abuse_is_refused_before_forwarding[get_whiteboard-arguments9]` | not flaky: intermediate state of a coupled change | The agent-core half of GST-07 (plain-object `get_whiteboard` schema) was synced into the agent integration branch before the agent half (the tool's own `task_id` rule). Rebuilt that state (agent tree before GST-07 plus the synced contracts): the test fails deterministically with `'timeout' == 'params_invalid'`. Nothing to change. |
| `runner/login/test_profile.py::test_the_installer_reports_a_timeout` | flaky, fixed here | The step that must succeed ran under the 0.2 s limit meant for the step that must time out; a child Python start under coverage (`patch = subprocess`) and load exceeds it (child that needs 0.3 s: exit 124). The successful step now runs under the real limit. |
| `guest/gateway/test_units.py::test_provisioning_refusals` (BrokenPipe) | flaky, already fixed (INF-01, `79987b45`) | The control server answers 401 before reading the body; the test client now reads the answer after `EPIPE`. Confirmed by the series. |
| Hang in `settle_queries` (gate logs of INF-01, INF-02, SCN-07) | fixed by HOT-01 (`3b52f48c`) | Same traceback (`asyncio.gather` at `tasks.py:861` under `settle_queries`); confirmed by the series. |

### 2. Further cases fixed here

| Test | Cause | Reproduction | Correction |
|---|---|---|---|
| `doubles/login_cli/test_authpage.py::test_malformed_token_requests_are_failures` (5 red gate runs) | The login double answered a refused token request before recording the failure, so the client could read the failure list too early. | New test `test_a_refused_token_request_is_recorded_before_the_answer` slows the recording by 0.3 s: red with the old order, green now. | Record, then answer (`src/agent_doubles/login_cli/authpage.py`, a test double; the HTML error path already did this). |
| `runner/login/test_automation.py::test_the_platform_chromium_below_the_runner_home_is_used` | `monkeypatch.delenv(raising=False)` of an absent variable records nothing; the variable that `use_browsers` sets afterwards leaked into the following tests of the worker (`PLAYWRIGHT_BROWSERS_PATH` pointing to an empty temporary directory). | A check test run after it in the same process sees the leaked path. | Set the variable before removing it, so the undo removes it again. |
| `runner/requests/test_service.py` (replaced, not listed, finished flows; loop offers) | Fixed sleeps (10-50 ms) or an iteration cap of about 1 s while the flows write their state to PostgreSQL. | Async PostgreSQL statements delayed by 60 ms (pytest plugin): `test_a_finished_flow_is_not_replaced_by_a_newer_root` red, green now. | New helper `tests/platform/runner/settle.py:until` (condition, plain or awaitable, 60 s bound, yields the loop); the tests wait for the stored state. |
| `guest/test_server.py::test_handler_timeout`, `::test_handler_slots_are_bounded` | A 0.6 s margin between the handler timeout and the slow handler; a fixed 1.2 s sleep for the slot to free. | - | The timeout is shown with a handler held until the test releases it (the answer comes while it still runs, within 5 s, so the lab's 0.3 s limit and not the default 10 s applied); the slot test polls until the slot is free. |
| `guest/mcp_tools/test_tools.py` (late runner answer, local check) | Answer after 0.6 s in a 1 s window; a 2 s bound where the alternative is a 20 s wait. | - | Window of 10 s; bound of 10 s. |
| Condition loops in `runner/sessions`, `runner/model`, `runner/operations/test_wiring.py`, `guest/test_recorder.py`, `guest/test_transcript.py`; `runner/test_supervisor.py`; `guest/test_control.py` | Polling caps of 2-2.5 s over PostgreSQL, threads and sockets; a 1 s bound where the hooks hang for ever; a fixed 0.2 s sleep before checking a thread ended. | - | Caps of 30 s (success returns at once), bound of 5 s for the 0.2 s budget, `join(30)`. |
| `runner/assignments/test_assignments.py::test_a_successful_run_resets_the_confirmation` (3 of 20 runs in a loaded series) | The test gave the revocation task 20 loop turns after each reconcile run; the task first reads the database (real I/O) and, when that takes longer, misses the first failed run (`key_revoked_seen` once instead of twice). | Async PostgreSQL statements delayed by 60 ms: `assert 1 == 2`, as in the series; green now. | `polled()`: wait until two of the task's polls (each ends with a `POLL_S` sleep on the host clock) have ended after the run, or the awaited outcome holds; `watch()` waits by condition. |
| `runner/sync/test_ws.py::test_backoff_resets_after_30_s_stable` (1 of 20) | Product race: when the hub closes normally while a heartbeat is being sent, `send` raises `ConnectionClosedOK` before the receive loop ends, and the client logged `ws_failed error=ConnectionClosedOK` instead of `ws_closed` (only the log line; reconnect and backoff were right). | New test `test_a_normal_close_seen_while_sending_is_a_close` with a socket whose `send` meets the close: red before, green after. | `src/agent_runner/sync/ws.py`: a `ConnectionClosedOK` is a close (`ws_closed`); every other error stays `ws_failed`. |
| `runner/sessions/test_paths.py::test_pool_path_then_running_path` (1 of 11 on the runner device), `runner/vm/test_pool.py::test_refill_fills_up_to_pool_size_two_in_parallel` (1 of 20) | The test guests run as threads of the test process, and the guest binds its sockets under the process-wide `umask(0o177)`. A directory created by another thread inside that window (the token store's parent via `mkdir(parents=True)`, a fake instance directory) lacks the search bit, so the token write (`PermissionError`) or the guest's bind fails at once and the pool stays empty (`vm_start_failed` after 0.45 s). Not a product defect: on a device every guest is its own VM. | The socket bind of the guest slowed by 0.3 s (pytest plugin): `(0, 0) == (3, 0)` three times out of three, as in the series; green now. | `tests/platform/runner/vm/guests.py`: the harness creates the token store's parent before any guest boots; one re-entrant lock covers every file operation of the fake Lima and the whole guest boot. The start-path assertion carries the VM and runner logs. |
| `guest/capture/test_capture.py::test_the_op_reports_a_written_file_at_once` (1 of 5 in the final series) | The session's periodic scan (every 0.02 s in the test) ran next to the `capture.file_written` op; when it saw the change first, the op rightly answered `reported: false`. | A scan right before the second op: `{'reported': False} == {'reported': True}`, as in the series. | The test stops the scan loop and joins its thread first (as its neighbour does), so the op is what reports the change. |
| `guest_linux/test_metrics_cgroup.py::test_session_values_move_under_load` (merge gate, Linux part) | Half a core over a fixed 4 s of wall time was expected from a spinning load with an fsync every 0.5 s; on the loaded runner device the VM's shared vCPU and disk gave 46 %. | The gate log shows 1.84 s of CPU against the 2 s bound. | The CPU delta is checked against the measured interval between the two collects, at a quarter of a core (the property: the session's values move under load). |
| `runner/sync/test_outbox.py`, `runner/test_store.py` | Rows compared as lists without `ORDER BY`. | - | `ORDER BY`. |
| `runner/operations/test_inbound.py`, `runner/guestlink/test_client.py`, `guest/test_apisock.py` | 2 s deadlines on loopback round trips. | - | 10-30 s. |

### 3. Safety net: hard limit per test

`tests/platform/pytest.ini`: `timeout = 180`, `timeout_method = signal` (pytest-timeout, added to `platform-test` in a
separate commit). The signal method fails the running test inside the worker process (setup, call and teardown) with
the stacks of every thread, so the xdist worker survives and its coverage data is written; the thread method would end
the worker with `os._exit` and lose its coverage. It is a safety net, not a cause fix: the longest test of the series
of runs is far below the limit (workspace report).

Two additions in `tests/platform/conftest.py` (after the independent review):
- Suites that run only on request and drive real VMs or services (markers `atk`, `vm`, `live`) get 3600/1800/1800 s
  where no explicit `pytest.mark.timeout` is set; the attack suite runs all attacks inside one session fixture that
  has taken up to about 200 s.
- A second stage replaces `faulthandler_timeout = 300` (one faulthandler timer per process): a test still running
  120 s after its limit (the signal fires once; a background task can swallow it, a teardown can wait on the hung work,
  a C call can hold the interpreter) ends its process through `faulthandler.dump_traceback_later(exit=True)` with the
  tracebacks of every thread. xdist reports the worker as crashed and replaces it (the run continues; that worker's
  coverage is lost, so the gate is red anyway); a single process ends. pytest's faulthandler plugin cancels pending
  dumps in `pytest_exception_interact`, which runs exactly when the first stage has failed the test, so the stage is
  re-armed there for the remaining time.

Proof in a throwaway copy: four deliberately hanging tests (busy loop, `asyncio.gather` over done futures, blocked
`Event.wait`, hanging teardown) were red after 180 s, the full `make platform-test` ended (478 s) and the combined
report with the Linux part stayed at 100 % lines and branches. The second stage was shown with a test whose teardown
waits on work hung after the first stage fired: single process ended with the tracebacks, under xdist the worker was
reported as crashed while the other tests passed. `scripts/guest-ci.sh` installs pytest-timeout in the guest-ci VM
(provision version 3); the two Linux modules that run real Claude Code (`guest_linux/test_sessions_linux.py`,
`guest_linux/delivery/test_delivery_linux.py`: package download on a cold cache, model turns) set 900 s.

### 4. Classified, not changed here

- `guest_linux/test_selftest_linux.py` (3 red runs, INF-02 only): two gate runs of the same package shared one guest-ci
  VM and its checkout directory (`scripts/guest-ci.sh` re-extracts it per run). A limit of the gate tooling (INF-02
  notes it), not of the tests.
- Probe-then-close ports of `guest/gateway/test_gateway.py::test_unreachable_upstream_is_502` and
  `atk/test_dispatch.py::test_an_unreachable_double_answers_502`: keeping the probe bound (not listening) was tried and
  reverted, because the device's network stack then drops the connection attempt and the tests waited for an 8 s
  connect timeout instead of the refusal they check; a parallel worker taking the same ephemeral port in between was
  never observed.
- `src/agent_cli/pg.py:_free_port` (probe-then-bind for the per-worker PostgreSQL cluster): legacy code, which the
  platform packages must not change; no failure observed.
- `runner/login/test_automation.py` page timeout of 1.5 s against real Chromium: no failure observed; left unchanged.

## FIX-01 - Flaky platform tests fixed at the root (agent-core)

Every case below was traced to its cause and reproduced on purpose (forced order, artificial delay, frozen clock)
before it was changed; nothing was deleted, skipped, marked as expected to fail or retried. The full classification of
the gate and merge logs, the series of runs and the measured durations are in the workspace report of FIX-01.

### 1. Fixed tests

| Test | Cause | Reproduction | Correction |
|---|---|---|---|
| `hub/usage/test_budget.py::test_platform_budget_lists_every_assigned_org` | The scene's two devices get UUIDv7 ids; created in the same millisecond, their order is random, so the scene's `device` (used for the first session) could be `meter.high` and both usage reports landed in one `usage_daily` row (`[(1,)] == [(2,)]`). | `Meter` with the scene's `device` set to the higher id: exactly the observed failure; with the correction green in both orders. | The two sessions and reports are pinned to `meter.low` and `meter.high`. Test defect. |
| `hub/core/test_pipeline_edges.py::test_writer_writes_rows_older_than_the_delay_at_once` | A real-time bound of 150 ms around a thread hop and a PostgreSQL transaction. | `ReadLog.flush` delayed by 0.4 s (pytest plugin): red. | The writer's clock is injected (`monotonic=`, `max_delay=60`, the row is 61 s old); the bound is 30 s, far below the delay a broken writer would wait. |
| `hub/core/test_read_log.py::test_writer_task`, `::test_writer_retries_after_a_failure` | Fixed sleeps (0.3 s, 0.2 s) as synchronisation with the writer thread. | Same delayed writer: both red. | Wait by condition (rows stored, log line present) with a 30 s bound. |
| `hub/core/test_read_log.py::test_rows_are_written_within_one_second` | Wall-clock bound of 1.5 s for request, writer thread and transaction. | Same delayed writer. | The one-second delay stays asserted as configuration; the wait only bounds a broken writer (30 s). The timing moved to the injected clock: the new test `test_writer_waits_the_rest_of_the_delay_for_a_new_row` checks the wait (delay minus the age of the oldest row, reset by a write), the test above that an old row is written at once. |
| `hub/core/test_jobs_signals.py` (failing job is retried) | `sleep(0.2)` expected three job passes (three thread hops). | - | Wait until three passes ran (30 s bound). |
| `hub/core/test_app.py` (lifespan job), `hub/core/test_cli.py` (`/v1/config` read) | 5 s / 1 s deadlines for work under the hub lifespan. | - | 30 s polling bound / 10 s read timeout (success returns at once). |
| `hub/stream/support.py` helpers, `hub/stream/test_sse.py` | Frames that must come were awaited with 5 s defaults or collected in a 0.5 s `quiet()` window; one `wait_for` result was not asserted. | - | `WAIT_S = 30` for the helpers' positive waits; positive frames are awaited with `frame()` and followed by `quiet() == []`; the `wait_for` result is asserted. Negative checks keep their short windows. |
| web `tests/ui/tasks.test.tsx` (countdown digits, "Verzögert … 12,x s") | `Date.now()` taken before the asynchronous setup; a slow setup changes the shown seconds. | - | `Date` is faked (frozen) as in the sibling test "follows the clock". |
| web `tests/ui/monitoring.test.tsx` ("Updated N s ago") | Real `Date` during the setup; more than 5 s of setup gives two digits. | - | `Date` is faked (frozen); the one-digit expectation stays. |

### 2. Safety net: hard limit per test

`tests/platform/pytest.ini`: `timeout = 180`, `timeout_method = signal` (pytest-timeout, added to `platform-test` in a
separate commit). The signal method fails the running test inside the worker process (setup, call and teardown) with
the stacks of every thread, so the xdist worker survives and its coverage data is written; the thread method would end
the worker with `os._exit` and lose its coverage. It is a safety net, not a cause fix: the longest test of the series
of runs is far below the limit (workspace report).

Two additions in `tests/platform/conftest.py` (after the independent review):
- Suites that run only on request and drive real VMs or services (markers `atk`, `vm`, `live`) get 3600/1800/1800 s
  where no explicit `pytest.mark.timeout` is set; the attack suite runs all attacks inside one session fixture that
  has taken up to about 200 s.
- A second stage replaces `faulthandler_timeout = 300` (one faulthandler timer per process): a test still running
  120 s after its limit (the signal fires once; a background task can swallow it, a teardown can wait on the hung work,
  a C call can hold the interpreter) ends its process through `faulthandler.dump_traceback_later(exit=True)` with the
  tracebacks of every thread. xdist reports the worker as crashed and replaces it (the run continues; that worker's
  coverage is lost, so the gate is red anyway); a single process ends. pytest's faulthandler plugin cancels pending
  dumps in `pytest_exception_interact`, which runs exactly when the first stage has failed the test, so the stage is
  re-armed there for the remaining time.

Proof in a throwaway copy: four deliberately hanging tests (busy loop, `asyncio.gather` over done futures, blocked `Event.wait`, hanging fixture
teardown) were red after 180 s, the full `make platform-test` ended, and the report of the other tests stayed at 100 %.
The second stage was shown with a test whose teardown waits on work hung after the first stage fired: the process
ended with the tracebacks (single process), and under xdist the worker was reported as crashed while the other tests
passed and the run ended.

### 3. Classified, not changed here

- Playwright specs in `web/e2e/` (`team()` helper, device list): timeouts only under very high device load (shared
  PostgreSQL and 18 browser workers, before INF-02). Not reproduced in this package; see the workspace report.
- Probe-then-bind free ports (`hub/stream/support.py:free_port`, `hub/core/test_cli.py`): a real but rare race between
  closing the probe and the server's bind; documented, not observed in any log or run.
- Testing Library's 1 s default for `findBy*`/`waitFor` in the web unit tests: not observed failing since the fixes of
  WEB-11/INF-01; left unchanged rather than raising every deadline without a failure to show.

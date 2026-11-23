# FIX-01 - Flaky platform tests fixed at the root (agent-core)

Every case below was traced to its cause and reproduced on purpose (forced order, artificial delay, frozen clock)
before it was changed; nothing was deleted, skipped, marked as expected to fail or retried. The full classification of
the gate and merge logs, the series of runs and the measured durations are in the workspace report of FIX-01.

## 1. Fixed tests

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

## 2. Safety net: hard limit per test

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

## 3. Classified, not changed here

- Playwright specs in `web/e2e/` (`team()` helper, device list): timeouts only under very high device load (shared
  PostgreSQL and 18 browser workers, before INF-02). Not reproduced in this package; see the workspace report.
- Probe-then-bind free ports (`hub/stream/support.py:free_port`, `hub/core/test_cli.py`): a real but rare race between
  closing the probe and the server's bind; documented, not observed in any log or run.
- Testing Library's 1 s default for `findBy*`/`waitFor` in the web unit tests: not observed failing since the fixes of
  WEB-11/INF-01; left unchanged rather than raising every deadline without a failure to show.

# INF-01 – Gate run time: parallel platform tests

Decisions of work package INF-01 in agent-core (the agent repository has its own `decisions/INF-01.md`). The gate stays
the same: the same tests and markers, the same steps, 100 % lines and branches of `agent_hub` with the pinned
`[tool.coverage]` configuration (`test_coverage_config.py` unchanged).

## Profile (before)
1. The platform tests (about 9,900) spent about 530 s in one process; less than 80 s of it was CPU time of the test
   process. Per test phase: setup 148 s, call 58 s, teardown 323 s. The teardown is dominated by the hub app's
   lifespan shutdown: the live bus's listener polls `notifies(timeout=0.25)` and the shutdown waits for the end of that
   window (about 235 ms per `hub` fixture, about 920 fixtures). Per database test, creating the roles and the database
   costs about 30 ms, the migrations about 38 ms, dropping about 17 ms.
2. Consequence: the suite waits far more than it computes, so parallel workers shorten it almost linearly until the
   CPU of the machine is the limit.

## Parallel workers
3. `make platform-test` runs pytest with pytest-xdist (`-n $(PLATFORM_TEST_WORKERS)`, default 4; `0` = one process,
   `auto` = one per core). Coverage of every worker comes from pytest-cov (`--cov --cov-report=`) with the unchanged
   `[tool.coverage]` of `pyproject.toml` (source `agent_hub`, branch, `fail_under = 100`); pytest-cov combines the
   worker data into `.coverage` and `coverage report` enforces the gate as before. `coverage run -m pytest` (07-testing.md
   section 3) cannot measure xdist workers without `parallel`/`patch = subprocess` in the pinned configuration; pytest-cov
   measures the same source with the same configuration. A one-process run (`PLATFORM_TEST_WORKERS=0`) is identical to
   the previous gate apart from the runner of the coverage.
4. Shared resources between workers: only the work package's 20-port range. Fixtures that serve on
   `WP_PORT_BASE + offset` already fall back to an ephemeral port without the variable (as in CI); the root conftest
   removes `WP_PORT_BASE` in xdist workers. A single process keeps the range. Databases, roles and temporary
   directories already carry random names.
5. `test_b64u_round_trip` had a parameter `os.urandom(33)` whose bytes were its test ID, so every worker collected a
   different test and xdist refused the run. The case keeps its random value and gets the fixed ID `random33`
   (same test, same count). The runner's copy of the test follows through `copy_tool.py` (agent decisions/INF-01.md).
6. Dependencies (separate commit): `pytest-xdist>=3.8`, `pytest-cov>=7.0` in `platform-test`.

## Concurrent gate steps
7. New target `platform-ci-concurrent`: the steps the work-package gates may run beside the others. It names
   `platform-e2e` (Python unit tests and linters of the E2E orchestrator: own coverage file, own ports, no browser) and
   nothing when `PLATFORM_TEST_WORKERS=0` (one test process keeps the port range, so everything runs in order).
   `platform-web` stays in order: with its browser suites beside the parallel tests, a Playwright test with a 30 s
   limit timed out on a fully loaded machine (load 17 on 10 cores). `make platform-ci` itself is unchanged.

## Races that parallel load exposed
8. `test_every_authenticated_get_writes_its_read_row` counted the rows of its own flush, but the read log's background
   writer (1 s delay) wrote part of the about 40 rows first when the requests took longer under load (7 ≠ 40). The test
   now runs on a hub whose writer waits an hour (`read_log_delay`), so its flush still has to find every row.

## Hang diagnosis
9. `tests/platform/pytest.ini` sets `faulthandler_timeout = 300`: a test that runs longer than five minutes dumps the
   tracebacks of every thread into the gate log; nothing is stopped and no limit of a test changes.

## Not done
10. A migrated template database cloned per test (`CREATE DATABASE … TEMPLATE`) would save about 38 ms per database test
   but must re-own every object and re-grant every right to the fresh roles; the gain (under 10 s with workers) does
   not justify the isolation risk. Dropping databases on a background thread brought nothing measurable (the cluster,
   not the waiting test, bounds it). A faster coverage core: `COVERAGE_CORE=sysmon` falls back to the C tracer on
   Python 3.12 with branch coverage ("sys.monitoring can't measure branches in this version").
11. Recommendation outside this package (product code): `agent_hub.stream.bus` could end the listener's wait after a
   notification (`notifies(timeout=POLL_SECONDS, stop_after=1)`), so the empty wake-up notification of `stop()` ends
   the shutdown at once instead of after up to 250 ms. That alone would remove about 215 s of waiting from a
   one-process run.

# INF-01 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## INF-01 – Gate run time: parallel platform tests, Linux part beside them

Decisions of work package INF-01 in agent (agent-core has its own `decisions/INF-01.md`). The gate stays the same: the
same tests and markers, the same steps, 100 % lines and branches of `.coveragerc-platform` combined with the Linux part
(`test_coverage_config.py` unchanged).

### Profile (before)
1. The platform tests (about 8,000) spent about 245 s in one process: setup 52 s, call 78 s, teardown 113 s, spread over
   many directories (guest 33 s, runner/vm 32 s, login and mail doubles 28 s, guest hooks 18 s). Most of it is waiting
   (server stops, socket timeouts, PostgreSQL), not CPU.

### Parallel workers
2. `make platform-test` runs pytest with pytest-xdist (`-n $(PLATFORM_TEST_WORKERS)`, default 4; `0` = one process,
   `auto` = one per core) under the unchanged `coverage run --rcfile=.coveragerc-platform`. Its `patch = subprocess`
   starts coverage in every worker; `coverage combine --keep` merges the workers, the main process and the Linux data
   as before.
3. Shared resources between workers: only the work package's 20-port range (`runner/sync`, `runner/credentials` kit).
   Both fall back to port 0 without `WP_PORT_BASE` (as in CI); the root conftest removes the variable in xdist workers.
4. `test_b64u_round_trip` (a copy of the agent-core reference test) had a parameter `os.urandom(33)` as its test ID, so
   xdist workers collected different tests. agent-core gives the case the fixed ID `random33`; the copy and
   `ORIGIN.json` follow through `copy_tool.py --rev origin/ad5f2026-agent-platform` (decisions/CRY-03.md section 6).
5. Dependency (separate commit): `pytest-xdist>=3.8` in `platform-test`.

### Linux part beside the local steps
6. New target `platform-ci-concurrent` names `platform-ci-linux`: the work-package gates run the Linux part on the
   runner device while the local steps run. They set `PLATFORM_LINUX_DONE` to a file they create when the Linux part
   has ended (green, red or failed); `platform-test` waits for it (at most `LINUX_WAIT_SECONDS`) before
   `coverage combine`. Without the variable (`make platform-ci`, CI) nothing waits.

### Races that parallel load exposed (fixed, with tests)
7. The mail double sent an IMAP/SMTP reply before it wrote the journal record, so a client that had read the reply
   could read the journal before the record existed (`test_magic_link_in_junk` missed `LOGOUT` under load). The double
   now records first and replies afterwards; `test_journal_order.py` proves the order with a slow recorder.
8. `http.client` sends the request head and body separately. The guest control server answers an unknown path (404)
   from the head and closes; under load the body's send then met a closed socket (`BrokenPipeError` in
   `test_stop_through_the_control_route`). The test client of `tests/platform/guest/conftest.py` reads the answer that
   is already there; `test_control_client.py` reproduces the order with a delayed body.
9. The model double appends a journal line to its file after it has answered; `test_stream_tool_use_and_journal`
   read the file right after the second answer and saw one line under load. The test waits (bounded, 5 s) for the
   second line.
10. These are the only changes outside the Makefile, the dependencies, `pytest.ini` and the conftest port rule; they
    add three tests (two in `doubles/mail`, one in `guest`); no test was removed, skipped or re-marked.

### Hang diagnosis
11. `tests/platform/pytest.ini` sets `faulthandler_timeout = 300`: a test that runs longer than five minutes dumps the
    tracebacks of every thread into the gate log (nothing is stopped). One gate run hung once in
    `runner/queries/test_rights.py` with a worker spinning in the test kit's `settle_queries`; it did not recur in
    repeated parallel runs and is left to the owners of that kit.

## INF-01 – Gate run time: parallel platform tests

Decisions of work package INF-01 in agent-core (the agent repository has its own `decisions/INF-01.md`). The gate stays
the same: the same tests and markers, the same steps, 100 % lines and branches of `agent_hub` with the pinned
`[tool.coverage]` configuration (`test_coverage_config.py` unchanged).

### Profile (before)
1. The platform tests (about 9,900) spent about 530 s in one process; less than 80 s of it was CPU time of the test
   process. Per test phase: setup 148 s, call 58 s, teardown 323 s. The teardown is dominated by the hub app's
   lifespan shutdown: the live bus's listener polls `notifies(timeout=0.25)` and the shutdown waits for the end of that
   window (about 235 ms per `hub` fixture, about 920 fixtures). Per database test, creating the roles and the database
   costs about 30 ms, the migrations about 38 ms, dropping about 17 ms.
2. Consequence: the suite waits far more than it computes, so parallel workers shorten it almost linearly until the
   CPU of the machine is the limit.

### Parallel workers
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

### Concurrent gate steps
7. New target `platform-ci-concurrent`: the steps the work-package gates may run beside the others. It names
   `platform-e2e` (Python unit tests and linters of the E2E orchestrator: own coverage file, own ports, no browser) and
   nothing when `PLATFORM_TEST_WORKERS=0` (one test process keeps the port range, so everything runs in order).
   `platform-web` stays in order: with its browser suites beside the parallel tests, a Playwright test with a 30 s
   limit timed out on a fully loaded machine (load 17 on 10 cores). `make platform-ci` itself is unchanged.

### Races that parallel load exposed
8. `test_every_authenticated_get_writes_its_read_row` counted the rows of its own flush, but the read log's background
   writer (1 s delay) wrote part of the about 40 rows first when the requests took longer under load (7 ≠ 40). The test
   now runs on a hub whose writer waits an hour (`read_log_delay`), so its flush still has to find every row.

### Hang diagnosis
9. `tests/platform/pytest.ini` sets `faulthandler_timeout = 300`: a test that runs longer than five minutes dumps the
   tracebacks of every thread into the gate log; nothing is stopped and no limit of a test changes.

### Not done
10. A migrated template database cloned per test (`CREATE DATABASE … TEMPLATE`) would save about 38 ms per database test
   but must re-own every object and re-grant every right to the fresh roles; the gain (under 10 s with workers) does
   not justify the isolation risk. Dropping databases on a background thread brought nothing measurable (the cluster,
   not the waiting test, bounds it). A faster coverage core: `COVERAGE_CORE=sysmon` falls back to the C tracer on
   Python 3.12 with branch coverage ("sys.monitoring can't measure branches in this version").
11. Recommendation outside this package (product code): `agent_hub.stream.bus` could end the listener's wait after a
   notification (`notifies(timeout=POLL_SECONDS, stop_after=1)`), so the empty wake-up notification of `stop()` ends
   the shutdown at once instead of after up to 250 ms. That alone would remove about 215 s of waiting from a
   one-process run.

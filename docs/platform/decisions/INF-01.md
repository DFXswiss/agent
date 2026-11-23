# INF-01 – Gate run time: parallel platform tests, Linux part beside them

Decisions of work package INF-01 in agent (agent-core has its own `decisions/INF-01.md`). The gate stays the same: the
same tests and markers, the same steps, 100 % lines and branches of `.coveragerc-platform` combined with the Linux part
(`test_coverage_config.py` unchanged).

## Profile (before)
1. The platform tests (about 8,000) spent about 245 s in one process: setup 52 s, call 78 s, teardown 113 s, spread over
   many directories (guest 33 s, runner/vm 32 s, login and mail doubles 28 s, guest hooks 18 s). Most of it is waiting
   (server stops, socket timeouts, PostgreSQL), not CPU.

## Parallel workers
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

## Linux part beside the local steps
6. New target `platform-ci-concurrent` names `platform-ci-linux`: the work-package gates run the Linux part on the
   runner device while the local steps run. They set `PLATFORM_LINUX_DONE` to a file they create when the Linux part
   has ended (green, red or failed); `platform-test` waits for it (at most `LINUX_WAIT_SECONDS`) before
   `coverage combine`. Without the variable (`make platform-ci`, CI) nothing waits.

## Races that parallel load exposed (fixed, with tests)
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

## Hang diagnosis
11. `tests/platform/pytest.ini` sets `faulthandler_timeout = 300`: a test that runs longer than five minutes dumps the
    tracebacks of every thread into the gate log (nothing is stopped). One gate run hung once in
    `runner/queries/test_rights.py` with a worker spinning in the test kit's `settle_queries`; it did not recur in
    repeated parallel runs and is left to the owners of that kit.

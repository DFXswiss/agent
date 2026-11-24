# HOT-15 – decisions

Package: urgent fix of tests that turned the merge gate red without a change of the code they check. The common rule
(E-19): no test sees the output of a thread it did not start, and no test depends on the wall clock in a narrow window
or on the load of the test device.

## 1. Capture threads outlived the control process (product fault)

`ControlService.stop()` closed the journal, but nothing stopped the threads that the start-up hooks and session events
had started: the session watchers and cleanup loop of the session manager (`sessions.py`) and the scanner, recorder
and transcript follower of the capture (`control_routes/capture.py`). In a test process these daemon threads went on
polling a closed journal. Every 0.2 s the follower logged `agent-guest-control: transcript capture failed …
JournalError: unavailable` to the stderr of whatever test ran later on the same worker. One victim was
`hub/db/test_cli.py::test_main_prints_one_line_on_stdout`, which asserts an empty stderr. In the guest the same
window existed between `stop()` and the end of the process.

**Decision:** `ControlContext` gets closers (`on_close`, `close`). `sessions.install` and `capture.install` register
the `close` of their part. `ControlService.stop()` runs the closers, latest first, after the servers stopped and
before the pending calls and the journal close. A failing closer is logged and the others still run.
`Capture.close()` stops every capture of the process: the scan thread, recorder and follower each write what they
have read. It runs no final scan, because the session has not ended; a restarted process attaches again through
`attach_running`. The units and tmux sessions keep running, as before.

## 2. Wall clock in a narrow window (`tool/test_waiting.py`)

`test_runtime_of_tasks_and_sessions` took `now` once per timestamp. The runtime of a closed task or an ended session
was therefore 60 s or 40 s plus the duration of the tool calls between two stamps, and the window allowed less than
2 s. **Decision:** every stamp is relative to one moment `base`. Closed runtimes are exact (`== 60`, `== 40`).
Runtimes still running are bounded by the time measured in the test itself (`since(n)` = n + elapsed time since
`base`). The search of `tests/platform/tool/` and `tests/platform/hub/` for the same pattern (`a <= seconds < a +
small` against the wall clock) found no further case. Upper bounds that prove "does not wait" are listed in the report.

## 3. `guest_linux/test_unit_linux.py::test_build_runs_with_a_pty_like_in_the_tmux_window`

The test ran `script -qec "systemd-run --pty …"` with the stdin of the test runner. In the gates that stdin is
`/dev/null` (the run is detached). `script` reads EOF at once and writes it into the pty while `systemd-run --pty` is
still starting. Now and then systemd-run then never ends: the output is only `^@`, and the 120 s timeout follows. The
earlier red runs of KOR-24, KOR-27, KOR-28 and KOR-39 all show exactly this output. Repro in the guest-ci VM: with
stdin `/dev/null`, 1 of 250 runs hung; with an open pipe, 0 of 450. **Decision:** the test gives `script` a pipe that
stays open until the command ended, as the tmux window does. The product is unchanged: in the tmux window the input
never reaches EOF.

## 4. Performance budget of the first screen

Withdrawn from this package by the orchestrator (addendum 2). `web/e2e/performance.spec.ts` and `06-web.md` §12.2
belong to HOT-16. This package changes neither. The measurements are in the report.

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

**Decision:** `ControlContext` gets closers (`on_close`, `close`). `sessions.install`, `capture.install` and
`delivery.install` register the `close` of their part. `ControlService.stop()` runs the closers (order below) after the
servers stopped and before the pending calls and the journal close. A failing closer is logged and the others still run.
`Capture.close()` stops every capture of the process: the scan thread, recorder and follower each write what they
have read. It runs no final scan, because the session has not ended; a restarted process attaches again through
`attach_running`. The units and tmux sessions keep running, as before.

**Decision (after review):** closers run in the order of registration. The session manager is created first and
emits the session events, so it stops (joins its watchers and its cleanup thread) before the capture and the delivery
that handle them. A closed capture attaches nothing any more (late session events, `capture.file_written`, the restart
hook); a closed delivery starts no worker. A part created while or after the context closes is closed at once.
`Delivery.close()` joins its workers. Before, it only set the event and nothing called it.

## 1a. The class check (E-19): `tests/platform/threadcheck.py`

After the teardown of every test, the threads that are new since its setup get 10 s in total to end. If one is still
alive, the teardown of **that** test fails with the name and target of the thread. The 10 s cost nothing when no
thread is left; they only keep a thread that is told to stop but not yet joined from failing on a loaded device. Not
counted:
- the workers of process-wide executors: a `ThreadPoolExecutor` held by a module attribute, such as the signal
  workers of the hub;
- the threads that a fixture wider than a function starts while it sets up (`pytest_fixture_setup`);
- the stand-in listener on port 7700 of the Linux suites, which a test restarts for its session fixture
  (`threadcheck.share`).

The hooks are imported into `tests/platform/conftest.py`, not registered through `pytest_plugins`: the legacy run
loads that conftest late, and pytest refuses `pytest_plugins` there. Limits: work still queued on a process-wide
executor is not seen (HOT-10 drains it in the conftests that use the signals), and the threads of wider fixtures are
not checked at their own teardown.

Leaks the check found and that are fixed:
- the redteam unit test did not stop its spend watch;
- the forwarder of the Linux session and delivery tests on 7700 left request threads behind. `_ForwardServer` now
  closes its open connections and joins them;
- the model double answered a step with `delay_ms` through `time.sleep`, so `stop()` could not end a delayed answer
  in flight. The delay now waits on the `stopping` event that `stop()` sets.

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
stdin `/dev/null`, 1 of 250 runs hung; with an open pipe, 0 of 450. These numbers alone are weak. The cause is shown
by the mechanism: with an open pipe, `script` writes nothing into the pty, so the only path that produces `^@` is
gone. **Decision:** the test gives `script` a pipe that stays open until the command ended, as the tmux window does. The product is unchanged: in the tmux window the input
never reaches EOF.

## 4. Performance budget of the first screen

Withdrawn from this package by the orchestrator (addendum 2). `web/e2e/performance.spec.ts` and `06-web.md` §12.2
belong to HOT-16. This package changes neither. The measurements are in the report.

## 5. `web/e2e/devices/chat-volume.spec.ts` (addendum 3)

**Scrolling up (`usable`, formerly line 112).** The test compared `window.scrollY` before and after a turn of the wheel
upwards (`Expected: < 18009, Received: 24141`). This is not a product fault: the timeline does not follow the end
(only "Jump to the latest item" scrolls). Rows outside the viewport skip their layout (`content-visibility: auto`,
placeholder height 4rem). When the user scrolls up, rows above render for the first time and grow. The browser's
scroll anchoring then raises `scrollY` so that what the user sees stays in place. A run on the test device showed
exactly the values of the red train: from `{top row 94, scrollY 18009, height 18848}` to `{top row 91, scrollY 24141,
height 29306}`, so the view moved up by three rows. Whether rows above were already laid out depends on timing, hence
the flakiness. **Decision:** the test checks the behaviour: after the wheel, an earlier row is at the top of the
viewport. It polls with up to 30 s, which is not a measuring window: it passes as soon as the row is there.

**Busy share of the live burst (`busy < 0.5`).** The share is the long-task time divided by the burst's wall time. The
hub double took the batches as fast as it could, so on an idle device the wall time was short (3.7 s) and the share
60–66 % (red). Under load it was lower (green): the opposite load dependence, which a train with the device to itself
would hit. **Decision:** a live session sends its events over time. The test sends at most one batch per second
(`BATCH_INTERVAL_MS`), so the burst's wall time is set by the test. The bound of HOT-05 (50 %) is unchanged; measured
20–21 % on the idle test device. 12 of 12 runs green (both scenarios, desktop and phone, three repeats).

## 6. `hub/core/test_read_log.py::test_sweeper_writes_expired_windows` (product fault)

Red in gate run 7 under load: 1 stored row instead of 2. `ReadLog.flush` takes the expired windows off the pending
list before its transaction commits. `stop()` cancels the sweeper task, but the flush thread under it keeps running.
The final flush of `stop` then found nothing and returned while the write was still in progress. Had that write failed
after `stop` (pool closed), the repeats would have been lost, although `stop` promises to write every window.
**Decision:** flushes are serialized (`_flushing`). The final flush waits for a write that is still running and writes
what a failed write gave back. New test `test_stop_waits_for_the_write_of_the_sweeper`: it holds the sweeper's write
on an event and is red without the fix (`stop` returned at once).

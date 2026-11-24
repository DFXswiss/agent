# HOT-10 - Two tests that depended on the order of background calls and on a real deadline

Rule: E-19 (fix the class, not only the instance).

## Session stop test (guest)
`test_stop_stops_the_unit_closes_the_window_and_keeps_the_directories` checked
`tmux_runner.subcommands()[-1] == "kill-session"`. The watcher thread of `SessionManager._watch` polls
`has-session` every `tmux_check` (0.05 s in the fixture) until the status file of the pane appears. Between
`stop()` (which calls `kill-session`) and the test writing the status file it may poll once more; the recording tmux
double answers 0 for every call, so the watcher keeps polling. The last call was then `has-session`.

This is intended behaviour, not a product defect: after a stop the watcher must still see the end of the pane (the
status file or a missing tmux session) and then emits `agent_ended`; it does not fail the session because the run is
marked `stopped`. Against a real tmux the session is gone after `kill-session` and the next poll ends the watcher.

Correction: the test joins the watcher thread after the end of the pane before it checks, and checks that after
`kill-session` nothing but the watcher's polling reached tmux. The `time.sleep(0.1)` before the last negative check
is gone (the watcher has ended; `stop` is synchronous).

## Login version check tests (runner doctor)
`test_a_version_that_does_not_come_in_time_warns_and_the_command_is_killed` (deadline 0.3 s) and its sibling
`test_a_descendant_that_left_the_group_does_not_hold_the_check` (0.5 s) started the real deadline of
`login_cli_version` with the process. On a loaded machine the shell had not yet written its pid file when the
deadline killed the process group, so the test failed with `FileNotFoundError` (the sibling would fail in its
`finally`), or passed without having checked that the group is killed.

Correction: the deadline is controlled by the test instead of by process start-up time. A `subprocess.Popen` subclass
(only for the login command) lets `communicate` wait until the command has written its pid file, then the unchanged
production code applies its deadline, kills the group and warns. The pid file is written atomically (`mv`). The
bound for the processes to disappear after `SIGKILL` is 30 s instead of 5 s (a maximum, not a delay). The production
code is unchanged; no defect was found there.

## Verification helpers
The repetition plugin and the load script used for the 300x evidence are not committed (the suite has no
repetition plugin and needs none); the report of the work package names them and the numbers.

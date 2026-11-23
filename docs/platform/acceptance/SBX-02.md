# SBX-02 acceptance (Landlock launcher, unit properties, sandbox self-test, Linux test job)

| Criterion | Evidence |
|---|---|
| Pure unit tests on macOS green | `tests/platform/guest/sandbox/test_{landlock,unit,selftest}_pure.py` (152 tests) in `make platform-ci` |
| Linux tests in `guest-ci` green | `make platform-ci-linux` on the runner device: 25 tests in `tests/platform/guest_linux/` (marker `linux`), Ubuntu 24.04 guest, kernel 6.8, Landlock ABI 4, systemd 255 |
| Combined coverage of the modules 100 % (lines and branches) | `make platform-ci` after `make platform-ci-linux`: `landlock.py`, `unit.py`, `selftest.py`, `probe.py` 100 %, total gate `fail_under = 100` green |
| Landlock refuses exec, writes and connect | `test_ruleset_refuses_exec_write_and_connect`, `test_second_layer_drops_the_interpreter`, `test_kernel_reports_errors_of_the_real_system_calls` |
| Unit properties take effect | `test_unit_properties_take_effect` (identity, environment, no capabilities, seccomp, `/tmp` noexec, own session only, cgroup limits, read-only system, hidden paths, no exec outside `ExecPaths=`, egress refused, no namespaces, own processes only); `test_build_runs_with_a_pty_like_in_the_tmux_window` |
| Self-test green on the correct and red on a weakened configuration | `test_selftest_passes_on_the_correct_configuration`; red: `NoExecPaths=` reset, `InaccessiblePaths=` reset, `TemporaryFileSystem=` reset, `ProtectProc=default`, timeout |
| Independent review | 1 high, 3 medium, 7 low; handled in `docs/platform/decisions/SBX-02.md` nos. 24–30 |
| Known residual risk | `ld.so` can map the Python interpreter inside a session (decision no. 25, pinned by a test) |

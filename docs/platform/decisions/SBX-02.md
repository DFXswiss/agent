# SBX-02 decisions (Landlock launcher, unit properties, sandbox self-test, Linux test job)

Small decisions of the work package; the specification is `05-runner.md` §4.3–§4.6 and `07-testing.md` §1, §3, §7.

## Module layout
1. Besides `landlock.py`, `unit.py` and `selftest.py` the package adds `src/agent_guest/sandbox/probe.py`. The probe
   runs inside the session unit as `python3 -I probe.py …`; `-I` keeps the package off `sys.path`, so the probe must
   be a standalone standard-library script (like the launcher). `selftest.py` is a package module (it imports
   `unit`) and cannot be that script. The probe loads `landlock.py` from its own directory.
2. Function signatures for GST-02 (`unit.build`, `unit.launcher_command`), VM-01/VM-02 (`selftest.run_selftest`,
   `python3 -m agent_guest.sandbox.selftest`) are listed in the module docstrings.

## Launcher (`landlock.py`)
3. The rules of §4.3 are built in (`READ_ONLY_PATHS`, `/tmp`, `DEVICE_PATHS`); the command line only names the
   per-session values (`--session-dir`, `--harness`, `--exec`, `--connect`). `--ro`/`--rw` are additive extras for
   the self-test probe (read access to the probe outside `/opt/agent-guest`) and the Linux tests (coverage data);
   `unit.build` never passes them.
4. Standard system paths that do not exist are skipped (a missing path grants nothing); every path named on the
   command line must exist (exit 126). The session directory must be a resolved direct child of `/srv/sessions`
   (no symlink, no `..`), so a wrong argument can never make `/` writable.
5. The ELF interpreter is read from the `PT_INTERP` header of each `--exec` file and resolved (spike: hard-coded
   per architecture). All rule paths are resolved with `os.path.realpath` (§4.6 no. 2). The program after `--` must
   be one of the `--exec` files.
6. Writable paths get `WRITE_FILE | REMOVE_* | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_SYM | REFER |
   TRUNCATE`, never `EXECUTE`, `MAKE_CHAR`, `MAKE_BLOCK` or `IOCTL_DEV`. Rules on non-directories carry only file
   rights (the kernel refuses directory rights there).
7. Every failure, including usage errors, exits with 126 (the session then fails with `sandbox_failed`).
8. Landlock scoping of ABI 6 (abstract UNIX sockets, signals) is not requested: §4.3 does not ask for it and the
   reference guest kernel (6.8) has ABI 4. A later package may add it behind an ABI check.

## Unit (`unit.py`)
9. The environment is passed with `--setenv=KEY=VALUE` (one D-Bus array entry each, no unquoting by systemd)
   instead of `-p Environment=…`, so a value can never split into further assignments. Values must be printable
   ASCII; model keys must be on the §10.3 allow-list; the unit itself sets the fixed §10.3 values, `HOME`,
   `CLAUDE_CONFIG_DIR`, both tokens, `TERM`, `LANG` and `PATH`.
10. All paths that end up in properties are restricted to `[A-Za-z0-9._+@/-]` without `..`, `.` or `//` segments
    (space-separated systemd lists, no quoting). The session id must be a canonical UUID.
11. `--quiet` is added to `systemd-run` (keeps "Running as unit" out of the recorded terminal). The unit name is
    `agent-session-<session id>`; the self-test uses `agent-selftest-<hex>` and `--pipe --wait`.
12. systemd itself adds `INVOCATION_ID`, `SYSTEMD_EXEC_PID`, `USER`, `LOGNAME`, `SHELL` and, for services with
    memory accounting, `MEMORY_PRESSURE_*` to the unit environment. These carry no secrets and are not inherited
    from the caller; the Linux test asserts that nothing else appears.
13. `exec-paths.json` (written at golden build, VM-01) is a sorted list of unique absolute paths;
    `build.json.exec_paths_sha256` is the SHA-256 hex digest of its canonical JSON (`04-crypto.md` §1, §4.6 no. 4).
    `unit.load_exec_paths` refuses anything else. `ExecPaths=` lists the Claude binary, the interpreter,
    `lib-dynload`, then the sorted exec paths.

## Self-test (`selftest.py`, `probe.py`)
14. The self-test creates its own throw-away session (user `s-<hex>` with the lowest free UID of 20000–29999,
    directories, harness file) and a second "other" session with a secret file, runs the probe in a unit with the
    session properties (kind `subagent`, so the runtime limit is 30 min) and removes both afterwards.
15. Checks beyond the list of §4.5: positive controls (write workspace and `/tmp`, connect to 7700), writes to
    `/opt/claude`, `/var/tmp` and `/dev/shm`, the masked `/etc/agent-guest`, the hidden system bus, systemd private
    socket and transient unit files (no. 24), the process list, UDP to an external address, binding a port,
    executing a copied binary from the workspace and `ld.so` on a workspace copy. Any unexpected exception becomes
    `error:<type>`, which no expectation accepts; a missing line counts as failed.
16. Where the kernel allows it, the accepted outcome names the layer that must refuse: `EACCES` on TCP connect,
    bind and `/dev/shm` writes (Landlock alone), `ENOENT` for the other session (mount namespace; `stat` is not
    subject to Landlock and the other session's files are world-readable, so neither Landlock nor file
    permissions can make the check pass), mode `0` for `/etc/agent-guest` and the hidden sockets
    (`InaccessiblePaths=`), `EPERM` for UDP (egress filter). Writes to read-only system paths accept `EROFS` or
    `EACCES`: the read-only mount answers before Landlock, so these two layers are not separable there; the Linux
    tests prove each layer separately (`test_landlock_linux.py` without the unit, `test_unit_linux.py` without
    Landlock). Listing `/srv/sessions` may end in `EACCES` (Landlock) or show only the own session.
17. Port 7700 must be reachable: callers run the self-test while `agent-guest-api` is up (golden build smoke test,
    pool fill).
18. The probe's second layer is the session's own ruleset (`landlock.plan` of `unit.launcher_args`), not a
    hand-built exec-only layer (review M2): a regression of the production rules shows up in the self-test. Extra
    rights of the outer launcher layer (the probe's interpreter, test weakenings) are therefore masked by design;
    the Linux tests weaken the unit (`NoExecPaths=`, `InaccessiblePaths=`, `TemporaryFileSystem=` reset,
    `ProtectProc=default`) to prove the self-test turns red.

## Linux test job (`scripts/guest-ci.sh`, `tests/platform/guest_linux/`)
19. The VM `${AGENT_RUNNER_VM_PREFIX}guest-ci` is created from `template:ubuntu-24.04` (vz, 4 CPUs, 4 GiB, 20 GiB,
    no mounts, no containerd) and provisioned once (python3-venv, git, nftables, acl, tmux; venv with pytest and
    coverage; marker file with a version). `GUEST_CI_GOLDEN=<instance>` clones a golden image instead (switch for
    the time after VM-01). The VM is stopped after every run and kept; `guest-ci.sh delete` or `cleanup.sh` (VM
    prefix) removes it.
20. The checkout is copied to `/var/lib/guest-ci/agent` in the VM: outside `/home` and `/root` (hidden by
    `ProtectHome=yes`) and matching the coverage path mapping `*/agent/src/`. Coverage data is written as
    `.coverage.linux.*` (`COVERAGE_FILE=.coverage.linux`, like the GitHub job) and copied back.
21. The fixtures stand in for the golden image: a copy of `/usr/bin/echo` at `/opt/agent-test/claude`, exec paths
    from `ldd` (the golden-build computation), `/srv/sessions` as a `noexec,nosuid,nodev` bind mount, a listener on
    127.0.0.1:7700, and the session part of the nftables table under the name `agent_sessions_test` (the production
    file belongs to SBX-01). Units run copies of `landlock.py`/`probe.py` from `/opt/agent-test/sandbox`, so a
    checkout below `/home` works too.
22. The GitHub workflow `platform-guest-linux.yml` (FND-00) is unchanged; the tests need `nft`, `git` and
    `systemd-run` and fail (never skip) without them. GitHub Actions have not run on the integration branch so
    far; `make platform-ci-linux` on the runner device is the binding gate (`07-testing.md` §8).

## Known residual risk (documented, tested)
23. Landlock checks `execve`, not `mmap(PROT_EXEC)`: outside the unit `ld.so <binary>` still runs
    (`test_ld_so_is_not_stopped_by_landlock_alone`); inside the unit `NoExecPaths=/` + `ExecPaths=` and the
    `noexec` mounts stop it for every file not in `ExecPaths=` (`test_unit_properties_take_effect`, self-test
    `ld_so_*`). The interpreter itself is in `ExecPaths=`, see no. 25.

## Review follow-ups (independent review, 1 high, 3 medium, 7 low)
24. (H1) The unit additionally gets `InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private
    -/run/systemd/transient`: over the system bus and systemd's private socket any local user can read every
    unit's `Environment` (another session's `PLATFORM_SESSION_TOKEN`), and the transient unit files contain the
    same lines. Landlock (ABI 4–6) cannot refuse a UNIX-socket connect by path. Hiding all of `/run/systemd` is not
    possible (systemd's inaccessible nodes live there; the unit fails with exit 226). Proposed addendum to the
    table of `05-runner.md` §4.3; the self-test and the Linux tests check it.
25. (M1, residual risk, spec level) `ExecPaths=` must contain the Python interpreter for the launcher and Landlock
    must grant `EXECUTE` on the ELF loader, and Landlock does not check `mmap(PROT_EXEC)`. So
    `ld.so /usr/bin/python3.12 …` starts an interpreter inside a session
    (`test_residual_risk_the_loader_can_start_python_inside_the_unit` pins this). It runs under the same
    Landlock, network, seccomp and unit restrictions as Claude Code itself (no exec, no writes outside the session,
    connect only to 7700), and reaching it needs code execution inside the session, which layer 1 denies. Remedy
    for a later package (SBX-01/ATS-01): a static launcher binary (Spike B report §8), after which neither the
    interpreter nor `lib-dynload` needs to be in `ExecPaths=`.
26. (M3) See no. 16; the other session is world-readable in the self-test.
27. (L4) A timed-out self-test returns a failed result (`unit_exit: -1`) and always stops its unit
    (`systemctl stop`) before the throw-away user and directories are removed.
28. (L5) `PT_INTERP` must be absolute; all integer arguments of the variadic `syscall()`/`prctl()` are passed as
    C `long`.
29. (L1) The Linux tests now also check the harness file is readable under its rule and that the hidden paths
    are hidden. Kept as is: `tcp_external` in the unit-only test accepts the nftables drop (the egress rules run
    before the cgroup filter); properties without an observable effect in a short probe (ProtectClock,
    KeyringMode, RuntimeMaxSec, …) are asserted on the argument list only.
30. Not changed: (L2) ABI 6/7 add no filesystem rights, so `handled_fs` is complete up to ABI 7; scoping see no. 8.
    (L3) DNS to the local resolver is stopped only by the egress rules (Landlock has no UDP control), a spec-level
    property of §4.3. (L6) `probe.py` see no. 1. (L7) `GUEST_CI_PYTEST_ARGS` is operator input on the runner device;
    the session token on the `systemd-run` command line is visible only outside the sandboxes (sessions have
    `ProtectProc=invisible`), as the specification passes it as environment.

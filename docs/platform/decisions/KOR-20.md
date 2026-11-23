# KOR-20 – Claude Code on the runner host, doctor check `login_cli`

Finding of the rollout: the subscription login runs `LOGIN_ARGV` (default `claude setup-token`) on the host
(05 §11.2), but neither the runbook nor `service install` nor `doctor` named Claude Code as a host prerequisite. A
device without it fails every login with `login_cli_failed`.

1. **One lookup.** `doctor` resolves `LOGIN_ARGV[0]` with `login.relay.resolve`, as the login does, and with the
   daemon's search path: the `PATH` of the installed LaunchDaemon plist (`service.read_plist`), which is the
   environment of the daemon and so of every login; without a plist (before `service install`, development) the
   caller's `PATH`. A doctor run from a shell with a wider `PATH` would otherwise report ok for a program the daemon
   does not find (review finding). `resolve` now also refuses a path that is no regular file (a directory has the execute bit); the
   login failed for it before as well, only later at spawn, with the same code `login_cli_failed`.
2. **One pin.** The expected version is `golden.load_source(GOLDEN_TEMPLATE).claude_version`, the function and file
   (`images/versions.json`) of `image build`. No second place holds the number. The check compares with the pin, not
   with the `ready` golden in the store: the pin is what the next golden and the upgrade gate use, and the doctor
   check of the golden itself is `golden`.
3. **Version query like the login.** `claude --version` runs in a new temporary directory (system temporary
   directory, 0700, removed afterwards; doctor leaves nothing in the runner home) with `relay.login_env` as the
   complete environment, stdin closed, its own process group, time limit 10 s (`DoctorOptions.login_timeout`); on
   timeout the whole group is killed. The version is the first plain `X.Y.Z` of the output.
4. **Results.** ok `version` (value: the version; `X.Y.Z` with an optional pre-release part) · warn
   `version_mismatch` (value: the host's version) · fail `login_cli_missing` (not found, not an executable file, exec
   refused, or `--version` exits with a failure – the login would fail the same way) · ok `executable` for a `LOGIN_ARGV`
   different from the default (test double, not started). The package named three outcomes; two more warnings
   cover the cases it left open, both `warn` because the login may still work: `version_unknown` (time limit or no
   version in the output; after killing the group a descendant that left it is waited for once more as long) and `pin_missing` (the checkout's `versions.json` is unreadable; value: the
   host's version). "Different" means not equal to `["claude","setup-token"]`; an absolute path to a real `claude`
   counts as different and is only checked for being executable.
5. **Order.** `login_cli` follows `landlock` (host and image prerequisites before the hub checks); the order and
   format of every other check is unchanged.
6. **Rollout notes of the runner (same change).** `runner-installation.md`: the service starts at once – install
   after enrollment or stop and disable it right after a preparatory install; the install runs from an administrator
   account with `sudo env -i … --user <runner account>` (the runner account has no sudo; `env -i` keeps the
   administrator's environment out of the plist, the given `PATH` becomes the daemon's); the virtual environment
   needs pip for the platform profile. `CUTOVER.md` §3.2 and S9 follow.
7. **Legacy-untouched test after the merge into `develop`** (both repositories). The test compares with the merge
   base of `origin/develop`. Before the merge every platform file was new there (`A`); since `develop` holds the
   platform, a change of a platform file shows as `M` and was refused. Platform paths are now accepted with any
   status; files outside the platform areas keep the old rules (new test with a platform file on the base).
8. **Review** (independent, fresh context): taken over – daemon `PATH` from the plist (#1), a failure exit of
   `--version` fails (#4), the as-built register row, the runbook check command with `PATH`, the real output format,
   `node` beside `claude`, `AGENT_RUNNER_HOME` equal to the enrollment home, a bounded second wait after the kill, a
   pre-release part in the version. The commit subject follows the workspace rule "subject = pull-request title".

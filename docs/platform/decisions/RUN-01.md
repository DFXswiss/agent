# RUN-01 decisions (runner core)

Small decisions of the runner-core package (05-runner.md §1.1–§1.4, 02-data-model.md §4/§6, ADR-022, ADR-027).

## Modules
1. Besides the owned modules (`__main__`, `config`, `cli`, `identity`, `service`, `supervisor`, `registry`, `store/`)
   the package adds four small root modules in its own area: `errors.py` (`RunnerError(code, name)`: a closed code
   plus an optional item name, never a value), `encoding.py` (strict b64u), `host.py` (all injectable host
   dependencies: environment, platform, streams, `launchctl`/`limactl`/`tmux`/`ssh` as argv callables, cluster
   helpers, discovery root, LaunchDaemons directory, clock, sleep, signal installation, user, daemon argv) and
   `lifecycle.py` (`run_daemon`, `uninstall_runner`, `DaemonContext`, `UninstallContext`). `agent_runner.crypto`
   (CRY-03) may add its own codec; `encoding.py` stays the dependency-free one for config and identity.
2. `store/cluster.py` imports `agent_cli.pg` read-only (no side effects on import, so no copy was needed) and keeps
   the runner data in database `agent_runner` of the cluster under `$AGENT_RUNNER_HOME/pg`. Legacy `PgError` texts
   (which may carry server output) become `store_unavailable`.

## Configuration (05 §1.3)
3. Every `AGENT_RUNNER_*` variable that is not one of the 24 names (the 23 of §1.3 plus `VM_PREFIX`) is refused with
   `config_invalid <NAME>`, so a typo never falls back to a default silently.
4. `HUB_URL` and `PLATFORM_ROOT_KEY` are required for `run` and `service install`; `service uninstall` and
   `uninstall` load without them (they never talk to the hub). Enrollment (RUN-03) chooses its own requirement set
   through `load_config(require=...)`.
5. Limits (inclusive): `VM_CPUS` 1–256, `VM_MEMORY_MIB` 1024–4194304, `VM_DISK_GIB` 5–65536, `POOL_SIZE` 0–64,
   `MEMORY_RESERVE_MIB` 0–4194304, idle seconds 60–604800; decimal digits only.
6. URLs: `https://` with a host, no user info, query, fragment, white space or port 0; trailing `/` removed;
   `http://` only to a loopback host (`localhost`, 127/8, `::1`) and only with `TEST_MODE=1`. The rule applies to
   the hub and to every upstream/double URL.
7. `HOME`, when it exists, must be a real directory with no group/other bits, and a home that cannot be inspected is
   refused (fail closed); it is created 0700 by the commands that write into it. All patterns are matched against the
   whole value (`fullmatch`), so a trailing newline is invalid.
8. `MODEL_CATALOG` and `GOLDEN_TEMPLATE`: `None` means "packaged default"; the consuming packages (RUN-12/GST-04,
   VM-01) resolve the packaged file. An explicit value must be an existing absolute file.
9. `OPENROUTER_KEY_FILE`: absolute, regular file (not a symlink), no group/other bits; refused when the identity in
   the configured home is an org device. Before enrollment the kind is unknown, so the file is accepted then.
   `LOGIN_AUTOMATION` defaults to `on` for platform devices and `off` otherwise (also before enrollment); the
   service plist carries it only when it was set explicitly, so an install before enrollment does not pin `off`.
10. `VM_PREFIX`: empty or `[a-z][a-z0-9-]{0,31}` (a valid start of a Lima instance name).
11. `unsupported_os` is checked first in `load_config`, so every configuration-loading command refuses non-darwin
    hosts; `--version` and `--help` do not load configuration.

## Identity (05 §1.4)
12. Reading refuses a symbolic-link `keys/` directory as well (`O_NOFOLLOW` covers only the last component).
    Files are written to a temporary `O_EXCL|O_NOFOLLOW` 0600 file in the same directory, synced, then hard-linked
    to the final name: `link(2)` fails for any existing entry (a dangling symlink included), so the write is atomic
    and never overwrites. Reads use `O_NOFOLLOW` and refuse non-regular files and group/other bits.
13. Both files are canonical JSON (sorted keys, no white space). `device.json` is validated on write and read:
    canonical UUID `device_id`, `kind` in `org|platform`, non-empty `hub_url`, `hub_key` 32 bytes b64u, `enrolled_at`
    ISO-8601 UTC with milliseconds (03-api.md §1). One file without the other is `identity_incomplete`.
14. The storage key derivation is left to CRY-03; RUN-01 fills no encrypted column.

## Store (02 §4/§6)
15. Column types: IDs `uuid` except `vm.id` (`text`, CHECK `^vm-[0-9a-f]{8}$`), `golden.version` and keys of
    `sync_state`. CHECK constraints exactly where §4 names a closed list (plus `scope` in `org|wg|cred` from the
    glossary and non-negative counters); `vm.state`, `session.state`, `operation.state` stay unconstrained text so
    later packages need no migration to add local states. `log_head` is its own table.
16. Migration files must be numbered 0001… without gaps; a database that knows a version the code does not is
    refused (`migration_unknown`). The advisory lock waits at most 60 s (`migration_locked`); an unreachable store and
    every other database error are `store_unavailable` (no server text). Each file runs in its own transaction;
    failure reports only `failed <step>`. Unexpected exceptions in the CLI print `runner.error code=internal
    name=<ExceptionType>` only.
17. `pyproject.toml` gains `agent_runner = ["store/migrations/*.sql"]` in `[tool.setuptools.package-data]` (separate
    commit) so a non-editable install ships the migrations.

## Registration points (ADR-027 §4)
18. The ADR-027 runner points are implemented as listed. Two further points are needed by this package's own scope
    and follow the same pattern: `<area>/shutdown.py:HOOKS` (sequence of `ShutdownHook`, awaited on SIGTERM before
    the tasks are cancelled; RUN-02 registers the outbox flush there) and `<area>/uninstall.py:CLEANERS` (sequence of
    `Cleaner`, called by `agent-runner uninstall` while the store still exists; VM-01 registers the VM deletion).
    Names are `[a-z][a-z0-9._-]{0,63}`; a name registered twice, a missing attribute or a wrong type stops the start.
19. CLI handlers are set with `set_defaults(handler=fn)`, `fn(args, CommandContext) -> int`; `CommandContext` gives
    `host`, `out` (facts) and `config()`.

## Supervisor (05 §1.1)
20. 05 §1.1 asks for both a 1 s → 30 s restart backoff and "more than 10 restarts in 60 s → exit 1". Measured on the
    wall clock the two cannot meet: the backoff alone spaces restarts so that at most six fit into 60 s. The window is
    therefore measured on the task's own clock, which advances only while the task runs and stands still during its
    backoff wait: the 11th restart within 60 s of run time ends the daemon with exit 1 (a task failing instantly is
    caught after eleven failures, about three minutes of wall time; one failing every 6.5 s of work never is). The
    backoff doubles per uninterrupted failure series and resets after a run of at least 60 s.
21. A task that returns normally is finished and not restarted. Dynamic tasks (`guestlink` per VM) use
    `Supervisor.spawn` / `cancel`.
22. SIGTERM/SIGINT: set `context.stopping`, give the shutdown hooks 80 % of the supervisor's stop budget
    (sequentially, each with the remaining hook budget), cancel the tasks with the rest, return 0. The daemon gives
    the supervisor 8 s, closing the pool at most 1 s, the fast cluster stop the rest of the 10 s. Only exception type
    names are logged.

## CLI and service (05 §1.2, §1.1)
23. Output facts are `runner.<fact> key=value …`; values with white space, quotes or backslashes, and empty values,
    are JSON-quoted. `--json` is accepted anywhere before `--` and prints one document `{"ok", "facts", "error"?}`.
    Errors print `runner.error code=<code> [name=<NAME>]` on stderr. `agent-runner` without a command prints help
    and exits 0 (unchanged from the FND-00 placeholder and its test); usage errors exit 2.
24. `run` facts: `runner.migrate result="<line>"` and `runner.run tasks=<n> enrolled=yes|no`. `run` wipes `tmp/`,
    creates `log/`, starts the cluster and stops it again when the daemon ends.
25. The plist `EnvironmentVariables` carry `AGENT_RUNNER_HOME`, `PATH` (current `PATH` or a default with the
    Homebrew directories) and every other runner setting that differs from its default, because the daemon needs
    `HUB_URL` and `PLATFORM_ROOT_KEY` to start and launchd provides no other configuration source. None of these is
    a secret (URLs, a public key, paths, numbers, switches). The plist is written 0644 atomically; `UserName` is
    `--user`, else `SUDO_USER`, else the current user; `root` and unknown accounts are refused. Without an explicit
    `AGENT_RUNNER_HOME` the home is `<account home>/.local/share/agent-runner` (not the home of the root caller).
    As root, missing directories down to `log/` are created 0700 and handed to the runner account (`chown`), so the
    daemon can write its home. launchctl: `bootout system/<label>` (an already unloaded service is fine) then
    `bootstrap system <plist>`. Under pytest the plist goes to the runner home and launchctl is not called.
26. `uninstall` needs root for the LaunchDaemon but the runner account for the store and the VMs (PostgreSQL refuses
    root; Lima state belongs to the account). As root it reads `UserName` and `EnvironmentVariables` from the
    installed plist, unloads and removes the service and runs `sudo -u <account> -H -- /usr/bin/env -i <plist env>
    <python> -m agent_runner uninstall [--keep-identity]`; the account step's output is relayed to stderr as
    `runner.uninstall_step …`, a failure is `uninstall_failed runner_account`; without a valid plist it refuses
    (`uninstall_as_root`). As the runner account it refuses while the system plist exists (`service_installed`),
    otherwise runs the cleaners with the store running, then deletes the store and the runner data (`pg`, `golden`,
    `log`, `tmp`, `vm`, the test plist); symbolic links are removed, never followed. Without `--keep-identity` the
    identity is deleted and the home removed when it is empty (foreign files stay). A failing cleaner aborts with
    `uninstall_failed <cleaner>` before anything is deleted. A broken identity does not block `uninstall` or
    `service uninstall`. `run` refuses root (`run_as_root`).

## Review follow-ups
27. Accepted as is: the extra root modules and `test_registry.py` (item 1) and the package-data key (item 17) lie
    outside the package's ownership list but inside its area; no other package owns them. `initdb --auth=trust` on
    loopback is prescribed by 05 §1.4 and inherited from `agent_cli.pg`; any local account can connect to the
    cluster, which matters on shared hosts only and is left to `doctor` (RUN-06) to report. A daemon whose tasks
    have all finished keeps running idle (item 21): tasks are loops by contract, and launchd would restart an
    exiting daemon into the same state.

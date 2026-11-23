# PRB-01 decisions (early measurements: TUI start under Landlock, clone path end to end, LIVE-03)

Small decisions of the PRB-01 package (09-milestones.md WP-M0-14; 05-runner.md §3.2, §6.1, §10.2, §10.6;
07-testing.md §2.4, §6; ADR-009, ADR-010, ADR-011, ADR-027 §8). Results: `../measurements/M0.md`, ADR-029.

## Code layout and gates
1. The probes are scripts (`scripts/probes/probe_*.py`, run by hand on the runner device, never part of
   `make platform-ci`). Their pure evaluation (distributions, step breakdown, budgets, go/no-go, CSV/Markdown, LIVE-03
   snapshot comparison, token extraction without refresh, secret scan) is the package `scripts/probes/probelib/`,
   unit-tested in `tests/platform/probes/`.
2. "Unit tests of the evaluation 100 % in `make platform-ci`" needs `probelib` inside the gates. Four central
   configuration lines were therefore extended (the only way to reach the requirement): `.coveragerc-platform`
   `source` gains the directory `scripts/probes/probelib` (and its pinned copy in `tests/platform/test_coverage_config.py`),
   `Makefile` `PY_PATHS` gains `scripts/probes` (ruff), `[tool.mypy]` gains `scripts/probes` in `files` and
   `mypy_path` (strict types for every probe script, so `probelib` resolves as top-level package). This deviates from
   the coverage table of 07-testing.md §3 by one source entry (to be taken into the specification text by ACC-M0); the probe drivers themselves stay outside coverage
   (they need real VMs, the keychain and the network).
3. Shared host code of the VM probes is `scripts/probes/probehost.py` (runner `VmManager` over real `limactl`, own
   PostgreSQL cluster as in VM-02's `real_lima.py`). Timing uses thin subclasses of `Lima` and `GuestClient` that only
   record timestamps; every call is the runner's own code.

## Measurement method
4. Percentiles are nearest rank (`probelib.stats.percentile`): with 10 runs the p95 is the maximum – the conservative
   reading for the 9 s rule.
5. Clock: host monotonic time on the runner device. Journal events (`agent_starting`, first hook, `prompt_accepted`,
   gateway `usage`) are marked when the guest journal's long poll delivers them to the host; the guest's own `at`/`ts`
   values are kept as a cross-check (`guest_tui_start_s`, `guest_prompt_accept_s` columns).
6. The model double runs **inside** each VM on loopback, started as the Lima default user. The credential contract
   allows plain HTTP only to loopback (`session-credential.request.json`), and the shared golden is not a TEST_MODE
   golden. Starting it is test setup and is excluded from the end-to-end time (`assigned` .. `session_begin`); on the
   pool path it is started before `begin` (pool fill time).
7. End-to-end time = `ensure_vm` called → `prompt_accepted` at the host, without the excluded setup. Not included: the
   hub (no hub in M0) and step 3 (credential choice, 0.05 s budget). The 2 s budget of step 6 ("from step 5") is
   applied to `tui_start` (guest `agent_starting` → first hook); `tui_up` (PUT credential → first hook) is reported
   without budget. A run that failed makes the verdict No-Go (`probelib.report.verdict(failed=...)`); M0.md is tied to
   its CSVs by `tests/platform/probes/test_m0_document.py`.
8. The TUI probe runs as root inside a dedicated golden clone through a one-time Lima system provisioning step (the
   pattern of VM-02's pool self-test), because the Lima default user of a hardened clone has no root. It uses the
   self-test's throw-away session (own user and directories) with the real driver command line and unit properties;
   the token is unknown to the guest API, so hooks are refused after the measured marks. The "without Landlock"
   variant is the same `systemd-run` and unit properties without the launcher. Marks: first painted screen and the
   transcript file (the hook arrival of a throw-away session is not observable; the hook-based start time comes from
   the clone-path probe's real sessions).
9. The pool path fills one pool VM (with self-test) per run before `begin`, then `ensure_vm` takes it; refill and pool
   fill are not timed (not hot path).

## LIVE-03
10. Profile: before the run the operator checked by hand with `ps` (environment `CLAUDE_CONFIG_DIR` of all processes)
    that no running process used the chosen profile, so that the before/after digests of all profile files are
    meaningful; after the review the workspace driver performs that check itself. Excluded profiles are refused by
    every subcommand of `probe_live03.py` (`--exclude`, `AGENT_LIVE_EXCLUDED_PROFILES`, compared after resolving the
    path) and by the driver. `run` exits 2 when the turn did not pass, 1 on a scan finding.
11. The keychain entry is found with the rule of `agent_cli.usage.keychain_service_name` (`Claude Code-credentials-`
    + first 8 hex of SHA-256 of the NFC profile path). `snapshot` uses `security find-generic-password` **without**
    `-w` (attributes only); the access token is read with `-w`, parsed in memory, and only `accessToken`, `expiresAt`
    and `subscriptionType` are used. A token with less than 30 min left is refused instead of refreshed.
12. Transfer: `probe_live03.py token` writes the token only to a pipe (refused on a terminal); the runner-device side
    reads it from standard input. The workspace driver uses the ssh options of `tools/wp` with the pipe as standard
    input (`remote.sh` itself forwards no standard input to the command). The token never appears in argv, the
    environment or a file on either device; errors are redacted.
13. The mini turn is a real Claude Code session in the sandbox with model `claude-haiku-5-5` (Haiku class) and the
    subscription access kind; the gateway's allow-list is the subscription list of `model-catalog.example.json`.
14. The usage endpoint answer is reduced to status and key names (`probelib.live03.usage_shape`): its values describe
    the account. The repository holds only the summary; the full result and the snapshots stay in the workspace.
15. `scan` reuses no stored token: it reads the access token again (expiry ignored, never refreshed); the snapshots
    before/after prove that the stored login did not change in between, so it is the token that was sent. The
    keychain snapshot covers attributes, not the access list: the read ran without any access prompt.
16. Setup tokens could not be tested (creating one needs a login; ADR-011 forbids live login flows). Recorded in
    ADR-029; quota polling stays feature-detected.

## Documentation
17. The ADR addendum is a new ADR (029) rather than edits of ADR-009/010 (ADR-027 §8: architecture-relevant results
    as the next free number); the index `docs/platform/adr/README.md` gets its line.
18. No acceptance file per milestone is written here: ACC-M0 takes the values from `M0.md`; the package evidence is
    `docs/platform/acceptance/PRB-01.md`.

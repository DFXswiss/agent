# GST-02 decisions (guest sessions: user, directories, harness, tmux, systemd-run, control)

Small decisions of the session package (05-runner.md §4.1, §4.3, §4.4, §5.1, §6.1 steps 4–5, §6.3, §7, §8, §10.3;
03-api.md §7.1; ADR-008, ADR-021, ADR-025, ADR-027 §4).

## Modules
1. `sessions.py` is the library of the control process (`SessionManager`: create, start, watch, stop, cleanup);
   `control_routes/sessions.py` only maps HTTP to it. `driver.py` (agent-specific command line), `harness.py`
   (rendering, file) and `tmux.py` (root tmux server, pane launcher) are pure or thin, injectable wrappers.
   Everything is standard library only.
2. The manager of a control process is created on first use (`sessions.manager(context)`) with the golden image's
   paths and attached to the `ControlContext` (`sessions.install`); `ControlContext`/`state.py` (GST-01) are not
   changed. Tests install a manager with a fake `System` (users, chown, `systemctl`) and a recording tmux runner;
   the Linux suite installs one with `LinuxSystem` and real tmux/systemd.

## Creation (`POST /v1/sessions`)
3. User `s-<first 12 hex>` (05 §4.3), UID = lowest ID of 20000–29999 that is neither a user nor a group ID; the
   group is created first with the same ID (`groupadd --gid`, `useradd --gid … --no-create-home --shell
   /usr/sbin/nologin`). An existing user of that name is never reused (`sandbox_failed`). Note: for UUIDv7 session
   IDs the first 12 hex digits are the millisecond timestamp, so two sessions created in the same millisecond in one
   VM collide; the second one fails with `sandbox_failed` (spec-level property, reported to the hub as a failed
   session; a spec addendum could use other digits).
4. Directories are created by root, `.claude.json` is written before the tree is handed to the user, so no write
   ever goes into a directory the session already controls. `work`, `home`, `home/.claude` belong to the session
   user (0700, 05 §4.3); `/srv/sessions/<id>` itself stays root's (0711), so a session cannot swap `work`/`home` for
   links that later root code (transcript reader, scanner, file import/export) would follow. Root code that reads
   inside `work`/`home` must still open with `O_NOFOLLOW`/`openat` (GST-05/GST-06). The seeded file lives at
   `home/.claude/.claude.json` (the file Claude Code reads with `CLAUDE_CONFIG_DIR`): onboarding done, theme set,
   `work` trusted. Verified with Claude Code 2.1.330: the TUI starts without any dialog.
5. Token: 32 random bytes b64u (43 characters). The session row and cache keep `sha256_hex(token)` (UTF-8 of the
   b64u text, as `apisock.token_sha256`); the token itself stays in memory until the unit gets it as
   `PLATFORM_SESSION_TOKEN`/`ANTHROPIC_AUTH_TOKEN`. The metadata file keeps no token. It never appears on a command
   line (`/proc/<pid>/cmdline` is world-readable): the two `--setenv=NAME=<token>` arguments of `sandbox/unit.py` are
   rewritten to `--setenv=NAME`, and `systemd-run` takes the value from its own environment, which the pane launcher
   sets from the root-only launch file (no. 13). Remaining exposure outside the sandboxes: systemd's transient unit
   file below `/run/systemd/transient` (hidden from sessions by SBX-02, readable by other local users); the unit of
   `agent-guest-api` should hide it too (`InaccessiblePaths=-/run/systemd/transient`, VM-01/SBX-01).
6. Request checks beyond the schema: `depth == 0` exactly when `parent_session_id` is null; `model.env` only for
   access `platform` and only keys of 05 §10.3; no NUL and no lone surrogate in `context_markdown` or the prompt;
   a blank context is invalid; a model ID or prompt the driver refuses. All `400 request_invalid`.
7. Idempotency (03 §1): the SHA-256 of the canonical request is kept in a root-only metadata file
   `/var/lib/agent-guest/sessions/<id>.json` (hidden from sessions). The same request for an active session answers
   `{"state":"sandbox_ready"}` again without side effects; a different one is `409 id_conflict`; a `stopped` or
   `failed` session is `409 state_conflict`. The response schema has no `replayed` field, so a replay is not marked.
   The identity of a session (`task_id`, `parent_session_id`, `depth`) is kept too; a resume with another identity
   is `id_conflict`.
8. A sandbox that cannot be created (user, directories, harness, incomplete image: rules file, interpreter, exec
   paths) is rolled back (only what this request created; a leftover directory is never deleted), recorded as
   session `failed` with reason `sandbox_failed` (journal) and answered `500 sandbox_failed`.
9. The resource limits of the request (`memory_max`, `cpu_quota`, `tasks_max`, `runtime_max_sec`) are appended to the
   unit properties of `sandbox/unit.py` (systemd: the last assignment wins), together with `TimeoutStopSec=10s`
   (05 §6.3: SIGTERM, 10 s, SIGKILL). Main/subagent kind = `depth == 0`.

## Harness
10. `harness.render` = `RULES.md` + `# Context of this session` with this fixed structure:
    1. the runner's `harness.context_markdown` verbatim (the confidential part only the runner knows: organization,
       workgroup, task with names and IDs, owner display name, rights, subagent limits). RUN-04 builds it with
       `harness.context_markdown(org=(id, name), workgroup=(id, name, is_default), task=(id, title), owner, rights,
       limits=(depth, parallel, total))`, which yields one bullet line each;
    2. `## Session`: session ID, task ID, parent session and depth, model, resource limits (known to the guest);
    3. `## Platform tools`: every tool of 03 §8.1 with one example call (the examples validate against
       `contracts/mcp/tools/*.input.json`, placeholders aside);
    4. `## Restarted session` (only when `resume_claude_session_id` is given or a suspended session is resumed):
       read the history with `get_task_messages`.
11. `harness_version` = SHA-256 hex of the bytes of `RULES.md`, journaled with `sandbox_ready` and
    `agent_starting`. The file is written atomically (temporary file + rename), root 0444, directory 0711.

## Start
12. The agent starts when the runner has provisioned the model credential: `PUT /v1/sessions/{id}/credential`
    (GST-04, `control_routes/credential.py`) MUST call `sessions.credential_provisioned(context, session_id)` after
    storing the credential. Reason: 05 §6.1 orders step 4 (`POST /v1/sessions` + `PUT …/credential`) before step 5
    (prompt hash, tmux, unit with the initial prompt); a TUI started before the credential exists sends its first
    model request into a gateway without credential, which ends the session with `model_access_unavailable`.
    A repeated call (credential refresh) starts nothing.
13. The pane command travels through a root-only launch file (`/run/agent-guest/launch/<id>.json`, 0600): tmux
    passes its command line through client/server messages of a few KiB, while the session command carries the
    initial prompt (up to 100 KiB). The pane runs `python3 -I …/agent_guest/tmux.py exec <file>`, which deletes the
    file, runs `systemd-run --pty …` on the pane terminal and writes the exit status to `<id>.status`.
14. The tmux client gets a fixed environment (`PATH`, `LANG`); every `new-session` runs inside its own transient
    scope (`systemd-run --scope`, unit `agent-tmux-<id>-<random>`): the first one keeps the tmux server for its
    whole life, so the server and the `systemd-run` clients survive a restart of `agent-guest-control` (whose unit
    would otherwise kill them with its control group). The name is unique per call because a resumed session opens
    `s-<id>` again while the first scope may still hold the server.
15. The initial prompt follows `--` on the command line, so a prompt beginning with `-` is never parsed as an option
    of Claude Code (deviation from the literal argv of 05 §4.1, which has no `--`; verified with Claude Code
    2.1.330). A prompt over 100 KiB (UTF-8) is not put on the command line; its hash is registered all the same and
    the text is kept in memory for GST-06's bracketed paste (`sessions.deferred_prompt`).
16. The initial prompt is registered at the start through GST-03's `prompts.PromptRegistry.register(session_id,
    prompt_id, text)` (same hash, same rules as `POST /v1/sessions/{id}/prompts`; defect 7 holds: registration only in
    the control process through the control API), before `session.state agent_starting` is journaled. A prompt the
    registry refuses (its `prompt_id` was accepted before, or names another text) is not put on the command line.
17. Failure reasons of the start: the pane command's exit status 126 (the Landlock launcher) → `sandbox_failed`;
    any other end within `START_WINDOW_SECONDS` (10 s) of the launch or a failing `tmux new-session` →
    `agent_start_failed`; a later end → `agent_exited`; an end during stop/suspend is expected. A watcher thread per
    started session reads the status file every 0.25 s and checks `tmux has-session` every 2 s (a pane closed
    without status counts as an end). Each run has its own status file and the watcher acts only while its run is
    the current one, so the watcher of a suspended run never touches the resumed run (it then emits no
    `agent_ended` either). A failed session's unit is stopped as well (the unit may outlive a killed pane). `oom`/`runtime_exceeded` are not told apart here (the unit is collected);
    left to the runner's metrics/GST-06.
18. Session states written here: `sandbox_ready`, `agent_starting`, `stopping`, `stopped`, `suspended` (via
    `stop(…, final="suspended")`), `failed`. `prompt_delivering`/`working`/`idle` belong to GST-05/GST-06.

## Stop, resume, retention
19. `stop`: journal `stopping`, `systemctl stop agent-session-<id>`, `tmux kill-session`, `systemctl stop` once
    more (a unit that `systemd-run` registered between the first stop and the closed window), journal `stopped`. A stop of
    an ended session answers `{}` (idempotent); a stop before the start just ends the session. The OS calls run
    outside the manager lock.
20. Resume = a new `POST /v1/sessions` for a `suspended` session (GST-06): same user, UID and directories, a new
    token and harness file (with the restart notice), nothing is written into the session's own directories.
21. Directories, harness file and user of `stopped`/`failed` sessions are removed 7 days after their end by a cleanup
    thread of the manager (hourly, without the manager lock: ended sessions never change); the end time is kept in
    the metadata file, which is deleted only after `userdel` succeeded (else the next run retries). Session rows stay
    in the journal database (no deletion interface in GST-01). The thread starts with the manager, i.e. at the first
    session request after a start of the control process.
22. Not persisted across a restart of the control process: the pending start (token, prompt) of a session in
    `sandbox_ready` and the watcher threads. The runner's retry of the same `POST /v1/sessions` prepares the start
    again (new token, the prompt from the request); recovery of running sessions after a control restart is left to
    GST-06/VM-02 (the tmux server keeps running, see no. 14). Superseded by KOR-02 (decisions 8 and 11).

## Extension points (ADR-027 §4, discovered from `control_routes/*` at call time; additions to the table)
23. `CONTROL_ACTIONS = {action: handler(context, session_id)}`: the control actions other than `stop` (GST-06:
    `interrupt`, `suspend` in `control_routes/control.py`, which also needs `ROUTES = []` for the registry). An action
    nobody registered is `400 request_invalid` (the package text's placeholder until GST-06); registering `stop` or an
    action twice is a `RegistryError`.
24. `SESSION_EVENTS = {event: callback(context, session_id)}` with events `sandbox_ready`, `agent_starting` (tmux
    session exists) and `agent_ended` (the pane command ended): the attachment points of GST-05 (recorder,
    transcript, scanner). Callback failures are logged and never change the session.

## Tests and tooling
25. `scripts/guest-ci.sh` (SBX-02) now also copies `contracts/` and `src/agent_doubles/` into the VM (one line): the
    Linux test drives the real control socket (schemas) and runs the model double next to Claude Code.
26. The Linux suite's fake agent is a generated static ELF (aarch64/x86_64) that blocks in `ppoll` until a signal;
    stock binaries reject Claude's options. The real-agent test downloads the pinned Claude Code 2.1.330 native
    package (SHA-256 checked, cached in the VM) and reaches the model double as `http://127.0.0.1:7700/model` through a
    small forwarder that stands in for the GST-04 gateway; it takes over port 7700 from the conftest's listener and
    restores a listener afterwards.
27. Observed on systemd 255: besides the allow-listed variables the unit environment contains systemd's own
    `USER`, `LOGNAME`, `INVOCATION_ID`, `SYSTEMD_EXEC_PID`, `MEMORY_PRESSURE_WATCH`, `MEMORY_PRESSURE_WRITE` (nothing
    of the control process). Harmless; `unit.py` (SBX) could drop the last two with `MemoryPressureWatch=off`.
28. Observed: Landlock refuses even listing `/srv/sessions` and `/run/agent-guest/harness` inside a session (no
    `READ_DIR` on the parents); the mount namespace alone would show only the own entry. Both are asserted.

## Review (independent subagent): 1 high, 10 medium, 8 low
29. Fixed: H1 scope name reused on resume (no. 14, Linux resume test); M2 watcher of an earlier run (no. 17); M3
    replay after a control restart (no. 22); M4 root-owned base directory (no. 4); M5 token on the `systemd-run`
    command line (no. 5, the Linux test scans every `/proc/*/cmdline`); M6 names in the harness context are collapsed
    to one line (`harness.inline`); M11 the Linux test uses limits other than the defaults; L12 unit stopped on
    failure; L13 second `systemctl stop`, `systemctl`/`userdel` time-outs; L14 cleanup without lock and with retry;
    L15 resume identity and `claude_session_id`; L16 lone surrogates and driver refusals are `request_invalid`, an
    incomplete image records `failed`; L18 more visibility probes (other harness file, launch file, tmux socket) and
    environment values; L19 acceptance wording.
30. M7 resolved after GST-03 was merged: the second hash implementation is gone, the start uses GST-03's registry
    (no. 16). Kept, with reasons: M8 the
    obligation of GST-04's credential route (no. 12) is recorded here and in the report for the orchestrator. M9
    `scripts/guest-ci.sh` (no. 25: one additive line the acceptance criteria need). M10 user name collisions are a
    property of the spec (no. 3; spec addendum proposed in the report). L17 `SESSION_EVENTS`/`CONTROL_ACTIONS` follow
    GST-01's `API_OPS` precedent (documented here and in the module docstring until ADR-027 §4 is amended).

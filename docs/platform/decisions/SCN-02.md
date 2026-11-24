# SCN-02 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## SCN-02 – decisions (agent side)

1. **TEST_MODE goldens keep the Lima default user's sudo** (ADR-036): `agent-guest-harden.service` removes
   `/etc/sudoers.d/91-agent-guest-test` on every boot and writes it again only when `/var/lib/agent-golden/install.json`
   says `"test_mode": true` (rule checked with `visudo -c`). Production goldens are unchanged. The E2E suite of
   agent-core needs it for its test hooks (typing into a pane, reading the recorder file, reads as a session user) and
   the shared golden caches are now built as TEST_MODE goldens (`--test-model-port 9`).
2. **Review**: the grant of the previous boot is removed before any exit path of the hardening script; the TEST_MODE
   decision is a named check (`TEST_MODE_CHECK`) that a unit test runs against install records (true, false, string,
   missing key, invalid JSON, missing file). Merge order: this side first, then the agent-core scenarios (the shared
   cache name does not encode TEST_MODE).
3. **No background egress of the base image** (found by the E2E egress check of SCN-02, T1 iteration 2): the Ubuntu
   base image ships `fwupd` with `fwupd-refresh.timer`, which ran after a clone's boot and fetched firmware metadata
   over HTTPS. The slim step disables and masks the timer and its service (the VM has no firmware to update);
   the other network timers of the base image were already disabled.

## SCN-02 – decisions (M1 scenarios B: E2E-10/11/12/13/14/23/34/35, plaintext scan)

1. **Root in the run's VMs and the hook fault switch**: E2E runs use TEST_MODE goldens; in such a golden the Lima
   default user keeps sudo for the test hooks (ADR-036). `tools/wp/golden-cache.sh` and `orchestrator/golden.py` build
   the shared cache with `--test-model-port 9` (closed port; the doubles are on the guest loopback, ADR-034).
2. **Test hooks** (`orchestrator/guestvm.py`, `routes/sessions_vm.py`): a session's VM is the run VM whose root tmux
   server has `s-<id>`; typing into the pane is `tmux send-keys -l` plus `Enter` (never a product path, ADR-016);
   reads "as the session user" run a small Python as root that enters the unit's mount namespace with `os.setns` (no
   exec inside the sandbox's `noexec` mounts) and then drops to the session user's UID/GID.
3. **Hook fault switch** (`routes/hooks.py`): a systemd drop-in for `agent-guest-api` on every running run VM, then a
   restart. The API process keeps model credentials in memory only, so E2E-34 switches *before* its task starts (the
   pool VMs) and switches back afterwards; with UserPromptSubmit answering 500 the first prompt is never accepted by
   the platform (session stays in its start states) while Claude Code, failing open, runs the turn – exactly the
   situation the managed settings and the OS layer must hold.
4. **Idle interval** (`routes/idle.py`): `AGENT_RUNNER_SESSION_IDLE_SECONDS` per runner, the runner restarted when the
   value changes; 60 s is the runner's minimum, so E2E-13 waits about a minute for `suspended` and restores the
   default afterwards.
5. **Outbox evidence** (`routes/runner_store.py`): read-only queries on the runner's own store (`runner.outbox`,
   `runner.sync_state`, `runner.vm`) with the agent checkout's psycopg. Gapless = `count(*) = max(origin_seq)` (the
   outbox is never pruned); no `origin_seq_conflict` = no `sync_fatal` row and the runner still running.
6. **Plaintext scan** (`checks/plaintext_scan.py`, `routes/plaintext.py`): sources are a fresh `pg_dump`, the hub and
   PostgreSQL logs and a tap of every byte the hub sent on the browser link (`relay.py` `tap`, one file per
   connection, HTTP-decoded: chunked, gzip/deflate). Canaries are searched raw, as hex and as base64/base64url at all
   three alignments. Every spec of the group calls it at its end with the canaries of its prompts, answers, file
   contents, titles (derived from the prompt), terminal bytes and the display/workgroup names; it also asserts that the
   SSE stream was captured (`sse_frames > 0`). SCN-09 extends the sources for E2E-09.
7. **Live terminal latency** (E2E-12): per chunk applied in the open view, `browser time − event ts` (the event header
   time is the recorder's journal time in the VM); asserted p95 < 1 s over at least 5 live chunks, as PERF-04 defines the
   criterion. Measured p95 0.5 s (T1). The model double holds its answer 12 s, so the TUI's waiting animation gives a
   steady stream of live frames.
8. **"No input path"** (E2E-12): while typing into the terminal view no non-GET request leaves the page, the screen's
   character count does not change and the text never reaches the model or the recorder file.
9. **Prompt origin** (E2E-35): the "duplicate registered prompt" is the already accepted first prompt typed again into
   the pane (its registration is used); both refusals are `session.prompt_refused` with `reason: unregistered`.
10. **Execution ban** (E2E-23): Bash/Agent/Task are not offered to the model (asserted on every journaled request) and
    fail as unknown tools; `hook.denied` comes from the file tools aimed outside the workspace (Glob rule 3, Read rule
    6). The harness is asserted on the first model request: rules, IDs of org/workgroup/task/session, platform tools.
11. **Isolation** (E2E-10): B's answer is held 30 s by the model double so both sessions work at the same time; A's
    prompt carries B's workspace path (captured by the scenario). Tool events of refused calls come from the
    transcript and successful writes from PostToolUse, so their order is compared as a set.
12. **One VM per workgroup** (E2E-11): the second workgroup is created with the web test helper; the helper now passes
    the verified platform state to `buildBatch` (an org with a platform device needs it, `log_rights` otherwise). The
    VM ↔ Lima instance mapping comes from the runner's store (the hub's `session.vm_id` is still empty, SCN-01 risk).
13. **Gap filling** (E2E-14): both links are cut after the first output reached the hub; the runner link is restored
    first, then the browser link, so the browser must fetch events it never saw live.
14. **Live latency on one clock** (review): the VM clock offset against the hub device (whose clock the browser
    shares) is measured per run (`GET /sessions/{id}/clock`, best of five round trips, error ≤ RTT/2) and subtracted;
    only chunks the hub received after the view opened count (no history pages).
15. **Duplicate registered prompt** (review): besides the used prompt typed again into the pane, E2E-35 sends the
    identical signed prompt event to the hub again; the hub answers `replayed` and nothing is delivered twice.
16. **`--resume`** (review): E2E-13 reads the command line of the resumed unit's main process (test hook) and asserts
    `--resume <claude session id>`.
17. **Merge order** (review): the shared cache name hashes the input files only, so a TEST_MODE cache exists only for
    trees that contain the agent change of ADR-036; the agent side is merged first and the cache is built from the
    merged agent tree before the acceptance runs.
18. **Accepted residual points** (review, low): the OS check of E2E-10 uses the session's identity and mount namespace
    without its Landlock domain (the attack suite covers Landlock); the scenarios carry the adversary steps of
    `agent_doubles.model` (ATK-01/03/04/13–18/24) as their own scenario files, because a spec scenario is one ordered
    script per test (ADR-027 §4); a VM stopped while the hooks are forced to 500 would keep the drop-in (the runs never
    stop a VM in between; E2E-34 switches back at once); ADR-036 is kept identical in both repositories; the web test
    helper fix is exercised by E2E-11 (helpers have no unit tests of their own).

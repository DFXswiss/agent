# KOR-02 decisions (guest restarts, lossless terminal recording)

Corrections of AUD-01 A-03, A-04, A-05, A-26, B-15, B-16 and B-17 (05-runner.md sections 4, 6, 7, 9; 03-api.md
section 11; 04-crypto.md section 6.4 E8; 08-monitoring.md section 3).

## A-03 Terminal chunks within E8
1. **The guest bounds the encoded body.** A `session.terminal` record keeps the contract limits (at most 16 KiB of
   UTF-8 data and 4096 events, every event at most 16384 characters) and is in addition cut so that its body JSON
   (sorted keys, no whitespace, non-ASCII kept: exactly what the runner encrypts) is at most 64 KiB minus the 16-byte
   AEAD tag. One event's data is at most 16 KiB as a JSON string, so 16 KiB of control characters (six bytes each
   after escaping) become several events and records instead of one 96 KiB record. Flushing is unchanged (100 ms or
   16 KiB of data); one flush may journal several records.
2. **The runner splits instead of dropping.** A well-formed terminal record whose sealed size would exceed E8 (a guest
   from before this correction) becomes several `session.terminal` events of consecutive frames
   (`sync.seal.split_terminal`; each event's `offset_ms_start` is its first frame's time, data cut at character
   boundaries to at most 16384 characters, at most 4096 frames per event). This is contract-conformant: every event
   satisfies `body_session_terminal` and E8, and the web client replays the events in order. Only a record that
   satisfies `body_session_terminal` apart from its size is split (at most 4096 frames, codes `o`/`m`, 16 KiB of data
   in all, `cols`/`rows` 1-1000): the guest is untrusted, and splitting anything else would let it push more terminal
   data per record than the contract allows. Such a record is still refused by the seal checks and skipped with the
   fact line `runner.journal record_skipped`. This replaces the example "a terminal chunk over 64 KiB" of
   decisions/RUN-04.md item 17 for well-formed chunks.

## A-04 Lost gateway credential
3. **`credential_missing` provisions again at once.** The guest gateway keeps a session's credential only in the
   memory of `agent-guest-api` (GST-04 decisions 3/5), so a restart of that unit loses it; the next model request
   answers `503` and journals `guest.error credential_missing` with the session ID. The runner's journal handler now
   drops the session's provisioning stamp and calls `model.gateways.credential_missing`, which runs
   `reprovision` in a task of its own (one per session at a time): never inside the journal page's transaction,
   because the failure path writes the session row that the page may hold locked. `reprovision` takes the session's
   provisioning lock (shared with renewal and withdrawal), builds the credential the session should have now and
   sends `PUT .../credential`; the guest's control process hands it to the API process on the next request, so the
   next model request succeeds without user action (Claude Code retries `503`).
4. **Deleted versus not usable now.** When the owner's record is gone, the session is withdrawn like a deleted
   credential (`model_access_unavailable` with its `session.status` event, guest session stopped; fact line cause
   `credential_missing`), as the package requires for a session without a valid credential. A credential that cannot
   be used right now (platform key file unreadable, record not opening on this device yet, hub fetch failure:
   `KEEP`) does not end the session (05 section 2.6 withdraws deleted credentials only): the stamp stays dropped and
   the next renewal (`refresh_all`, every reconcile run) provisions it; meanwhile the gateway answers `503` and Claude
   Code retries (fact line `reprovision_deferred`).
5. **A guest that does not take the credential now** (unreachable, refusal) leaves the stamp dropped, so the next
   renewal provisions it. Sessions whose start or stop runs (they provision or end themselves; checked under the
   session's lock), withdrawn sessions, ended sessions and records of sessions not on this VM are left alone.
6. **Stale records are ignored for `COOLDOWN_S` (10 s).** The API process takes a new credential with the session's
   next request, so the records of the requests it refused before (a burst, Claude Code retries `503`) are stale. The
   guest is untrusted; without the cooldown it could force a decryption and a `PUT` per record, and every provisioning
   restarts the guest's observed platform cost at the hub's `spent_usd` (GST-04 item 6), which may lag behind the real
   spend. A real second restart within 10 s is provisioned by the first record after the cooldown. The provisioning
   tasks are not tracked by the daemon's shutdown: an interrupted one only loses in-memory state that a daemon restart
   loses anyway; the next renewal repeats a withdrawal whose record is still gone.

## A-05 Restart of `agent-guest-control`
7. **Start-up hooks.** New guest extension point `control_routes/<name>.py:STARTUP` (`hook(context)`), discovered by
   `control.py` in sorted module order (like `SESSION_EVENTS`, no central list; the registry module is unchanged) and
   run once at the end of `ControlService.start`, after both sockets serve. A failing hook is logged; a non-callable
   `STARTUP` is `registry_invalid`. This adds a row to the guest extension points of ADR-027 section 4 (DOC-01).
8. **Agent watchers again** (`control_routes/sessions.py:STARTUP` -> `SessionManager.reattach`). The start now records
   its run in the root-only meta file (`run: {status_file, started_at, deferred_prompt_id}`; never prompt text or
   token). A restarted process watches every session in `agent_starting`/`prompt_delivering`/`working`/`idle` again
   from that status file, with the elapsed time counted from `started_at` (so `agent_start_failed` versus
   `agent_exited` is decided as before); without a usable record the end is seen when the tmux session is gone.
   An interrupted stop (`stopping`) is finished (the repeated `stopping` record is no transition for the runner); an
   interrupted suspend leaves no state behind and ends as `agent_exited`. A `sandbox_ready` session whose pending start
   (token, model, prompt: memory only) was lost fails with `agent_start_failed`: it could never start, and the runner
   does not repeat `POST /v1/sessions` (`sessions/start.py`), so the replay path of GST-02 decision 22 only helps a
   request that arrives before the hook ran. This supersedes that decision's expectation of a runner retry (DOC-01).
   The hooks run synchronously at the end of `ControlService.start` (a stop they finish may take up to two
   `systemctl stop` timeouts); `ControlService(..., startup=())` runs none (tests on machines without tmux).
9. **Capture and TUI-ready watcher again.** `control_routes/capture.py:STARTUP` attaches recorder, transcript follower
   and scanner of running sessions at once (`Capture.attach_running`, so the gap of GST-05 decision 2 is closed
   without waiting for an event); the recorder continues its cast file with the marker `recorder restarted`.
   `control_routes/prompts.py:STARTUP` starts the TUI-ready watcher of every `agent_starting` session.
10. **Prompt queue.** Follow-up prompts queued for delivery are not rebuilt: the runner sends a prompt that was handed
    over and has no result again after 150 s (RUN-05 decision 9), safe through the registry's `prompt_id` keying. The
    deferred initial prompt (too long for the command line) is different: it travels on the runner's `start` channel,
    which the runner never sends again while the session is in its start chain, so it is kept in a root-only file
    `<meta dir>/<id>.prompt` (0600) from the start until it is handed out (or the session is stopped or cleaned up)
    and restored by `reattach`; the TUI-ready watcher then delivers it as before. The VM holds the plaintext of its
    workgroup anyway (00-overview.md section 3 B4; the transcript holds the same text).
11. **Runner deadline for `agent_starting`.** None existed. New reconcile step 56 `agent_start_timeout`
    (`agent_runner/startguard/reconcile.py`, a new area so no file of another package changes): a session that stays
    `agent_starting` for `AGENT_START_TIMEOUT_S` (120 s, counted in memory from the first reconcile run that saw it;
    a daemon restart starts the count again) fails `agent_start_failed` - decided again under the row lock, so a
    session the journal moved on meanwhile is left alone - and its guest session is stopped in the background.
    Sessions whose start or stop task runs are left to it. 05-runner.md section 6.1 names no such deadline (DOC-01).
    The new area is added to the area list of `tests/platform/test_skeleton.py`, as every new runner area was.

## B-16 OOM
12. **`oom` from the unit's OOM kills.** Session units run with `--collect`, so after the end their cgroup and
    properties are gone. When the agent ends unexpectedly (not a launcher failure, status 126), the guest asks
    `LinuxSystem.oom_killed` (outside the manager's lock): `oom_kill` of the unit's `memory.events` while the cgroup
    still exists, else the result `oom-kill` that systemd itself logged for the unit since the run's start
    (`journalctl _PID=1 UNIT=<unit> --since`; a session can write to the journal but not as PID 1). A kill by the OOM
    killer stops the whole unit (systemd's default `OOMPolicy=stop`), so a kill anywhere in the unit ends the agent and
    is reported as `oom`. An agent that ended while the control process was down is classified with the time until
    the restarted watcher saw it.

## B-17 IO accounting
13. **`IOAccounting=yes` is a unit property** (`sandbox/unit.py`, after the resource limits), so `io.stat` exists from
    the session's first byte (08-monitoring.md section 3). The metrics fallback of RUN-15 (`systemctl set-property
    --runtime ... IOAccounting=yes`) stays for units started before this change. The property list of the unit tests
    (`EXPECTED_PROPERTIES`) and the Linux session test (`IOAccounting`, `io.stat`) pin it. Beyond the table of
    05-runner.md section 4.3 (DOC-01).

## A-26 Usage of a response that is never read
14. **The relay is an iterator object, not a generator.** `gateway.Relay` streams the upstream response and ends the
    request exactly once (`Gateway._finish`: close the connection, charge the observed cost, journal `usage`): after
    the last chunk, on a read error or on `close()` (in the iterating thread), or - when the body is dropped without
    being read to its end - through `weakref.finalize`, which hands the finish to a thread of its own (the garbage
    collector may run it in a thread that holds a lock the finish needs). A generator's `finally` never runs when the
    generator was never started (client gone before the status line, handler timeout after the upstream headers),
    which lost the record; this supersedes the accepted gap of GST-04 decision 27. Both cases drop the response the
    same way; the test covers the client that disappears before the status line (`usage` with `response_bytes` 0 and
    the upstream's status). `routing.py` is unchanged.

## B-15 `pending_expired`
15. **Expiry instead of a contract change.** `Journal.prune_pending` (control process: at start and every
    `PRUNE_SECONDS`) removes pending MCP calls older than `PENDING_SECONDS` (1 h): an unanswered one with
    `guest.error pending_expired` of its session in the same transaction, an answered one silently. The contract
    already lists the code; neither the contract nor 02-data-model.md section 5 names an age, and every MCP call
    waits at most 30 s (03-api.md section 12) and nothing reads a stored late answer again, so the age only bounds the
    table; one hour is well beyond the runner's answer window (`operations/pipeline.py`, 900 s). A later answer of the
    runner is refused `not_found`; the runner counts a refusal as delivered and still pings the session with
    `operation.finished`, so nothing is lost. The record carries only the code (the contract allows no `pending_id`),
    also for sessions that already ended. The age is measured on the wall clock (a forward jump of more than an hour
    expires early; an MCP wait in progress then ends `not_found`). Supersedes GST-07 decision 11 ("no expiry job") and
    completes GST-01 decision 18; DOC-01 should name the age in 02-data-model.md section 5.

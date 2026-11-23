# GST-05 decisions (transcript, terminal recorder, workspace scanner)

Small decisions of the capture package (05-runner.md sections 4.6 nos. 5 and 6, 5.5, 6.4, 7; 03-api.md section 7.2;
02-data-model.md section 5; ADR-016, ADR-025, ADR-027 section 4, ADR-029).

## Attachment
1. **Lifecycle through `SESSION_EVENTS`, no change to `sessions.py`.** `control_routes/capture.py` registers callbacks
   for the three events GST-02 offers: `sandbox_ready` starts the workspace scanner, `agent_starting` starts recorder
   and transcript follower of this run, `agent_ended` ends both with a last read and makes a last scan. No missing hook
   had to be added. The capture reuses the tmux client (socket and runner) of the session manager for `pipe-pane`,
   because the tmux client of GST-02 has no `pipe-pane` call.
2. **Restart of `agent-guest-control`.** The control process has no startup extension point; a restart loses the
   capture threads (and GST-02's watcher threads). Every capture event and the `capture.file_written` op therefore
   attach the capture of running sessions that have none and whose tmux session still exists
   (`Capture.attach_running`). Until then, pane output of a session that triggers no event is not recorded (open
   item for GST-01/RUN-04: a start-up hook of `control.py`, or a call of an existing route at control start).
   A capture also ends by itself when its session leaves the capturable states (`sandbox_ready`, `agent_starting`,
   `prompt_delivering`, `working`, `idle`, `resuming`) without `agent_ended` (stopped before the start, or attached
   again after a restart): the scan thread notices it within one pass. State that must survive is on disk: the cast file (recorder offset), the scanner index
   `<session base>/capture-index.json` (root 0600) and the journal (records the follower already wrote). The session
   whose event is being handled is never attached again by that event. A start-up hook in `control.py` (GST-01) would
   make the re-attachment immediate; it is noted as a follow-up for the owner of `control.py`.

## Transcript (05 section 6.4)
3. **One `session.output` per API message.** Claude Code 2.1.330 writes one transcript line per content block, all
   with the same `message.id`. The text blocks are joined by a blank line and written when an entry of another message
   or type arrives, or after `QUIET_SECONDS` (1 s) without new lines. A block arriving after its message was written
   becomes a further record with `message_id` `<id>-<n>` (the contract pattern allows it; message IDs are made safe by
   replacing other characters with `_`). `thinking` blocks and side chains are not shown.
4. **`turn`** counts prompt entries (`user` with text that are neither `isMeta` nor tool results); before the first
   prompt a message belongs to turn 1.
5. **Tool calls.** `input` is the JSON text of the tool input, `result_summary` the result text (text blocks joined
   by a line break, other blocks as `[type]`), both cut at 64 KiB of UTF-8 (which also satisfies the contract's 64 Ki
   characters); `input_summary` is the first `file_path`/`path`/`pattern`/`glob`/`type` argument, else the compact
   input, at most 256 characters. `duration_ms` is the difference of the two entries' timestamps (0 if unknown or
   negative). A `tool_use` without result when the session ends is written with `is_error: true` and the result
   `interrupted`, so the chat never shows a call that is still "running" forever.
6. **No duplicates with the PostToolUse hook.** GST-03 journals `session.tool` for successful `Write`/`Edit` calls
   from the hook. The follower holds these pairs up to `HOOK_GRACE_SECONDS` (5 s) and writes them only if no record
   with that `tool_use_id` is in the journal by then (the hook may have failed: fail-open, 05 section 5.5). Failed
   `Write`/`Edit` calls (no PostToolUse) are written at once.
7. **File selection, rotation and bounds.** An entry whose `uuid` was already read is skipped, so re-reading a file
   (replaced, truncated, history copied into a new file) repeats no record and no turn. A poll reads at most 8 MiB;
   text of one message beyond 64 KiB is not kept in memory. The recorder reads at most 256 KiB per drain, so flushes
   and the stop check keep going under constant output.
   The follower reads the newest `<uuid>.jsonl` of the project directory (on a
   resume the file of the known Claude session ID first) and switches to a newer file after reading the old one to
   its end (`/clear`, a resume under a new ID). A file whose inode changed or that became shorter is read again from
   its start; an unfinished last line waits for its end; a line longer than 32 MiB is dropped. Records whose
   `message_id`/`tool_use_id` the journal already holds are never written again.
8. **`prompt_delivering` and the Claude session ID (ADR-029).** Claude Code 2.1.330 sends no `SessionStart` hook.
   "TUI up" (05 section 6.1 step 6) is therefore the first pane output seen by the recorder or the transcript file,
   whichever comes first; it moves a session that is still `agent_starting` to `prompt_delivering` (checked inside
   the journal transaction against the session row, so it never overtakes a `prompt_accepted` → `working` of the
   prompt check). The Claude session ID (the name of the followed transcript file, which is what `--resume` needs; never a
   value from inside the session-written lines) is stored in the session row
   (for `--resume`) and journaled with that `prompt_delivering`, or – when the session is already further – as a
   repeated `session.state` of its current state (`prompt_delivering`, `working`, `idle` or `resuming`) carrying the
   ID. Such a repetition is no transition; RUN-04 must treat an unchanged state with a new `claude_session_id` as an
   ID update only.

## Recorder (05 section 7, ADR-016)
9. **FIFO and pipe.** `tmux pipe-pane -O -t =s-<id>: 'exec cat >> <fifo>'`, the FIFO in the root-only directory
   `/run/agent-guest/capture/` (0700; FIFO 0600). The recorder opens it read-write and non-blocking, so it never sees
   end-of-file while `cat` is replaced and needs no reopen. Output written before `pipe-pane` takes effect (the first
   few milliseconds after `new-session`, before Claude Code paints) is not recorded.
10. **Offsets.** `t_ms` counts from the first recording of the session; a restarted recorder (control restart or
    resume) continues at the last offset of the cast file and writes the marker `[t, "m", "recorder restarted"]`;
    the time the session was suspended or the recorder was down is not part of the timeline. Offsets never decrease.
11. **Records.** At most 16 KiB of UTF-8 data and 4096 events per `session.terminal`; a longer read is split at
    character boundaries. Flushed 100 ms after the first buffered event or at once at 16 KiB. On the session's end an
    incomplete trailing character is written as U+FFFD.
12. **History copy.** `<session base>/terminal.cast` (root 0600, removed with the session directory after the 7-day
    retention): asciicast v2 header (`width` 160, `height` 48, `timestamp`) and exactly the journaled events with the
    time in seconds (`t_ms / 1000`), written after each journal flush. E2E-12 compares the replay with this file.

## Scanner (05 sections 5.5, 6.4)
13. **Safe access as root.** All reads go through directory descriptors opened with `O_NOFOLLOW` from the
    root-owned session base; only regular files owned by the session UID are read (non-blocking open, so a FIFO
    cannot stall the scanner). Symlinks, special files, `.git` segments and paths the `rel_path` contract refuses
    are skipped. The same helpers guard the transcript follower (a planted link to another session's transcript or to
    a root file is never read).
14. **Baseline.** Files present when the scanner first starts (imported by the runner before the agent started) are
    not reported. The index file keeps the known state across control restarts.
15. **Ops.** New file `write`, changed `edit`, vanished `delete` (`sha256`/`size` null). The PostToolUse addon
    `hook_addons/file_changes.py` reports a written file at once through the api.sock op `capture.file_written` with
    the tool's op (`Write` → `write`, `Edit` → `edit`); the scanner then has nothing new to report. GST-03's file hints
    are checked on every scan pass.
16. **Files above 10 MiB** are neither hashed nor reported (05 section 6.4 hashes only files of at most 10 MiB, and
    the contract requires `sha256` for `write`/`edit`); this also applies to the PostToolUse path, so a session cannot
    make root read a huge or sparse file. Names that are not UTF-8 are skipped (they cannot be journaled). The known
    state of the scanner changes only together with the journal records: a failed journal write restores it and the
    next pass reports the changes again.
17. **Patches.** Unified diff (`a/<path>`, `b/<path>`; `/dev/null` for a new file; git's "No newline at end of
    file" line) against the last known content, only for UTF-8 text of at most 256 KiB per file (16 MiB of known
    contents per session in memory) and only if the patch has at most 64 KiB. After a control restart the previous
    content is unknown and the patch is left out. Walk limits: 100 000 entries, depth 64; deletions are only reported
    after a complete walk.

## Session user names (open point of ACC-M0)
18. **Collision fixed.** The user name was `s-` plus the first 12 hex digits of the UUIDv7 session ID, which are its
    millisecond: two sessions of one VM started in the same millisecond collided (`useradd` failed → `sandbox_failed`).
    The name is now `s-` plus all 32 hex digits without digit 13 (UUID version) and digit 17 (variant bits), 32
    characters (the `useradd` limit), unique for distinct UUIDv7 IDs that differ in more than the two variant bits.
    No other package had fixed it on the integration branch. The attack suite's copy of the rule
    (`agent_doubles/atk/driver.py`) and its account-line patterns (`atk/evidence.py`) follow; a test pins both to the
    guest's function. Existing VMs keep their users (names are only computed for new sessions; a resumed session is
    looked up by the same function, so a VM with sessions created by the old rule must not be resumed after an
    upgrade – pool VMs are recreated from the golden image anyway).

## Follow-ups for other packages
19. RUN-04: a `session.state` with an unchanged state and a new `claude_session_id` is an ID update, not a
    transition (decision 8). JSON escaping of control characters can make a `session.terminal` body of 16 KiB data
    several times larger; the event encoder must stay within the 64 KiB terminal event limit (split if needed).
20. GST-02: `tmux.Tmux` has no public `pipe-pane`; the capture uses the runner of the session manager's client. A
    public `pipe_pane(name, command)` would remove that coupling. The 7-day cleanup looks users up by the current
    name rule, so users created by the old rule on an upgraded VM are not removed (pool VMs come from the golden image,
    so this only affects long-lived VMs across a guest upgrade).
21. The workspace specification `05-runner.md` section 4.3 still names the old user rule; the repository copy is
    updated.

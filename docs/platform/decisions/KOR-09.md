# KOR-09 decisions (prompt after an interrupt, interrupted suspend, child sessions after a store wipe)

Corrections of AUD-02 NA-01, NA-03, NA-06, NA-11 and NB-14 and the rest of AUD-01 A-04 (05-runner.md sections 6, 7,
10 and 11; 03-api.md sections 8 and 11).

## NA-01 Prompt after an interrupt
1. **Cause, observed against Claude Code 2.1.330.** Claude Code puts an interrupted prompt back into its input box
   (when the turn had no answer yet), and it does the same with a prompt the UserPromptSubmit hook blocked. One
   `C-e C-u` removes one visual row of the input, so the three rounds before the old delivery left a longer text
   (a plain paste of up to about 800 characters and two line breaks stays uncollapsed, six rows in the experiment)
   partly in the box. The interrupt also marks the session `idle` as soon as `Escape` is sent, and the empty input box
   is visible during a turn, so the next delivery can start before the TUI has put the text back. The next prompt was
   then submitted mixed with the old text; the hook refused it (`unregistered`, the hash did not match) and the TUI
   put the mixed text back; the old pane check looked for the first 30 characters of the prompt with white space
   collapsed, and a line wrap inside a word hid them, so the delivery took the text as submitted and refused the
   prompt ten seconds later (`delivery_failed`, exactly `ACCEPT_SECONDS` after the refusal in the LVT-01 run).
2. **The input is emptied by reading it.** Before anything is written the delivery captures the pane and clears
   (`C-e C-u` three times per round) until the input box reads empty, at most `CLEAR_ROUNDS` (20) rounds 100 ms apart.
   Text that two rounds of clearing do not change is no input (a hint the TUI may show in an empty box) and is left
   to the check of item 3. An input whose text keeps changing without becoming empty, or no input box, refuses the
   prompt before anything is written, so the runner's retry (RUN-05) applies. A prompt that is refused in the end is
   emptied from the input as well (unless the hook accepted it meanwhile).
3. **The input must show the prompt alone before `Enter`.** After typing or pasting, the box must hold one paste
   placeholder alone (only for a pasted prompt; any `[Pasted text…]` label without a second one, as the old check
   already relied on the `[Pasted text` prefix) or the text itself, compared without white space (wrapping); nothing may stand before it and the box may not hold more characters than the text (the pane may
   render fewer, never more). Otherwise the attempt starts again with emptying.
4. **A hook refusal during the delivery is not final.** A `prompt_refused unregistered` of the session journaled
   after the attempt's `Enter`, while the prompt's own registration is unused, means the submitted text was not the
   prompt: the delivery empties the input and starts again, at most `RETRIES` (3) more attempts, then refuses
   (`delivery_failed`); a prompt the hook accepted in the meantime is not typed again. Only the delivery writes into
   the pane, so such a refusal belongs to its own submission. Each refused submission is still a real
   `prompt_refused unregistered` and becomes an audit row `session.prompt_refused` at the runner, so one prompt may
   leave up to four such rows; they are correct (the text the TUI submitted was not registered).
5. **The pane check ignores white space.** "The text left the input" compares the first 30 characters of the prompt
   without white space against the box without white space, so a wrap inside a word no longer looks like a
   submitted prompt.
6. **The interrupt still marks `idle` at once.** Waiting in the interrupt for the TUI to put the text back would
   depend on screen texts of one Claude Code version; items 2-4 make the delivery independent of when the text comes
   back.
7. **Tests.** The Linux TUI dummy now behaves like Claude Code 2.1.330 as observed (wrapped visual rows, `C-u` removes
   the last row, long pastes collapsed, interrupted and blocked prompts put back, the latter driven by the test's hook
   stand-in); a hermetic test with the real Claude Code 2.1.330 runs the LVT-01 sequence (long turn on a slow
   model-double step, interrupt after three seconds, prompt with a leading `?`).
8. **Deviation from 05-runner.md section 6.2 (documentation for KOR-12).** Step 2 "clear input (`C-e C-u` x3)" is now
   "clear until the input box reads empty" (no keys at all for an empty box); step 3 gains the check that the box
   shows the prompt alone before `Enter` and the new attempts after a hook refusal (items 2-4). The specification is
   unchanged in this package.
9. **Limits of the evidence.** The hermetic real-Claude test has no hook server (the test's stand-in checks the
   transcripts), so the path "hook blocks, TUI puts the text back, delivery tries again" against the real TUI is shown
   by the experiment of item 1 and covered by the dummy; the LIVE runs are in the workspace report.

## NA-03 Restart of `agent-guest-control` during a suspend
10. **Cause.** A suspend keeps the session's state (`idle`) until the session unit is stopped, and it was recorded
    only in memory (`_stopping`); a control process that died in `systemctl stop` left nothing behind, so the restarted
    process watched the session again and ended it `failed agent_exited` (decisions/KOR-02.md item 8).
11. **The suspend is recorded in the meta file.** `stop(..., "suspended")` sets `"suspending": true` in the session's
    root-only meta file before it stops the unit and removes it after the `suspended` transition, like the other
    restart states of KOR-02 (no new journal state: `03-api.md` section 11 knows no "suspending"). The start-up hook
    (`reattach`) finishes such a session as `suspended` (the stop is repeated, the tmux session closed) instead of
    watching it; a mark next to an ended session is ignored. A mark that cannot be written is logged and the suspend
    goes on (as before). The session is then resumed by the runner's next start request like any suspended one.

## NA-11 NUL in operation parameters
12. **Where the record was dropped.** The runner's journal handlers refuse a record with a NUL character or a lone
    surrogate in any string, or nested deeper than 64 levels (`agent_runner.operations.journal.storable`: PostgreSQL
    `jsonb` cannot hold them), and skip it (`JournalRecordInvalid`) for `operation.request` and `platform.query`
    alike; the guest had journaled a call with a NUL as a pending call, so the session got no answer until
    `PENDING_SECONDS`. A lone surrogate never got that far: the guest's own UTF-8 encoding of the pending call failed
    (the call ended with an error inside the guest); it is refused the same way now.
13. **The guest refuses such calls before the journal.** `mcp_tools/_forward` checks the same rule
    (`storable`, a test compares it with the runner's) in `forward` and `read`, the only ways a tool hands arguments
    to the runner. A call that breaks it raises `ToolRefused`: the session gets `params_invalid` at once and the MCP
    server journals `platform.refused {code: params_invalid, tool}` without the arguments, as for a call refused by
    its input schema. No `operation.request` is journaled, so there is no operation and no hub record for it; this
    follows the formal refusal of 03-api.md section 8 (the input never becomes a valid operation request). Today only
    `request_operation` can carry such text (`type`, `params`); the read tools' inputs are closed by their schemas,
    the check there is defence in depth.
    **Deviation from 05-runner.md section 12 step 1 (documentation for KOR-12):** a request whose parameters break
    `storable` is not forwarded with `local_check: "rejected"` and gets no `operation_id`; it is refused like a call
    whose arguments do not match the tool's input schema (03-api.md section 8), because the runner could not record it.

## NA-06 Child sessions after a store wipe
14. **Children end like main sessions.** The subagent controller's reconcile now handles a child the hub lists for
    this device in a state that needs a local session (not `requested`, `stopped`, `failed`) when the store does not
    know it and its start is not running here: `SessionService.store_lost` (made public, it already took `parent_id`
    and `depth`) records it and reports `failed` with `device_shutdown` (03-api.md section 11; `stopped` when the hub
    wants it stopped), exactly as for main sessions since `ffc89b93`. The controller's next pass then sends the parent
    its `subagent.finished` ping (a failed child at once, a stopped one after the usual settle time), and
    `get_subagent_result` reads the ended child from the store; a parent that ended itself (wiped too) gets no ping,
    as before. The hub sees the child end and no longer waits for it.
15. **Guards.** `store_lost` changes nothing when its insert finds the session recorded (a child start of the
    operation executor that ended after the store was read; this also holds for main sessions), and the controller
    leaves a child alone while its task has an accepted `subagent.request` that may still start it (as for
    `requested`).
16. **What "the parent gets a result" means after a full wipe (deviation from the package text).** A real wipe loses
    the parent as well: the service ends it (`device_shutdown`), and an ended parent gets no ping (it waits for
    nothing; its next follow-up starts a new main session). The ping reaches a parent that still runs on this device
    (a store that lost only child rows, or a parent recorded again by a restarted start). In both cases the child no
    longer shows as running at the hub. A lost child the hub wanted stopped reads as `stopped` without a final text.

## NB-14 Real restart of `agent-guest-control`
17. **The control process as a real unit in the Linux suite.** `tests/platform/guest_linux/guest_unit.py` runs
    `agent-guest-control` from the checkout in a transient systemd unit with the production unit's settings
    (`Restart=always`, `RestartSec=1s`, `StartLimitIntervalSec=0`, `NoNewPrivileges=yes`, `UMask=0077`; working
    directory `/` and the configuration as `AGENT_GUEST_*`, because the test VM has no `/opt/agent-guest`); its first start-up hook installs the session manager with the suite's
    test paths (fake agent, test tmux socket), then the discovered hooks run. `test_units_linux.py` talks to it only
    over the control socket and restarts it with `systemctl restart` while a session runs: the agent's unit and
    process stay, the output is recorded again (`recorder restarted`), and the agent's later end is `agent_exited`.
    Coverage of that process is not measured (systemd starts it, not the test); the code it runs is covered by the
    in-process tests. The in-process A-05 test stays (it measures coverage).

## Rest of AUD-01 A-04: first model request after a restart of `agent-guest-api`
18. **What the session noticed (measured, real Claude Code 2.1.330, real `systemctl restart` of the API unit,
    `guest_linux/test_units_linux.py`, a stand-in for the runner with its rule: a `credential_missing` within
    `model.gateways.COOLDOWN_S` (10 s) after its last provisioning is ignored, any other one provisioned at once).**
    When the runner took the report, Claude Code retried the `503` transparently (answered after 1.1 s). When the
    runner had provisioned the session again shortly before (`done_at` of an earlier `reprovision`, e.g. a second
    restart within 10 s), all four reports of Claude
    Code's tries fell into the cooldown: the turn ended with `API Error: 503 …` after 4.7 s, the prompt got no answer.
19. **The gateway holds the request until the credential is there.** Without a credential the gateway gives its
    model slot back (the wait must not block the other sessions of the VM), journals `credential_missing` and takes
    over every 0.2 s until the runner's new provisioning arrives, at most `CREDENTIAL_WAIT_SECONDS` (22 s) and only while
    the session is active; then the request goes on unchanged (with a slot again, else `529`). The report is repeated
    every `CREDENTIAL_REPORT_SECONDS` (10.5 s, beyond the runner's cooldown), and only one request per session waits
    and reports; the session's other requests wait for it (no polling load, no report burst), at most
    `CREDENTIAL_WAITERS` (4) per session in all - every waiting request holds a handler slot of the server, so a
    further one is answered `503` at once. Measured with the
    correction: answered after 11.7 s in the cooldown case (first report ignored, the repeated one taken). The
    credential stays memory only in the API process (05-runner.md section 10.2); asking the control process for it
    at start would need a copy outside that process, so the hold is the smaller change. A request whose client gives
    up during the wait is still forwarded once the credential arrives (one answer nobody reads); accepted.
20. **Ownership.** `src/agent_guest/gateway.py` is not in the package's list of owned files; the orchestrator's note
    on A-04 asks for this correction at the root of the service, so it is changed here (the admission path and the
    new wait), with a test file of its own; `gateway_credentials.py` (KOR-10) is untouched. Documentation need for
    KOR-12: the specification does not describe the lost-credential path (neither the old `503` nor the hold).
21. **Test rig.** `test_units_linux.py` now also runs `agent-guest-api` (the production module) as a unit (as root:
    the test VM has no user `platform`; otherwise the production unit's settings); that rig installs no managed
    settings, so no hook runs there. Its fixture cleans up in-process without the start-up hooks and reuses the port
    helpers of `test_sessions_linux.py`.

# GST-06 decisions (prompt delivery in the TUI, idle, suspend/resume, interrupt/stop)

Small decisions of the delivery package (05-runner.md §5.4, §5.5, §6.1 steps 5–7, §6.2, §6.3, §7, §13; 03-api.md §7.1,
§7.2; ADR-016, ADR-025, ADR-029).

## Modules and extension points
1. `delivery.py` is the library (`Delivery`: queue, delivery, verification, refusal, interrupt key, TUI-ready watcher);
   `control_routes/prompts.py` (GST-03's route) only queues the registered prompt and registers the watcher as
   `SESSION_EVENTS["agent_starting"]` of GST-02; `control_routes/control.py` holds the `CONTROL_ACTIONS` `interrupt`
   and `suspend` and the api.sock op `session.turn_ended`; `hook_addons/idle.py` is the Stop addon. No central list
   and no module of another package was changed, except three tests of GST-02/GST-03 that asserted the absence of
   GST-06 (no. 16).
2. The delivery of a control process is created on first use (`delivery.manager(context)`) on the tmux client of the
   session manager (same root socket `/run/agent-guest/tmux.sock`) and attached to the `ControlContext`. All tmux
   calls go through the injectable `Pane` protocol (`TmuxPane` in the guest, a TUI simulation in the unit tests, real
   tmux with a TUI dummy and real Claude Code in the Linux suite). `load-buffer` reads the text from stdin into a
   buffer named per session (`p-<id>`), so no prompt is ever on a command line and two sessions never share a buffer.

## Delivery (05 §6.2)
3. Typed versus pasted follows 05 §6.2 (≤ 200 characters on one line → `send-keys -l --` in 40-character chunks,
   40 ms apart; else bracketed paste), with additions for safety: a text whose first non-blank character is `!`, `#`,
   `/`, `?` or `&`, or that contains a tab or `@`, is pasted, because typing those keys switches Claude Code's input
   mode or opens a completion. Never written to the pane, refused at once with `delivery_failed`: any C0/C1 control
   character or DEL other than LF and tab (ESC could end a bracketed paste early and turn the rest into keystrokes; CR
   would submit); text that is no valid UTF-8 (a lone surrogate reaches tmux through `surrogateescape` as a raw byte,
   for example a C1 CSI); and text whose stripped form starts with `!` or with `/name` followed by a blank or the end
   (Claude Code decides bash mode and slash commands from the submitted text, and such input may never reach the
   UserPromptSubmit hook). Paths such as `/src/main.py: explain` stay deliverable. Browser text areas produce only LF
   line ends, so no ordinary prompt is affected.
4. tmux ends a command at an argument that ends with `;`. A chunk that ends with `;` is therefore sent as `…\;`
   (tmux turns it back into `;`); verified with real tmux (`test_prompts_arrive_exactly[semicolons]`).
5. Verification (05 §6.2 step 3): after `Enter` the delivery polls for one second per attempt, `Enter` is retried
   up to three times. A delivery counts as verified when the prompt's registry row is used (the UserPromptSubmit hook
   accepted it: the authoritative signal) or when the input box no longer contains the first 30 characters of the
   first line (whitespace collapsed) nor the placeholder `[Pasted text`. After a failed delivery the input is cleared
   again (`C-e C-u` ×3).
6. Refusal = `session.state prompt_refused {prompt_id, reason: "delivery_failed"}` and, in the same transaction, the
   unused registry row is deleted, so text left behind in the TUI can never be accepted later (it would be blocked as
   unregistered). If the hook accepted the prompt in the meantime, nothing is refused. The session state itself is not
   changed by the guest; the runner (RUN-05) decides about `failed delivery_failed`.
7. Readiness: a prompt is typed only when the session is `prompt_delivering` or `idle` and the input box is visible.
   `working` is waited for without limit (05 §6.2: a prompt that arrives while working waits until idle; normally
   the runner already holds it back); `sandbox_ready`, `agent_starting` and `suspended` (start or resume in progress)
   at most 120 s; `stopping`, `stopped`, `failed` refuse. The state is checked again under the pane lock, so a suspend
   between the check and the typing makes the delivery wait again instead of typing into a closing window.
8. One worker thread per session delivers in arrival order; the same `prompt_id` is queued once (a runner retry of
   `POST …/prompts` does not type twice; a retry after a refusal is queued again), and a prompt whose registry row is
   already used is skipped. After a verified delivery the worker waits up to 10 s for the hook's acceptance; a
   prompt the pane shows as submitted but the hook never accepted (changed by a completion, blocked) is refused, so
   the runner always gets an outcome and the row can never be matched by a later prompt with the same text. Then the
   next prompt is taken; it sees `working` and waits for `idle`.

## TUI ready (05 §6.1 step 6, ADR-029)
9. Claude Code 2.1.330 sends no `SessionStart` hook. The guest derives "TUI up" from the pane: when a session reaches
   `agent_starting`, a watcher polls `capture-pane` (every 50 ms for 30 s, then once a second, until the state
   changes) until Claude Code's input box (the `❯` line below a `─` rule) is visible and moves `agent_starting` →
   `prompt_delivering` in one transaction with the state check. While an initial prompt on the command line is
   registered and not yet accepted the transition is held back, so no follow-up is typed into a TUI that is still
   taking the initial prompt; the hook's acceptance makes the session `working` directly (ADR-029). Measured with real
   Claude Code in the guest-ci VM: 0.32 s from the start to `prompt_delivering`.
10. The initial prompt that did not fit on the command line (`sessions.deferred_prompt`, > 100 KiB) is queued when the
    watcher ends and delivered by bracketed paste. A follow-up queued before it would be delivered first; the runner
    sends no follow-up before the first prompt is accepted (RUN-05, strict event order), so this does not occur.

## Control (05 §6.3)
11. `interrupt` = one `Escape` (under the pane lock) for a session whose agent runs (`agent_starting`,
    `prompt_delivering`, `working`, `idle`), otherwise `409 state_conflict` (also when the pane is gone). Claude Code
    sends no Stop hook for an interrupted turn, so a `working` session becomes `idle` here (journal
    `session.state idle`, transition allowed by `session-states.json`); without it the next prompt would wait forever.
    That Claude Code runs no Stop hook for a user interrupt is its documented hook behaviour; it was not measured in
    this package.
    A second interrupt within 2 s is answered `{}` without a key: two quick `Escape` presses open Claude Code's rewind
    dialog, which would hide the input box.
12. `suspend` only from `idle` (`suspended` again answers `{}`; every other state `409 state_conflict`, matching the
    transitions of `session-states.json`): GST-02's `stop(…, "suspended")` stops the unit and closes the window and
    keeps directories, user and transcript. A prompt queued while the session is suspended waits for the resume at
    most 120 s (no. 7); the runner resumes before it posts the prompt (RUN-05).
    Resume is GST-02's `POST /v1/sessions` with `resume_claude_session_id` (`--resume`); the new start emits
    `agent_starting` and the watcher of no. 9 makes it `prompt_delivering` again. These are guest journal states; the
    runner maps them to the hub's `suspended → resuming → working` (RUN-05).
13. `stop` stays GST-02's (`systemctl stop`, `TimeoutStopSec=10s`, window closed → `stopped`); queued prompts of a
    stopped session are refused with `delivery_failed`.

## Stop hook, idle, subagents (05 §5.5, §13)
14. `hook_addons/idle.py` handles `Stop` and `StopFailure` (a failed turn also returns the TUI to its input; without
    `idle` the next prompt would wait forever; the error itself is journaled by GST-03 as `session.hook`) and calls
    `session.turn_ended {session_id, claude_session_id, failed}`. Only a
    `working` session becomes `idle`; a repeated Stop changes nothing. The Claude session ID of the hook body (UUID
    form only) is journaled with `idle` and kept in the session cache, so the runner knows the ID to resume with even
    before the transcript reader (GST-05) reports it. The hook answer stays as decided (no text).
15. Subagent sessions (`depth` > 0 in GST-02's session metadata) also become `idle`; on that transition the
    `SUBAGENT_STOP` callbacks of the `control_routes` modules run, after a successful `Stop` only (a `StopFailure` is
    no completion) (`callback(context, session_id)`, a list or tuple; checked when called: wrong types raise
    `registry_invalid`, counted as a hook handler failure; a failing callback is logged and changes nothing). The
    session kind is read from GST-02's session metadata (`SessionManager._meta`; the watcher reads
    `SessionManager._deferred` the same way), which has no public accessor. This is
    the guest hook for RUN-11 (first Stop ends a subagent); the runner also sees the `session.hook` Stop and
    `session.state idle` records in the journal.

## Changed tests of other packages
16. `test_hooks.py::test_stop_is_only_journaled` (GST-03) asserted that Stop changes no state; renamed and now expects
    the `idle` transition of the addon. `test_sessions.py::test_control_actions_are_discovered` and
    `test_routes.py::test_other_actions_need_a_registered_handler` (GST-02) asserted that no module registers control
    actions; they now hide `control_routes/control.py` for that check. Behaviour of the other packages is unchanged.

## Review findings not changed (independent review)
17. A model could print a fake input box (a `─` rule and a `❯` line) that the parser accepts while the real one is
    hidden behind a dialog; the hook stays authoritative (no. 8), so at worst a prompt is refused.
18. Queued prompts and the watcher live in memory; a restart of `agent-guest-control` loses them without an outcome
    (`ControlService`, GST-01, does not close the delivery). A runner retry of `POST …/prompts` after the restart
    queues the prompt again (the registration is idempotent); RUN-05 owns the retry.
19. The Linux suite reuses fixtures and helpers of GST-02's `test_sessions_linux.py` (Claude Code package, model
    forwarder) instead of copying them.

## Not verified here
20. The Linux rigs have no hook server; a stand-in feeds every text the TUI took (dummy output, Claude Code's
    transcript) through `PromptRegistry.check`. With real Claude Code both the typed and the pasted prompt matched
    their registered hashes. Interrupt with real Claude Code and pasted texts starting with `?`, `#`, `&` were not run
    against Claude Code (the dummy proves exact arrival); both sandbox layers hold regardless (ADR-008).

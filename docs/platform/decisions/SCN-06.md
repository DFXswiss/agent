# SCN-06 – decisions (M2d scenarios: E2E-25, E2E-26)

1. **Pane input log** (`orchestrator/routes/pane_input.py`, E2E-26 "the tmux pane receives no input bytes"): every byte
   a pane receives as input is written by the tmux server into the pane's pty master (`send-keys`, pasted buffers of
   the guest's prompt delivery, keys of attached clients, answers to terminal queries). The route attaches `strace` (part
   of the Ubuntu base image; root through the TEST_MODE golden, ADR-036) to the guest's tmux server, limited to write
   calls, and later counts the successful writes to the descriptor of exactly that pane (found through `#{pane_tty}`
   and the `tty-index` of the server's `/dev/ptmx` descriptors). A log whose unit stopped early is refused (409), so an
   empty result always means "watched and nothing arrived". E2E-26 runs a positive control after the turn: text typed
   into the pane (without Enter) is seen byte for byte.
2. **Guest journal route** (`orchestrator/routes/guest_journal.py`, a new file next to the one named in the package):
   the order of `session.hook` and `ping.delivered` records and the guest's `ping` rows are only visible inside the
   VM; the route reads the guest journal read-only as root. E2E-26 asserts with it that the notice was attached to the
   PreToolUse answer of `Read` (the `ping.delivered {via:"hook"}` record directly follows that `session.hook`) and that
   `get_notifications` delivered the same ping (`via:"tool"`).
3. **Where the notice reaches the model**: Claude Code 2.1.330 sends a hook's `additionalContext` as a `system` message
   after the tool result (`PreToolUse:Read hook additional context: <notice>`). The model double's matchers look at the
   last *user* message only, so the scenario does not match the notice; the spec checks the double's journal instead:
   exactly one model request (the one after the hold) carries the fixed notice of 03-api.md section 8.2 among its new
   messages, and the entry's text first appears in the request that carries the result of the session's own
   `get_whiteboard`.
4. **A working session, deterministically**: the scenario's `hold` step answers after 150 s (`delay_ms`; it covers the three
   members joining, the pane log, the UI write and the ping); the spec
   writes the entry while that request is pending (the step is consumed when the request arrives) and asserts that the
   guest stored the ping before any later step was consumed and while the hub still reports `working`. The turn
   continues with the next tool call; the spec asserts one `prompt` event, the transitions `working → idle` only once,
   and the scripted steps all consumed without failure. Recipients: Rita (reader) and Wendy (writer) of the workgroup
   are both notified, Carol (member without access) and Ada (the author) are not.
5. **Out-of-scope session**: Ada's quiet task in the default workgroup stays `idle` while the workgroup entry is written;
   it gets no hub `session_ping`, no guest `ping` row and no `ping.delivered` (scope rule of 03-api.md section 4.9).
6. **Members through the UI** (`fixtures/team.ts`, a new fixture file of the two specs): the admin invites on the member
   screen with the workgroup access of the scenario; the member registers in a browser profile of its own through the
   invitation link. Each registration waits `ONBOARD_SPACING_MS` first (anonymous ceremonies; the hub allows 30 per
   minute and source).
7. **First task of a workgroup by the admin** (E2E-25): only an admin assigns a device to a workgroup (the first-task
   flow appends the assignment); a member's first task in a workgroup without a device is refused ("No execution device
   available"). The task-level board therefore lives on Ada's Research task; Wendy (writer) writes on it.
8. **Status of refused writes**: the package text says "403"; the hub refuses a whiteboard write by a reader or a
   non-admin on the org level with `400 author_not_writer`, which is the binding status of 03-api.md section 11 (every
   code of 04-crypto.md section 8 is 400). The specs assert that code for entries on all three levels and for an edit.
9. **No project level** (R-12.2): asserted in the UI (the level bar lists exactly the org, the default workgroup and
   Research), in the hub's projection (only `org`, `workgroup`, `task` rows) and in the API (`level=project` is
   `400 request_invalid`).
10. **Toast text**: the names in a notification are decrypted and pulled by the recipient's browser after the row
    arrives. In about one of eight runs the reader's toast kept the neutral fallback "…" for the workgroup name for
    more than 15 s (the row, its kind and its target were right). E2E-26 asserts the delivery (R-12.8) strictly and the
    workgroup text with the name or the fallback, and writes a note when the name did not appear; the name resolution
    is handed to the web package (WEB-11, `core/notifications`, `pullOnce` of the profile events).
11. **Egress sampler** (`orchestrator/run.py`, `egress_patterns`): the sampler matched every process whose command line
    contains `cache-golden-`. On the shared runner device another work package built a golden cache during a run (apt,
    npm, Canonical services) and the run reported its downloads as its own egress (seen in this package and in SCN-03 at
    the same second). Only the run prefix is matched now; the run's own golden cache is only cloned (clones carry the run
    prefix). A run that had to build its cache now fails with that reason (`golden.BUILT_MARK`), so the build's
    downloads can never pass unsampled.
12. **Golden bundle and model double (agent)**: the MCP tool schemas (`contracts/mcp/tools/`) were missing in the golden
    image, so `tools/list` failed and no session could use a platform tool; the runner's spend poll of the platform key
    (`GET /v1/key`) was unknown to the model double. Both were fixed in SCN-04 (not merged yet when this package needed
    them); this package carries the same two commits (identical patches, so the later rebase of SCN-04 drops them) and
    adds a test that the checkout bundle holds the schemas of every guest MCP tool.
13. **MCP tools at session start**: Claude Code sends the first model request of a session before the platform MCP server
    is connected, so the platform tools appear from the second request on. The scenario asserts the tools only where the
    session uses them (`tools_include` on the request that calls `get_notifications`).
14. **Model tunnels** (`orchestrator/tunnels.py`): Lima's `ssh.config` shares one master connection (`ControlMaster
    auto`, `ControlPersist yes`). A tunnel `ssh -N -R` attached to an existing master ended at once (rc 0, restarted
    every 1.3 s) and its forward lived only as long as that master; in one run the second session of the iteration
    never reached the model. Each tunnel now has its own connection (`ControlMaster=no`, `ControlPath=none`); after
    the change the tunnels stay open.
15. **Review (independent subagent)**: 0 high, 6 medium. Fixed: golden build inside a run (item 11), a second
    recipient (item 4), the history re-read after the refused edit, the longer hold, whole hub `session_ping` rows and
    all guest `ping` columns compared, `kinds` required by the guest journal route, the pane log's scope stated in its
    docstring. Handed over: the model double's `GET /v1/key` accepts any bearer token (SCN-04 owns that change; the
    double should compare it with the platform key). Kept: the fixture `fixtures/team.ts`, the route
    `guest_journal.py` and the shared-file changes (`run.py`, `golden.py`, `tunnels.py` with their tests) are new files
    at registration points or small component fixes with their own commits and tests (package rule); a pane-only strace
    filter is not available in the guest's strace 6.8, so the trace file holds all server writes until `stop`.
16. **First prompt right behind a membership change (found, handed to the runner packages)**: when the admin sent the
    first task of a new workgroup within about 250 ms after a member's `accept` (log entry n, prompt `lh = n`, the
    task's `device_assign` n+1), the runner started the session without an initial prompt (`first_prompt` found none)
    and never delivered it afterwards: the session stayed `prompt_delivering` with an empty input box (terminal
    evidence), in three of about fifteen iterations (T1 and T2). Expected by `prompts/deliver.py`: delivery as soon as
    the prompt is routed. The device refreshes an org's log only on demand (its verified head stayed several entries
    behind the hub's for over a minute), so a spec cannot wait for it. E2E-26 therefore starts the Research task before
    the members join (the hold is 150 s), which keeps its scenario - a working session receiving a ping - and avoids
    the race; on a timeout it writes the device's `prompt_delivery` rows and the session's terminal into the report.
    The defect belongs to the runner's prompt routing and inbox (RUN packages) and is listed as an open risk.
    **Addendum (SPC-01, 2026-11-22): workaround lifted.** The defect had two causes in the runner, both fixed in the
    `agent` repository: a wrap refused once as `wrap_unauthorized` (it arrived before the log entry that authorises it)
    was remembered for the daemon's lifetime (SCN-08, `a99eec04`/`a8ffee50`), and a stale offered log head from the
    state fetch was taken for a `log_rollback` and stopped the org (HOT-03, `f19e17d6`). HOT-03 ran the original order
    ten times green on T1. E2E-26 starts the Research task right after the joins again, with the hold back at 90 s
    (this replaces the 150 s and the joins inside the hold of item 4);
    the evidence on a timeout (`prompt_delivery` rows, terminal) stays. See `decisions/SPC-01.md`.

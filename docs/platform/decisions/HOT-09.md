# HOT-09 – decisions

Package: the first turn of a session has every platform tool; the issue list is never empty by mistake. Architecture:
ADR-068. Concept references: [§3](../KONZEPT.md#k-3) (a working session in under ten seconds),
[E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19).

## Cause (evidence)

- E2E-52 against the guest of `develop` before this package (artefacts kept with the report): the model double
  refused the first request six times with "tool 'mcp__platform__get_context' not offered"; the runner logged
  `prompt_delivering` 846 ms after the start step began, then `refused code=method_not_found`, then `working`.
- Claude Code 2.1.330 connects MCP servers in the background (`MCP_CONNECTION_NONBLOCKING` defaults to on) and runs a
  command-line prompt at once. The refused method is `server/discover`, the probe of the newer MCP protocol
  generation; `-32601` is its documented fallback signal, Claude Code then connects with `initialize` (measured after
  the change: `method=server/discover` in every start, tools listed about 0.35 s after it).

## Decisions

1. **No prompt on the command line at all** (driver, `Launch` without a prompt field) instead of a hold-back of the
   transition: a prompt already handed to the agent cannot be held back.
2. **The proof is the agent's own `tools/list`**, bound to the token of the run (a restart, resume or retry needs a
   new listing), kept in `guest_meta` and served from memory (the watcher polls every 50 ms; a journal transaction
   per poll slowed other threads in the tests).
3. **One writer for `agent_starting` -> `prompt_delivering`: the delivery watcher.** The transcript follower and the
   recorder's first pane output used to make that move as well ("TUI up"); in the first green run they did so before
   the tools arrived, and the prompt was typed then. They now only name the Claude session; the class of bypass is
   closed at one place.
4. **Restart in the guest, not in the runner**: the guest owns the agent process and the waiting prompt; restarting
   there keeps the prompt registered and undelivered, needs no new session ID at the hub and no new failure reason.
   Each attempt is visible (`guest.error tools_unavailable` -> `session.error` in the task). After two restarts the
   start fails `agent_start_failed` (the hub's "Retry" then starts a new session).
5. **Deadline 25 s, configurable 1-30 s** (`AGENT_GUEST_TOOLS_SECONDS`): three runs, each with its own start of the unit and the TUI,
   must stay within the runner's 120 s limit for `agent_starting` (reconcile), or the runner would fail the session
   first without the guest's reason (review finding: 3 x 39 s left no room for the starts).
6. **`server/discover` stays `-32601`**: the result format of the new generation is not published for this version;
   a wrong answer could break the connection. The other connecting lists answer empty.
7. **Refused methods are named from a closed list** (`METHODS`, else `other`): the guest is untrusted towards the
   runner, so no text of the session reaches the log.
8. **Issue list: issue-based pages over a scan of GitHub's list** (pages of 100 from the start, flood limit 10 pages,
   `note` when the limit ends a page early). The REST list cannot leave pull requests out; GraphQL has no page numbers
   for the contract's `page`. The other list operations do not filter by kind after the call: `list_pull_requests` and the
   comments of `get_issue` read one GitHub page and leave out only entries that break the contract (`truncated`);
   `list_repositories` reads every page and cuts at 10000 repositories without a flag (unchanged here, not the same
   class of error: no page of it is ever empty while entries follow).
9. **E2E-52 registered at milestone 3** (the highest the traceability check knows) and traced to R-8.7.

## Resume (KOR-33)

A resumed session (`--resume`, also KOR-33's restore from a checkpoint) starts the agent through the same
`SessionManager.start`: new token, `agent_starting`, the watcher waits for the tools of the new run; the follow-up
prompt that resumes the session is the deferred initial prompt of that start. KOR-33's session note (appended to the
first hook answer) is unaffected: it rides on the `UserPromptSubmit` hook of the first prompt, which now comes after
the tools.

## Review (subagent, fresh context: "How can a turn still begin without the tools?")

No high findings. Every prompt path (new session, resume, control-process restart, agent restart, follow-ups,
subagents, deferred hand-out) goes through the gate. Medium and low findings and what became of them:

1. **Medium, fixed**: the mark took the session's *current* token, so a listing of run 1 reported after a restart
   could make run 2 ready. The report now carries the hash of the token the `tools/list` request was authenticated
   with; `mark` refuses any other (test `test_a_listing_of_an_earlier_run_never_counts_for_the_current_one`).
2. **Medium, fixed**: a report lost while the control process restarts (Claude Code lists only once per run) would
   have failed the start. The API process repeats an `unavailable` report every second, up to 30 times.
3. **Low, kept**: the mark is set a moment before the `tools/list` answer leaves the API process. Typing the prompt
   takes at least 300 ms (pause before Enter), so the agent has the list before the prompt is submitted.
4. **Low, fixed**: deadline 25 s, at most 30 s (3 runs plus their starts within the runner's 120 s).
5. **Medium, kept as documented limit**: a page beyond the first 1000 entries of GitHub's list (issues and pull
   requests of the requested `state`) is not reachable; the scan always starts at the beginning (GitHub offers no
   issue-only paging in REST, GraphQL no page numbers). With the default `state: open` this needs 1000 open issues
   and pull requests; the `note` names the ways that remain (labels, state, `direction`). Raising the limit would
   cost one GitHub request per 100 entries on every call.
6. **Low, fixed**: a page cut by the result budget now carries a `note` (ask with a smaller `per_page`).
7. **Low, fixed**: the statement on `list_repositories` above.
8. **Low, partly**: the Linux stand-in for `/mcp` marks with the session's token and cannot show findings 1 and 2;
   both are covered by unit tests of the guest.

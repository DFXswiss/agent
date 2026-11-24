# ADR-068: The first prompt reaches the agent only after its platform tools

Status: Accepted (2026-11-24)

## Context
- A session start counts when the session can work ([KONZEPT §3 point 6](../KONZEPT.md#k-3): under ten seconds);
  missing prerequisites are set in motion, not explained ([E-05](../KONZEPT.md#e-05)); nothing may hold the work up
  needlessly ([E-11](../KONZEPT.md#e-11)); a gap gets a check that keeps it from coming back
  ([E-19](../KONZEPT.md#e-19)).
- Observed in operation (2026-11-24, HOT-09): the first turn of every new session ended after six seconds with "the
  platform tools are still connecting … once they are there I will …" and nothing happened afterwards; the second
  prompt of the same session worked. Reproduced hermetically (E2E-52 against the guest before this decision): the
  first model request of the session did not list `mcp__platform__*`.
- Cause: ADR-009 put the initial prompt on the agent's command line. Claude Code 2.1.330 connects its MCP servers in
  the background (`MCP_CONNECTION_NONBLOCKING` is on unless switched off) and runs a command-line prompt at once, so
  the first turn starts before `tools/list` reached the platform server. The `method_not_found` the runner logged at
  that moment is Claude Code's probe `server/discover` of the newer MCP protocol generation: the answer `-32601` is
  what the protocol foresees for a server of the 2025 generation, the client then connects with `initialize` - it is
  a symptom of the connection being set up, not its cause.

## Decision
1. **No prompt on the command line.** The driver's command line has no prompt argument at all (`Launch` has no field
   for one). Every initial prompt waits in the guest (`deferred_prompt`) and is typed or pasted into the TUI like a
   follow-up (05-runner.md §6.2).
2. **Ready means tools at the agent.** The platform MCP server reports the session's first successful `tools/list` of
   a run of the agent to the control process (op `session.tools_listed`). The mark is bound to the run's session
   token, so a restart, resume or retry has to list the tools again; it is kept in `guest_meta` (survives a restart
   of the control process) and served from memory to the watcher. The watcher moves `agent_starting` ->
   `prompt_delivering` only when the input box is visible **and** the tools were listed, then the prompt is queued.
   This holds for every start: new session, resume, restart. The signal is the agent's own request, not a wait time.
3. **Measured as its own step.** The first listing of a run journals `session.tools_ready {tools, at}`; the runner logs
   it as `start_step … step=tools_ready` (budget 2 s after `agent_starting`); `prompt_delivering` is then measured
   from the tools.
4. **Tools that do not come are a visible state and a retry.** After `AGENT_GUEST_TOOLS_SECONDS` (25 s, 1-30) without a
   listing the guest journals `guest.error tools_unavailable` (shown in the task as `session.error`) and starts the
   agent again (new run, new token, the prompt keeps waiting), at most twice; then the start fails
   `agent_start_failed` and the waiting prompt is refused. Three runs stay within the runner's 120 s limit for
   `agent_starting`. A prompt is never typed into an agent without the tools.
5. **Second safeguard in the harness.** `RULES.md` tells the session to wait and call a missing or "connecting"
   platform tool again and never to end a turn with an announcement; this catches a change of the vendor's behaviour
   that step 2 would not see.
6. **Every connecting method is answered.** `resources/list`, `resources/templates/list`, `prompts/list` answer an
   empty list, `logging/setLevel` `{}`; other methods stay `-32601`, and the journaled refusal names the method when
   it is one of the protocol's (`platform.refused {code:"method_not_found", tool:null, method}`, else `other`).

## Consequences
- The start gains the agent's MCP handshake (measured in E2E-52: tools about 0.3 s after `agent_starting` on a pool
  VM) and the typing of the prompt; the warm-pool start stays far below 10 s.
- The prompt text waits in the root-only meta directory until it is handed out (as long prompts did before).
- Guest, golden and runner change: the golden has to be rebuilt; a new runner against an old golden measures no
  `tools_ready`; an old runner ignores the new journal kind.

## Alternatives considered
- Switching off `MCP_CONNECTION_NONBLOCKING`: depends on an internal, undocumented switch of the vendor; a change
  would bring the fault back silently. Not chosen as the safeguard (the guest's own observation is).
- A fixed wait before the prompt: either slow or wrong under load; ruled out by the task.
- Answering `server/discover`: its result format is not published for this version; a wrong answer could break the
  connection. `-32601` is the documented fallback.

## Deviation from DESIGN.md
None.

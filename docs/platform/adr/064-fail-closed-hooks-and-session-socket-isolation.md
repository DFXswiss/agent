# ADR-064: Hooks fail closed, sessions cannot offer sockets, guest errors reach the task

Status: Accepted (2026-11-24)

## Context
- Hooks block forbidden actions before they run, and the sandbox enforces the bans a second time
  ([KONZEPT §10 point 5](../KONZEPT.md#k-10-b5)); the harness states the bans
  ([§10 point 2](../KONZEPT.md#k-10-b2)). Sessions of a VM are isolated from each other
  ([§3](../KONZEPT.md#k-3)). Errors belong to what is synchronised ([§6 point 2](../KONZEPT.md#k-6-b2)). Rule
  [E-19](../KONZEPT.md#e-19): close the class of error, not the case.
- The concept review (AUD-03) found:
  - **L12** – hooks fail open (ADR-008, 05-runner.md §5.5). Claude Code treats a failing HTTP hook (timeout,
    connection refused, 5xx) as a non-blocking error and goes on. The only protection against a prompt the platform
    never registered (a forged prompt typed into the pane) is the `UserPromptSubmit` hook: with the hooks down,
    ATK-38 in mode `fault` showed forged prompts taken. The session kept running, nothing said why.
  - **L17** – the sessions of a VM share one network namespace; Landlock ABI 4 of the golden's kernel (6.8) cannot
    scope abstract unix sockets (that needs ABI 6, kernel 6.12). A session could bind `\0name` and another could
    connect to it. Exploiting it needs code execution in both sessions, which hooks and sandbox prevent, but the
    isolation itself had a hole.
  - **L11** – `guest.error` (hook handler failures, gateway route errors, expired MCP calls, credential gaps) stayed
    in the guest journal and a log line of the runner; neither the task history nor the owner saw it.
- Constraints: the owner wants sessions to be fully productive ([E-08](../KONZEPT.md#e-08),
  [E-11](../KONZEPT.md#e-11)): a safeguard must not end healthy sessions. Two parallel packages change the guest and
  the golden; the golden is to be rebuilt once.

## Decision

### 1. No turn without the hook's confirmation (gateway turn gate)
- A turn is **confirmed** when the `UserPromptSubmit` hook accepted a registered prompt (`prompt.check`: the session
  becomes `working`); it ends with the `Stop`/`StopFailure` hook or an interrupt (`idle`).
- Every model request of a session goes through the guest's gateway (nftables let only the gateway out, 05 §4.3). The
  gateway forwards a `POST /v1/messages` that **can make the agent act** - its body has a non-empty `tools`, a
  `tool_choice` or an `mcp_servers` - only while the session's turn is confirmed (api.sock op `turn.check` of the
  control process; a failing op refuses as well). `count_tokens` and requests without tools (title generation and
  similar side requests of Claude Code) pass as before.
- An unconfirmed request is answered `403 permission_error` and journaled as `guest.error turn_unconfirmed` (it reaches
  the task history, §5). The refusal alone does not end the session: an interrupt that Claude Code absorbed, or a
  background request after `Stop`, is refused without harming a healthy session. Without tools the model cannot act,
  so a prompt the hook did not confirm - forged, or taken while the hook was down - never makes the agent act.
- Input into the pane is possible only through the platform's delivery (root tmux server, ADR-016), which types a
  prompt only into an `idle` session; typing into a running turn needs root in the VM (outside the threat model of a
  session).

### 2. A failing hook path ends the session visibly
- The decision hooks are `UserPromptSubmit`, `PreToolUse`, `Stop` and `StopFailure`. When the API process sees one of
  them fail - the fault switch, a control call that failed, an unexpected exception, or an answer that took longer
  than Claude Code waits (`HOOK_TIMEOUT_SECONDS`: Claude Code went on without it) - it reports
  `session.hook_failed {session_id, event}` to the control process, which ends the session:
  `session.state failed {reason: "hook_unavailable"}`, unit stopped, pane closed. The runner records it like every
  guest failure (`session.status failed`, audit `session.failed`); the web shows the reason. `PreToolUse` and
  `UserPromptSubmit` keep their closed answers.
- A failure of another hook (`PostToolUse`, `Notification`, ...) only journals `guest.error hook_handler_failed` (now a
  visible `session.error`, §5); it decides nothing.
- Without the API process there is no gateway either, so no model request at all; prompts are refused
  (`delivery_failed`, visible) and the agent cannot act. The control process being unreachable makes `turn.check`
  fail, which refuses.

### 3. The fault switch keeps the sandbox-alone attack runs
`AGENT_GUEST_FAULT_HOOKS` takes `500` - hooks answer 500 and the production reaction of §1-§2 applies - or `open` -
hooks answer 500 and the reaction is suppressed (turn gate open, no session ended). Both have an effect only in a
TEST_MODE golden: one function `fault_mode(config)` reads the switch together with the golden's install record, and
the hook handler, the gateway and the control process use only it. The attack suite's mode `fault` uses `open`: it
proves the second layer alone, as before. ATK-38 adds the run `outage` (`500`): the failing hook of the registered
prompt ends the session with `hook_unavailable` before any request with tools reaches the model. The turn gate itself
(a request with tools outside a confirmed turn) is proven by the gateway's tests against the real control process and
the model double.

### 4. Sessions cannot offer sockets to each other
- Loopback TCP and UDP between sessions are already closed: nftables let session UIDs reach only `127.0.0.1:7700`
  (05 §4.3), and Landlock refuses every TCP bind of a session.
- Abstract unix sockets live in the shared network namespace. A unix socket gets an abstract name only by `bind` or
  by autobind, which happens on `connect`/`sendmsg` of a socket with `SO_PASSCRED` or `SO_PASSPIDFD` set. The session
  unit refuses `bind` and `listen` (`SystemCallFilter=~bind:EPERM listen:EPERM`) and the io_uring system calls
  (`io_uring_setup`, `io_uring_enter`, `io_uring_register`: newer kernels bind and listen through io_uring; Node and
  Bun run without it, as under Docker's default profile). The Landlock launcher adds a seccomp filter that refuses
  `setsockopt(SOL_SOCKET, SO_PASSCRED | SO_PASSPIDFD)` with `EPERM`. No socket of a session can then carry a name
  another session could reach; `socketpair` (child processes' stdio) stays allowed.
- Claude Code needs no listening socket in a session (hooks, MCP and the model are outbound calls to the API process).
  Checked by Linux tests of the session unit and the launcher (bind of an abstract name, autobind via `SO_PASSCRED`,
  io_uring refused; a second session cannot reach the first) and by the E2E and attack runs with real Claude Code.
- Trade-off: `bind` and `listen` are refused for path sockets as well (they are already hidden from other sessions by
  the mount namespace). Nothing in a session needs them: the session runs no programs (§10 of the concept; everything
  executable runs in the work VM of E-08, which has no such filter). The self-test sees `EPERM` for a TCP bind now;
  Landlock still refuses it underneath (its `NET_BIND_TCP` rule is unchanged).
- A kernel with Landlock ABI ≥ 6 would allow `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`; the golden's kernel 6.8 has ABI 4.
  A per-session network namespace (the API listener inside each) was rejected again (ADR-045): larger change of the
  guest, same effect here.

### 5. Guest errors are events of the session
The runner turns every `guest.error` record of a session into the event kind `session.error` (body `{code}`, the
closed code list of the guest journal), sealed and synchronised like every other session event; the task history shows
it, and the situation view can count it as a blocking sign. Records without a session (VM-level) stay in the runner
log and the metric `agent_runner_guest_errors_total{code}`.

### 6. Subagents stay one-shot (checked, unchanged)
**Superseded by [ADR-088](088-full-subagents-and-limits-as-settings.md) (KOR-45):** a child now keeps running across
follow-up instructions of its main agent ([A10](../KONZEPT.md#a-10)).
A child session ends at its first `Stop` (ADR-017, ADR-038). This is intended: a subagent is a delegated job that
reports to its parent ([Idee 26](../KONZEPT.md#k-idee-26)); inside its one turn it may take as many steps as it needs,
and the parent starts another child (or resumes, KOR-33) for further work. No restriction of
[E-11](../KONZEPT.md#e-11) is involved: the turn has no step limit beyond the runtime limit of the child.

## Consequences
- A broken hook path can no longer leave a session running silently; it ends with a reason the user sees.
- A golden rebuild is needed (guest code and unit properties change); the runner and the hub must know the failure
  reason `hook_unavailable` and the event kind `session.error` first. Order: hub, runner, golden.
- The second layer is still proven alone by the attack suite (`fault` = `open`).
- Residual: a `Stop` hook that never reaches the API process (process restarting) leaves the turn confirmed until the
  next hook of the session; typing into that session needs root in the VM (§1). A `PreToolUse` that Claude Code gave
  up on lets that one tool run with layer 2 only, and then ends the session (§2).

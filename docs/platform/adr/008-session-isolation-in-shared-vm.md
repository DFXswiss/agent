# ADR-008: Session isolation inside the workgroup VM (KONZEPT §14.2)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §3: one VM per workgroup and device; several tasks share the VM but run in isolated session sandboxes. §10: Claude Code must not run programs, scripts, shells, git, tests, builds or subagents, nor tamper with hooks; hooks block, the sandbox enforces.
- Spike B (Claude Code 2.1.330, Ubuntu 24.04, kernel 6.8): 27 scripted attacks from a deterministic model double and 42 tool calls of a red-teamed DeepSeek session (26 hook denies) – no breakout; 38 OS-level probes in an identical sandbox – no breakout. Each layer alone stopped every attack.

## Decision
Two independent layers per session (`05-runner.md` §4):
1. **Claude Code configuration**: `--restricted`, `--tools Read,Glob,Grep,Edit,Write`, deny list for all other tools, root-owned managed settings (`allowManaged*Only`, bypass and auto mode disabled, `Edit(...)` denies), HTTP hooks to the guest agent with deterministic PreToolUse policy (paths resolved with `realpath`, absolute and `..` glob patterns refused) and prompt-origin check in UserPromptSubmit.
2. **OS sandbox**: own UID per session, transient systemd unit with namespace/syscall/capability hardening and resource limits, Landlock launcher (exec only the Claude binary + ELF interpreter, write only own session dir and `/tmp`, connect only to port 7700), `noexec` mounts and `ExecPaths` limited to required libraries, nftables egress per UID range to `lo:7700` only.
- The guest runs as **two processes**: an unprivileged API process (user `platform`; MCP, hooks, model gateway on 127.0.0.1:7700) and a root control process (session lifecycle, tmux, recorder, transcripts, file import/export, journal, control socket).

## Consequences
- Residual risks: exec of the Claude binary itself is allowed (needed for ripgrep; a second instance gains nothing); HTTP hooks fail open (therefore never the only barrier); Landlock does not cover `mmap(PROT_EXEC)` (covered by noexec + ExecPaths); session token visible inside the sandbox (only its own events); dependency on Claude Code 2.1.x flags and keys.
- Claude Code is pinned and auto-update disabled; every upgrade must pass the attack suite (ATK) as a gate.

## Alternatives considered
bubblewrap (blocked by Ubuntu unprivileged-userns restriction), rootless containers (need user namespaces), AppArmor per session (root-loaded profiles, no session dynamics), seccomp alone (cannot filter paths), VM per session (RAM cost ~1 GB each, start time).

## Deviation from DESIGN.md
None (DESIGN does not claim a sandbox; `docs/issue-coordinator.md` "not claimed").

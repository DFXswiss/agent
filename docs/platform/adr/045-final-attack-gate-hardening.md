# ADR-045: Hardening from the final attack gate: instruction files, empty /run, no executable memfd

Status: Accepted (2026-11-22)

## Context
The final attack gate (ATS-03, 07-testing.md §2.3, §8; 09-milestones.md WP-M3-02) assumes, in its extended OS
probe, code that got past Claude Code in a session unit (05-runner.md §4.5), and it checks which files Claude Code
2.1.330 reads as its own instructions. It found three gaps:

1. **Instruction files.** Claude Code 2.1.330 loads `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md` from the project
   root, its ancestors and nested directories, plus `.claude/CLAUDE.md`, `.claude/AGENTS.md` and `.claude/rules`.
   05 §4.2/§5.3 protect only `CLAUDE.md` and `.claude/`: a session (or text injected into it) could write `AGENTS.md`
   or `CLAUDE.local.md` into its workspace and turn data into instructions for later turns, resumed sessions and
   subagents; a checked-out repository brought all of them in (AUD-01 B-09).
2. **Unix sockets under `/run`.** The unit hid the system bus, systemd's private socket and the transient unit files,
   but every other socket below `/run` stayed reachable (Landlock ABI 4 does not restrict connecting to a unix
   socket): the varlink API of systemd-resolved (`/run/systemd/resolve/io.systemd.Resolve`) resolves names for any
   caller and sends the queries itself - a DNS channel past the egress rules, which only stop the session UIDs -, and
   socket activation started root services (oomd, uuidd, the LXD installer). The abstract socket of iscsid
   (`@ISCSIADM_ABSTRACT_NAMESPACE`) is visible to every process of the VM's network namespace.
3. **Fileless exec.** A program written into a `memfd` ran through `/proc/self/fd/<n>`: the kernel's internal mount
   of memfds is outside every Landlock rule and outside the unit's `NoExecPaths=/`, so the claim "exec only the Claude
   binary and its loader" did not hold for code already running in the session.

## Decision
- **Layer 1:** `CLAUDE.local.md` and `AGENTS.md` are protected names like `CLAUDE.md`: the PreToolUse policy refuses
  writes to them (any segment, case-insensitive; `agent_guest.policy`), and the managed settings deny
  `Edit(**/CLAUDE.local.md)` and `Edit(**/AGENTS.md)` (effective without the hooks: ATK-19 in mode `fault`).
- **Files:** a checkout never brings a protected name into the workspace: the runner's tarball rewrite skips
  `.claude/`, `.claude.json`, `.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` and `.gitconfig`
  (`tarsafe.protected`), the guest refuses an import that contains one (`path_invalid`), its export leaves such files
  out, and `github.commit` keeps the parent's protected entries and never commits one (ATK-40, ATK-33 executors).
- **Layer 2:** the session unit gets `TemporaryFileSystem=/run:ro,nosuid,nodev,noexec,mode=0755` instead of a tmpfs on
  the harness directory only; only the own harness file is bound in. The golden image masks `iscsid.socket`,
  `iscsid.service`, `lxd-installer.socket`, `lxd-installer.service` (10-slim) and sets `vm.memfd_noexec = 2`
  (`/etc/sysctl.d/60-agent-guest.conf`, applied by the install step and at every boot).
- **Self-test:** the guest's sandbox probe (05 §4.5, run at golden build and pool fill) checks `memfd_exec` (`EACCES`)
  and `unix_sockets` (no unix socket of the VM reachable); a failure marks the VM broken.

## Consequences
- An abstract unix socket that a future package opens would be reachable again (no mount namespace hides it, Landlock
  scoping needs ABI 6); the self-test then fails the golden build, so it cannot go unnoticed.
- Sessions cannot resolve names at all (no `/etc/resolv.conf` target); they never needed to (loopback only).
- Repositories keep their instruction files at GitHub; a session never sees or changes them. A user who wants such a
  file in a session's context pastes it as a prompt.
- 05 §4.2, §4.3, §4.5 and §5.3 name fewer protected names and properties than implemented; this ADR is the record.

## Alternatives considered
`RestrictAddressFamilies` without `AF_UNIX` (untested with Claude Code's runtime; `/run` already removes the
reachable sockets), `PrivateNetwork=yes` (would also cut the loopback port 7700), a per-socket `InaccessiblePaths`
list (misses sockets of later packages), refusing whole repositories that contain instruction files (most
repositories do), `vm.memfd_noexec = 1` (a process could still ask for an executable memfd with `MFD_EXEC`).

## Deviation from DESIGN.md
None beyond ADR-001.

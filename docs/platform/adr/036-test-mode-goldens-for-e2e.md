# ADR-036: E2E runs use TEST_MODE goldens with root for the test hooks

Status: Accepted (2026-11-22)

## Context
The M1 scenarios B (SCN-02) need three things inside the run's VMs that the hardened golden does not allow:
1. **Hooks forced to HTTP 500** (E2E-34, `07-testing.md` §2.1, §9 fault switches): the guest honours
   `AGENT_GUEST_FAULT_HOOKS=500` only in a TEST_MODE golden (`install.json` `"test_mode": true`, GST-03), so that a
   production VM can never be switched.
2. **Text typed straight into a session's tmux pane** (E2E-12, E2E-35, "test hook" in `07-testing.md` §2.1): the tmux
   server is root's (`/run/agent-guest/tmux.sock`, 0600) and ADR-016 forbids any guest endpoint that accepts
   keystrokes.
3. **Evidence read as root**: the recorder's asciicast file (E2E-12 compares the history with it), file facts of the
   managed settings, harness and workspaces (E2E-34), reads with a session user's identity inside and outside its
   mount namespace (E2E-10), the VM of a session (E2E-10/11).

The boot hardening (`agent-guest-harden.service`, VM-02) limits the Lima default user's sudo to restarting the guest
services, and the shared golden caches (`cache-golden-<sha12>`, ADR-027 §7) were built without TEST_MODE (ADR-034,
Context). The attack suite (ATS-01) solved the same need with its own per-run TEST_MODE golden and a test clone whose
default user keeps sudo.

## Decision
- The shared golden caches are **TEST_MODE goldens**: `tools/wp/golden-cache.sh` and `e2e/platform/orchestrator/
  golden.py` build them with `AGENT_RUNNER_TEST_MODE=1 agent-runner image build --tag <sha12> --test-model-port 9`.
  Port 9 is the closed port of the hermetic rules: the guest reaches the doubles through the tunnel on its loopback
  (ADR-034), so the TEST_MODE nftables rule for the host gateway stays unused. This matches `07-testing.md` §5.2
  ("test images") and supersedes the remark of ADR-034 that the caches are built without TEST_MODE.
- `agent-guest-harden.service` keeps the Lima default user's **unrestricted sudo only in a TEST_MODE golden**
  (`/etc/sudoers.d/91-agent-guest-test`, removed and re-evaluated on every boot; written only when
  `/var/lib/agent-golden/install.json` says `"test_mode": true`). A production golden never has it.
- The E2E orchestrator's test hooks (`orchestrator/guestvm.py`, `routes/sessions_vm.py`, `routes/hooks.py`) use
  `limactl shell <vm> sudo -n …` on VMs with the run prefix only; they are reachable only through the run's control API
  (bearer token), never from the hub, the browser or a session.

## Consequences
- Session isolation is unchanged: session users are not in `sudo`, units run with `NoNewPrivileges`, an empty
  capability set and Landlock (exec only the Claude binary); the Lima default user is the host's own channel (trust
  boundary B4: the host is the trusted side). The restriction of that user remains defence in depth for production.
- E2E runs and the attack suite exercise a TEST_MODE golden; the hash of the cache inputs (`images/`, guest, VM code)
  changes with this decision, so the next cache is built anew (≈ 36 s).
- PERF numbers are unaffected (same image apart from one nftables rule and the sudoers file).

## Alternatives considered
A test-only guest control endpoint for typing (contradicts ADR-016), a runner-provided Lima parameter that switches
test mode at clone time (moves a production decision into a run-time parameter), per-run TEST_MODE goldens inside the
hermetic run (the build needs the package mirrors: egress), a per-port cache name (ADR-034's port problem).

## Deviation from DESIGN.md
None.

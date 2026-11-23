# ADR-034: Test doubles reach the guest on loopback through a reverse SSH tunnel

Status: Accepted (2026-11-22)

## Context
`07-testing.md` §5.2 puts the model double on the runner device and lets the guest reach it "via the Lima host
address". The guest model gateway, however, accepts plain `http://` only to a loopback upstream: the credential
contract `guest/session-credential.request.json` restricts `upstream_base_url` to `https://…` or
`http://127.0.0.1|localhost|[::1]`, and the runner validates its upstream URLs the same way in test mode (`05-runner.md`
§1.3). The M0 acceptance left this contradiction open (`acceptance/M0.md`, open points). M0 measurements and the
runner's real-VM tests (PRB-01, RUN-04, RUN-05) avoided it by starting the model double inside the VM; a full E2E run
cannot do that, because the orchestrator must switch scenarios and read the double's state, and pool VMs appear and
disappear during the run. A TEST_MODE golden with `--test-model-port` opens one host port for the guest's `platform`
user, but the port is baked into the image while every run has its own ports, and the shared golden caches
(`cache-golden-<sha12>`, ADR-027 §7) are built without it.

Options:
1. Relax the contract to allow `http://` to the Lima host gateway (`host.lima.internal`). Weakens a production rule for
   a test need; the gateway would send credentials in clear over the VM link; the guest cannot tell a test VM from a
   production VM without extra state.
2. TLS from the guest to the host (test CA in the image, certificate for the host gateway). Needs a test-only image
   variant or a CA in every image, and a TLS front for every double; the port problem of option 1 remains.
3. Double inside every VM (PRB-01 style). Scenario switching and state reads through `limactl shell` into every VM;
   VMs created during a test would need the double before their first session.
4. **Reverse SSH tunnel per VM on loopback**: the guest sees the double at exactly the loopback address the runner was
   configured with.

## Decision
Option 4. The E2E orchestrator runs a watcher on the runner device (`e2e/platform/orchestrator/tunnels.py`) that keeps,
for every running Lima instance with the run's prefix, one

    ssh -F <instance dir>/ssh.config -N -R 127.0.0.1:<port>:127.0.0.1:<port> lima-<name>

per forwarded double port (today the model double relay). The runner keeps its test configuration
`AGENT_RUNNER_MODEL_UPSTREAM_* = http://127.0.0.1:<model relay>` and hands the guest that upstream unchanged; inside the
guest the port is bound on 127.0.0.1 by the SSH daemon. A tunnel that ends (VM still booting, VM stopped) is opened
again after one second while the VM runs.

- Production code and contracts are unchanged: `http://` stays loopback-only, production images contain nothing for
  tests, the runner does nothing test-specific.
- Sandboxes cannot use the tunnel: the image's nftables let session users (UID 20000–29999) reach only
  127.0.0.1:7700; the gateway's `platform` user may use loopback, as in production.
- The `--test-model-port` image variant (SBX-01) stays available for ad-hoc tests but is not used by the E2E suite.
- The guest operating system's own time sync and name lookups (NTP, DNS) leave through the Lima host agent; the
  egress check allows a Lima host agent's connections to port 123 (any time server) and to port 53 of the device's
  own resolvers (`/etc/resolv.conf`), and keeps every other VM connection a violation, including DNS to any other
  server (`e2e/platform/orchestrator/egress.py`). They carry no platform content; production VMs need them. This
  amends the hermetic rule of `07-testing.md` §2.1/§5.2 ("no outbound connection except to the doubles") for the
  guest operating system only; the session sandboxes and the guest services stay bound by it (nftables, ATK suite).

## Consequences
- The orchestrator needs Lima's SSH configuration of the run's own instances (it reads `limactl list --json`); every
  run gets the double on the same loopback port inside each VM, so scenarios stay in one place (the runner device).
- The first model request of a session on a freshly cloned VM can race the tunnel; in practice the tunnel is up within
  about a second after Lima reports the VM running, well before Claude Code's first request (the session start after
  `vm_ready` takes longer), and Claude Code retries failed requests.
- Further doubles (GitHub, mail) reached from the guest use the same watcher with more `--port` values.
- The image fragment `agent-sessions.test.nft.in` and option 1 remain unnecessary for E2E.

## Alternatives considered
See options 1–3 above.

## Deviation from DESIGN.md
None beyond ADR-001.

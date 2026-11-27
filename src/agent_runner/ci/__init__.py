"""Self-hosted GitHub Actions jobs in throw-away VMs, stage 1 (docs/platform/spec/16-ci-runners.md sections 13 and 17;
KONZEPT E-34, E-35, E-36).

Two processes on a pool device (decision D-3 (b)):

- the **CI controller** `agent-ci` (`controller.py`), a service of its own under its own account: it holds the key of
  the GitHub App "agent CI" (decision D-4), runs the scale-set listener (`bridge.py` and the Go sidecar
  `cmd/agent-ci-listener`, decision D-2 (a)), decides which job it takes (`attribution.py`) and hands the JIT
  configuration of an accepted job to its VM;
- the **CI area** of `agent-runner` (`area.py`), reached by the controller over a local socket (`link.py`): it looks
  pull requests up in the runner's own records, starts and deletes the CI VMs through the work VM machinery and the
  device's one admission (kind `ci`), and writes the audit rows; the VM's runtime is billed by the existing meter to
  the session and task the job belongs to (`workvm/billing_vms.py`).

A job is taken only when it belongs to a pull request a session of agent.dfx.swiss created, or to the fixed task of
its repository (scheduled runs and deploys); everything else is not acquired and stays at GitHub (decision D-12).
"""

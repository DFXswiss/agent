# Runbook: monitoring patch handover

Scope: bringing the platform's dashboards and alert rules into the existing DFX monitoring stack
([08](../spec/08-monitoring.md), ADR-018, ADR-042). The assets live in the agent repository under
`deploy/monitoring/` (details: `deploy/monitoring/README.md` and `deploy/monitoring/patch/README.md`). The
infrastructure repository is **never** pushed to from this work: the patch is rendered here and applied and reviewed
there by its operators.

## 1. Prerequisites

- The runner device is a fleet host of the monitoring stack (node_exporter with textfile collector, Alloy push with
  the `server` label, host folder with `node_exporter.args` in the infrastructure repository).
- The runner writes its textfile: `AGENT_RUNNER_TEXTFILE_DIR` set to a directory owned by the runner account
  ([runner-installation.md](runner-installation.md)); `agent-runner doctor` shows `textfile ok`.

## 2. Render the patch (agent checkout, operator machine)

```bash
PYTHONPATH=deploy/monitoring .venv/bin/python -m agent_monitoring.patch \
  --infra <checkout of the infrastructure repository> \
  --device <runner-device> [--device <runner-device> …] \
  --runner-textfile-dir <the runner's TEXTFILE_DIR on the device> \
  --out agent-platform.patch
```

The renderer only reads the checkout (two `git clone --no-local` copies in a temporary directory) and proves
`git apply --check` on a fresh copy. It prints the required **hand edits** on stderr and as `Hand edit:` lines in the
patch header (watched hosts of the fleet absence rule and its regression test, the inventory tile's description).

## 3. Contents

- Dashboards `agent-platform-{devices,vms,sessions}.json` in the Grafana folder `Agent platform`.
- `alerting/agent-platform.yaml`: the seven rules of 08 §5 (folder `Infrastructure`, 1 min evaluation,
  `component="agent-platform"`), the textfile absence rule with one zero fallback per runner device.
- Only when missing: the device in the fleet absence rule `fleet-host-memory-absent`, an inventory tile.
- Per device: section `## Agent runner textfile` in the host README (root creates one symlink `agent_runner.prom` in
  node_exporter's textfile directory pointing to the runner's file).
- The regression test `test_agent_platform_alerts.py` for the infrastructure repository's test suite.

## 4. Handover

1. Open a pull request in the infrastructure repository with the patch and the hand edits; attach the renderer's
   stderr.
2. Its CI runs the monitoring-stack tests including the new regression test and the UID-length guard.
3. After its merge the infrastructure operators apply the stack as usual (their SSH-gated apply); on each runner
   device root creates the textfile symlink of the host README section.
4. Verify: Prometheus has series with `server=<runner-device>` for the runner metrics (e.g.
   `agent_runner_device_vm_slots`), the three dashboards show data, the seven rules evaluate without error.

## 5. Before handing over: test stack (optional, runner device)

`deploy/monitoring/test-stack/up.sh --prefix <prefix> --textfile-dir <dir> --check` starts a Lima VM with the
pinned Prometheus, node_exporter and Grafana versions of the existing stack, provisions the dashboards and rules and
runs `promtool check/test rules`, `promtool check metrics` and the Grafana API checks; `down.sh --prefix <prefix>`
removes it. E2E-17 runs the same against a real runner's textfile.

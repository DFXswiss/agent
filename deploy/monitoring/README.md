# Monitoring assets of the agent platform

Grafana dashboards, alert rules and installation steps for the runner's Prometheus textfile
(`docs/platform/spec/08-monitoring.md` sections 3, 5 and 7, ADR-018). They follow the conventions of the existing
DFX monitoring stack (node_exporter textfile collector + host Alloy push with a `server` label, Grafana provisioning
files) and are delivered to the infrastructure repository as a **prepared patch**; nothing here is pushed there.
This repository is public: no machine is named, the runner device is the placeholder `<runner-device>`.

| Path | Content | Target in the infrastructure repository |
|---|---|---|
| `dashboards/agent-platform-{devices,vms,sessions}.json` | uids `agent-platform-*`, variables from `label_values(…)`; device (slots, pool, VM memory budget, VMs and sessions by state, outbox backlog, hub connection, sync state, capacity low), VM (CPU, memory and disk against the limit, network, uptime, sessions, probe), session (CPU, memory against the limit, workspace, IO, OOM kills, hook and egress denies, model tokens) | `…/monitoring-stack/grafana/provisioning/dashboards/json/Agent platform/` |
| `alerting/agent-platform.yaml` | the seven rules of 08 §5 (`apiVersion: 1`, folder `Infrastructure`, 1m, `execErrState`/`noDataState` OK, labels `severity` and `component="agent-platform"`, header comment per rule) | `…/grafana/provisioning/alerting/` |
| `alerting/agent-platform.test.yaml` | `promtool test rules` cases: every rule fires in its failure case and stays silent when healthy | – (run by the test stack) |
| `textfile-dir.md` | install step: the runner's own textfile directory and one root-owned symlink in the node_exporter textfile directory | section of the host README |
| `inventory-tile.toml` | `[[servers]]` tile for a runner device that has none | `dashboard-inventory.toml` |
| `patch/` | patch renderer instructions and the infrastructure-side regression test | see `patch/README.md` |
| `test-stack/` | Lima VM with Prometheus v3.11.0, node_exporter v1.8.2, Grafana 12.4.2 (pinned archives; `fetch.sh` + `up.sh --archives` for the hermetic offline mode) | – |
| `agent_monitoring/` | helpers: placeholder rendering, Grafana → Prometheus rules, patch renderer, sample textfile, stack checks | – |

The absence of a device's samples is watched twice, both with a zero fallback per device (a bare `or vector(0)`
would fire forever): the device's host series in the fleet rule `fleet-host-memory-absent` (added by the patch when
missing) and the runner textfile in `agent-runner-textfile-absent`.

The total of the VM slots is `agent_runner_device_vm_slots` (ADR-042), not `…_vm_slots_total`. The metric names and
labels used here are checked against the table of 08 §3 and against the runner's renderer
(`tests/platform/monitoring/test_assets.py`).

## Test stack

On the runner device (prefix `wp-<tag>-` in work packages, see ADR-027 §7):

```bash
mkdir -p "$PWD/.monitoring-textfile"
PYTHONPATH=deploy/monitoring .venv/bin/python -m agent_monitoring.sample --dir "$PWD/.monitoring-textfile" --capacity-low
deploy/monitoring/test-stack/up.sh --prefix "$AGENT_RUNNER_VM_PREFIX" --textfile-dir "$PWD/.monitoring-textfile" \
  --check --expect-active agent-runner-capacity-low
deploy/monitoring/test-stack/down.sh --prefix "$AGENT_RUNNER_VM_PREFIX"
```

`up.sh` creates `<prefix>monitoring` from `test-stack/lima.yaml`, mounts this directory and the runner's
`TEXTFILE_DIR` read-only and runs `provision.sh` in the VM: archives checked against `versions.env`, node_exporter
reading the textfile through the symlink of `textfile-dir.md`, Prometheus scraping it every 15 s with
`server=<--server>`, Grafana provisioning the data source `prometheus`, the three dashboards and the alert file.
Guest ports stay in the VM unless `--forward-port PORT` (Prometheus on `127.0.0.1:PORT`, Grafana on `PORT+1`).
`--check` runs `check.sh`: `promtool check metrics` on the textfile, `promtool check rules` and `promtool test rules`
on the converted rules, no provisioning error in Grafana's journal, and `agent_monitoring.stack` (series of every
textfile metric in Prometheus; dashboards in folder `Agent platform` with data for every panel and variable query
through Grafana; seven rules in folder `Infrastructure`, evaluated without error; the `--expect-active` rules pending
or firing). E2E-17 runs the same scripts against the textfile of a real runner.

Offline mode (hermetic E2E runs, `07-testing.md` §5.2): `test-stack/fetch.sh DIR` downloads the pinned archives once
on the host (checked by `archive.sh`); `up.sh --archives DIR` mounts DIR read-only at `/mnt/agent-monitoring-archives`,
`provision.sh` copies the archives from there and never downloads, and the boot script of `lima.yaml` (parameter
`AGENT_MONITORING_OFFLINE=1`) drops every packet the VM opens itself from its first boot on except loopback, name
lookups and NTP (answers on connections the host opened pass) – a plain Ubuntu image otherwise contacts Canonical's
servers at boot (pollinate, snapd, motd, apt). `check.sh` fails when the drop is missing.

```bash
deploy/monitoring/test-stack/fetch.sh "$HOME/monitoring-archives"
deploy/monitoring/test-stack/up.sh --prefix "$AGENT_RUNNER_VM_PREFIX" --textfile-dir "$PWD/.monitoring-textfile" \
  --archives "$HOME/monitoring-archives" --check
```

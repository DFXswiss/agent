# Prepared patch for the infrastructure repository

The monitoring assets reach the infrastructure repository as a patch that an operator of that repository applies
and reviews there (pull request, tests, SSH-gated apply of the monitoring hub). This repository never pushes there.

Because this repository is public and names no machine, the patch is rendered per installation:

```bash
PYTHONPATH=deploy/monitoring .venv/bin/python -m agent_monitoring.patch \
  --infra <checkout of the infrastructure repository> \
  --device <runner-device> [--device <runner-device>…] \
  --runner-textfile-dir <the runner's TEXTFILE_DIR on the device> \
  --out agent-platform.patch
```

The checkout is only read (two `git clone --no-local` copies in a temporary directory); the second, fresh copy
proves `git apply --check`. Apply in the infrastructure repository with `git apply agent-platform.patch`.

## What the patch contains

Below the one monitoring stack of the repository (`infrastructure/*/monitoring-stack`):

- `grafana/provisioning/dashboards/json/Agent platform/agent-platform-{devices,vms,sessions}.json` – new folder
  `Agent platform` (the provider uses `foldersFromFilesStructure`).
- `grafana/provisioning/alerting/agent-platform.yaml` – the seven rules, the absence rule rendered with one zero
  fallback per runner device.
- `grafana/provisioning/alerting/fleet-host-memory.yaml` – only for a device missing in `fleet-host-memory-absent`:
  the device in the selector and its own zero fallback (refused unless selector and fallbacks then name the same
  hosts). The regression test `infrastructure/tests/monitoring-stack/test_fleet_host_memory_alerts.py` (watched hosts,
  expected expression) and the rule's description name the hosts too: the renderer lists these hand edits on stderr
  and as `Hand edit:` lines in the patch header; make them in the same pull request.
- `dashboard-inventory.toml` – only for a device without a tile (keys read with `tomllib`): `inventory-tile.toml` and
  the device at the end of `server_display_order` (hand edit: the tile's description and the display-order comment).

Per runner device:

- `infrastructure/<runner-device>/README.md` – section `## Agent runner textfile` (`../textfile-dir.md`) with the
  textfile directory of the host's `node_exporter.args`: the runner writes into its own directory, root creates one
  symlink `agent_runner.prom` in the textfile directory.

And `tree/`, copied as is:

- `infrastructure/tests/monitoring-stack/test_agent_platform_alerts.py` – regression contract of the alert file
  (seven UIDs ≤ 40 characters, group conventions, labels, query/reduce/threshold wiring, one zero fallback per device,
  the three dashboard UIDs).

Prerequisite: the runner device is a fleet host (host baseline: node_exporter with textfile collector, Alloy push,
host folder with `node_exporter.args`). The renderer refuses a device without `node_exporter.args`.

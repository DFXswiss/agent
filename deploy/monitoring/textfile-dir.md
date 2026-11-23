## Agent runner textfile

The agent runner (agent.dfx.swiss, LaunchDaemon `swiss.dfx.agent-runner`) samples the device, its Lima VMs and
their AI sessions every 15 s and writes the Prometheus textfile `agent_runner.prom` (metrics `agent_runner_*`,
labels are IDs only). The host node_exporter reads it through its textfile collector and the host Alloy pushes it
with `server="<runner-device>"`, like `macos-memory-free.sh`. No new port, no exporter process, nothing in the VMs.

The runner user gets no write access to the root-owned textfile directory `<textfile-dir>` (the directory of
`node_exporter.args`, reset to mode 755 by `install-monitoring.sh`). The runner writes the file atomically (temp
file `.agent_runner.prom.XXXXXX`, mode 0644, `rename`) into its own directory `<runner-textfile-dir>`, and one
root-owned symlink in the textfile directory points at it. node_exporter follows the symlink; the runner can neither
replace nor delete the other files of the directory.

Install (once, after `install-monitoring.sh`; idempotent):

```bash
# as the runner user: the runner's own textfile directory, configured for the daemon
install -d -m 0755 <runner-textfile-dir>
# AGENT_RUNNER_TEXTFILE_DIR=<runner-textfile-dir> in the runner configuration, then
# `agent-runner service install` (root) rewrites the LaunchDaemon with it
# as root: the link the textfile collector reads
sudo ln -sfn <runner-textfile-dir>/agent_runner.prom <textfile-dir>/agent_runner.prom
```

Check:

```bash
ls -l <textfile-dir>/agent_runner.prom                   # root-owned symlink
curl -s localhost:9100/metrics | grep -c '^agent_runner_'  # > 0 once the runner sampled
curl -s localhost:9100/metrics | grep '^node_textfile_scrape_error'  # 0
```

Until the runner wrote its first file the link dangles and node_exporter reports `node_textfile_scrape_error 1`;
the alert `agent-runner-textfile-absent` covers that state. Remove: `sudo rm <textfile-dir>/agent_runner.prom`.
Dashboards `Agent platform/*` and the rules of `agent-platform.yaml` read these series (folder Infrastructure).

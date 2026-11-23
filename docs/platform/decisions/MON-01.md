# MON-01 decisions (Grafana dashboards, alert rules, infrastructure patch, monitoring test stack)

Small decisions of the MON-01 package (08-monitoring.md §3 dashboard side, §5, §7; ADR-018; ADR-042; ADR-027 §4, §6).

## Installation on the runner device
1. **No separate textfile LaunchDaemon.** 00-overview §5 and ADR-018 mention a "launchd plist template for the
   textfile writer", the package text asks for `swiss.dfx.agent-runner-textfile.plist.template` only "if 08 §5 needs
   it". 08 §5 lists no plist, and the textfile writer is the runner daemon itself (RUN-15 sink A), whose LaunchDaemon
   `swiss.dfx.agent-runner` is written by `agent-runner service install` (`agent_runner/service.py`). A second job
   would duplicate the sampler. No plist is delivered.
2. **Symlink instead of write access to the textfile directory.** 08 §3 says the runner user needs write access to
   the node_exporter textfile directory. In the infrastructure repository that directory is root-owned and
   `install-monitoring.sh` resets it to mode 755 on every apply, and write access would let the runner replace or
   delete the host's other textfiles (memory, launchd job status). Instead the runner writes atomically into its own
   `TEXTFILE_DIR` (unchanged RUN-15 code: temp file, 0644, `rename`) and root creates one symlink
   `<textfile dir>/agent_runner.prom → <runner TEXTFILE_DIR>/agent_runner.prom` (`textfile-dir.md`, host README
   section of the patch). node_exporter follows the link; the test stack installs exactly this link and proves it
   with node_exporter v1.8.2. The installation step stays in the prepared patch, as 08 §3 requires.
   Residual risk, the same as with the write access of 08 §3 and smaller: the runner user controls the content of
   its file, so it can export any series under the host's `server` label, and it can replace its file by a symlink
   to another file that root (node_exporter) then parses; a file that is not exposition format only yields
   `node_textfile_scrape_error 1`. With write access to the directory it could do the same and in addition delete
   or replace the host's other textfiles. Filtering the pushed series to `agent_runner_.*` would be a change of the
   host Alloy configuration and is left to the infrastructure repository.

## Assets
3. **Placeholder and rendering per installation.** The repository is public, so the alert file and the inventory tile
   carry `<runner-device>`. `agent_monitoring.assets.render_devices` sets the selector of the absence rule to all
   devices and repeats the zero-fallback line per device (the infrastructure's per-host fallback pattern). Device
   names are refused unless they are plain host labels.
4. **Rule shape.** Every rule is query `A` (instant, data source `prometheus`) → `B` reduce `last` → `C` threshold,
   like the infrastructure's rules. The sketches `… == 1` of 08 §5 are thresholds `gt 0` on 0/1 gauges, `== 0` are
   `lt 1`; queries aggregate `by (server)` (plus `vm`, `session` where the alert names a VM or a session) so that one
   alert instance names one device, VM or session.
5. **Severities** (08 §5 names none): `critical` for textfile absent, sync fatal and hub disconnected (the device does
   no or no visible work), `warning` for stale textfile, capacity low, VM probe failed and session OOM.
6. **Dashboards** are committed JSON (schema version 39 like the existing dashboards), folder `Agent platform`, data
   source uid `prometheus`, `refresh` 30s, time 6h; every query is restricted by every variable of its dashboard
   (`server`, then `vm`, then `session`, each from `label_values`). The model spend gauge is not on a dashboard:
   08 §5 does not list it and the runner exports it only with RUN-12.
7. **Infrastructure patch as renderer, not as committed diff.** A committed diff against the infrastructure
   repository would contain host names (context lines). `agent_monitoring.patch` renders the patch from a checkout:
   new files, the device in `fleet-host-memory-absent` and in the inventory only when missing (inventory keys read
   with `tomllib`; the absence rule is refused unless selector and fallbacks name the same hosts afterwards), the host
   README section, and an infrastructure-side regression test (`patch/tree/`). Texts and pinned lists the renderer
   does not rewrite (the host list in the absence rule's description and in its regression test, a new tile's
   description) are reported as hand edits on stderr and in the patch header. The concrete patch of an installation is kept
   out of this repository (workspace report). The monitoring stack directory is found by its `dashboard-inventory.toml`,
   so no host name is needed to locate it.

## Tests
8. **promtool on converted rules.** `promtool` reads Prometheus rule files, not Grafana provisioning. `agent_monitoring.rules`
   converts each rule to `(A) > x` / `(A) < x` with the same `for` and labels (alert name = UID) and refuses any other
   rule shape, so the tested expression is the one Grafana evaluates. `promtool test rules` compares annotations,
   so the test conversion leaves them out (`--without-annotations`); `promtool check rules` runs on the full file.
   promtool lists firing alerts only, so each rule with `for` is evaluated one minute before and exactly at the end
   of its window, healthy series are checked after the window, and the absence rule runs with a second, healthy
   device (`test-device`, `healthy-device`); a unit test requires the boundary pair for every such rule. Changing a
   `for` by one minute, a threshold, the OOM window or the zero fallback fails `promtool test rules` (checked by
   mutation while writing the cases).
9. **promtool and Grafana run in the test stack, not in `make platform-ci`.** The gates have no Prometheus binaries
   (and must not skip); the unit suite checks schema, conventions, conversion, rendering, scripts (with a `limactl`
   double) and metric consistency, and the test stack on the runner device runs `promtool check metrics/rules`,
   `promtool test rules` and the Grafana HTTP API checks (acceptance `docs/platform/acceptance/MON-01.md`).
10. **Python helpers in the gates.** The helpers live next to the assets (`deploy/monitoring/agent_monitoring`, run
    from the checkout and, for `assets`/`rules`/`stack`, with the VM's system Python and PyYAML). Like PRB-01's
    `probelib` they are added to the coverage source (`.coveragerc-platform`, pinned in `test_coverage_config.py`),
    to `mypy` (`pyproject.toml`) and to `ruff` (`Makefile` `PY_PATHS`), so they are held to 100 % lines and branches.
11. **Test stack without containers.** The infrastructure runs the stack as Docker images; the test VM installs the
    release archives of the same versions (Prometheus v3.11.0, node_exporter v1.8.2, Grafana 12.4.2, linux-arm64,
    SHA-256 pinned in `versions.env`, Grafana's digest also checked against the downloaded archive) as systemd units,
    which needs no Docker in the VM and no image registry. Prometheus scrapes node_exporter in the VM and adds
    `server=<--server>`, the label the host Alloy adds in the fleet. Every service listens on the VM loopback and
    every guest port stays in the VM unless `--forward-port` is given (E2E use). The Grafana admin password is random
    per VM and read through `GF_SECURITY_ADMIN_PASSWORD__FILE` (root:agent-grafana 0640). `up.sh` records the mounts
    and forwards in the VM at creation and refuses to reuse a VM created with others (`down.sh` first).
12. **Stack check of the dashboards** runs every panel and variable query through Grafana (`/api/ds/query`) with the
    variables replaced by `.+` and `$__rate_interval` by `1m`, so a query that Grafana cannot run or that has no
    data with the runner's textfile fails the check.
13. **E2E-17** does not exist yet (SCN-07); `up.sh`/`down.sh` take the prefix and the runner's `TEXTFILE_DIR` as
    parameters for it.

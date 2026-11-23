"""Helpers of the agent platform monitoring assets (08-monitoring.md sections 5 and 7, ADR-018).

The assets themselves are files next to this package: `dashboards/`, `alerting/`, `textfile-dir.md`,
`inventory-tile.toml`, `patch/` and `test-stack/`. The modules here render the device placeholder of the alert file
(`assets`), convert the Grafana rules into Prometheus rules for `promtool` (`rules`), prepare the patch for the
infrastructure repository (`patch`), write a sample textfile with the runner's own renderer (`sample`) and check a
running test stack over the Prometheus and Grafana HTTP APIs (`stack`).

`assets`, `rules` and `stack` need only the standard library and PyYAML, so they also run with the system Python of
the test-stack VM; `sample` and `patch` run in the repository environment.
"""

# ADR-018: Monitoring reuses the existing stack (KONZEPT §7)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §7: full resource monitoring device → Lima VM → AI session (CPU, RAM, storage, network, runtime, reachability, state, free capacity), historised; reuse the monitoring stack of `DFXswiss/server` (Grafana and proven components); separate operation allowed but technologically consistent; relevant metrics also in the web UI; optional: bottleneck warnings and capacity-based task distribution.
- Infrastructure conventions: node_exporter + Alloy (Homebrew, LaunchDaemon) pushing via `remote_write` with a `server` label; own host metrics via the textfile collector with atomic writes, `*_timestamp_seconds` and `probe_ok`; Grafana dashboards/alerts as provisioned files; no cgroup exporter in use; metric stores are treated as personal data (IDs only).
- Spike A: VM metrics readable from the host (`limactl list --json`, VZ process via `lsof` on the disk file); session metrics need an in-VM agent (cgroups).

## Decision
- The runner samples every 15 s: device (host), VMs (host side + guest `/v1/metrics`), sessions (cgroup values of the transient units).
- Two sinks from the same sampler: (a) Prometheus text files in the node_exporter textfile directory (prefix `agent_runner_`, labels pseudonymous IDs only), pushed by the existing Alloy agent; (b) `metrics` outbox items to the hub for the web UI (raw 48 h, 5-min rollups 90 days).
- Dashboards, alert rules (absence, stale, capacity low, VM unreachable) and the launchd plist live in `agent/deploy/monitoring/` and are delivered to the infrastructure repository as a **prepared patch** (no push there).
- Optional §7 points included in v1: capacity-based device selection (ADR-013) and a `capacity_low` warning (< 10 % free slots) in UI and Grafana.

## Consequences
- Same technology as the fleet; user-owned devices without the DFX Prometheus still show metrics in the web UI via the hub.
- Session-level series in Prometheus increase cardinality (bounded by 15-day retention; IDs only).

## Alternatives considered
Hub querying Prometheus (unavailable for user hardware, extra read login), a new exporter daemon with a port (textfile pattern is the convention), cAdvisor (dropped in the fleet).

## Deviation from DESIGN.md
None (DESIGN §25.12 "monitoring adapters" is compatible).

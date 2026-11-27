# 08 Monitoring: device → VM → session

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#08-monitoring) (DOC-01).

Status: binding. Implements KONZEPT §7 (incl. the optional extensions, decided in ADR-018) and the resource part
of §6. Components: RUN (`agent_runner/metrics/`), GST (`/v1/metrics`), HUB (`agent_hub/monitoring/`), WEB
(monitoring screen), MON (`agent`: `deploy/monitoring/`). Conventions reused from the infrastructure repository
(file paths there: host Alloy agent `infrastructure/<host>/alloy-agent/config.alloy`, textfile scripts
`infrastructure/scripts/macos-memory-free.sh`, per-VM example `infrastructure/<vm-host>/bin/slot-vm-disk.sh`,
Grafana provisioning `infrastructure/<monitoring-hub>/monitoring-stack/grafana/provisioning/`, policies
`docs/policy/logging.md`, `docs/policy/host-baseline.md`).

## 1. Levels and metrics

Sampling interval 15 s (same as the existing scrape interval). All values historised (§4, §5).

| Metric (logical) | Device (physical Mac) | VM (Lima) | Session (sandbox unit) |
|---|---|---|---|
| CPU | `host_statistics` via `sysctl`/`top -l 1` parsing → used cores ratio | VZ process `%cpu` (`ps -o %cpu,rss,etime -p <pid>`) and guest `cpu_usage_usec` delta | cgroup `cpu.stat usage_usec` delta |
| RAM | `vm_stat` + `sysctl hw.memsize` → used/free/reclaimable bytes | VZ physical footprint (`ps rss`; high-water mark) + hostagent RSS; guest `mem_used/mem_total` | cgroup `memory.current`, `memory.max`, `memory.events oom_kill` |
| Disk | `df -k` of the volume holding `$LIMA_HOME` | allocated blocks of `<instance>/disk` (`stat`, `du -k`), guest filesystem used/total | workspace bytes (scanner), cgroup `io.stat` read/write bytes |
| Network | `netstat -ib` counters of active interfaces | `nettop -P -L 1 -p <hostagent pid>` bytes (usernet runs in hostagent), guest `/proc/net/dev` | cgroup socket counters are not available: per-session model traffic bytes from the gateway (`usage` records: request/response bytes) |
| Runtime | `sysctl kern.boottime` | VZ process `etime`, guest `uptime` | session `created_at`/state durations |
| Reachability | hub presence (heartbeat) | `GET /v1/health` over the control socket (`probe_ok`) | unit active + last hook/journal activity |
| State | runner state, sync state | VM state (`02-data-model.md` §2.4) | session state (§2.4.1) |
| Free capacity | `vm_slots_free/total`, free VM memory budget, pool ready | – | – |

VZ process lookup: `lsof -t <lima instance dir>/disk` (same user, no sudo), excluding the hostagent PID
(Spike A `vm-metrics.sh`). Command parsing is stdlib `subprocess` with fixed argv, 5 s timeout; a failing probe
yields `probe_ok 0` for that target, never an exception that stops sampling.

## 2. Sampling pipeline (runner)

1. Every 15 s the `metrics` task collects device values, `limactl list --json`, VZ/hostagent process values per
   VM, and `GET /v1/metrics` from every running guest (timeout 2 s).
2. Builds one sample per entity (`02-data-model.md` §2.8 columns) and
3. writes **sink A** (textfile, §3) and **sink B** (hub outbox item `metrics`, §4).
Sampling runs independently of the hub connection; outbox items queue during outages (bounded: samples older
than 48 h are dropped before push, counted in `agent_runner_device_metrics_dropped_total`).

## 3. Sink A – Prometheus via the existing stack

- File `<TEXTFILE_DIR>/agent_runner.prom`, written atomically like the infrastructure scripts: temp file
  `.agent_runner.prom.XXXXXX` (not ending in `.prom`) in the same directory, one write of the complete body,
  `chmod 0644`, `rename`. `TEXTFILE_DIR` is the directory of the host's `node_exporter.args`
  (`--collector.textfile.directory=…`). The runner user needs write access to that directory (installation
  step documented in the prepared infrastructure patch).
- Scraped by the existing host node_exporter and pushed by the existing host Alloy agent, which adds
  `server="<host>"`. **No new ports, no exporter process, no Prometheus or Alloy inside VMs, no push password in
  any VM** (the VM boundary of `00-overview.md` B4 stays intact).
- Every metric has `# HELP` and `# TYPE`. Labels carry IDs only (`vm` = `vm-<8 hex>`, `session` = session UUID,
  `wg` = workgroup UUID, `state`) – no names, e-mails or other personal data (`docs/policy/logging.md` §2).
  Cardinality: session series exist only while the session is not `stopped` plus 15 min; at ≤ 56 VMs × a few
  sessions per VM and 15-day retention this stays in the low thousands of series per device.

| Metric | Type | Labels |
|---|---|---|
| `agent_runner_textfile_timestamp_seconds` | gauge | – |
| `agent_runner_device_up` / `agent_runner_device_hub_connected` / `agent_runner_device_sync_fatal` | gauge | – |
| `agent_runner_device_outbox_backlog` | gauge | – |
| `agent_runner_device_outbox_held` / `agent_runner_device_outbox_held_scopes` (items and scopes waiting in the scope queues of the outbox, outside the backlog; KOR-08, KOR-10) | gauge | – |
| `agent_runner_device_vm_slots_total` / `_free` / `agent_runner_device_vm_memory_budget_bytes` / `_used_bytes` / `agent_runner_device_pool_ready` | gauge | – |
| `agent_runner_device_capacity_low` | gauge (1 when free slots < 10 %) | – |
| `agent_runner_device_verify_failures_total`, `agent_runner_device_metrics_dropped_total`, `agent_runner_vm_start_failures_total` | counter | – |
| `agent_runner_operator_github_connects_total`, `agent_runner_operator_github_connect_failures_total` (connects through the operator's GitHub token; KOR-27, ADR-053) | counter | – |
| `agent_runner_device_model_spend_usd` (platform key, daily) | gauge | `window` |
| `agent_runner_operator_subscription_files` (token files of the operator's subscriptions by state `usable`, `exhausted`, `blocked`, `deferred`, `ignored`; only while `OPERATOR_SUBSCRIPTION_DIR` is set; ADR-052, `exhausted` ADR-081) | gauge | `state` |
| `agent_runner_operator_subscription_sessions` (sessions bound to a token file) | gauge | `file` (file name without `.token`, chosen by the operator) |
| `agent_runner_operator_subscription_newest_change_age_seconds` (since the newest change of a token file) | gauge | – |
| `agent_runner_vm_probe_ok` | gauge | `vm`, `wg` |
| `agent_runner_vm_state` | gauge (1 for the current state) | `vm`, `wg`, `state` |
| `agent_runner_vm_cpu_ratio`, `agent_runner_vm_memory_bytes`, `agent_runner_vm_memory_limit_bytes`, `agent_runner_vm_disk_used_bytes`, `agent_runner_vm_disk_limit_bytes`, `agent_runner_vm_uptime_seconds` | gauge | `vm`, `wg` |
| `agent_runner_vm_network_receive_bytes_total` / `_transmit_bytes_total` | counter | `vm`, `wg` |
| `agent_runner_vm_sessions` | gauge | `vm`, `wg`, `state` |
| `agent_runner_session_cpu_ratio`, `agent_runner_session_memory_bytes`, `agent_runner_session_memory_limit_bytes`, `agent_runner_session_workspace_bytes`, `agent_runner_session_runtime_seconds` | gauge | `vm`, `session` |
| `agent_runner_session_io_read_bytes_total` / `_write_bytes_total`, `agent_runner_session_oom_kills_total`, `agent_runner_session_hook_denies_total`, `agent_runner_session_hook_errors_total`, `agent_runner_session_sandbox_denials_total`, `agent_runner_session_egress_denied_total`, `agent_runner_session_model_tokens_total` (`direction`) | counter | `vm`, `session` |

Device-level host metrics (CPU, RAM, disk, network, boot time) are **not** duplicated: they come from the
existing node_exporter + `macos-memory-free.sh` series under the same `server` label.

## 4. Sink B – hub (web display and user-owned devices)

- Outbox item `metrics` (`03-api.md` §6.1) with samples of the device, its VMs and sessions → `metric_sample`
  (raw, 48 h) → 5-minute `metric_rollup` (avg/max, 90 days) built by a hub background task every 5 min.
- Works for user-owned devices that have no DFX monitoring stack.
- Usage, cost, context and quota values are not resource metrics; they arrive as `usage`/`quota` items
  (`usage_counter`, `quota_snapshot`).

## 5. Grafana assets (`agent`: `deploy/monitoring/`)

Delivered as a **prepared patch** for the infrastructure repository (created under `deploy/monitoring/patch/`,
never pushed there; `PROJECT.md`):

| Asset | Target path in the infrastructure repo | Content |
|---|---|---|
| `dashboards/agent-platform-devices.json` | `grafana/provisioning/dashboards/json/Agent platform/` | per `server`: slots total/free, pool, VM memory budget, VMs by state, outbox backlog, hub connected, sync fatal; uid `agent-platform-devices` |
| `dashboards/agent-platform-vms.json` | same folder | variable `vm` from `label_values(agent_runner_vm_state, vm)`; CPU, memory vs limit, disk, network, uptime, sessions by state, probe |
| `dashboards/agent-platform-sessions.json` | same folder | variable `session`; CPU, memory vs limit, workspace, IO, OOM, hook denies, egress denies, model tokens |
| `alerting/agent-platform.yaml` | `grafana/provisioning/alerting/` | rules below; format `apiVersion: 1`, folder `Infrastructure`, interval 1m, `execErrState: OK`, `noDataState: OK`, header comment explaining each rule |
| `textfile-dir.md` + install step | host README / install script | runner user write access to the textfile directory |
| inventory tile | `dashboard-inventory.toml` | `[[servers]]` tile for the runner device (`prom_server`, `prom_instance = "localhost:9100"`) if not present |
| absence alert | `fleet-host-memory-absent` selector + own zero fallback | for a new runner device |

Alert rules (all `severity` + `component="agent-platform"`):

| Rule | Expression (sketch) | For |
|---|---|---|
| `agent-runner-textfile-absent` | `count_over_time(agent_runner_textfile_timestamp_seconds{server=…}[10m]) < 20 or label_replace(vector(0),…)` (pushed metrics go stale, `up` is useless – existing guest-memory pattern) | 10m |
| `agent-runner-textfile-stale` | `time() - agent_runner_textfile_timestamp_seconds > 120` | 5m |
| `agent-runner-capacity-low` (bottleneck warning) | `agent_runner_device_capacity_low == 1` | 10m |
| `agent-runner-vm-probe-failed` | `max by (server, vm)(agent_runner_vm_probe_ok) == 0` | 5m |
| `agent-runner-session-oom` | `increase(agent_runner_session_oom_kills_total[15m]) > 0` | 0m |
| `agent-runner-sync-fatal` | `agent_runner_device_sync_fatal == 1` | 0m |
| `agent-runner-hub-disconnected` | `agent_runner_device_hub_connected == 0` | 10m |

Runner alerts outside the textfile: `runner.alert <name>` log lines, some also outbox audit rows
`runner.alert {alert,value}` at the hub: `origin_seq_conflict` (also `agent_runner_device_sync_fatal`),
`outbox_items_lost` (audit; items lost after a hub restore below the runner's 1 h outbox retention, `05-runner.md`
§2.3, KOR-10), `outbox_conflict_recovered` (audit; items the store lost in a power cut while the hub had them, the
runner took the hub's position, `05-runner.md` §2.3, HOT-25) and `outbox_discarded` (audit; ADR-047). No alert rule watches them yet; they are read in the audit log
and the runner log.

## 6. Display in agent.dfx.swiss

| Viewer | Device level | VM level | Session level |
|---|---|---|---|
| Org member | devices of the org and platform devices assigned to the org's workgroups: online, CPU, RAM, disk, free capacity, `capacity_low` badge | VMs of workgroups with `wg:read` | sessions of workgroups with `wg:read` incl. usage/cost/context |
| Org admin | same | same | same + audit links |
| Task reader | – | – | sessions of the task (task screen) |
| Quota owner | – | – | own subscription quotas (`GET /v1/me/quotas`) |

A platform device is shared between orgs: other orgs' VMs and sessions are never shown, only device totals.
API: `GET /v1/monitoring/overview`, `GET /v1/monitoring/series` (`03-api.md` §4.11), SSE `metrics` frames while the
monitoring screen is open. Ranges offered: 15 min, 1 h, 6 h, 24 h (raw), 7 d, 30 d, 90 d (rollups). Values shown
with units; stale data (> 60 s) is greyed with the age. Smartphone: one column, sparklines (`06-web.md`).

### 6.1 UI health of the web clients (early warning, ADR-086 §5, KOR-35)

The web client observes long tasks of its main thread (`PerformanceObserver('longtask')`, else event-loop gaps of a
1 s timer) and counts, per **view type** – a closed list of route patterns and tabs in
`contracts/enums/ui-views.json` (e.g. `task.chat`, `monitoring`), never an ID – the blocks over 200 ms (`count`),
their summed duration (`blocked_ms`) and the bucket of the longest one (`max_bucket`: 200, 500, 1 000, 2 000,
5 000, 10 000, 30 000 ms). At most once an hour, and only when it counted something, it sends
`POST /v1/telemetry/ui {version, views}` **anonymously** (no signature, no cookie, `keepalive`).

The hub accepts at most 64 views and 8 KiB per report, clamps each view's values (`count` ≤ 100 000, `blocked_ms` ≤
3 600 000, `max_bucket` ≤ 6), counts a view type it does not know (a newer client) as `other`, and adds the values to
`hub.ui_health_daily` per UTC day, version and view type (`reports` counts the reports). Nothing about the sender is
stored (no user, org, device, address); only the version the hub serves (its own, the web client's `config.version`)
is kept by name, every other version (a tab of an older build, an invented one) counts as `other`; the reports of a
day are added under an advisory lock of that day; rows older than 90 days are deleted (job
`telemetry.ui_health_purge`, hourly); the anonymous rate limit (30/min per source address) applies. With very few users a day's counter may in practice belong to one
person; it still names no person, task or content.

Platform operators (`platform:inviter`) read the counters with `GET /v1/platform/ui-health?days=` (1–90, default 7;
newest day first, at most 10 000 rows) or on the hub host with `agent-hub ui-health [--days N]` (one JSON line). A view
type whose `count` or `max_bucket` rises after a release is the early warning that a list of that view does not
withstand its amount of data.

## 7. Tests

- Unit: parsers for every probe command with recorded fixtures (incl. failure outputs), textfile renderer
  (escaping, HELP/TYPE, atomic write, permissions), rollup computation, rights filtering of monitoring APIs.
- Integration: guest `/v1/metrics` in a real VM with a running session unit (cgroup values move under a CPU/memory
  load generated by the probe from §4.5 of `05-runner.md`).
- E2E (`07-testing.md`): local monitoring stack in a Lima VM with Prometheus `v3.11.0`, node_exporter `v1.8.2`
  and Grafana `12.4.2` (versions of the existing stack): node_exporter reads the runner's textfile directory via
  a scrape of the runner device, Prometheus scrapes, `promtool check metrics` on the textfile and `promtool test
  rules` for the alert expressions, Grafana provisioning loads the three dashboards and the alert file without
  errors (Grafana HTTP API), queries return series for device, VM and session of a running E2E task; the web
  monitoring screen shows the same values as the hub API.
